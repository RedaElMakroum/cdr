# Configuration Reference

All configuration is done via environment variables in the `.env` file.

## Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `CEREBRAS_API_KEY` | Cerebras API key for LLM inference | `csk-...` |
| `ENTSOE_API_KEY` | ENTSO-E Transparency Platform API key | `abc123...` |

## Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CEREBRAS_MODEL` | `gpt-oss-120b` | LLM model to use |
| `BIDDING_ZONE` | `AT` | ENTSO-E bidding zone for electricity prices |
| `VENV_PATH` | (empty) | Path to Python virtual environment (local dev only) |
| `HEMS_WORK_DIR` | (auto-detected) | Working directory override (set in Docker) |
| `REASONING_EFFORT` | `high` | LLM reasoning effort: `low`, `medium`, `high` |
| `REASONING_FORMAT` | `parsed` | Reasoning output format: `parsed`, `raw`, `hidden` |

## Agent Configuration

Agent behavior is configured in `config.py`:

- **Temperature**: `0.0` (deterministic outputs)
- **Max iterations**: 15 (ReAct loop limit, set in orchestrator)
- **Compact tool outputs**: Enabled (reduces token usage)

## Appliance Configuration

Each appliance in `AVAILABLE_APPLIANCES` (in `config.py`) has:

| Field | Description |
|-------|-------------|
| `agent_file` | System prompt filename (e.g., `battery_agent.md`) |
| `capacity_kwh` | Energy capacity |
| `power_rating_kw` | Maximum power draw/output |
| `control_type` | `variable` (battery) or `binary` (appliances) |
| `api_config` | Optional Home Assistant integration (disabled by default) |

## Battery State

Battery parameters are in `data/battery_state.json`:

```json
{
  "battery_id": "BAT-001",
  "capacity_kwh": 15.0,
  "current_soc_kwh": 4.5,
  "current_soc_pct": 30,
  "max_charge_kw": 8.0,
  "max_discharge_kw": 8.0,
  "min_soc_pct": 20,
  "round_trip_efficiency": 0.92,
  "pv_forecast_kwh": 8.5
}
```

Modify these values to simulate different household configurations.

## Aggregator Settings

Aggregator parameters are in `data/aggregator_settings.json`. This includes:

- Grid region and bidding zone
- Default DR event parameters
- Communication preferences

## Portfolio

The household portfolio is in `data/portfolio.json`. It tracks:

- Registered households and their assets
- Flexible capacity per household
- Availability schedules and constraints

## API Server

The Flask API server (`api.py`) settings:

| Setting | Value |
|---------|-------|
| Port | 5001 |
| Rate limits | 200/day, 50/hour (global); per-endpoint limits vary |
| CORS | Enabled for all origins (development) |
| Orchestrator timeout | 300 seconds |
| Model API timeout | 10 seconds |

## Docker

Docker-specific variables are set in `docker-compose.yml`:

- `PYTHONUNBUFFERED=1`: Ensures real-time log output
- `HEMS_WORK_DIR=/app`: Container working directory
- API keys are read from `.env` via `env_file`
