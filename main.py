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
          --yellow: #eab308; --purple: #a855f7; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
         sans-serif; background: var(--bg); color: var(--text); padding: 24px; }
  .header { display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 20px; }
  h1 { font-size: 1.5rem; }
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

  /* Create ticket button */
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 10px 20px;
         border: none; border-radius: 8px; font-size: 0.875rem; font-weight: 600;
         cursor: pointer; transition: background 0.15s; }
  .btn-primary { background: var(--blue); color: #fff; }
  .btn-primary:hover { background: #2563eb; }
  .btn-secondary { background: var(--border); color: var(--text); }
  .btn-secondary:hover { background: #3a3d47; }

  /* Modal */
  .modal-overlay { display: none; position: fixed; inset: 0;
                   background: rgba(0,0,0,0.6); z-index: 100;
                   align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--card); border: 1px solid var(--border);
           border-radius: 12px; padding: 28px; width: 480px; max-width: 95vw; }
  .modal h2 { font-size: 1.2rem; margin-bottom: 20px; }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; color: var(--muted); font-size: 0.8rem;
                      text-transform: uppercase; letter-spacing: 0.05em;
                      margin-bottom: 6px; }
  .form-group input, .form-group textarea {
    width: 100%; padding: 10px 12px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-size: 0.875rem; font-family: inherit;
    outline: none; transition: border-color 0.15s; }
  .form-group input:focus, .form-group textarea:focus {
    border-color: var(--blue); }
  .form-group textarea { resize: vertical; min-height: 80px; }
  .modal-actions { display: flex; gap: 12px; justify-content: flex-end;
                   margin-top: 20px; }
  .form-error { color: var(--red); font-size: 0.8rem; margin-top: 6px;
                display: none; }

  /* Toast notifications */
  .toast-container { position: fixed; top: 20px; right: 20px; z-index: 200;
                     display: flex; flex-direction: column; gap: 8px;
                     max-width: 380px; }
  .toast { padding: 14px 18px; border-radius: 10px; font-size: 0.85rem;
           animation: slideIn 0.3s ease; border: 1px solid;
           display: flex; align-items: flex-start; gap: 10px; }
  .toast .toast-close { background: none; border: none; color: inherit;
                        cursor: pointer; font-size: 1.1rem; padding: 0;
                        line-height: 1; opacity: 0.6; flex-shrink: 0; }
  .toast .toast-close:hover { opacity: 1; }
  .toast .toast-body { flex: 1; }
  .toast .toast-title { font-weight: 600; margin-bottom: 2px; }
  .toast.success { background: rgba(34,197,94,0.12); border-color: rgba(34,197,94,0.3);
                   color: var(--green); }
  .toast.error   { background: rgba(239,68,68,0.12); border-color: rgba(239,68,68,0.3);
                   color: var(--red); }
  .toast.warning { background: rgba(234,179,8,0.12); border-color: rgba(234,179,8,0.3);
                   color: var(--yellow); }
  .toast.info    { background: rgba(59,130,246,0.12); border-color: rgba(59,130,246,0.3);
                   color: var(--blue); }
  @keyframes slideIn { from { transform: translateX(100%); opacity: 0; }
                       to   { transform: translateX(0);    opacity: 1; } }
</style>
</head>
<body>

<!-- Toast container -->
<div class="toast-container" id="toasts"></div>

<!-- Header -->
<div class="header">
  <h1>Devin Ticket Dashboard</h1>
  <button class="btn btn-primary" onclick="openModal()">
    + New Ticket
  </button>
</div>
<div class="refresh">Auto-refreshes every 10 s</div>

<!-- Stats -->
<div class="stats" id="stats"></div>

<!-- Ticket table -->
<table>
  <thead>
    <tr>
      <th>Ticket</th><th>Title</th><th>Status</th><th>Elapsed</th>
      <th>Session</th><th>PR</th><th>Created</th>
    </tr>
  </thead>
  <tbody id="rows"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
</table>

<!-- Create-ticket modal -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h2>Create Ticket</h2>
    <form id="ticketForm" onsubmit="return submitTicket(event)">
      <div class="form-group">
        <label for="tid">Ticket ID *</label>
        <input id="tid" name="id" required placeholder="e.g. PROJ-123" />
      </div>
      <div class="form-group">
        <label for="ttitle">Title *</label>
        <input id="ttitle" name="title" required placeholder="Short summary" />
      </div>
      <div class="form-group">
        <label for="tdesc">Description</label>
        <textarea id="tdesc" name="description"
                  placeholder="Detailed description (optional)"></textarea>
      </div>
      <div class="form-group">
        <label for="turl">Ticket URL</label>
        <input id="turl" name="url" type="url"
               placeholder="https://...  (optional)" />
      </div>
      <div class="form-error" id="formError"></div>
      <div class="modal-actions">
        <button type="button" class="btn btn-secondary"
                onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary"
                id="submitBtn">Create</button>
      </div>
    </form>
  </div>
</div>

<script>
/* ---- Modal ---- */
function openModal()  { document.getElementById('modal').classList.add('open'); }
function closeModal() {
  document.getElementById('modal').classList.remove('open');
  document.getElementById('ticketForm').reset();
  document.getElementById('formError').style.display = 'none';
}
document.getElementById('modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeModal();
});

async function submitTicket(e) {
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  const errEl = document.getElementById('formError');
  errEl.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Creating...';

  const form = document.getElementById('ticketForm');
  const body = {
    id: form.id.value.trim(),
    title: form.title.value.trim(),
    description: form.description.value.trim(),
    url: form.url.value.trim(),
  };

  try {
    const res = await fetch('/webhook/ticket', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `Server returned ${res.status}`);
    }
    const data = await res.json();
    closeModal();
    showToast('info', 'Ticket created',
      `#${body.id} — Devin session started`);
    load();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create';
  }
  return false;
}

/* ---- Toasts ---- */
function showToast(level, title, msg) {
  const container = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast ${level}`;
  el.innerHTML = `<div class="toast-body">
    <div class="toast-title">${title}</div>
    <div>${msg}</div>
  </div>
  <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>`;
  container.appendChild(el);
  setTimeout(() => el.remove(), 8000);
}

/* ---- Notification polling ---- */
let lastNotifTs = '';
async function pollNotifications() {
  try {
    const url = lastNotifTs
      ? `/api/notifications?since=${encodeURIComponent(lastNotifTs)}`
      : '/api/notifications';
    const res = await fetch(url);
    const items = await res.json();
    for (const n of items) {
      showToast(n.level, `#${n.ticket_id} ${n.title}`, n.message);
      lastNotifTs = n.timestamp;
    }
  } catch (e) { /* ignore */ }
}

/* ---- Ticket table ---- */
async function load() {
  const res = await fetch('/api/tickets');
  const tickets = await res.json();

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
setInterval(pollNotifications, 10000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
