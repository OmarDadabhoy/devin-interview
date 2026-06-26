# devin-interview

Webhook endpoint that creates a [Devin](https://devin.ai) session when a ticket is created.

Built with the [Devin API](https://docs.devin.ai/api-reference/overview).

## Quick Start

```bash
export DEVIN_API_KEY="your-key-here"
docker compose up --build
```

The server starts on `http://localhost:8000`.

## Stopping & Restarting

```bash
docker compose down          # stop (keeps data)
docker compose up --build    # restart
docker compose down -v       # stop and remove data
```

## Dashboard

Open `http://localhost:8000/` to view the dashboard.

- **Summary cards** -- active, completed, failed, and blocked ticket counts at a glance.
- **Create ticket** -- click **+ New Ticket**, fill in the ID, title, and optional description/URL, then hit **Create**. A Devin session starts automatically.
- **Ticket table** -- lists every ticket with its status, elapsed time, Devin session link, and PR link. Click a row to expand progress details and Devin's report.
- **Notifications** -- toast alerts appear in real time when a session finishes or fails.
- **Auto-refresh** -- data refreshes every 10 seconds; no manual reload needed.

Ticket data persists in a Docker volume. Override the CSV path with the `CSV_PATH` environment variable.

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/ticket` | Create a Devin session for a ticket |
| `GET`  | `/api/tickets` | List all logged tickets (JSON) |
| `GET`  | `/` | Dashboard UI |
| `GET`  | `/health` | Health check |
