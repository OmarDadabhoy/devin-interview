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
            logger.info("Session %s ended with status=%s", session_id, raw_status)
            return

        if raw_status == "blocked" and not blocked_alerted:
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Serve the dashboard HTML page."""
    return DASHBOARD_HTML


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Devin Ticket Dashboard</title>
<style>
  :root { --bg: #0f1117; --card: #1a1d27; --border: #2a2d37; --text: #e1e4eb;
          --muted: #8b8fa3; --green: #22c55e; --red: #ef4444; --blue: #3b82f6;
          --yellow: #eab308; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
         sans-serif; background: var(--bg); color: var(--text); padding: 24px; }
  h1 { font-size: 1.5rem; margin-bottom: 20px; }
  .stats { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border);
               border-radius: 10px; padding: 20px 24px; min-width: 160px;
               flex: 1; }
  .stat-card .label { color: var(--muted); font-size: 0.8rem;
                      text-transform: uppercase; letter-spacing: 0.05em; }
  .stat-card .value { font-size: 2rem; font-weight: 700; margin-top: 4px; }
  .stat-card .value.green  { color: var(--green); }
  .stat-card .value.red    { color: var(--red); }
  .stat-card .value.blue   { color: var(--blue); }
  .stat-card .value.yellow { color: var(--yellow); }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border: 1px solid var(--border); border-radius: 10px;
          overflow: hidden; }
  th, td { text-align: left; padding: 12px 16px; border-bottom: 1px solid
           var(--border); font-size: 0.875rem; }
  th { color: var(--muted); font-weight: 600; text-transform: uppercase;
       font-size: 0.75rem; letter-spacing: 0.05em; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 9999px;
           font-size: 0.75rem; font-weight: 600; }
  .badge.active   { background: rgba(59,130,246,0.15); color: var(--blue); }
  .badge.completed{ background: rgba(34,197,94,0.15);  color: var(--green); }
  .badge.failed   { background: rgba(239,68,68,0.15);  color: var(--red); }
  .badge.blocked  { background: rgba(234,179,8,0.15);  color: var(--yellow); }
  a { color: var(--blue); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .empty { text-align: center; padding: 48px; color: var(--muted); }
  .refresh { color: var(--muted); font-size: 0.8rem; margin-bottom: 16px; }
</style>
</head>
<body>
<h1>Devin Ticket Dashboard</h1>
<div class="refresh">Auto-refreshes every 10 s</div>
<div class="stats" id="stats"></div>
<table>
  <thead>
    <tr>
      <th>Ticket</th><th>Title</th><th>Status</th><th>Elapsed</th>
      <th>Session</th><th>PR</th><th>Created</th>
    </tr>
  </thead>
  <tbody id="rows"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
</table>

<script>
async function load() {
  const res = await fetch('/api/tickets');
  const tickets = await res.json();

  // Stats
  const total = tickets.length;
  const active = tickets.filter(t => t.status === 'active').length;
  const completed = tickets.filter(t => t.status === 'completed').length;
  const failed = tickets.filter(t => t.status === 'failed').length;
  const blocked = tickets.filter(t => t.status === 'blocked').length;

  document.getElementById('stats').innerHTML = `
    <div class="stat-card"><div class="label">Total</div>
      <div class="value">${total}</div></div>
    <div class="stat-card"><div class="label">Active</div>
      <div class="value blue">${active}</div></div>
    <div class="stat-card"><div class="label">Completed</div>
      <div class="value green">${completed}</div></div>
    <div class="stat-card"><div class="label">Failed</div>
      <div class="value red">${failed}</div></div>
    <div class="stat-card"><div class="label">Blocked</div>
      <div class="value yellow">${blocked}</div></div>
  `;

  // Table
  const tbody = document.getElementById('rows');
  if (!tickets.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No tickets yet</td></tr>';
    return;
  }
  tbody.innerHTML = tickets.map(t => `<tr>
    <td>${t.ticket_url ? '<a href="'+t.ticket_url+'">#'+t.ticket_id+'</a>' : '#'+t.ticket_id}</td>
    <td>${t.title}</td>
    <td><span class="badge ${t.status}">${t.status}</span></td>
    <td>${t.elapsed_minutes} min</td>
    <td><a href="${t.session_url}" target="_blank">View</a></td>
    <td>${t.pr_url ? '<a href="'+t.pr_url+'" target="_blank">PR</a>' : '—'}</td>
    <td>${t.created_at ? new Date(t.created_at).toLocaleString() : '—'}</td>
  </tr>`).join('');
}
load();
setInterval(load, 10000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
