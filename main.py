"""Webhook endpoint that creates a Devin session when a ticket is created.

Each session is instructed to:
  1. Create a single PR that resolves the ticket.
  2. Post a concise report covering what was solved, how, and concrete results.

A background monitor polls session status and sends the user a status update
if the session is blocked or has been running longer than 20 minutes.

Every ticket is logged to a local CSV file (``tickets.csv``) for persistence.
A dashboard is served at ``/`` showing active/completed tasks, success/failure
signals, and progress tracking.

Environment variables:
    DEVIN_API_KEY  – API key for https://api.devin.ai
    CSV_PATH       – path to the ticket log (default: ``tickets.csv``)
"""

import asyncio
import csv
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEVIN_API_BASE = "https://api.devin.ai/v1"
SESSION_TIMEOUT_SECONDS = 20 * 60
POLL_INTERVAL_SECONDS = 60

CSV_PATH = Path(os.environ.get("CSV_PATH", "tickets.csv"))
CSV_FIELDS = [
    "ticket_id",
    "title",
    "description",
    "ticket_url",
    "session_id",
    "session_url",
    "status",
    "created_at",
    "updated_at",
    "elapsed_minutes",
    "pr_url",
]

app = FastAPI(title="Devin Ticket Trigger")

# In-memory notification log (ephemeral; resets on restart)
_notifications: list[dict[str, str]] = []


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _ensure_csv() -> None:
    if not CSV_PATH.exists():
        with CSV_PATH.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def _append_row(row: dict[str, str]) -> None:
    _ensure_csv()
    with CSV_PATH.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)


def _read_rows() -> list[dict[str, str]]:
    _ensure_csv()
    with CSV_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def _update_row(session_id: str, updates: dict[str, str]) -> None:
    rows = _read_rows()
    for row in rows:
        if row["session_id"] == session_id:
            row.update(updates)
            row["updated_at"] = _now_iso()
            break
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Ticket(BaseModel):
    """Minimal ticket payload expected from the webhook."""

    id: str
    title: str
    description: str = ""
    url: str = ""


class SessionResponse(BaseModel):
    session_id: str
    url: str


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

REPORT_INSTRUCTIONS = (
    "\n\n---\n"
    "Requirements:\n"
    "1. Create exactly ONE pull request that resolves this ticket.\n"
    "2. After the PR is created, post a concise report as a message with:\n"
    "   a. What we solved – one-liner summary of the fix.\n"
    "   b. How we did it – brief description of the approach.\n"
    "   c. Concrete results – test output, before/after, or metrics.\n"
    "Keep the report as short as possible — no filler."
)


def _build_prompt(ticket: Ticket) -> str:
    parts = [f"Ticket #{ticket.id}: {ticket.title}"]
    if ticket.description:
        parts.append(f"\nDescription:\n{ticket.description}")
    if ticket.url:
        parts.append(f"\nTicket URL: {ticket.url}")
    parts.append(REPORT_INSTRUCTIONS)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Devin API helpers
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    api_key = os.environ.get("DEVIN_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="DEVIN_API_KEY is not set")
    return api_key


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


async def _create_devin_session(prompt: str) -> dict[str, Any]:
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DEVIN_API_BASE}/sessions",
            headers=_auth_headers(api_key),
            json={"prompt": prompt},
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Devin API returned {resp.status_code}: {resp.text}",
            )
        return resp.json()


async def _get_session_status(session_id: str) -> dict[str, Any]:
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{DEVIN_API_BASE}/sessions/{session_id}",
            headers=_auth_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


async def _send_message(session_id: str, message: str) -> None:
    api_key = _get_api_key()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DEVIN_API_BASE}/sessions/{session_id}/message",
            headers=_auth_headers(api_key),
            json={"message": message},
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Background session monitor
# ---------------------------------------------------------------------------

def _add_notification(ticket_id: str, title: str, message: str,
                      level: str = "info") -> None:
    _notifications.append({
        "ticket_id": ticket_id,
        "title": title,
        "message": message,
        "level": level,
        "timestamp": _now_iso(),
    })


def _resolve_status(raw_status: str) -> str:
    """Map Devin API status to a simpler label for the dashboard."""
    mapping = {
        "finished": "completed",
        "expired": "failed",
        "blocked": "blocked",
        "working": "active",
    }
    return mapping.get(raw_status, "active")


async def _monitor_session(session_id: str, ticket: Ticket) -> None:
    """Poll session status; nudge the user if blocked or running too long."""
    start = time.monotonic()
    timeout_alerted = False
    blocked_alerted = False

    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

        try:
            data = await _get_session_status(session_id)
        except Exception:
            logger.exception("Failed to poll session %s", session_id)
            continue

        raw_status = data.get("status_enum") or data.get("status", "")
        elapsed = time.monotonic() - start
        elapsed_min = int(elapsed // 60)
        dash_status = _resolve_status(raw_status)

        pr_url = ""
        pr_info = data.get("pull_request")
        if pr_info and isinstance(pr_info, dict):
            pr_url = pr_info.get("url", "")

        _update_row(session_id, {
            "status": dash_status,
            "elapsed_minutes": str(elapsed_min),
            "pr_url": pr_url,
        })

        if raw_status in ("finished", "expired"):
            end_level = "success" if raw_status == "finished" else "error"
            end_label = "completed" if raw_status == "finished" else "failed"
            _add_notification(
                ticket.id, ticket.title,
                f"Session {end_label} after {elapsed_min} min",
                level=end_level,
            )
            logger.info("Session %s ended with status=%s", session_id, raw_status)
            return

        if raw_status == "blocked" and not blocked_alerted:
            blocked_alerted = True
            _add_notification(
                ticket.id, ticket.title,
                "Session is blocked — needs input",
                level="warning",
            )
            try:
                await _send_message(
                    session_id,
                    f"Status update: session for ticket #{ticket.id} is blocked. "
                    "Please check the session and provide any needed input.",
                )
            except Exception:
                logger.exception("Failed to send blocked alert for %s", session_id)

        if elapsed > SESSION_TIMEOUT_SECONDS and not timeout_alerted:
            timeout_alerted = True
            _add_notification(
                ticket.id, ticket.title,
                f"Session running for {elapsed_min} min — check progress",
                level="warning",
            )
            try:
                await _send_message(
                    session_id,
                    f"Status update: session for ticket #{ticket.id} has been "
                    f"running for {elapsed_min} minutes. Please check progress.",
                )
            except Exception:
                logger.exception("Failed to send timeout alert for %s", session_id)

        if raw_status in ("finished", "expired") or (timeout_alerted and blocked_alerted):
            return


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/webhook/ticket", response_model=SessionResponse)
async def on_ticket_created(ticket: Ticket) -> SessionResponse:
    """Handle a ticket-creation webhook and start a Devin session."""
    prompt = _build_prompt(ticket)
    data = await _create_devin_session(prompt)
    session_id = data["session_id"]
    now = _now_iso()

    _append_row({
        "ticket_id": ticket.id,
        "title": ticket.title,
        "description": ticket.description,
        "ticket_url": ticket.url,
        "session_id": session_id,
        "session_url": data["url"],
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "elapsed_minutes": "0",
        "pr_url": "",
    })

    asyncio.create_task(_monitor_session(session_id, ticket))

    return SessionResponse(session_id=session_id, url=data["url"])


@app.get("/api/tickets")
async def list_tickets() -> list[dict[str, str]]:
    """Return all logged tickets as JSON."""
    return _read_rows()


@app.get("/api/notifications")
async def list_notifications(since: str = "") -> list[dict[str, str]]:
    """Return notifications, optionally filtered to those after *since*."""
    if not since:
        return _notifications[-50:]
    return [n for n in _notifications if n["timestamp"] > since][-50:]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the dashboard HTML page."""
    return (_TEMPLATE_DIR / "dashboard.html").read_text()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
