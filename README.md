# Conversational Demand Response (CDR)

An agentic AI system for conversational coordination of demand response between aggregators and prosumers. Built on LLM-based ReAct agents that negotiate, evaluate, and commit to DR events through natural language -- replacing rigid API protocols with flexible, human-interpretable communication.

This repository accompanies the paper:

> **Flexibility Provision through Agentic AI: Conversational Demand Response for Residential Prosumers**

## Overview

The system implements bidirectional communication between an aggregator and a prosumer household:

- **Downstream (aggregator to prosumer):** The aggregator dispatches a DR event. The HEMS orchestrator evaluates feasibility using battery state, PV forecasts, and a MILP optimizer, then responds with acceptance, partial commitment, or rejection.
- **Upstream (prosumer to aggregator):** The prosumer sends free-text messages (e.g., "I just plugged in my EV", "Keep my battery above 50%"). The aggregator classifies the intent, extracts structured updates, and applies them to its portfolio.

## Quick Start

### Prerequisites

- Python 3.10+
- [Cerebras API key](https://cloud.cerebras.ai/) (LLM inference)
- [ENTSO-E API key](https://transparency.entsoe.eu/) (electricity prices)

### Setup

```bash
# Clone the repository
git clone https://github.com/redaelmakroum/cdr.git
cd cdr

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt -r requirements-api.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys
```

### Run

```bash
# Start the API server
python src/api.py

# Open the dashboard
# Aggregator view: http://localhost:5001/?role=aggregator
# Prosumer view:   http://localhost:5001/?role=prosumer
```

### Docker

```bash
cp .env.example .env
# Edit .env with your API keys
docker compose up --build
# Dashboard: http://localhost:8000
# API: http://localhost:5001
```

## Architecture

```
Aggregator Agent (ReAct)           Prosumer HEMS (ReAct)
       |                                  |
  Dispatch DR event ----------------> Receive event
       |                                  |
       |                           Orchestrator evaluates:
       |                             - Battery state
       |                             - PV forecast
       |                             - MILP optimizer
       |                                  |
  Collect response <----------------- Submit DR response
       |                           (accept/partial/reject)
       |
  Update portfolio
```

The orchestrator uses a ReAct (Reasoning + Action) loop with tool-calling:

| Tool | Description |
|------|-------------|
| `GET_BATTERY_STATE` | Current SoC, capacity, PV forecast |
| `EVALUATE_FEASIBILITY` | MILP-based DR feasibility check |
| `CALL_AGENT` | Delegate to specialist agent (battery, appliances) |
| `SUBMIT_DR_RESPONSE` | Send response back to aggregator |
| `GET_PRICES` | Day-ahead electricity prices from ENTSO-E |
| `CALCULATE_WINDOW_SUMS` | Compute cost/generation in a time window |

## Benchmark Reproduction

See [EVALUATION_GUIDE.md](EVALUATION_GUIDE.md) for instructions on reproducing the benchmark results from the paper.

## Project Structure

```
cdr/
  run_benchmark.py              # Benchmark runner (6 scenarios x N runs)
  dashboard.html                # Single-page dashboard (aggregator + prosumer views)

  src/                          # Application code
    api.py                      # Flask API server
    orchestrator_agent_react.py # ReAct orchestrator + agent classes
    tools.py                    # HEMS tool implementations
    aggregator_tools.py         # Aggregator-side tools (DR dispatch, portfolio)
    battery_optimizer.py        # MILP optimizer for battery scheduling
    config.py                   # Configuration (reads from .env)
    security.py                 # Input validation and rate limiting
    event_logger.py             # Execution trace logger
    entsoe_client.py            # ENTSO-E price data client

  prompts/                      # Agent system prompts
    hems_orchestrator.md        # Main HEMS orchestrator prompt
    battery_agent.md            # Battery specialist agent
    washing_machine_agent.md    # Washing machine specialist
    dishwasher_agent.md         # Dishwasher specialist
    ev_charger_agent.md         # EV charger specialist
    aggregator_orchestrator.md  # Aggregator orchestrator prompt
    dr_event_handler.md         # DR event handler prompt

  data/                         # Configuration and runtime data
    battery_state.json          # Battery parameters and current state
    aggregator_settings.json    # Aggregator configuration
    portfolio.json              # Aggregator household portfolio
    profiles/                   # Load profiles (demand, PV, prices)
    market_obligations/         # Sample market obligation data
    examples/                   # Example DR events, responses, requests

  docs/                         # Documentation
```

## Data

The `data/profiles/` directory contains 15-minute resolution profiles for a single household:

- `household_demand.csv` -- Residential load profile (~20 kWh/day), based on Austrian standard load profiles
- `pv_generation.csv` -- PV generation profile (~34 kWh/day peak, corresponding to a ~7 kWp system in Central Europe)
- `electricity_price.csv` -- Day-ahead prices from the Austrian bidding zone (ENTSO-E), range 0.04--0.29 EUR/kWh

All profiles use 96 time slots (24h at 15-minute resolution), consistent with European electricity market settlement periods.

## Documentation

- [Setup Guide](docs/SETUP.md) -- API keys and environment configuration
- [Configuration Reference](docs/CONFIGURATION.md) -- All configuration options
- [API Reference](docs/API.md) -- REST API endpoints
- [Architecture](docs/ARCHITECTURE.md) -- System design and data flow
- [Troubleshooting](docs/TROUBLESHOOTING.md) -- Common issues and solutions
- [Evaluation Guide](EVALUATION_GUIDE.md) -- Benchmark reproduction

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT License. See [LICENSE](LICENSE) for details.

## Contact

Reda El Makroum -- [elmakroum@eeg.tuwien.ac.at](mailto:elmakroum@eeg.tuwien.ac.at)

Energy Economics Group, TU Wien
