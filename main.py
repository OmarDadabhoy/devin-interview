"""Webhook endpoint that creates a Devin session when a ticket is created.

Each session is instructed to:
  1. Create a single PR that resolves the ticket.
  2. Post a concise report covering what was solved, how, and concrete results.

A background monitor polls session status and sends the user a status update
if the session is blocked or has been running longer than 20 minutes.

Environment variables:
    DEVIN_API_KEY  – API key for https://api.devin.ai
"""

import asyncio
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEVIN_API_BASE = "https://api.devin.ai/v1"
SESSION_TIMEOUT_SECONDS = 20 * 60
POLL_INTERVAL_SECONDS = 60

app = FastAPI(title="Devin Ticket Trigger")


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

        status = data.get("status_enum") or data.get("status", "")
        elapsed = time.monotonic() - start
        elapsed_min = int(elapsed // 60)

        if status in ("finished", "expired"):
            logger.info("Session %s ended with status=%s", session_id, status)
            return

        if status == "blocked" and not blocked_alerted:
            blocked_alerted = True
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
            try:
                await _send_message(
                    session_id,
                    f"Status update: session for ticket #{ticket.id} has been "
                    f"running for {elapsed_min} minutes. Please check progress.",
                )
            except Exception:
                logger.exception("Failed to send timeout alert for %s", session_id)

        if status in ("finished", "expired") or (timeout_alerted and blocked_alerted):
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

    asyncio.create_task(_monitor_session(session_id, ticket))

    return SessionResponse(session_id=session_id, url=data["url"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
