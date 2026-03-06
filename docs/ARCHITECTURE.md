# Architecture

## System Overview

The CDR system implements bidirectional conversational demand response between an aggregator and prosumer households. It uses LLM-based agents following the ReAct (Reasoning + Action) pattern.

```
+---------------------+          +----------------------+
|    Aggregator UI     |          |     Prosumer UI      |
|  (dashboard.html)    |          |   (dashboard.html)   |
+----------+----------+          +----------+-----------+
           |                                |
           v                                v
+----------+----------+          +----------+-----------+
|     Flask API        |<-------->|     Flask API         |
|     (api.py)         |          |     (api.py)          |
+----------+----------+          +----------+-----------+
           |                                |
           v                                v
+----------+----------+          +----------+-----------+
| Aggregator Agent     |          | HEMS Orchestrator     |
| (ReAct loop)         |          | (ReAct loop)          |
| aggregator_          |          | orchestrator_         |
|  orchestrator.md     |          |  agent_react.py       |
+----------+----------+          +----------+-----------+
           |                                |
           v                                v
+----------+----------+          +----------+-----------+
| Aggregator Tools     |          | HEMS Tools            |
| aggregator_tools.py  |          | tools.py              |
|  - dispatch_dr_event |          |  - get_battery_state  |
|  - collect_response  |          |  - evaluate_feasibility|
|  - portfolio mgmt    |          |  - get_prices         |
|  - triage messages   |          |  - calculate_sums     |
+----------------------+          +----------+-----------+
                                             |
                                             v
                                  +----------+-----------+
                                  | Specialist Agents     |
                                  |  - Battery agent      |
                                  |  - Appliance agents   |
                                  +----------+-----------+
                                             |
                                             v
                                  +----------+-----------+
                                  | Battery Optimizer     |
                                  | battery_optimizer.py  |
                                  | (MILP via PuLP/CBC)   |
                                  +----------------------+
```

## Communication Flows

### Downstream: Aggregator to Prosumer

1. Aggregator dispatches DR event via `dispatch_dr_event()`
2. Event is stored in `data/dr_events/`
3. HEMS orchestrator picks up the event and starts a ReAct loop:
   - Reads battery state (`GET_BATTERY_STATE`)
   - Evaluates DR feasibility (`EVALUATE_FEASIBILITY`) using the MILP optimizer
   - Optionally delegates to specialist agents (`CALL_AGENT`)
   - Submits response (`SUBMIT_DR_RESPONSE`): accept, partial, or reject
4. Response is stored in `data/dr_responses/`
5. Aggregator collects and displays the response

### Upstream: Prosumer to Aggregator

1. Prosumer sends a free-text message via the dashboard
2. `process_prosumer_message()` handles it in a single LLM call:
   - Classifies intent (availability, constraint, asset update, etc.)
   - Extracts structured changes
   - Generates a confirmation message
3. Changes are applied to the portfolio (`data/portfolio.json`)
4. A household request record is created in `data/household_requests/`

## ReAct Loop

The orchestrator follows a Reasoning + Action pattern:

```
While iterations < max_iterations:
    1. LLM generates: Thought + Action
    2. Parse the action (e.g., GET_BATTERY_STATE, CALL_AGENT)
    3. Execute the tool
    4. Append observation to context
    5. If action is FINISH -> return result
```

Available actions:

| Action | Description |
|--------|-------------|
| `GET_PRICES` | Fetch day-ahead electricity prices from ENTSO-E |
| `CALCULATE_WINDOW_SUMS` | Compute cost/generation for a time window |
| `GET_BATTERY_STATE` | Read battery SoC, capacity, PV forecast |
| `EVALUATE_FEASIBILITY` | Run MILP optimizer for DR feasibility |
| `CALL_AGENT` | Delegate to a specialist agent |
| `SUBMIT_DR_RESPONSE` | Submit DR commitment to aggregator |
| `SCHEDULE` | Set appliance schedule |
| `FINISH` | End the loop with a final answer |

## Battery Optimizer (MILP)

The battery optimizer (`battery_optimizer.py`) formulates a mixed-integer linear program:

- **Decision variables**: Charge/discharge power per 15-minute slot
- **Objective**: Minimize electricity cost (or maximize revenue during DR)
- **Constraints**: SoC bounds, power limits, efficiency losses, DR commitment window
- **Solver**: PuLP with CBC (COIN-OR Branch and Cut)

When evaluating DR feasibility, it checks whether the battery can meet the requested discharge target while respecting minimum SoC and considering PV generation for pre-charging.

## Security

Input validation (`security.py`):
- `SecurityValidator` class with 50+ regex injection patterns (available but disabled in current release for research flexibility)
- API rate limiting provides the primary protection layer

API rate limiting (Flask-Limiter):
- Global: 200/day, 50/hour
- Per-endpoint limits (see [API Reference](API.md))

## Data Storage

All data is stored as JSON files:

| Directory | Content |
|-----------|---------|
| `data/dr_events/` | DR event definitions |
| `data/dr_responses/` | HEMS responses to DR events |
| `data/household_requests/` | Upstream prosumer requests |
| `data/event_logs/` | Execution traces |
| `data/runs/` | Orchestrator run logs |
| `data/market_obligations/` | Aggregator market commitments |

## Time Resolution

All scheduling uses 15-minute intervals (96 slots per 24 hours), consistent with European electricity market settlement periods.
