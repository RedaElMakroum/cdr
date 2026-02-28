# Evaluation Guide

This guide explains how to reproduce the benchmark results from the paper.

## Benchmark Overview

The benchmark runs 6 scenarios (3 downstream, 3 upstream) x 5 repetitions each, measuring:

- **Iterations**: Number of ReAct loop cycles
- **Tool calls**: Number of tool invocations (excluding output actions)
- **Tokens**: Total LLM tokens consumed
- **Time**: Wall-clock execution time (seconds)

### Scenarios

| # | Direction | Scenario | Description |
|---|-----------|----------|-------------|
| 1 | Downstream | Full acceptance | 3 kW DR event, battery can fully commit |
| 2 | Downstream | Rejection | 3 kW DR event, prosumer declines (blackout warning) |
| 3 | Downstream | High-target request | 5 kW DR event, HEMS evaluates and commits via PV pre-charging |
| 4 | Upstream | Availability update | "I'm away on holiday next week..." |
| 5 | Upstream | Constraint tightening | "Keep my battery above 50%..." |
| 6 | Upstream | Asset addition | "I just plugged in my EV..." |

## Prerequisites

1. Complete the [Setup Guide](docs/SETUP.md)
2. Ensure your Cerebras API key is configured in `.env`
3. Ensure your ENTSO-E API key is configured in `.env`

## Running the Benchmark

```bash
source venv/bin/activate
python run_benchmark.py
```

The script will:
1. Run each scenario 5 times with delays between runs (to respect rate limits)
2. Retry on 429 (rate limit) errors with exponential backoff
3. Save raw results to `data/benchmark_results_<timestamp>.json`
4. Print a summary table to stdout

Expect the full benchmark to take approximately 15-20 minutes depending on API response times.

## Configuration

Key parameters in `run_benchmark.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `RUNS_PER_SCENARIO` | 5 | Number of repetitions per scenario |
| `DELAY_BETWEEN_RUNS` | 12 | Seconds between consecutive runs |
| `DELAY_BETWEEN_SCENARIOS` | 15 | Extra delay between different scenarios |
| `MAX_RETRIES` | 3 | Maximum retry attempts on 429 errors |

## Expected Results

Results will vary due to LLM non-determinism (even at temperature 0.0, Cerebras inference can produce slight variations). The paper reports:

| Direction | Scenario | Iterations | Tool calls | Tokens | Time (s) |
|-----------|----------|-----------|------------|--------|----------|
| Downstream | Full acceptance | 3.6 +/- 0.5 | 2.6 +/- 0.5 | ~23k | 8.3 +/- 2.1 |
| Downstream | Rejection | 5.0 +/- 0.7 | 3.6 +/- 0.5 | ~34k | 9.8 +/- 1.9 |
| Downstream | High-target request | 3.4 +/- 0.5 | 2.4 +/- 0.5 | ~22k | 7.8 +/- 2.2 |
| Upstream | Availability update | 1 | 0 | ~1.3k | 1.3 +/- 0.8 |
| Upstream | Constraint tightening | 1 | 0 | ~1.0k | 1.7 +/- 1.6 |
| Upstream | Asset addition | 1 | 0 | ~1.6k | 1.3 +/- 1.2 |

Downstream scenarios use a multi-step ReAct loop (3-5 iterations), while upstream scenarios use a single LLM call for classification and extraction.

## Output Format

The results JSON file contains:

```json
{
  "timestamp": "20260228_160949",
  "runs_per_scenario": 5,
  "raw": {
    "full_acceptance": [
      {
        "iterations": 4,
        "tool_calls": 3,
        "tokens": 25431,
        "time": 9.12,
        "success": true,
        "exit_reason": "finished",
        "event_id": "DR-...",
        "error": false
      }
    ]
  },
  "summaries": {
    "full_acceptance": {
      "iterations": "3.6 +/- 0.5",
      "tool_calls": "2.6 +/- 0.5",
      "tokens": "23422.4 +/- 4041.8",
      "time": "8.3 +/- 2.1",
      "n_valid": 5
    }
  }
}
```

## Troubleshooting

- **429 Rate Limit errors**: The script handles these automatically with retries. If you see many failures, increase `DELAY_BETWEEN_RUNS`.
- **Missing API keys**: Ensure `.env` is configured. See [Setup Guide](docs/SETUP.md).
- **Different token counts**: Token counts depend on the exact model version. Cerebras periodically updates their inference stack, which may cause minor variations.
- **Different iteration counts**: The ReAct loop is sensitive to model behavior. Slight differences (e.g., 3 vs 4 iterations) are expected.
