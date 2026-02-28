"""
Battery DR Feasibility Optimizer -- Tool backend for the battery agent.

Called by: assess_dr_feasibility tool in battery_agent_chat.py
Inputs:   dr_target_kw, dr_start_slot, dr_end_slot, compensation_eur_kwh
Loads:    household_demand.csv, pv_generation.csv, electricity_price.csv, battery_state.json
Returns:  {feasibility, max_deliverable_kw, economics, soc_impact, ...}

Behavior:
  1. Solves baseline (no DR) -- cost-optimal battery schedule
  2. Solves DR-committed -- forces discharge >= dr_target_kw during DR window
  3. If DR infeasible, binary-searches max deliverable kW (10 iterations, ~0.001 kW precision)
  4. Returns feasibility="full"|"partial"|"infeasible", opportunity cost, compensation, net benefit
  5. Persists full run data to data/runs/battery_optimizer/run_{timestamp}.json

All schedules use 96 x 15-min slots (slot 0 = 00:00, slot 68 = 17:00, slot 96 = 24:00).
MILP with Big-M binaries prevents simultaneous charge/discharge and import/export.
"""

import json
import csv
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

import pulp


def _log(section: str, msg: str):
    print(f"  [{section}] {msg}")


def _log_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# Constants
N_SLOTS = 96
DT = 0.25  # 15 minutes in hours
FEED_IN_TARIFF = 0.04  # EUR/kWh -- low feed-in typical of post-subsidy European markets

# Battery degradation cost (throughput model)
# C_deg = replacement_cost / (2 * capacity_kwh * max_cycles)
# Assumes ~8000 EUR replacement, 15 kWh, 6000 cycles -> ~0.044 EUR/kWh
# This penalizes every kWh cycled, discouraging unnecessary charge/discharge
DEGRADATION_COST_EUR_KWH = 0.015

# Peak-discharge smoothing weight (standard power-systems practice)
# Prevents LP degeneracy from concentrating discharge into a single slot.
# Small enough (~0.001 EUR/kW) to not affect cost-optimal decisions.
PEAK_DISCHARGE_WEIGHT = 0.001

DATA_DIR = Path(__file__).parent / "data"
PROFILES_DIR = DATA_DIR / "profiles"
OPTIMIZER_RUNS_DIR = DATA_DIR / "runs" / "battery_optimizer"


def load_profile(filename: str, value_column: str) -> List[float]:
    """Load a 96-slot (15-min) profile from data/profiles/{filename}. Returns list of floats."""
    filepath = PROFILES_DIR / filename
    values = []
    with open(filepath, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            values.append(float(row[value_column]))
    if len(values) != N_SLOTS:
        raise ValueError(f"Expected {N_SLOTS} rows in {filename}, got {len(values)}")
    return values


def load_battery_state() -> Dict[str, Any]:
    """Read data/battery_state.json. Returns dict with capacity_kwh, current_soc_kwh, power limits, efficiency."""
    state_path = DATA_DIR / "battery_state.json"
    with open(state_path, 'r') as f:
        return json.load(f)


def _save_optimizer_run(
    dr_request: Dict[str, Any],
    battery_state: Dict[str, Any],
    profile_summary: Dict[str, Any],
    baseline: Optional[Dict[str, Any]],
    dr_solution: Optional[Dict[str, Any]],
    result: Dict[str, Any],
    assess_time_s: float,
):
    """Persist full run data to data/runs/battery_optimizer/ for audit and reproducibility."""
    OPTIMIZER_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_data = {
        "timestamp": datetime.now().isoformat(),
        "dr_request": dr_request,
        "battery_state": battery_state,
        "profile_summary": profile_summary,
        "baseline": baseline,
        "dr_solution": dr_solution,
        "result": result,
        "assess_time_s": assess_time_s,
    }
    filepath = OPTIMIZER_RUNS_DIR / f"run_{ts}.json"
    with open(filepath, 'w') as f:
        json.dump(run_data, f, indent=2)
    _log("Saved", f"Optimizer run: {filepath.relative_to(Path(__file__).parent)}")


def _solve_battery_lp(
    demand: List[float],
    pv: List[float],
    price: List[float],
    soc_init_kwh: float,
    capacity_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    soc_min_kwh: float,
    soc_max_kwh: float,
    eta_ch: float,
    eta_dis: float,
    dr_target_kw: Optional[float] = None,
    dr_start_slot: Optional[int] = None,
    dr_end_slot: Optional[int] = None,
    lock_baseline: Optional[Dict[str, Any]] = None,
    lock_until_slot: int = 0,
    name: str = "battery"
) -> Optional[Dict[str, Any]]:
    """
    Internal solver. Minimizes daily electricity cost subject to energy balance,
    SoC dynamics, and optional DR discharge constraint.

    When dr_target_kw is None: baseline solve (no DR).
    When dr_target_kw is set: forces p_dis[t] >= dr_target_kw for slots [dr_start_slot, dr_end_slot).
    When lock_baseline is set with lock_until_slot > 0: fixes charge/discharge for
    slots 0..lock_until_slot-1 to the baseline solution (already happened, untouchable).

    Returns dict with total_cost_eur, soc_trajectory_kwh (97 values), charge/discharge
    schedules (96 values each), grid flows, solve_time_s. Returns None if infeasible.
    """
    prob = pulp.LpProblem(name, pulp.LpMinimize)

    M = 100.0  # Big-M upper bound for binary constraints

    # Variables: charge, discharge, grid import/export per slot; SoC per slot+1; direction binaries
    p_ch = [pulp.LpVariable(f"p_ch_{t}", lowBound=0, upBound=max_charge_kw) for t in range(N_SLOTS)]
    p_dis = [pulp.LpVariable(f"p_dis_{t}", lowBound=0, upBound=max_discharge_kw) for t in range(N_SLOTS)]
    grid_imp = [pulp.LpVariable(f"grid_imp_{t}", lowBound=0) for t in range(N_SLOTS)]
    grid_exp = [pulp.LpVariable(f"grid_exp_{t}", lowBound=0) for t in range(N_SLOTS)]
    soc = [pulp.LpVariable(f"soc_{t}", lowBound=soc_min_kwh, upBound=soc_max_kwh) for t in range(N_SLOTS + 1)]
    y_grid = [pulp.LpVariable(f"yg_{t}", cat="Binary") for t in range(N_SLOTS)]  # 1=import, 0=export
    y_bat = [pulp.LpVariable(f"yb_{t}", cat="Binary") for t in range(N_SLOTS)]   # 1=charge, 0=discharge
    d_peak = pulp.LpVariable("d_peak", lowBound=0)  # tracks max discharge across all slots

    # Lock slots before request arrival to baseline values (already happened)
    if lock_baseline and lock_until_slot > 0:
        bl_ch = lock_baseline["charge_schedule_kw"]
        bl_dis = lock_baseline["discharge_schedule_kw"]
        for t in range(lock_until_slot):
            prob += p_ch[t] == bl_ch[t]
            prob += p_dis[t] == bl_dis[t]

    # Objective: min net electricity cost + battery degradation cost + peak discharge smoothing
    # Degradation term penalizes every kWh cycled (throughput model)
    # Peak discharge term prevents LP degeneracy from concentrating discharge in one slot
    prob += pulp.lpSum(
        (grid_imp[t] * price[t] - grid_exp[t] * FEED_IN_TARIFF) * DT
        + (p_ch[t] + p_dis[t]) * DEGRADATION_COST_EUR_KWH * DT
        for t in range(N_SLOTS)
    ) + d_peak * PEAK_DISCHARGE_WEIGHT

    prob += soc[0] == soc_init_kwh

    for t in range(N_SLOTS):
        # Energy balance
        prob += pv[t] + grid_imp[t] + p_dis[t] == demand[t] + p_ch[t] + grid_exp[t]
        # SoC transition
        prob += soc[t + 1] == soc[t] + p_ch[t] * eta_ch * DT - p_dis[t] * (1.0 / eta_dis) * DT
        # No simultaneous import/export
        prob += grid_imp[t] <= M * y_grid[t]
        prob += grid_exp[t] <= M * (1 - y_grid[t])
        # No simultaneous charge/discharge
        prob += p_ch[t] <= max_charge_kw * y_bat[t]
        prob += p_dis[t] <= max_discharge_kw * (1 - y_bat[t])
        # DR window check (used by multiple constraints below)
        is_dr_window = (dr_target_kw is not None and dr_start_slot is not None
                        and dr_end_slot is not None and dr_start_slot <= t < dr_end_slot)

        # Discharge capped at demand -- battery covers household load, never exports.
        # Exempt during DR: DR discharge feeds the grid by design.
        if not is_dr_window:
            prob += p_dis[t] <= demand[t]

        # No discharge while PV is generating (standard residential self-consumption).
        if pv[t] > 0.01 and not is_dr_window:
            prob += p_dis[t] == 0

        # Peak discharge tracking (evening/night slots only)
        prob += d_peak >= p_dis[t]

        # Self-consumption priority: charge all PV surplus before exporting.
        # Standard residential battery behavior -- store everything, export only overflow.
        # Relaxes linearly as SoC approaches capacity (cannot charge when full).
        # Skip for locked slots (already fixed to baseline values).
        if t >= lock_until_slot:
            pv_surplus = max(pv[t] - demand[t], 0.0)
            if pv_surplus > 0:
                S = min(pv_surplus, max_charge_kw)
                prob += p_ch[t] + (S / soc_max_kwh) * soc[t] >= S

    # DR constraint: force minimum discharge during DR window
    if dr_target_kw is not None and dr_start_slot is not None and dr_end_slot is not None:
        for t in range(dr_start_slot, dr_end_slot):
            prob += p_dis[t] >= dr_target_kw

    # Log problem formulation
    n_vars = len(prob.variables())
    n_constraints = len(prob.constraints)
    prob_type = "MILP" if any(v.cat == "Binary" for v in prob.variables()) else "LP"
    dr_label = f", DR={dr_target_kw}kW" if dr_target_kw is not None else ""
    _log("Solver", f"{n_vars} vars, {n_constraints} constraints, {prob_type}{dr_label}")

    # Solve (suppress output)
    solver = pulp.PULP_CBC_CMD(msg=0)
    t0 = time.time()
    status = prob.solve(solver)
    solve_time = time.time() - t0

    if pulp.LpStatus[status] != "Optimal":
        _log("Solver", f"Status: {pulp.LpStatus[status]} | {solve_time:.3f}s")
        return None

    _log("Solver", f"Status: Optimal | {solve_time:.3f}s | Obj: {pulp.value(prob.objective):.4f} EUR")

    # Extract solution
    soc_trajectory = [soc[t].varValue for t in range(N_SLOTS + 1)]
    charge_schedule = [p_ch[t].varValue for t in range(N_SLOTS)]
    discharge_schedule = [p_dis[t].varValue for t in range(N_SLOTS)]
    grid_import_schedule = [grid_imp[t].varValue for t in range(N_SLOTS)]
    grid_export_schedule = [grid_exp[t].varValue for t in range(N_SLOTS)]

    total_cost = pulp.value(prob.objective)
    total_grid_import = sum(grid_imp[t].varValue * DT for t in range(N_SLOTS))
    total_grid_export = sum(grid_exp[t].varValue * DT for t in range(N_SLOTS))

    result = {
        "status": "optimal",
        "total_cost_eur": round(total_cost, 4),
        "total_grid_import_kwh": round(total_grid_import, 4),
        "total_grid_export_kwh": round(total_grid_export, 4),
        "soc_trajectory_kwh": [round(v, 4) for v in soc_trajectory],
        "charge_schedule_kw": [round(v, 4) for v in charge_schedule],
        "discharge_schedule_kw": [round(v, 4) for v in discharge_schedule],
        "final_soc_kwh": round(soc_trajectory[-1], 4),
        "solve_time_s": round(solve_time, 3),
    }

    _log("Solution", f"Cost: {total_cost:.4f} EUR | Grid import: {total_grid_import:.2f} kWh | Export: {total_grid_export:.2f} kWh")
    _log("Solution", f"SoC: {soc_init_kwh:.1f} -> {soc_trajectory[-1]:.1f} kWh | "
         f"Peak charge: {max(charge_schedule):.2f} kW | Peak discharge: {max(discharge_schedule):.2f} kW")

    return result


def assess_dr_feasibility(
    dr_target_kw: float,
    dr_start_slot: int,
    dr_end_slot: int,
    compensation_eur_kwh: float = 0.0,
    request_slot: int = 0,
) -> Dict[str, Any]:
    """
    Main entry point called by the battery agent's assess_dr_feasibility tool.

    Inputs (from agent tool call):
      dr_target_kw      - requested discharge power (kW)
      dr_start_slot     - first slot inclusive (e.g. 68 = 17:00)
      dr_end_slot       - last slot exclusive (e.g. 76 = 19:00)
      compensation_eur_kwh - aggregator payment rate (default 0.0)
      request_slot      - slot when the DR request arrives (default 0 = day-ahead).
                          Slots before request_slot are locked to the baseline schedule
                          (already executed, untouchable). Only slots from request_slot
                          onward can be re-optimized to accommodate DR.

    Output keys the agent should use:
      feasibility       - "full" | "partial" | "infeasible"
      max_deliverable_kw - actual deliverable power (= target if full)
      economics         - {baseline_cost_eur, dr_committed_cost_eur, opportunity_cost_eur,
                           compensation_rate_eur_kwh, dr_compensation_eur, net_benefit_eur}
      soc_impact        - SoC at DR window start/end/min, baseline vs DR final SoC
      reason            - explanation string (only if partial or infeasible)
    """
    _log_header("DR FEASIBILITY ASSESSMENT")
    assess_t0 = time.time()
    # Guard against None passed by LLM tool calls
    if compensation_eur_kwh is None:
        compensation_eur_kwh = 0.0

    demand = load_profile("household_demand.csv", "demand_kw")
    pv = load_profile("pv_generation.csv", "generation_kw")
    price = load_profile("electricity_price.csv", "price_eur_kwh")

    start_time_str = f"{(dr_start_slot * 15) // 60:02d}:{(dr_start_slot * 15) % 60:02d}"
    end_time_str = f"{(dr_end_slot * 15) // 60:02d}:{(dr_end_slot * 15) % 60:02d}"
    _log("Input", f"DR request: {dr_target_kw} kW, slots {dr_start_slot}-{dr_end_slot} ({start_time_str}-{end_time_str})")
    _log("Profiles", f"Demand: {sum(d * DT for d in demand):.2f} kWh total, {max(demand):.2f} kW peak")
    _log("Profiles", f"PV: {sum(p * DT for p in pv):.2f} kWh total, {max(pv):.2f} kW peak")
    _log("Profiles", f"Price: {min(price):.4f}-{max(price):.4f} EUR/kWh, mean {sum(price)/len(price):.4f}")

    bat = load_battery_state()
    capacity_kwh = bat["capacity_kwh"]
    soc_init_kwh = bat["current_soc_kwh"]
    max_charge_kw = bat["max_charge_kw"]
    max_discharge_kw = bat["max_discharge_kw"]
    soc_min_pct = bat["min_soc_pct"]
    rt_efficiency = bat["round_trip_efficiency"]

    soc_min_kwh = capacity_kwh * (soc_min_pct / 100.0)
    soc_max_kwh = capacity_kwh
    eta_ch = math.sqrt(rt_efficiency)
    eta_dis = math.sqrt(rt_efficiency)

    _log("Battery", f"Capacity: {capacity_kwh} kWh | SoC: {soc_init_kwh} kWh ({bat['current_soc_pct']}%)")
    _log("Battery", f"Power: {max_charge_kw} kW charge, {max_discharge_kw} kW discharge | Eff: {rt_efficiency}")
    _log("Battery", f"Min SoC: {soc_min_pct}% ({soc_min_kwh:.1f} kWh)")

    # Early exit: request exceeds hardware limit
    if dr_target_kw > max_discharge_kw:
        return {
            "success": True,
            "feasibility": "infeasible",
            "reason": f"Requested {dr_target_kw} kW exceeds max discharge power {max_discharge_kw} kW",
            "max_deliverable_kw": max_discharge_kw,
        }

    dr_slots = dr_end_slot - dr_start_slot
    dr_duration_h = dr_slots * DT
    dr_energy_kwh = dr_target_kw * dr_duration_h * (1.0 / eta_dis)  # accounting for discharge losses

    soc_headroom = soc_init_kwh - soc_min_kwh
    _log("DR", f"Energy needed (from battery): {dr_energy_kwh:.2f} kWh | SoC headroom: {soc_headroom:.2f} kWh")

    # Solve 1: Baseline (no DR)
    _log_header("SOLVE 1: BASELINE (no DR)")
    baseline = _solve_battery_lp(
        demand, pv, price,
        soc_init_kwh, capacity_kwh,
        max_charge_kw, max_discharge_kw,
        soc_min_kwh, soc_max_kwh,
        eta_ch, eta_dis,
        name="baseline"
    )

    if baseline is None:
        _log("Solver", "Baseline solve FAILED (should not happen)")
        return {
            "success": False,
            "feasibility": "error",
            "reason": "Baseline optimization failed (should not happen)",
        }

    # Solve 2: DR-committed (lock pre-request slots to baseline)
    _log_header("SOLVE 2: DR-COMMITTED")
    if request_slot > 0:
        _log("Lock", f"Slots 0-{request_slot - 1} locked to baseline (request arrives at slot {request_slot})")
    dr_solution = _solve_battery_lp(
        demand, pv, price,
        soc_init_kwh, capacity_kwh,
        max_charge_kw, max_discharge_kw,
        soc_min_kwh, soc_max_kwh,
        eta_ch, eta_dis,
        dr_target_kw=dr_target_kw,
        dr_start_slot=dr_start_slot,
        dr_end_slot=dr_end_slot,
        lock_baseline=baseline if request_slot > 0 else None,
        lock_until_slot=request_slot,
        name="dr_committed"
    )

    # If full target infeasible, binary-search for max deliverable kW
    max_deliverable_kw = dr_target_kw
    if dr_solution is None:
        _log_header("BINARY SEARCH: MAX DELIVERABLE kW")
        _log("Search", f"Full {dr_target_kw} kW infeasible, searching max deliverable...")
        lo, hi = 0.0, dr_target_kw
        best_solution = None
        # Binary search (10 iterations gives ~0.001 kW precision)
        for iteration in range(10):
            mid = (lo + hi) / 2.0
            trial = _solve_battery_lp(
                demand, pv, price,
                soc_init_kwh, capacity_kwh,
                max_charge_kw, max_discharge_kw,
                soc_min_kwh, soc_max_kwh,
                eta_ch, eta_dis,
                dr_target_kw=mid,
                dr_start_slot=dr_start_slot,
                dr_end_slot=dr_end_slot,
                lock_baseline=baseline if request_slot > 0 else None,
                lock_until_slot=request_slot,
                name="dr_search"
            )
            if trial is not None:
                lo = mid
                best_solution = trial
                _log("Search", f"  iter {iteration+1}: {mid:.3f} kW -> feasible  [lo={lo:.3f}, hi={hi:.3f}]")
            else:
                hi = mid
                _log("Search", f"  iter {iteration+1}: {mid:.3f} kW -> infeasible [lo={lo:.3f}, hi={hi:.3f}]")

        max_deliverable_kw = round(lo, 2)
        dr_solution = best_solution
        _log("Search", f"Max deliverable: {max_deliverable_kw} kW")

    # Derive economics: opportunity cost = DR cost - baseline cost
    _log_header("COMPARISON: BASELINE vs DR")
    opportunity_cost = 0.0
    dr_compensation = 0.0
    net_benefit = 0.0
    feasibility = "infeasible"

    if dr_solution is not None:
        opportunity_cost = round(dr_solution["total_cost_eur"] - baseline["total_cost_eur"], 4)
        delivered_kwh = max_deliverable_kw * dr_duration_h
        dr_compensation = round(delivered_kwh * compensation_eur_kwh, 4)
        net_benefit = round(dr_compensation - opportunity_cost, 4)

        if max_deliverable_kw >= dr_target_kw:
            feasibility = "full"
        else:
            feasibility = "partial"

    _log("Result", f"Feasibility: {feasibility.upper()}")
    if dr_solution is not None:
        _log("Result", f"Baseline cost: {baseline['total_cost_eur']:.4f} EUR | DR cost: {dr_solution['total_cost_eur']:.4f} EUR")
        _log("Result", f"Opportunity cost: {opportunity_cost:.4f} EUR | Compensation: {dr_compensation:.4f} EUR")
        _log("Result", f"Net benefit: {net_benefit:.4f} EUR")
    else:
        _log("Result", "No feasible DR solution found")

    # SoC at DR window boundaries (for agent to report impact)
    dr_soc_start = None
    dr_soc_end = None
    dr_soc_min = None
    if dr_solution is not None:
        soc_traj = dr_solution["soc_trajectory_kwh"]
        dr_soc_start = soc_traj[dr_start_slot]
        dr_soc_end = soc_traj[dr_end_slot]
        dr_soc_min = min(soc_traj[dr_start_slot:dr_end_slot + 1])

    # Assemble return dict (this is what the agent receives as tool output)
    start_time = f"{(dr_start_slot * 15) // 60:02d}:{(dr_start_slot * 15) % 60:02d}"
    end_time = f"{(dr_end_slot * 15) // 60:02d}:{(dr_end_slot * 15) % 60:02d}"

    result = {
        "success": True,
        "feasibility": feasibility,
        "dr_request": {
            "target_kw": dr_target_kw,
            "window": f"{start_time}-{end_time}",
            "duration_hours": dr_duration_h,
            "energy_requested_kwh": round(dr_target_kw * dr_duration_h, 2),
        },
        "max_deliverable_kw": max_deliverable_kw,
        "energy_deliverable_kwh": round(max_deliverable_kw * dr_duration_h, 2),
        "battery_state": {
            "initial_soc_kwh": soc_init_kwh,
            "initial_soc_pct": round(soc_init_kwh / capacity_kwh * 100, 1),
            "capacity_kwh": capacity_kwh,
            "min_soc_pct": soc_min_pct,
        },
        "economics": {
            "baseline_cost_eur": baseline["total_cost_eur"],
            "dr_committed_cost_eur": dr_solution["total_cost_eur"] if dr_solution else None,
            "opportunity_cost_eur": opportunity_cost,
            "compensation_rate_eur_kwh": compensation_eur_kwh,
            "dr_compensation_eur": dr_compensation,
            "net_benefit_eur": net_benefit,
        },
        "soc_impact": {
            "dr_window_soc_start_kwh": dr_soc_start,
            "dr_window_soc_end_kwh": dr_soc_end,
            "dr_window_soc_min_kwh": dr_soc_min,
            "baseline_final_soc_kwh": baseline["final_soc_kwh"],
            "dr_final_soc_kwh": dr_solution["final_soc_kwh"] if dr_solution else None,
        },
    }

    # Agent uses "reason" to explain partial/infeasible to the user
    if feasibility == "partial":
        result["reason"] = (
            f"Battery can deliver {max_deliverable_kw} kW (requested {dr_target_kw} kW). "
            f"Limited by available energy and household demand during DR window."
        )
    elif feasibility == "infeasible":
        result["reason"] = "Battery cannot deliver any flexibility during the requested window."

    # Persist run data
    assess_time = time.time() - assess_t0
    _log("Timing", f"Total assessment: {assess_time:.3f}s")
    try:
        _save_optimizer_run(
            dr_request={
                "target_kw": dr_target_kw,
                "start_slot": dr_start_slot,
                "end_slot": dr_end_slot,
                "compensation_eur_kwh": compensation_eur_kwh,
            },
            battery_state=bat,
            profile_summary={
                "demand_total_kwh": round(sum(d * DT for d in demand), 4),
                "demand_peak_kw": round(max(demand), 4),
                "pv_total_kwh": round(sum(p * DT for p in pv), 4),
                "pv_peak_kw": round(max(pv), 4),
                "price_min": round(min(price), 4),
                "price_max": round(max(price), 4),
                "price_mean": round(sum(price) / len(price), 4),
            },
            baseline=baseline,
            dr_solution=dr_solution,
            result=result,
            assess_time_s=round(assess_time, 3),
        )
    except Exception as e:
        _log("Saved", f"Warning: could not save run: {e}")

    return result


if __name__ == "__main__":
    # Test: 3 kW discharge, 17:00-19:00 (slots 68-76), 0.22 EUR/kWh compensation
    result = assess_dr_feasibility(
        dr_target_kw=3.0,
        dr_start_slot=68,
        dr_end_slot=76,
        compensation_eur_kwh=0.22,
    )
    print(json.dumps(result, indent=2))
