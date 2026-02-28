#!/usr/bin/env python3
"""
CDR Benchmark: Run 6 scenarios x 5 repetitions and collect metrics for paper table.

Scenarios:
  Downstream (aggregator -> prosumer):
    1. Full acceptance: DR event 3kW, HEMS evaluates and accepts
    2. Rejection: DR event 3kW, prosumer declines (blackout warning)
    3. Partial commitment: DR event 5kW, battery can only spare ~3kW

  Upstream (prosumer -> aggregator):
    4. Availability update: "I'm away on holiday next week..."
    5. Constraint tightening: "Keep my battery above 50%..."
    6. Asset addition: "I just plugged in my EV..."

Outputs:
  - Raw results JSON: data/benchmark_results_<timestamp>.json
  - Summary table printed to stdout
"""

import sys
import os
import json
import time
import numpy as np
from datetime import datetime

# Ensure we can import from the project
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(project_root, 'src'))
os.chdir(project_root)

from orchestrator_agent_react import OrchestratorAgentReAct
from aggregator_tools import dispatch_dr_event, process_prosumer_message

RUNS_PER_SCENARIO = 5
DELAY_BETWEEN_RUNS = 12  # seconds, to avoid Cerebras rate limits
DELAY_BETWEEN_SCENARIOS = 15  # extra delay between scenarios
MAX_RETRIES = 3


def count_tool_calls(result):
    """Count actual tool invocations (not FINISH/EXPLAIN output actions)."""
    tool_actions = {'CALL_AGENT', 'GET_BATTERY_STATE', 'EVALUATE_FEASIBILITY',
                    'SUBMIT_DR_RESPONSE', 'GET_PRICES', 'CALCULATE_WINDOW_SUMS'}
    actions = result.get('actions_taken', [])
    return sum(1 for a in actions if a.get('action', {}).get('type', '') in tool_actions)


def run_downstream(scenario_name, target_kw, prosumer_msg=None):
    """Run a downstream DR scenario and return metrics list."""
    print(f"\n{'='*60}")
    print(f"SCENARIO: {scenario_name} ({RUNS_PER_SCENARIO} runs)")
    print(f"{'='*60}")

    metrics = []
    for i in range(RUNS_PER_SCENARIO):
        print(f"\n--- Run {i+1}/{RUNS_PER_SCENARIO} ---")

        # Create fresh DR event each time
        dr_result = dispatch_dr_event(
            household_id='HH-001',
            window_start='17:00',
            window_end='19:00',
            target_kw=target_kw,
            compensation_eur_kwh=0.20
        )

        if not dr_result.get('success'):
            print(f"  [ERROR] Failed to create DR event: {dr_result}")
            metrics.append({'iterations': 0, 'tool_calls': 0, 'tokens': 0, 'time': 0, 'error': True})
            continue

        event_id = dr_result['event_id']
        print(f"  Created DR event: {event_id}")

        # Run HEMS DR handler with retry on 429
        result = None
        for attempt in range(MAX_RETRIES):
            try:
                orchestrator = OrchestratorAgentReAct()
                if prosumer_msg:
                    result = orchestrator.run_dr_response(event_id, prosumer_message=prosumer_msg)
                else:
                    result = orchestrator.run_dr_response(event_id)
                break  # Success
            except Exception as e:
                if '429' in str(e) and attempt < MAX_RETRIES - 1:
                    wait = DELAY_BETWEEN_RUNS * (attempt + 2)
                    print(f"  [RETRY] 429 rate limit, waiting {wait}s (attempt {attempt+1})")
                    time.sleep(wait)
                    # Create fresh event for retry
                    dr_result = dispatch_dr_event(
                        household_id='HH-001',
                        window_start='17:00',
                        window_end='19:00',
                        target_kw=target_kw,
                        compensation_eur_kwh=0.20
                    )
                    event_id = dr_result['event_id']
                else:
                    print(f"  [ERROR] {e}")
                    result = {
                        'iterations': 0, 'total_usage': {'total_tokens': 0},
                        'duration_seconds': 0, 'actions_taken': [],
                        'success': False, 'exit_reason': 'error'
                    }
                    break

        m = {
            'iterations': result.get('iterations', 0),
            'tool_calls': count_tool_calls(result),
            'tokens': result.get('total_usage', {}).get('total_tokens', 0),
            'time': round(result.get('duration_seconds', 0), 2),
            'success': result.get('success', False),
            'exit_reason': result.get('exit_reason', ''),
            'event_id': event_id,
            'error': not result.get('success', False)
        }
        metrics.append(m)
        print(f"  Result: {m['iterations']} iters, {m['tool_calls']} tools, "
              f"{m['tokens']} tokens, {m['time']}s")

        if i < RUNS_PER_SCENARIO - 1:
            time.sleep(DELAY_BETWEEN_RUNS)

    return metrics


def run_upstream(scenario_name, message):
    """Run an upstream prosumer message scenario and return metrics list."""
    print(f"\n{'='*60}")
    print(f"SCENARIO: {scenario_name} ({RUNS_PER_SCENARIO} runs)")
    print(f"{'='*60}")

    metrics = []
    for i in range(RUNS_PER_SCENARIO):
        print(f"\n--- Run {i+1}/{RUNS_PER_SCENARIO} ---")

        start = time.time()
        result = process_prosumer_message(
            household_id='HH-001',
            message=message,
            sandbox=True
        )
        elapsed = time.time() - start

        usage = result.get('llm_usage', {})
        m = {
            'iterations': 1,  # Single LLM call
            'tool_calls': 0,  # No tool use in upstream path
            'tokens': usage.get('total_tokens', 0),
            'time': round(usage.get('latency_seconds', elapsed), 2),
            'success': result.get('success', False),
            'request_type': result.get('request_type', ''),
            'error': False
        }
        metrics.append(m)
        print(f"  Result: {m['tokens']} tokens, {m['time']}s, type={m.get('request_type','')}")

        if i < RUNS_PER_SCENARIO - 1:
            time.sleep(DELAY_BETWEEN_RUNS)

    return metrics


def summarize(metrics):
    """Compute mean +/- std for each metric across runs."""
    valid = [m for m in metrics if not m.get('error')]
    if not valid:
        return {'iterations': '--', 'tool_calls': '--', 'tokens': '--', 'time': '--'}

    iters = [m['iterations'] for m in valid]
    tools = [m['tool_calls'] for m in valid]
    tokens = [m['tokens'] for m in valid]
    times = [m['time'] for m in valid]

    def fmt(vals):
        mean = np.mean(vals)
        std = np.std(vals, ddof=1) if len(vals) > 1 else 0
        if mean == int(mean) and std == 0:
            return str(int(mean))
        if std == 0:
            return f"{mean:.1f}"
        return f"{mean:.1f} +/- {std:.1f}"

    return {
        'iterations': fmt(iters),
        'tool_calls': fmt(tools),
        'tokens': fmt(tokens),
        'time': fmt(times),
        'n_valid': len(valid)
    }


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = {}

    # ---- DOWNSTREAM SCENARIOS ----
    print("\n" + "#"*60)
    print("# DOWNSTREAM SCENARIOS (aggregator -> prosumer)")
    print("#"*60)

    # 1. Full acceptance: standard 3kW DR event
    all_results['full_acceptance'] = run_downstream(
        "Full acceptance", target_kw=3.0
    )

    time.sleep(DELAY_BETWEEN_SCENARIOS)

    # 2. Rejection: prosumer declines due to blackout warning
    all_results['rejection'] = run_downstream(
        "Rejection", target_kw=3.0,
        prosumer_msg="I need my battery fully charged for a blackout warning tonight. I cannot participate in this DR event."
    )

    time.sleep(DELAY_BETWEEN_SCENARIOS)

    # 3. High-target acceptance: request 5kW. MILP optimizer pre-charges
    #    from PV before the DR window, enabling full commitment.
    all_results['partial_commitment'] = run_downstream(
        "High-target acceptance", target_kw=5.0
    )

    # ---- UPSTREAM SCENARIOS ----
    print("\n" + "#"*60)
    print("# UPSTREAM SCENARIOS (prosumer -> aggregator)")
    print("#"*60)

    time.sleep(DELAY_BETWEEN_SCENARIOS)

    # 4. Availability update
    all_results['availability_update'] = run_upstream(
        "Availability update",
        "I'm away on holiday next week. Maximize revenue from my battery and EV."
    )

    time.sleep(DELAY_BETWEEN_SCENARIOS)

    # 5. Constraint tightening
    all_results['constraint_change'] = run_upstream(
        "Constraint tightening",
        "Keep my battery above 50% from now on."
    )

    time.sleep(DELAY_BETWEEN_SCENARIOS)

    # 6. Asset addition
    all_results['asset_addition'] = run_upstream(
        "Asset addition",
        "I just plugged in my EV, it needs 80% by tomorrow 7am."
    )

    # ---- SAVE RAW RESULTS ----
    output_path = f"data/benchmark_results_{timestamp}.json"
    os.makedirs("data", exist_ok=True)

    # Summarize
    summaries = {}
    for name, metrics in all_results.items():
        summaries[name] = summarize(metrics)

    save_data = {
        'timestamp': timestamp,
        'runs_per_scenario': RUNS_PER_SCENARIO,
        'raw': all_results,
        'summaries': summaries
    }
    with open(output_path, 'w') as f:
        json.dump(save_data, f, indent=2)
    print(f"\n[Saved] Raw results: {output_path}")

    # ---- PRINT TABLE ----
    print("\n" + "="*80)
    print("CDR COMPUTATIONAL FEASIBILITY - RESULTS TABLE")
    print("="*80)
    print(f"{'Direction':<14} {'Scenario':<24} {'Iterations':<14} {'Tool calls':<14} {'Tokens':<18} {'Time (s)':<14}")
    print("-"*98)

    ds_scenarios = [
        ('Downstream', 'Full acceptance', 'full_acceptance'),
        ('', 'Rejection', 'rejection'),
        ('', 'High-target acceptance', 'partial_commitment'),
    ]
    us_scenarios = [
        ('Upstream', 'Availability update', 'availability_update'),
        ('', 'Constraint change', 'constraint_change'),
        ('', 'Asset addition', 'asset_addition'),
    ]

    for direction, label, key in ds_scenarios + us_scenarios:
        s = summaries[key]
        print(f"{direction:<14} {label:<24} {s['iterations']:<14} {s['tool_calls']:<14} {s['tokens']:<18} {s['time']:<14}")

    print(f"\n(n={RUNS_PER_SCENARIO} runs per scenario, values shown as mean +/- std)")


if __name__ == '__main__':
    main()
