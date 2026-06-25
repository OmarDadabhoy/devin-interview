# devin-interview

Webhook endpoint that creates a [Devin](https://devin.ai) session when a ticket is created.

Built with the [Devin API](https://docs.devin.ai/api-reference/overview).

## Quick Start (Docker)

```bash
# Set your Devin API key
export DEVIN_API_KEY="your-key-here"

# Build and run
docker compose up --build
```

The server starts on `http://localhost:8000`. Ticket data persists in a Docker
volume across restarts.

To stop:

```bash
docker compose down        # keeps data
docker compose down -v     # removes data volume too
```

## Local Setup (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DEVIN_API_KEY="your-key-here"
python main.py
```

The server starts on `http://localhost:8000`.

## Usage

```bash
curl -X POST http://localhost:8000/webhook/ticket \
  -H "Content-Type: application/json" \
  -d '{"id": "PROJ-123", "title": "Fix login bug", "description": "Users cannot log in", "url": "https://example.com/tickets/123"}'
```

## Dashboard

Open `http://localhost:8000/` in a browser to view the ticket dashboard.
It shows active/completed counts, success/failure signals, and a table of
every ticket submitted to the webhook. Data auto-refreshes every 10 seconds.

Ticket data is persisted to a local CSV file (`tickets.csv` by default).
Override the path with the `CSV_PATH` environment variable.

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/ticket` | Create a Devin session for a ticket |
| `GET`  | `/api/tickets` | List all logged tickets (JSON) |
| `GET`  | `/` | Dashboard UI |
| `GET`  | `/health` | Health check |
