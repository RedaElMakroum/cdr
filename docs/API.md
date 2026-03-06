# API Reference

The Flask API server runs on port 5001. All endpoints return JSON unless otherwise noted.

## Health

### `GET /api/health`
Health check. No rate limit.

**Response:**
```json
{"status": "ok"}
```

## HEMS Orchestration

### `POST /api/run`
Run the HEMS orchestrator (blocking). Rate limit: 20/min.

**Body:**
```json
{
  "prompt": "Schedule my washing machine for lowest cost",
  "model": "gpt-oss-120b"
}
```

### `POST /api/run/stream`
Run the HEMS orchestrator with SSE streaming. Rate limit: 20/min.

Same body as `/api/run`. Returns Server-Sent Events with iteration progress.

**SSE event types:**
- `stdout`: ReAct loop output (streamed line by line)
- `done`: Stream complete (includes return code)
- `error`: Error message

### `GET /api/models`
List available LLM models. Rate limit: 30/min.

## Aggregator

### `POST /api/aggregator/stream`
Run the aggregator orchestrator with SSE streaming. Rate limit: 20/min.

**Body:**
```json
{
  "message": "Dispatch a DR event to HH-001",
  "model": "gpt-oss-120b"
}
```

## DR Events

### `POST /api/dr-event`
Create a new DR event. Rate limit: 20/min.

**Body:**
```json
{
  "household_id": "HH-001",
  "window_start": "17:00",
  "window_end": "19:00",
  "target_kw": 3.0,
  "compensation_eur_kwh": 0.20
}
```

### `GET /api/dr-events`
List all DR events. Rate limit: 30/min.

### `GET /api/dr-events/pending`
List pending DR events (not yet responded to). Rate limit: 60/min.

### `GET /api/dr-event/<event_id>/response`
Get the DR response for an event. Rate limit: 30/min.

### `GET /api/dr-event/<event_id>/log`
Get the execution log for a DR event response. Rate limit: 30/min.

### `POST /api/dr-event/<event_id>/respond/stream`
Run DR event response handler with SSE streaming. Rate limit: 20/min.

**Body:**
```json
{
  "model": "gpt-oss-120b"
}
```

### `POST /api/dr-event/<event_id>/submit-response`
Manually submit a DR response. Rate limit: 20/min.

**Body:**
```json
{
  "commitment_kw": 3.0,
  "commitment_type": "FULL",
  "conditions": "Battery discharge 17:00-19:00"
}
```

## Prosumer Communication (Upstream)

### `POST /api/prosumer-message`
Process a free-text prosumer message. Rate limit: 10/min.

**Body:**
```json
{
  "household_id": "HH-001",
  "message": "I just plugged in my EV, it needs 80% by tomorrow 7am."
}
```

**Response:**
```json
{
  "success": true,
  "request_type": "asset_update",
  "summary": "EV charger connected, charging target set",
  "confirmation_message": "Registered your EV charger...",
  "changes_applied": ["Added EV charger asset"],
  "request_id": "REQ-..."
}
```

### `GET /api/portfolio`
Get portfolio status. Rate limit: 30/min.

## Household Requests

### `POST /api/household-request`
Create a household request record. Rate limit: 20/min.

### `GET /api/household-requests`
List all household requests. Rate limit: 30/min.

### `POST /api/household-requests/<request_id>/acknowledge`
Acknowledge a household request. Rate limit: 30/min.

## Run History

### `GET /api/runs`
List past orchestrator runs. Rate limit: 30/min.

**Query params:** `limit` (default 10), `offset` (default 0).

## Rate Limits

Global limits: 200 requests/day, 50 requests/hour.

Per-endpoint limits are listed above. When exceeded, the API returns HTTP 429 with a `Retry-After` header.

## CORS

CORS is enabled for all origins (suitable for development). For production, restrict origins in `api.py`.
