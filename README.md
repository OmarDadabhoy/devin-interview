# devin-interview

Webhook endpoint that creates a [Devin](https://devin.ai) session when a ticket is created.

Built with the [Devin API](https://docs.devin.ai/api-reference/overview).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Set your Devin API key:

```bash
export DEVIN_API_KEY="your-key-here"
```

## Run

```bash
python main.py
```

The server starts on `http://localhost:8000`.

## Usage

```bash
curl -X POST http://localhost:8000/webhook/ticket \
  -H "Content-Type: application/json" \
  -d '{"id": "PROJ-123", "title": "Fix login bug", "description": "Users cannot log in", "url": "https://example.com/tickets/123"}'
```

## Health check

```
GET /health
```
