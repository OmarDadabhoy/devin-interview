"""Webhook endpoint that creates a Devin session when a ticket is created.

Receives a POST with ticket data, then calls the Devin API (v1) to spin up
a new session whose prompt includes the ticket details.

Environment variables:
    DEVIN_API_KEY  – API key for https://api.devin.ai
"""

import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DEVIN_API_URL = "https://api.devin.ai/v1/sessions"

app = FastAPI(title="Devin Ticket Trigger")


class Ticket(BaseModel):
    """Minimal ticket payload expected from the webhook."""

    id: str
    title: str
    description: str = ""
    url: str = ""


class SessionResponse(BaseModel):
    session_id: str
    url: str


def _build_prompt(ticket: Ticket) -> str:
    parts = [f"Ticket #{ticket.id}: {ticket.title}"]
    if ticket.description:
        parts.append(f"\nDescription:\n{ticket.description}")
    if ticket.url:
        parts.append(f"\nTicket URL: {ticket.url}")
    return "\n".join(parts)


async def _create_devin_session(prompt: str) -> dict[str, Any]:
    api_key = os.environ.get("DEVIN_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="DEVIN_API_KEY is not set")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            DEVIN_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"prompt": prompt},
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Devin API returned {resp.status_code}: {resp.text}",
            )
        return resp.json()


@app.post("/webhook/ticket", response_model=SessionResponse)
async def on_ticket_created(ticket: Ticket) -> SessionResponse:
    """Handle a ticket-creation webhook and start a Devin session."""
    prompt = _build_prompt(ticket)
    data = await _create_devin_session(prompt)
    return SessionResponse(session_id=data["session_id"], url=data["url"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
