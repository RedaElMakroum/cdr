"""
LLM-Based HEMS Orchestrator Agent (ReAct Pattern)
Uses Reasoning-Action pattern for Cerebras (no tool calling support needed).
"""

import json
import re
import requests
import sys
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

from tools import get_electricity_prices, call_appliance_agent, schedule_appliance, get_calendar_ev_constraint, calculate_window_sums, get_battery_state
from config import CEREBRAS_API_KEY, CEREBRAS_MODEL, TEMPERATURE, AVAILABLE_APPLIANCES, REASONING_EFFORT, REASONING_FORMAT
from security import validate_and_prepare_input
from event_logger import log_event


class OrchestratorAgentReAct:
    """LLM-based orchestrator using ReAct pattern (reasoning + action)."""

    def __init__(self):
        """Initialize the ReAct orchestrator agent."""
        # Create enhanced system prompt with tool instructions
        self.system_prompt = self._create_system_prompt()

        self.api_key = CEREBRAS_API_KEY
        # Allow model override from environment (set by API)
        self.model = os.environ.get('CEREBRAS_MODEL_OVERRIDE', CEREBRAS_MODEL)
        self.temperature = TEMPERATURE
        self.base_url = "https://api.cerebras.ai/v1"

        print(f"[Orchestrator] Using model: {self.model}")

    def _build_household_assets_section(self) -> str:
        """Build the household assets section from config.py AVAILABLE_APPLIANCES."""
        lines = ["You manage the following assets:\n"]
        idx = 1
        for appliance_id, spec in AVAILABLE_APPLIANCES.items():
            if appliance_id == "battery":
                cap = spec.get("capacity_kwh", "?")
                pwr = spec.get("power_rating_kw", "?")
                min_soc = spec.get("min_soc_pct", 20)
                eff = int(spec.get("round_trip_efficiency", 0.9) * 100)
                lines.append(f"{idx}. **battery** - Home battery, {cap} kWh capacity, {pwr} kW charge/discharge, {eff}% round-trip efficiency, min SoC {min_soc}%")
            else:
                pwr = spec.get("power_rating_kw", "?")
                dur = spec.get("default_duration_minutes", "?")
                slots = int(dur / 15) if isinstance(dur, (int, float)) else "?"
                extra = ", V2G capable" if appliance_id == "ev_charger" else ""
                lines.append(f"{idx}. **{appliance_id}** - {pwr} kW, {dur} minutes ({slots} slots){extra}")
            idx += 1
        return "\n".join(lines)

    def _create_system_prompt(self) -> str:
        """Create system prompt with ReAct pattern instructions."""
        # Load base orchestrator prompt
        prompt_path = Path(__file__).parent.parent / "prompts" / "hems_orchestrator.md"
        with open(prompt_path, 'r') as f:
            base_prompt = f.read()

        # Inject household asset specs from config
        base_prompt = base_prompt.replace("{HOUSEHOLD_ASSETS}", self._build_household_assets_section())

        # Add ReAct instructions
        react_instructions = """

## ReAct Pattern: Reasoning and Action

You will work through this task step-by-step using a Thought-Action-Observation cycle.

### Available Actions

You can perform these actions by outputting them in the specified format:

**Action: GET_PRICES**
Fetches electricity prices for the next 24 hours.
Format: `ACTION: GET_PRICES`

**Action: GET_CALENDAR_CONSTRAINT**
Fetches calendar events and extracts EV charging constraints.
Format: `ACTION: GET_CALENDAR_CONSTRAINT`

**Action: GET_BATTERY_STATE**
Reads current battery state (SoC, capacity, power limits, available energy).
Format: `ACTION: GET_BATTERY_STATE`
Use this before calling the battery agent for DR events to know exact SoC.

**Action: CALCULATE_WINDOW_SUMS**
Calculates sums for all consecutive price windows of a given size.
Format: `ACTION: CALCULATE_WINDOW_SUMS | window_size=<slots>`
Example: `ACTION: CALCULATE_WINDOW_SUMS | window_size=12` (for 3-hour windows)

**Action: CALL_AGENT**
Delegates to a specialist appliance agent.
Format: `ACTION: CALL_AGENT | agent_name=<name> | user_request=<request>`
Example: `ACTION: CALL_AGENT | agent_name=washing_machine_agent | user_request=Schedule for 2 hours, optimize for cost`

**Action: SCHEDULE**
Executes a schedule for an appliance.
Format: `ACTION: SCHEDULE | appliance_id=<id> | start_slot=<slot> | duration_slots=<slots> | reasoning=<why>`
Example: `ACTION: SCHEDULE | appliance_id=washing_machine | start_slot=14 | duration_slots=8 | reasoning=Optimal cost window`

**Action: EVALUATE_FEASIBILITY**
Aggregates appliance agent results into a DR feasibility assessment. Only use during DR event handling.
Format: `ACTION: EVALUATE_FEASIBILITY`

**Action: EXPLAIN_TO_PROSUMER**
Signals the system to pause and show your explanation to the prosumer. Only use during DR event handling.
**CRITICAL**: You MUST write the full conversational explanation to the prosumer BEFORE this action line. The text you write before ACTION: EXPLAIN_TO_PROSUMER is what the prosumer will see. If you output the action without explanation text, the prosumer sees nothing.
Format: Write your explanation first, then on a new line: `ACTION: EXPLAIN_TO_PROSUMER`

**Action: SUBMIT_DR_RESPONSE**
Submits the prosumer's decision (accept/reject) back to the aggregator. Only use during DR event handling.
Format: `ACTION: SUBMIT_DR_RESPONSE | accepted=<true/false> | commitment_kw=<kw> | appliances=<comma-separated list> | reasoning=<text>`
Example: `ACTION: SUBMIT_DR_RESPONSE | accepted=true | commitment_kw=2.0 | appliances=washing_machine | reasoning=Prosumer approved shifting washing machine`

**Action: FINISH**
Completes orchestration and presents final summary to user.
Format: `ACTION: FINISH | summary=<your summary message>`

### Your Workflow

**STEP 0: Determine Request Type**

First, determine if the input is:
- **Scheduling request** -- user wants to schedule appliances (normal workflow)
- **DR event** -- an incoming demand response event from the aggregator (DR workflow)
- **Out of scope** -- unrelated to HEMS

Valid HEMS requests involve:
- Scheduling appliances (washing machine, dishwasher, EV, heat pump, battery)
- Optimizing energy consumption timing
- Checking electricity prices or price patterns
- Coordinating multiple flexible loads
- Evaluating and responding to DR events from the aggregator (battery is the primary DR asset)

If the request is completely unrelated (e.g., sports scores, general knowledge, unrelated tasks), immediately respond:
```
Thought: This request is outside my scope as a Home Energy Management System. I can only help with appliance scheduling and energy optimization.
ACTION: FINISH | summary=I can only help with home energy management tasks like scheduling appliances (washing machine, dishwasher, EV, battery) and optimizing energy consumption. Please ask me about scheduling your flexible loads or checking electricity prices.
```

**STEP 1+: Normal Workflow**

For valid HEMS requests, follow this cycle:

1. **Thought**: Explain what you're thinking and what action to take next
2. **Action**: Output EXACTLY ONE action in the format above, then STOP
3. **Observation**: The system will execute the action and show you the result
4. **Repeat** until you execute ACTION: FINISH

**Analytical Queries**: For price analysis, use CALCULATE_WINDOW_SUMS with appropriate window_size (e.g., 1 hour = 4 slots at 15min resolution). To identify expensive periods, use the MAXIMUM sum; to find cheap periods, use the MINIMUM sum.

**CRITICAL**: After outputting an ACTION, you MUST STOP and wait for the system to provide an Observation. DO NOT continue reasoning, DO NOT assume what the result will be, DO NOT output multiple actions in one response. Output ONE action, then wait.

### Required Workflow Order (CRITICAL)

**PRIORITY 3 FIX**: You MUST follow this exact sequence:

1. **First**: ACTION: GET_PRICES (always required)
2. **Second** (if EV involved): ACTION: GET_CALENDAR_CONSTRAINT (BEFORE calling any agents)
3. **Third**: ACTION: CALL_AGENT (for each appliance, one at a time)
4. **Fourth**: ACTION: SCHEDULE (after each agent recommendation)
5. **Final**: ACTION: FINISH (when all schedules executed)

**EV Detection**: If the user request mentions EV or charging keywords, call GET_CALENDAR_CONSTRAINT before calling agents.

**DR Event Detection**: If the input is a DR event, call GET_BATTERY_STATE before calling the battery agent. The battery is the primary flexible asset for DR events.

### Example

```
Thought: The user wants to schedule all flexible loads. First, I need to fetch electricity prices.
ACTION: GET_PRICES

[System executes and shows prices]

Thought: I have prices. The request mentions "all", which likely includes EV. I MUST check calendar constraints BEFORE calling any agents to ensure agents have complete constraint information.
ACTION: GET_CALENDAR_CONSTRAINT

[System shows calendar result]

Thought: Calendar shows user has work at 8am, so EV must be charged by 7:30am (slot 30). Now I can call each appliance agent with full constraint knowledge. Starting with washing machine...
ACTION: CALL_AGENT | agent_name=washing_machine_agent | user_request=Schedule for lowest cost

[System shows agent recommendation]

Thought: Washing machine agent recommends slot 50. Now I'll execute this schedule.
ACTION: SCHEDULE | appliance_id=washing_machine | start_slot=50 | duration_slots=8 | reasoning=Cost-optimized window

[Continue until all agents called and schedules executed]

Thought: All schedules executed successfully. Time to present final summary.
ACTION: FINISH | summary=I've optimized schedules for 4 appliances: ...
```

### DR Event Workflow Order

When handling a DR event, do NOT call GET_PRICES. Prices are irrelevant -- the aggregator provides the compensation rate directly.

1. **First**: ACTION: GET_BATTERY_STATE (check battery SoC and capacity)
2. **Second**: ACTION: CALL_AGENT (call battery_agent with battery state and DR details)
3. **Third**: ACTION: EVALUATE_FEASIBILITY (aggregate what the household can offer)
4. **Fourth**: Write the conversational explanation, then ACTION: EXPLAIN_TO_PROSUMER (STOP and wait)
5. **After prosumer responds**: ACTION: SUBMIT_DR_RESPONSE
6. **Final**: ACTION: FINISH

### DR Event Example

```
Thought: This is a DR event from the aggregator requesting 3 kW discharge between 17:00-19:00. Prices are not needed for DR events. Let me check the battery state first.
ACTION: GET_BATTERY_STATE

[System shows battery: 60% SoC, 6.0 kWh, 4.0 kWh available above min]

Thought: Battery has 4.0 kWh available above minimum SoC. The request is 3 kW for 2 hours = 6.0 kWh, which exceeds available energy. Let me ask the battery agent for a detailed assessment.
ACTION: CALL_AGENT | agent_name=battery_agent | user_request=DR event requesting 3 kW discharge for 17:00-19:00 (2 hours). Battery at 60% SoC (6.0 kWh), 4.0 kWh available above 20% min. Assess feasibility.

[System shows agent result]

Thought: Battery agent says partially feasible -- can offer 3 kW for 80 minutes. Let me evaluate overall feasibility.
ACTION: EVALUATE_FEASIBILITY

[System shows feasibility assessment]

Thought: We can offer 3 kW for 80 minutes. Now I need to explain this to the prosumer. I must write the full explanation BEFORE the action line -- this is what the prosumer will read.

Your aggregator is requesting a 3 kW discharge between 17:00-19:00.

Here's what I found:
- Your battery is at 60% SoC with 4.0 kWh available above the 20% reserve.
- The full 6 kWh request can't be met, but I can offer 3 kW for 80 minutes.
- You'd earn approximately 0.80 EUR, with your SoC dropping to about 25%.

Would you like me to accept this partial offer, or decline?

ACTION: EXPLAIN_TO_PROSUMER

[System pauses -- prosumer sees the explanation above and can respond]
```

**IMPORTANT**: Always output actions in the exact format shown. The system will parse your output and execute the actions.
"""

        return base_prompt + react_instructions

    def _call_llm(self, messages: List[Dict[str, Any]]) -> str:
        """Call the LLM and return response text with usage and latency."""
        import time as _time
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 8000,  # Allows comprehensive reasoning and detailed summaries
            "stream": False,
            "reasoning_effort": REASONING_EFFORT,
            "reasoning_format": REASONING_FORMAT,
        }

        call_start = _time.time()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120
        )
        response.raise_for_status()
        call_latency = _time.time() - call_start

        result = response.json()
        msg = result['choices'][0]['message']
        usage = result.get('usage', {})
        usage['latency_seconds'] = round(call_latency, 3)

        # Extract and display reasoning if present
        reasoning = msg.get('reasoning', '')
        if reasoning:
            print(f"\n[Reasoning - Orchestrator]\n{reasoning}\n")
            usage['reasoning_text'] = reasoning
            usage['has_reasoning'] = True
        else:
            usage['has_reasoning'] = False

        return msg.get('content', ''), usage

    def _parse_action(self, response: str) -> Optional[Dict[str, Any]]:
        """Parse action from LLM response with flexible matching."""
        # Extract just the ACTION line (not trailing text after it)
        action_line_match = re.search(r'ACTION:.*', response, re.IGNORECASE)
        if action_line_match:
            response = action_line_match.group(0)

        # Try multiple patterns for robustness across different LLMs

        # Pattern 1: Standard format - ACTION: TYPE | params
        action_match = re.search(r'ACTION:\s*([A-Z_]+)(?:\s*\|\s*(.+))?$', response, re.IGNORECASE)

        # Pattern 2: Allow underscores/hyphens in action name
        if not action_match:
            action_match = re.search(r'ACTION:\s*([A-Z_-]+)(?:\s*\|\s*(.+))?$', response, re.IGNORECASE)

        # Pattern 3: Allow ACTION without colon (some models forget it)
        if not action_match:
            action_match = re.search(r'ACTION\s+([A-Z_]+)(?:\s*\|\s*(.+))?$', response, re.IGNORECASE)

        if not action_match:
            return None

        action_type = action_match.group(1).upper().replace('-', '_').replace(' ', '_')
        action_params_str = action_match.group(2)

        action = {"type": action_type}

        # Parse parameters if present
        if action_params_str:
            params = {}
            parts = action_params_str.split('|')
            for param in parts:
                param = param.strip()
                if '=' in param:
                    key, value = param.split('=', 1)
                    key = key.strip().strip('"\'')
                    value = value.strip().strip('"\'')
                    params[key] = value
            action["params"] = params

        return action

    def _execute_action(self, action: Dict[str, Any], context: Dict[str, Any]) -> str:
        """Execute an action and return observation."""
        action_type = action["type"]
        params = action.get("params", {})

        if action_type == "GET_PRICES":
            print("\n   [Action] Fetching electricity prices...")
            prices_data = get_electricity_prices()
            context["prices_data"] = prices_data
            return f"✓ Fetched {len(prices_data['prices'])} price points for {prices_data['date']}. Price range: {min(prices_data['prices']):.4f} - {max(prices_data['prices']):.4f} EUR/kWh. Prices stored in context."

        elif action_type == "GET_CALENDAR_CONSTRAINT":
            print("\n   [Action] Checking calendar for constraints...")
            constraint = get_calendar_ev_constraint()
            context["calendar_constraint"] = constraint
            if constraint:
                return f"✓ Calendar constraint found: Event '{constraint['event_title']}' at {constraint['event_time']}. EV deadline: {constraint['deadline_time']}. Reasoning: {constraint['reasoning']}"
            else:
                return "ℹ No calendar constraints found."

        elif action_type == "GET_BATTERY_STATE":
            print("\n   [Action] Reading battery state...")
            battery_state = get_battery_state()
            if battery_state.get("success"):
                context["battery_state"] = battery_state
                return (
                    f"✓ Battery state: {battery_state['current_soc_pct']}% SoC "
                    f"({battery_state['current_soc_kwh']} kWh / {battery_state['capacity_kwh']} kWh). "
                    f"Available above min SoC: {battery_state['available_energy_kwh']} kWh. "
                    f"Max discharge: {battery_state['max_discharge_kw']} kW. "
                    f"PV forecast: {battery_state.get('pv_forecast_kwh', 'N/A')} kWh."
                )
            return f"✗ Error: {battery_state.get('error', 'Unknown error')}"

        elif action_type == "CALCULATE_WINDOW_SUMS":
            window_size = params.get("window_size")

            if not window_size:
                return "✗ Error: Missing window_size parameter"

            if "prices_data" not in context:
                return "✗ Error: Must call GET_PRICES before calculating window sums"

            try:
                window_size = int(window_size)
            except (ValueError, TypeError):
                return "✗ Error: window_size must be an integer"

            print(f"\n   [Action] Calculating window sums for {window_size} slots...")
            result = calculate_window_sums(
                prices=context["prices_data"]["prices"],
                window_size=window_size
            )

            if result.get("success"):
                # Find both min and max for completeness
                min_idx = result["min_window_index"]
                min_sum = result["min_window_sum"]
                max_idx = result["window_sums"].index(max(result["window_sums"]))
                max_sum = result["window_sums"][max_idx]

                return (f"✓ Calculated {result['window_count']} windows of size {window_size} slots. "
                       f"Minimum sum: {min_sum:.2f} at slot {min_idx} ({self._slot_to_time(min_idx)}). "
                       f"Maximum sum: {max_sum:.2f} at slot {max_idx} ({self._slot_to_time(max_idx)}).")
            else:
                return f"✗ Calculation failed: {result.get('error', 'Unknown error')}"

        elif action_type == "CALL_AGENT":
            agent_name = params.get("agent_name")
            user_request = params.get("user_request")

            if not agent_name or not user_request:
                return "✗ Error: Missing agent_name or user_request parameters"

            # DR events don't need prices -- skip the check if a DR event is in context
            is_dr = "dr_event" in context
            if "prices_data" not in context and not is_dr:
                return "✗ Error: Must call GET_PRICES before calling agents"

            print(f"\n   [Action] Calling {agent_name}...")
            result = call_appliance_agent(
                agent_name=agent_name,
                prices_data=context.get("prices_data"),
                user_request=user_request
            )

            # Store agent result ALWAYS (even on error, for debugging)
            appliance_id = agent_name.replace("_agent", "")
            if "agent_results" not in context:
                context["agent_results"] = {}
            context["agent_results"][appliance_id] = result

            if "error" in result:
                return f"✗ Agent error: {result['error']}"

            # Variable-control assets (battery): return feasibility assessment directly
            if result.get('recommended_slot') is None:
                return f"✓ Agent {agent_name} feasibility assessment: {result['reasoning'][:300]}"

            # Validate agent recommendation against actual price data
            validation_result = self._validate_agent_recommendation(
                result,
                context["prices_data"],
                appliance_id,
                context
            )

            # Format cost (handle None for heat pump)
            cost = result.get('cost')
            cost_str = f"€{cost:.3f}" if cost is not None else "TBD"

            base_msg = f"✓ Agent recommended: Slot {result['recommended_slot']} ({self._slot_to_time(result['recommended_slot'])}), duration {result['duration_slots']} slots, cost {cost_str}. Reasoning: {result['reasoning'][:100]}..."

            # Append validation warning if present
            if validation_result:
                return base_msg + f"\n\n⚠️  VALIDATION WARNING: {validation_result}"

            return base_msg

        elif action_type == "SCHEDULE":
            appliance_id = params.get("appliance_id")
            start_slot = params.get("start_slot")
            duration_slots = params.get("duration_slots")
            reasoning = params.get("reasoning", "LLM orchestrator recommendation")

            # Validate parameters
            try:
                start_slot = int(start_slot)
                duration_slots = int(duration_slots)
            except (ValueError, TypeError):
                return "✗ Error: start_slot and duration_slots must be integers"

            print(f"\n   [Action] Executing schedule for {appliance_id}...")
            schedule_result = schedule_appliance(
                appliance_id=appliance_id,
                start_slot=start_slot,
                duration_slots=duration_slots,
                user_info=reasoning
            )

            if schedule_result.get("success"):
                if "executed_schedules" not in context:
                    context["executed_schedules"] = []
                context["executed_schedules"].append({
                    "appliance_id": appliance_id,
                    "schedule": schedule_result["schedule"]  # Extract just the 96-element array
                })
                return f"✓ Schedule executed: {appliance_id} from {schedule_result['start_time']} to {schedule_result['end_time']} ({schedule_result['duration_minutes']} minutes)"
            else:
                return f"✗ Schedule failed: {schedule_result.get('error', 'Unknown error')}"

        elif action_type == "EVALUATE_FEASIBILITY":
            print("\n   [Action] Evaluating DR feasibility...")
            dr_event = context.get("dr_event", {})
            agent_results = context.get("agent_results", {})
            battery_state = context.get("battery_state", {})

            if not agent_results:
                return "✗ Error: No agent results available. Call appliance agents first."

            total_kw = 0
            contributing = []
            soc_info = ""
            for appliance_id, result in agent_results.items():
                if "error" not in result:
                    power_kw = AVAILABLE_APPLIANCES.get(appliance_id, {}).get("power_rating_kw", 0)
                    total_kw += power_kw
                    contributing.append({"appliance_id": appliance_id, "power_kw": power_kw})

            # Add battery SoC context if available
            if battery_state and battery_state.get("success"):
                available_kwh = battery_state.get("available_energy_kwh", 0)
                current_soc = battery_state.get("current_soc_pct", 0)
                soc_info = f" Battery SoC: {current_soc}%, available energy: {available_kwh} kWh above min."

            target_kw = dr_event.get("target_kw", 0)
            feasible = total_kw >= target_kw

            context["feasibility"] = {
                "total_available_kw": total_kw,
                "target_kw": target_kw,
                "feasible": feasible,
                "contributing_appliances": contributing,
                "battery_state": battery_state
            }

            asset_list = ", ".join(f"{a['appliance_id']} ({a['power_kw']} kW)" for a in contributing)
            status = "FEASIBLE" if feasible else "PARTIALLY FEASIBLE"
            return f"✓ {status}: {total_kw} kW available from {len(contributing)} assets ({asset_list}). Target: {target_kw} kW.{soc_info}"

        elif action_type == "EXPLAIN_TO_PROSUMER":
            print("\n   [Action] Generating prosumer explanation...")
            dr_event = context.get("dr_event", {})
            feasibility = context.get("feasibility", {})

            window = f"{dr_event.get('window_start', '?')} - {dr_event.get('window_end', '?')}"
            target = dr_event.get("target_kw", 0)
            compensation_rate = dr_event.get("compensation_eur_kwh", 0)
            duration_hours = dr_event.get("duration_slots", 0) * 15 / 60
            max_earnings = target * duration_hours * compensation_rate

            appliances = feasibility.get("contributing_appliances", [])
            appliance_names = [a["appliance_id"].replace("_", " ") for a in appliances]

            context["prosumer_explanation_ready"] = True
            context["awaiting_prosumer_response"] = True

            return (
                f"✓ Explanation prepared for prosumer. DR event: {target} kW reduction during {window}. "
                f"Potential earnings: {max_earnings:.2f} EUR. "
                f"Contributing appliances: {', '.join(appliance_names)}. "
                f"Now waiting for prosumer response."
            )

        elif action_type == "SUBMIT_DR_RESPONSE":
            from aggregator_tools import submit_dr_response

            event_id = context.get("dr_event", {}).get("event_id")
            if not event_id:
                return "✗ Error: No DR event in context"

            accepted = params.get("accepted", "false").lower() == "true"
            commitment_kw = float(params.get("commitment_kw", 0))
            appliances_str = params.get("appliances", "")
            appliances_list = [a.strip() for a in appliances_str.split(",") if a.strip()]
            reasoning = params.get("reasoning", "")

            print(f"\n   [Action] Submitting DR response for {event_id}...")
            result = submit_dr_response(
                event_id=event_id,
                accepted=accepted,
                commitment_kw=commitment_kw,
                accepted_appliances=appliances_list,
                reasoning=reasoning,
                conversation_summary=context.get("final_summary", "")
            )

            if result["success"]:
                context["response_submitted"] = True
                return f"✓ Response submitted: {'Accepted' if accepted else 'Rejected'}. Committed: {commitment_kw} kW."
            return f"✗ Failed to submit response: {result.get('error', 'Unknown error')}"

        elif action_type == "FINISH":
            summary = params.get("summary", "Orchestration completed.")
            context["final_summary"] = summary
            return f"✓ Orchestration complete. Final summary ready."

        else:
            return f"✗ Unknown action type: {action_type}"

    def _slot_to_time(self, slot: int) -> str:
        """Convert slot index to HH:MM time string."""
        hours = (slot * 15) // 60
        minutes = (slot * 15) % 60
        return f"{hours:02d}:{minutes:02d}"

    def _validate_agent_recommendation(
        self,
        agent_result: Dict[str, Any],
        prices_data: Dict[str, Any],
        appliance_id: str,
        context: Dict[str, Any]
    ) -> Optional[str]:
        """
        Validate agent's recommendation against actual price data.
        Returns warning message if significant discrepancy detected, None otherwise.
        """
        # Skip validation for heat pump (complex thermal constraints)
        if appliance_id == "heat_pump":
            return None

        # Extract agent recommendation
        recommended_slot = agent_result.get("recommended_slot")
        duration_slots = agent_result.get("duration_slots")
        agent_cost = agent_result.get("cost")

        if not all([recommended_slot is not None, duration_slots, agent_cost]):
            return None  # Cannot validate without complete data

        # Get appliance power rating
        power_kw = AVAILABLE_APPLIANCES.get(appliance_id, {}).get("power_rating_kw", 1.8)

        # Calculate actual optimal window
        prices = prices_data["prices"]
        min_cost = float('inf')
        optimal_slot = 0

        # Evaluate all possible windows
        for start_slot in range(96 - duration_slots + 1):
            window_prices = prices[start_slot:start_slot + duration_slots]
            # Divide by 1000 to convert EUR/MWh to EUR
            window_cost = sum(price * power_kw * 0.25 / 1000 for price in window_prices)

            if window_cost < min_cost:
                min_cost = window_cost
                optimal_slot = start_slot

        # Calculate agent's window cost for verification
        agent_window_prices = prices[recommended_slot:recommended_slot + duration_slots]
        # Divide by 1000 to convert EUR/MWh to EUR
        agent_window_cost = sum(price * power_kw * 0.25 / 1000 for price in agent_window_prices)

        # Calculate discrepancy
        cost_diff = agent_window_cost - min_cost
        percentage_diff = (cost_diff / min_cost) * 100 if min_cost > 0 else 0

        # Threshold for triggering warning (20% worse than optimal)
        DISCREPANCY_THRESHOLD = 20.0

        if percentage_diff > DISCREPANCY_THRESHOLD:
            # Track retry attempts
            if "agent_retries" not in context:
                context["agent_retries"] = {}
            retry_count = context["agent_retries"].get(appliance_id, 0)

            # Only suggest retry if haven't exceeded max retries
            MAX_RETRIES = 1
            if retry_count < MAX_RETRIES:
                context["agent_retries"][appliance_id] = retry_count + 1
                return (
                    f"Agent recommended slot {recommended_slot} ({self._slot_to_time(recommended_slot)}) "
                    f"at €{agent_window_cost:.4f}, but actual optimal is slot {optimal_slot} "
                    f"({self._slot_to_time(optimal_slot)}) at €{min_cost:.4f}. "
                    f"Discrepancy: {percentage_diff:.1f}% higher than optimal. "
                    f"Consider calling {appliance_id}_agent again with explicit instruction to find the global minimum."
                )
            else:
                return (
                    f"Agent recommended slot {recommended_slot} at €{agent_window_cost:.4f} "
                    f"(actual optimal: slot {optimal_slot} at €{min_cost:.4f}, {percentage_diff:.1f}% discrepancy). "
                    f"Max retries reached - proceeding with agent recommendation."
                )

        return None

    def _save_run_data(self, result: Dict[str, Any], context: Dict[str, Any]) -> None:
        """Save detailed run data to JSON file for dashboard."""
        from datetime import datetime
        import os

        # Calculate total cost from agent results
        agent_results = context.get("agent_results", {})
        total_cost = sum(
            agent_result.get("cost", 0) or 0
            for agent_result in agent_results.values()
        )

        # Prepare run data
        run_data = {
            "timestamp": datetime.now().isoformat(),
            "model": self.model,  # Include model name
            "user_request": result["user_request"],
            "success": result["success"],
            "exit_reason": result.get("exit_reason", "finish"),
            "error": result.get("error"),
            "iterations": result["iterations"],
            "duration_seconds": result.get("duration_seconds", 0),
            "total_tokens": result["total_usage"]["total_tokens"],
            "prompt_tokens": result["total_usage"]["prompt_tokens"],
            "completion_tokens": result["total_usage"]["completion_tokens"],
            "total_cost": total_cost,
            "num_appliances": len(result.get("executed_schedules", [])),
            "appliances_scheduled": [s["appliance_id"] for s in result.get("executed_schedules", [])],
            "iteration_metrics": result.get("iteration_metrics", []),
            "prices_data": context.get("prices_data", {}),
            "calendar_constraint": context.get("calendar_constraint", {}),
            "agent_results": context.get("agent_results", {}),
            "executed_schedules": result.get("executed_schedules", []),
            "actions_taken": result.get("actions_taken", []),
            "final_summary": result.get("final_summary", "")
        }

        # Save to model-specific subfolder
        project_root = str(Path(__file__).parent.parent)
        model_folder = os.path.join(project_root, f"data/runs/{self.model}")
        os.makedirs(model_folder, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = f"{model_folder}/run_{timestamp}.json"

        with open(filepath, "w") as f:
            json.dump(run_data, f, indent=2)

        print(f"\n[Saved] Run data: {filepath}")

    def run_scheduling(self, user_request: str) -> Dict[str, Any]:
        """
        Run ReAct-based orchestration workflow.

        Args:
            user_request: User's scheduling request

        Returns:
            Dictionary with scheduling results
        """
        import time
        start_time = time.time()

        print("\n" + "=" * 80)
        print("LLM-BASED ORCHESTRATOR AGENT (ReAct Pattern)")
        print("=" * 80)
        print(f"\nUser Request: {user_request}\n")
        print("=" * 80)

        # Security: Validate and sanitize user input
        print("\n[Security] Validating user input...")
        validation_result = validate_and_prepare_input(user_request)

        if not validation_result["is_valid"]:
            print(f"[Security] ❌ Input rejected - {validation_result['rejection_reason']}")
            if validation_result.get("detected_patterns"):
                print(f"[Security] Detected patterns: {validation_result['detected_patterns']}")
            return {
                "success": False,
                "error": f"Security validation failed: {validation_result['rejection_reason']}",
                "risk_level": validation_result["risk_level"],
                "warnings": validation_result["warnings"],
                "user_request": user_request,
                "actions_taken": [],
                "iterations": 0,
                "total_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }

        # Log security warnings (if any)
        if validation_result["warnings"]:
            print(f"[Security] ⚠️  Warnings: {', '.join(validation_result['warnings'])}")
        print(f"[Security] ✓ Input validated (risk level: {validation_result['risk_level']})")

        # Use prepared input (XML-wrapped sanitized content) for privilege separation
        prepared_input = validation_result["prepared_input"]

        # Initialize conversation with privilege-separated input
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prepared_input}
        ]

        context = {}
        total_prompt_tokens = 0
        total_completion_tokens = 0
        actions_taken = []
        iteration_metrics = []
        max_iterations = 15

        print("\n[LLM Orchestrator] Starting ReAct workflow...\n")

        result = None
        try:
            for iteration in range(max_iterations):
                print(f"\n{'='*80}")
                print(f"Iteration {iteration + 1}")
                print(f"{'='*80}")

                # Get LLM response
                try:
                    llm_response, usage = self._call_llm(messages)
                    total_prompt_tokens += usage.get('prompt_tokens', 0)
                    total_completion_tokens += usage.get('completion_tokens', 0)
                except requests.exceptions.HTTPError as e:
                    error_msg = f"API Error: {e.response.status_code} - {e.response.text}"
                    print(f"\n[ERROR] {error_msg}")
                    result = {
                        "success": False,
                        "exit_reason": "error",
                        "error": error_msg,
                        "user_request": user_request,
                        "actions_taken": actions_taken,
                        "iterations": iteration + 1,
                        "duration_seconds": time.time() - start_time,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result
                except Exception as e:
                    error_msg = f"Unexpected error calling LLM: {str(e)}"
                    print(f"\n[ERROR] {error_msg}")
                    result = {
                        "success": False,
                        "exit_reason": "error",
                        "error": error_msg,
                        "user_request": user_request,
                        "actions_taken": actions_taken,
                        "iterations": iteration + 1,
                        "duration_seconds": time.time() - start_time,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result

                print(f"\n[LLM Thought/Action]:\n{llm_response}\n")

                # Parse action
                action = self._parse_action(llm_response)

                if not action:
                    # Record metrics even for no-action iterations
                    iteration_metrics.append({
                        "iteration": iteration + 1,
                        "prompt_tokens": usage.get('prompt_tokens', 0),
                        "completion_tokens": usage.get('completion_tokens', 0),
                        "latency_seconds": usage.get('latency_seconds', 0),
                        "action_type": None,
                        "has_reasoning": usage.get('has_reasoning', False),
                    })
                    print("[System] No action detected. Prompting LLM to continue...")
                    messages.append({"role": "assistant", "content": llm_response})
                    messages.append({"role": "user", "content": "Please output your next ACTION in the required format."})
                    continue

                # Execute action
                observation = self._execute_action(action, context)
                actions_taken.append({
                    "iteration": iteration + 1,
                    "action": action,
                    "observation": observation
                })

                # Record per-iteration metrics
                iteration_metrics.append({
                    "iteration": iteration + 1,
                    "prompt_tokens": usage.get('prompt_tokens', 0),
                    "completion_tokens": usage.get('completion_tokens', 0),
                    "latency_seconds": usage.get('latency_seconds', 0),
                    "action_type": action["type"],
                    "has_reasoning": usage.get('has_reasoning', False),
                })

                print(f"\n[Observation]: {observation}")

                # Add to conversation
                messages.append({"role": "assistant", "content": llm_response})
                messages.append({"role": "user", "content": f"Observation: {observation}\n\nWhat's your next thought and action?"})

                # Check if finished
                if action["type"] == "FINISH":
                    duration_seconds = time.time() - start_time

                    print("\n" + "=" * 80)
                    print("ORCHESTRATOR FINAL SUMMARY")
                    print("=" * 80)
                    print(context.get("final_summary", "No summary provided."))
                    print("=" * 80)
                    print(f"\nTotal execution time: {duration_seconds:.2f} seconds")

                    result = {
                        "success": True,
                        "exit_reason": "finish",
                        "user_request": user_request,
                        "final_summary": context.get("final_summary", ""),
                        "actions_taken": actions_taken,
                        "executed_schedules": context.get("executed_schedules", []),
                        "iterations": iteration + 1,
                        "iteration_metrics": iteration_metrics,
                        "duration_seconds": duration_seconds,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result

            # Max iterations reached
            duration_seconds = time.time() - start_time
            result = {
                "success": False,
                "exit_reason": "max_iterations",
                "error": "Max iterations reached without FINISH action",
                "user_request": user_request,
                "actions_taken": actions_taken,
                "iteration_metrics": iteration_metrics,
                "duration_seconds": duration_seconds,
                "total_usage": {
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "total_tokens": total_prompt_tokens + total_completion_tokens
                }
            }
            return result
        finally:
            if result:
                self._save_run_data(result, context)

    def run_dr_response(self, event_id, prosumer_message=None):
        """
        Run DR event handling using the same orchestrator with DR event context.
        Reuses the same ReAct loop, system prompt, and tools.

        Args:
            event_id: The DR event to evaluate
            prosumer_message: Optional prosumer response message

        Returns:
            Dictionary with DR handling results
        """
        import time
        start_time = time.time()

        print("\n" + "=" * 80)
        print("HEMS ORCHESTRATOR - DR EVENT HANDLING")
        print("=" * 80)
        print(f"\nDR Event: {event_id}")
        print("=" * 80)

        # Load DR event
        event_path = Path(__file__).parent.parent / "data" / "dr_events" / f"{event_id}.json"
        if not event_path.exists():
            return {
                "success": False,
                "error": f"DR event {event_id} not found",
                "user_request": event_id,
                "actions_taken": [],
                "iterations": 0,
                "total_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }

        with open(event_path, 'r') as f:
            dr_event = json.load(f)

        # Build prompt describing the DR event
        event_description = (
            f"A Demand Response event has been received from the aggregator:\n"
            f"- Event ID: {dr_event['event_id']}\n"
            f"- Type: {dr_event['event_type']}\n"
            f"- Time window: {dr_event['window_start']} - {dr_event['window_end']}\n"
            f"- Target reduction: {dr_event['target_kw']} kW\n"
            f"- Compensation: {dr_event['compensation_eur_kwh']} EUR/kWh\n"
            f"- Maximum earnings: {dr_event.get('max_compensation_eur', 'N/A')} EUR\n\n"
            f"Please evaluate this DR event: check battery state, assess feasibility with the battery agent, "
            f"and prepare a conversational explanation for the prosumer."
        )

        # Only validate the prosumer message (untrusted user input),
        # not the system-built event description (trusted internal data)
        if prosumer_message:
            validation_result = validate_and_prepare_input(prosumer_message)
            if not validation_result["is_valid"]:
                return {
                    "success": False,
                    "error": f"Security validation failed: {validation_result['rejection_reason']}",
                    "user_request": event_id,
                    "actions_taken": [],
                    "iterations": 0,
                    "total_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                }
            event_description += f"\n\nThe prosumer has responded: {validation_result['prepared_input']}"

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": event_description}
        ]

        context = {"dr_event": dr_event}
        total_prompt_tokens = 0
        total_completion_tokens = 0
        actions_taken = []
        iteration_metrics = []
        max_iterations = 15

        print("\n[HEMS Orchestrator] Starting DR event evaluation...\n")

        # Log lifecycle: evaluation started
        log_event(event_id, source="hems", action="evaluation_started", model=self.model, details={
            "event_id": event_id,
        })

        result = None
        try:
            for iteration in range(max_iterations):
                print(f"\n{'='*80}")
                print(f"Iteration {iteration + 1}")
                print(f"{'='*80}")

                try:
                    llm_response, usage = self._call_llm(messages)
                    total_prompt_tokens += usage.get('prompt_tokens', 0)
                    total_completion_tokens += usage.get('completion_tokens', 0)
                except requests.exceptions.HTTPError as e:
                    error_msg = f"API Error: {e.response.status_code} - {e.response.text}"
                    print(f"\n[ERROR] {error_msg}")
                    log_event(event_id, source="hems", action="error", model=self.model, details={
                        "error": error_msg, "iteration": iteration + 1
                    })
                    result = {
                        "success": False, "exit_reason": "error", "error": error_msg,
                        "user_request": event_id,
                        "actions_taken": actions_taken,
                        "iteration_metrics": iteration_metrics,
                        "iterations": iteration + 1,
                        "duration_seconds": time.time() - start_time,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result
                except Exception as e:
                    error_msg = f"Unexpected error: {str(e)}"
                    print(f"\n[ERROR] {error_msg}")
                    log_event(event_id, source="hems", action="error", model=self.model, details={
                        "error": error_msg, "iteration": iteration + 1
                    })
                    result = {
                        "success": False, "exit_reason": "error", "error": error_msg,
                        "user_request": event_id,
                        "actions_taken": actions_taken,
                        "iteration_metrics": iteration_metrics,
                        "iterations": iteration + 1,
                        "duration_seconds": time.time() - start_time,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result

                print(f"\n[LLM Thought/Action]:\n{llm_response}\n")

                action = self._parse_action(llm_response)

                if not action:
                    iteration_metrics.append({
                        "iteration": iteration + 1,
                        "prompt_tokens": usage.get('prompt_tokens', 0),
                        "completion_tokens": usage.get('completion_tokens', 0),
                        "latency_seconds": usage.get('latency_seconds', 0),
                        "action_type": None,
                        "has_reasoning": usage.get('has_reasoning', False),
                    })
                    print("[System] No action detected. Prompting LLM to continue...")
                    messages.append({"role": "assistant", "content": llm_response})
                    messages.append({"role": "user", "content": "Please output your next ACTION in the required format."})
                    continue

                observation = self._execute_action(action, context)
                actions_taken.append({
                    "iteration": iteration + 1,
                    "action": action,
                    "observation": observation
                })

                # Record per-iteration metrics
                iteration_metrics.append({
                    "iteration": iteration + 1,
                    "prompt_tokens": usage.get('prompt_tokens', 0),
                    "completion_tokens": usage.get('completion_tokens', 0),
                    "latency_seconds": usage.get('latency_seconds', 0),
                    "action_type": action["type"],
                    "has_reasoning": usage.get('has_reasoning', False),
                })

                # Log per-iteration action
                action_param = action.get("params", {}).get("summary") or action.get("params", {}).get("agent_name") or ""
                log_event(event_id, source="hems", action="iteration", model=self.model, details={
                    "iteration": iteration + 1,
                    "action_type": action["type"],
                    "action_param": str(action_param)[:100] if action_param else None,
                })

                print(f"\n[Observation]: {observation}")

                messages.append({"role": "assistant", "content": llm_response})

                # If we just explained to prosumer and no prosumer message yet, pause
                if action["type"] == "EXPLAIN_TO_PROSUMER" and not prosumer_message:
                    # Save conversation history to DR event JSON for follow-up turns
                    messages.append({"role": "user", "content": f"Observation: {observation}"})
                    self._save_conversation_history(event_id, messages)

                    # Extract the conversational explanation from the LLM response
                    explanation_text = re.split(r'ACTION:\s*EXPLAIN_TO_PROSUMER', llm_response, flags=re.IGNORECASE)[0].strip()
                    explanation_text = re.sub(r'^Thought:\s*', '', explanation_text, flags=re.IGNORECASE).strip()

                    print("\n" + "=" * 80)
                    print("HEMS EXPLANATION")
                    print("=" * 80)
                    print(explanation_text)
                    print("=" * 80)

                    duration_seconds = time.time() - start_time

                    # Log lifecycle: evaluation paused (awaiting prosumer)
                    log_event(event_id, source="hems", action="evaluation_paused", model=self.model, details={
                        "iterations": iteration + 1,
                        "actions": [a["action"]["type"] for a in actions_taken],
                        "explanation": explanation_text[:500],
                        "feasibility": context.get("feasibility", {}),
                        "tokens": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens,
                        },
                        "duration_seconds": round(duration_seconds, 1),
                    })

                    result = {
                        "success": True,
                        "exit_reason": "paused",
                        "awaiting_prosumer": True,
                        "user_request": event_id,
                        "explanation": explanation_text,
                        "actions_taken": actions_taken,
                        "iteration_metrics": iteration_metrics,
                        "iterations": iteration + 1,
                        "duration_seconds": duration_seconds,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result

                messages.append({"role": "user", "content": f"Observation: {observation}\n\nWhat's your next thought and action?"})

                if action["type"] == "FINISH":
                    duration_seconds = time.time() - start_time

                    print("\n" + "=" * 80)
                    print("DR EVENT HANDLER SUMMARY")
                    print("=" * 80)
                    print(context.get("final_summary", "No summary provided."))
                    print("=" * 80)

                    # Log lifecycle: evaluation finished
                    log_event(event_id, source="hems", action="evaluation_finished", model=self.model, details={
                        "iterations": iteration + 1,
                        "summary": context.get("final_summary", "")[:500],
                        "response_submitted": context.get("response_submitted", False),
                        "tokens": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens,
                        },
                        "duration_seconds": round(duration_seconds, 1),
                    })

                    result = {
                        "success": True,
                        "exit_reason": "finish",
                        "awaiting_prosumer": False,
                        "user_request": event_id,
                        "final_summary": context.get("final_summary", ""),
                        "actions_taken": actions_taken,
                        "iteration_metrics": iteration_metrics,
                        "response_submitted": context.get("response_submitted", False),
                        "iterations": iteration + 1,
                        "duration_seconds": duration_seconds,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result

            # Max iterations reached
            duration_seconds = time.time() - start_time
            log_event(event_id, source="hems", action="max_iterations", model=self.model, details={
                "iterations": max_iterations,
                "last_actions": [a["action"]["type"] for a in actions_taken[-3:]],
            })
            result = {
                "success": False,
                "exit_reason": "max_iterations",
                "error": "Max iterations reached",
                "user_request": event_id,
                "actions_taken": actions_taken,
                "iteration_metrics": iteration_metrics,
                "duration_seconds": duration_seconds,
                "total_usage": {
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "total_tokens": total_prompt_tokens + total_completion_tokens
                }
            }
            return result
        finally:
            if result:
                self._save_run_data(result, context)

    def _save_conversation_history(self, event_id, messages):
        """Save conversation history to the DR event JSON file for follow-up turns."""
        event_path = Path(__file__).parent.parent / "data" / "dr_events" / f"{event_id}.json"
        if not event_path.exists():
            print(f"[Warning] Cannot save conversation history: {event_id} not found")
            return

        with open(event_path, 'r') as f:
            dr_event = json.load(f)

        dr_event["conversation_history"] = messages
        dr_event["conversation_status"] = "awaiting_prosumer"

        with open(event_path, 'w') as f:
            json.dump(dr_event, f, indent=2)

        print(f"\n[Saved] Conversation history ({len(messages)} messages) to {event_path}")

    def run_dr_followup(self, event_id, prosumer_message):
        """
        Handle a follow-up question from the prosumer during DR negotiation.
        Loads saved conversation history, makes one LLM call, saves updated history.

        Args:
            event_id: The DR event ID
            prosumer_message: The prosumer's follow-up question

        Returns:
            Dictionary with follow-up results
        """
        import time
        start_time = time.time()

        print("\n" + "=" * 80)
        print("DR FOLLOW-UP CONVERSATION")
        print("=" * 80)
        print(f"\nDR Event: {event_id}")
        print(f"Prosumer message: {prosumer_message}")
        print("=" * 80)

        # Load DR event JSON
        event_path = Path(__file__).parent.parent / "data" / "dr_events" / f"{event_id}.json"
        if not event_path.exists():
            return {
                "success": False,
                "error": f"DR event {event_id} not found",
                "total_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }

        with open(event_path, 'r') as f:
            dr_event = json.load(f)

        # Load conversation history
        conversation_history = dr_event.get("conversation_history")
        if not conversation_history:
            return {
                "success": False,
                "error": "No conversation history found. Run initial evaluation first.",
                "total_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }

        # Validate prosumer message
        validation_result = validate_and_prepare_input(prosumer_message)
        if not validation_result["is_valid"]:
            return {
                "success": False,
                "error": f"Security validation failed: {validation_result['rejection_reason']}",
                "total_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }

        prepared_message = validation_result["prepared_input"]

        # Rebuild messages from saved history and append prosumer follow-up
        messages = list(conversation_history)
        followup_prompt = (
            f"The prosumer has responded about the DR event: {prepared_message}\n\n"
            f"Respond conversationally. You have all the context from the evaluation above. "
            f"Answer their question directly based on what you already know about the DR event "
            f"and appliance feasibility.\n\n"
            f"If the prosumer is approving or rejecting the DR event, you MUST submit their decision "
            f"by outputting the appropriate action at the end of your reply:\n"
            f"  ACTION: SUBMIT_DR_RESPONSE | accepted=true | commitment_kw=<kw> | appliances=<list> | reasoning=<text>\n"
            f"  ACTION: SUBMIT_DR_RESPONSE | accepted=false | commitment_kw=0 | appliances= | reasoning=<text>\n"
            f"followed by:\n"
            f"  ACTION: FINISH | summary=<summary>\n\n"
            f"If the prosumer is just asking a question, respond naturally without any ACTION commands "
            f"and remind them they can approve or reject when ready."
        )
        messages.append({"role": "user", "content": followup_prompt})

        # Single LLM call with one retry on empty/failed response
        llm_response = ""
        usage = {}
        for attempt in range(2):
            try:
                llm_response, usage = self._call_llm(messages)
            except Exception as e:
                print(f"\n[Followup] LLM call failed (attempt {attempt + 1}): {e}")
                if attempt == 1:
                    return {
                        "success": False,
                        "error": f"LLM call failed after 2 attempts: {str(e)}",
                        "total_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                    }
                continue

            if llm_response and llm_response.strip():
                break
            print(f"\n[Followup] Empty response from LLM (attempt {attempt + 1}), retrying...")

        if not llm_response or not llm_response.strip():
            print("\n[Followup] LLM returned empty response after 2 attempts")
            return {
                "success": False,
                "error": "LLM returned empty response after 2 attempts",
                "total_usage": usage
            }

        print(f"\n[HEMS Reply]:\n{llm_response}\n")

        # Check if the LLM included a decision action (SUBMIT_DR_RESPONSE)
        context = {"dr_event": dr_event}
        action = self._parse_action(llm_response)
        decision_submitted = False

        if action and action["type"] == "SUBMIT_DR_RESPONSE":
            print(f"\n[Followup] Detected SUBMIT_DR_RESPONSE action, executing...")
            observation = self._execute_action(action, context)
            print(f"[Followup] {observation}")
            decision_submitted = context.get("response_submitted", False)

            # Check for a FINISH action after SUBMIT_DR_RESPONSE
            # The LLM may output both in sequence
            remaining = llm_response.split("SUBMIT_DR_RESPONSE", 1)[-1]
            finish_action = self._parse_action(remaining)
            if finish_action and finish_action["type"] == "FINISH":
                self._execute_action(finish_action, context)

        # Reload event from disk to capture status changes from submit_dr_response
        # (which updates status/responded_at directly on file)
        with open(event_path, 'r') as f:
            dr_event = json.load(f)

        # Append both messages to history and save back
        messages.append({"role": "assistant", "content": llm_response})
        dr_event["conversation_history"] = messages
        turn_count = sum(1 for m in messages if m["role"] == "user") - 1  # Exclude initial prompt

        with open(event_path, 'w') as f:
            json.dump(dr_event, f, indent=2)

        # Print reply between delimiters for dashboard parsing
        print("\n" + "=" * 80)
        print("DR FOLLOW-UP REPLY")
        print("=" * 80)
        print(llm_response)
        print("=" * 80)

        duration_seconds = time.time() - start_time

        # Log lifecycle: follow-up conversation turn
        log_event(event_id, source="hems", action="followup", model=self.model, details={
            "prosumer_message": prosumer_message[:300],
            "hems_reply": llm_response[:500],
            "turn": turn_count,
            "tokens": usage,
            "duration_seconds": round(duration_seconds, 1),
            "decision_submitted": decision_submitted,
        })

        if decision_submitted:
            return {
                "success": True,
                "awaiting_prosumer": False,
                "decision_submitted": True,
                "reply": llm_response,
                "final_summary": context.get("final_summary", ""),
                "turn_count": turn_count,
                "duration_seconds": duration_seconds,
                "total_usage": usage
            }

        return {
            "success": True,
            "awaiting_prosumer": True,
            "reply": llm_response,
            "turn_count": turn_count,
            "duration_seconds": duration_seconds,
            "total_usage": usage
        }


class AggregatorAgentReAct:
    """LLM-based aggregator agent using ReAct pattern for DR event management."""

    def __init__(self):
        """Initialize the aggregator ReAct agent."""
        self.system_prompt = self._create_system_prompt()
        self.api_key = CEREBRAS_API_KEY
        self.model = os.environ.get('CEREBRAS_MODEL_OVERRIDE', CEREBRAS_MODEL)
        self.temperature = TEMPERATURE
        self.base_url = "https://api.cerebras.ai/v1"
        print(f"[Aggregator] Using model: {self.model}")

    def _build_portfolio_summary(self) -> str:
        """Build portfolio summary from data/portfolio.json."""
        portfolio_path = Path(__file__).parent.parent / "data" / "portfolio.json"
        try:
            with open(portfolio_path, 'r') as f:
                portfolio = json.load(f)
            lines = ["You manage the following registered households:\n"]
            for idx, hh in enumerate(portfolio.get("households", []), 1):
                hh_id = hh.get("household_id", "?")
                cap = hh.get("total_flexible_capacity_kw", "?")
                lines.append(f"{idx}. **{hh_id}** - Total flexible capacity: {cap} kW")
                for asset in hh.get("assets", hh.get("appliances", [])):
                    asset_id = asset.get("asset_id", asset.get("appliance_id", "?"))
                    specs = []
                    if "capacity_kwh" in asset:
                        specs.append(f"{asset['capacity_kwh']} kWh")
                    if "power_kw" in asset:
                        specs.append(f"{asset['power_kw']} kW charge/discharge")
                    lines.append(f"   - {asset_id.replace('_', ' ').title()} ({', '.join(specs)})")
            return "\n".join(lines)
        except Exception:
            return "Portfolio could not be loaded. Use GET_PORTFOLIO_STATUS to fetch current data."

    def _create_system_prompt(self) -> str:
        """Create system prompt with ReAct pattern for aggregator."""
        prompt_path = Path(__file__).parent.parent / "prompts" / "aggregator_orchestrator.md"
        with open(prompt_path, 'r') as f:
            base_prompt = f.read()

        # Inject portfolio from data file
        base_prompt = base_prompt.replace("{PORTFOLIO_SUMMARY}", self._build_portfolio_summary())

        react_instructions = """

## ReAct Pattern: Reasoning and Action

You will work through this task step-by-step using a Thought-Action-Observation cycle.

### Available Actions

**Action: GET_MARKET_OBLIGATION**
Fetches market obligation details (day-ahead flexibility commitments).
Format: `ACTION: GET_MARKET_OBLIGATION`

**Action: GET_PORTFOLIO_STATUS**
Checks registered households and their flexible assets.
Format: `ACTION: GET_PORTFOLIO_STATUS`

**Action: DISPATCH_DR_EVENT**
Sends a battery discharge request to a household.
Format: `ACTION: DISPATCH_DR_EVENT | household_id=<id> | window_start=<HH:MM> | window_end=<HH:MM> | target_kw=<kw>`
Optional: `| compensation_eur_kwh=<rate>` (loaded from aggregator settings if omitted)
Example: `ACTION: DISPATCH_DR_EVENT | household_id=HH-001 | window_start=17:00 | window_end=18:00 | target_kw=2.0`

**Action: COLLECT_RESPONSE**
Checks household response for a dispatched DR event.
Format: `ACTION: COLLECT_RESPONSE | event_id=<id>`

**Action: HANDLE_HOUSEHOLD_REQUEST**
Processes a bottom-up message from a household.
Format: `ACTION: HANDLE_HOUSEHOLD_REQUEST | request_id=<id>`

**Action: GET_ACTIVE_DR_EVENTS**
Lists all active/unresolved DR events across the portfolio (dispatched but not yet accepted or rejected).
Format: `ACTION: GET_ACTIVE_DR_EVENTS`
Use when the operator asks about pending events or status without specifying an event ID.

**Action: FINISH**
Completes aggregator workflow and presents summary.
Format: `ACTION: FINISH | summary=<your summary>`

### Workflow

1. **Thought**: Explain your reasoning
2. **Action**: Output EXACTLY ONE action, then STOP
3. **Observation**: System executes and shows result
4. **Repeat** until FINISH

**CRITICAL**: After outputting an ACTION, STOP and wait for the Observation. Do NOT output multiple actions. Do NOT predict, fabricate, or assume tool results. You must wait for the real Observation from the system before proceeding.

**ANTI-HALLUCINATION**: You CANNOT know household responses without actually dispatching and collecting. If the request involves kW, flexibility, or dispatch, you MUST call DISPATCH_DR_EVENT and COLLECT_RESPONSE before FINISH. Any FINISH that claims results without prior tool calls will be rejected.

### Typical Workflow Order

**For conversational/informational queries** ("What can you do?", "Hello", "How does this work?"):
1. FINISH with a conversational response. Do NOT call any other tools.

**For status queries** ("What's my portfolio?", "Any pending events?"):
1. GET_PORTFOLIO_STATUS or COLLECT_RESPONSE (read-only)
2. FINISH (summarize)

**For actionable dispatch requests** ("I need 2kW reduction 17:00-18:00", "Fulfill today's obligation"):
1. GET_MARKET_OBLIGATION (understand what's needed)
2. GET_PORTFOLIO_STATUS (check available capacity)
3. DISPATCH_DR_EVENT (send to appropriate households)
4. COLLECT_RESPONSE (check results)
5. FINISH (summarize)

CRITICAL: Only dispatch DR events when the operator gives an explicit, actionable command. General questions should be answered with FINISH immediately.
"""
        return base_prompt + react_instructions

    def _call_llm(self, messages):
        """Call the LLM and return response text with usage and latency."""
        import time as _time
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": 8000,
            "stream": False,
            "reasoning_effort": REASONING_EFFORT,
            "reasoning_format": REASONING_FORMAT,
        }
        call_start = _time.time()
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120
        )
        response.raise_for_status()
        call_latency = _time.time() - call_start

        result = response.json()
        msg = result['choices'][0]['message']
        usage = result.get('usage', {})
        usage['latency_seconds'] = round(call_latency, 3)

        reasoning = msg.get('reasoning', '')
        if reasoning:
            print(f"\n[Reasoning - Aggregator]\n{reasoning}\n")
            usage['reasoning_text'] = reasoning
            usage['has_reasoning'] = True
        else:
            usage['has_reasoning'] = False

        return msg.get('content', ''), usage

    def _parse_action(self, response):
        """Parse action from LLM response. Reuses same parsing logic."""
        action_line_match = re.search(r'ACTION:.*', response, re.IGNORECASE)
        if action_line_match:
            response = action_line_match.group(0)

        action_match = re.search(r'ACTION:\s*([A-Z_]+)(?:\s*\|\s*(.+))?$', response, re.IGNORECASE)
        if not action_match:
            action_match = re.search(r'ACTION:\s*([A-Z_-]+)(?:\s*\|\s*(.+))?$', response, re.IGNORECASE)
        if not action_match:
            return None

        action_type = action_match.group(1).upper().replace('-', '_').replace(' ', '_')
        action_params_str = action_match.group(2)
        action = {"type": action_type}

        if action_params_str:
            params = {}
            parts = action_params_str.split('|')
            for param in parts:
                param = param.strip()
                if '=' in param:
                    key, value = param.split('=', 1)
                    key = key.strip().strip('"\'')
                    value = value.strip().strip('"\'')
                    params[key] = value
            action["params"] = params

        return action

    def _execute_action(self, action, context):
        """Execute an aggregator action and return observation."""
        from aggregator_tools import (
            get_market_obligation, get_portfolio_status,
            dispatch_dr_event, collect_response, handle_household_request,
            get_active_dr_events
        )

        action_type = action["type"]
        params = action.get("params", {})

        if action_type == "GET_MARKET_OBLIGATION":
            print("\n   [Action] Fetching market obligation...")
            result = get_market_obligation(params.get("obligation_id"))
            if result["success"]:
                context["obligation"] = result
                events = result.get("events", [])
                total_kw = sum(e.get("target_kw", 0) for e in events)
                return f"Loaded obligation {result.get('obligation_id', 'N/A')} for {result.get('date', 'N/A')}. {len(events)} events, total target: {total_kw} kW."
            return f"Failed: {result.get('error', 'Unknown error')}"

        elif action_type == "GET_PORTFOLIO_STATUS":
            print("\n   [Action] Checking portfolio status...")
            result = get_portfolio_status()
            if result["success"]:
                context["portfolio"] = result
                return f"Portfolio: {result['total_households']} households, {result['total_flexible_capacity_kw']} kW total capacity."
            return f"Failed: {result.get('error', 'Unknown error')}"

        elif action_type == "DISPATCH_DR_EVENT":
            household_id = params.get("household_id")
            window_start = params.get("window_start")
            window_end = params.get("window_end")
            target_kw = params.get("target_kw")
            compensation = params.get("compensation_eur_kwh")

            if not all([household_id, window_start, window_end, target_kw]):
                return "Error: Missing required parameters (household_id, window_start, window_end, target_kw)"

            try:
                target_kw = float(target_kw)
                if compensation is not None:
                    compensation = float(compensation)
            except (ValueError, TypeError):
                return "Error: target_kw and compensation_eur_kwh must be numbers"

            print(f"\n   [Action] Dispatching DR event to {household_id}...")
            kwargs = {
                "household_id": household_id,
                "window_start": window_start,
                "window_end": window_end,
                "target_kw": target_kw,
            }
            if compensation is not None:
                kwargs["compensation_eur_kwh"] = compensation
            result = dispatch_dr_event(**kwargs)

            if result["success"]:
                if "dispatched_events" not in context:
                    context["dispatched_events"] = []
                context["dispatched_events"].append(result)
                return f"DR event dispatched: {result['event_id']} to {household_id}. Window: {window_start}-{window_end}, target: {target_kw} kW, compensation: {compensation} EUR/kWh. Status: {result['status']}."
            return f"Dispatch failed: {result.get('error', 'Unknown error')}"

        elif action_type == "COLLECT_RESPONSE":
            event_id = params.get("event_id")
            if not event_id:
                return "Error: Missing event_id parameter"

            print(f"\n   [Action] Collecting response for {event_id}...")
            result = collect_response(event_id)

            if result["success"]:
                status = result["status"]
                if result["response"]:
                    resp = result["response"]
                    commitment = resp.get("commitment_kw", 0)
                    appliances = ", ".join(resp.get("accepted_appliances", []))
                    return f"Event {event_id}: {status}. Committed: {commitment} kW. Appliances: {appliances}. Reasoning: {resp.get('reasoning', 'N/A')}"
                return f"Event {event_id}: {status}. No response yet -- household has not responded."
            return f"Failed: {result.get('error', 'Unknown error')}"

        elif action_type == "HANDLE_HOUSEHOLD_REQUEST":
            request_id = params.get("request_id")
            if not request_id:
                return "Error: Missing request_id parameter"

            print(f"\n   [Action] Processing household request {request_id}...")
            result = handle_household_request(request_id)

            if result["success"]:
                return f"Request {request_id}: Triage = {result['triage']}. Action: {result['suggested_action']}. Type: {result['request'].get('type', 'unknown')}."
            return f"Failed: {result.get('error', 'Unknown error')}"

        elif action_type == "GET_ACTIVE_DR_EVENTS":
            print(f"\n   [Action] Checking active DR events...")
            result = get_active_dr_events()
            if result["success"]:
                events = result["active_events"]
                if not events:
                    return "No active DR events. All dispatched events have been resolved."
                lines = [f"Found {len(events)} active DR event(s):"]
                for e in events:
                    lines.append(f"  - {e['event_id']}: {e['household_id']}, {e['event_type']}, {e['window_start']}-{e['window_end']}, {e['target_kw']} kW, status={e['status']}")
                return "\n".join(lines)
            return f"Failed: {result.get('error', 'Unknown error')}"

        elif action_type == "FINISH":
            summary = params.get("summary", "Aggregator workflow completed.")

            # Guard: reject FINISH if the request mentions kW/flexibility/dispatch
            # but no dispatch actually happened -- the LLM is hallucinating results
            dispatched = context.get("dispatched_events", [])
            request_keywords = ["kw", "flexibility", "dispatch", "discharge", "reduction", "obligation"]
            request_text = context.get("user_request", "").lower()
            looks_actionable = any(kw in request_text for kw in request_keywords)

            if looks_actionable and not dispatched:
                return (
                    "REJECTED: You called FINISH without dispatching any DR events. "
                    "The operator's request requires dispatching to households. "
                    "You MUST call DISPATCH_DR_EVENT first, then COLLECT_RESPONSE, "
                    "then FINISH with real results. Do NOT fabricate outcomes."
                )

            context["final_summary"] = summary
            return "Aggregator workflow complete. Final summary ready."

        else:
            return f"Unknown action type: {action_type}"

    def _save_run_data(self, result, context):
        """Save aggregator run data to JSON file."""
        from datetime import datetime
        run_data = {
            "timestamp": datetime.now().isoformat(),
            "agent_type": "aggregator",
            "model": self.model,
            "user_request": result["user_request"],
            "success": result["success"],
            "exit_reason": result.get("exit_reason", "finish"),
            "error": result.get("error"),
            "iterations": result["iterations"],
            "duration_seconds": result.get("duration_seconds", 0),
            "total_tokens": result["total_usage"]["total_tokens"],
            "prompt_tokens": result["total_usage"]["prompt_tokens"],
            "completion_tokens": result["total_usage"]["completion_tokens"],
            "iteration_metrics": result.get("iteration_metrics", []),
            "dispatched_events": context.get("dispatched_events", []),
            "actions_taken": result.get("actions_taken", []),
            "final_summary": result.get("final_summary", "")
        }

        project_root = str(Path(__file__).parent.parent)
        model_folder = os.path.join(project_root, f"data/runs/aggregator/{self.model}")
        os.makedirs(model_folder, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = f"{model_folder}/run_{timestamp}.json"

        with open(filepath, "w") as f:
            json.dump(run_data, f, indent=2)
        print(f"\n[Saved] Aggregator run data: {filepath}")

    def run_aggregator(self, user_request):
        """
        Run aggregator ReAct workflow.

        Args:
            user_request: Aggregator operator's request

        Returns:
            Dictionary with aggregator results
        """
        import time
        start_time = time.time()

        print("\n" + "=" * 80)
        print("AGGREGATOR AGENT (ReAct Pattern)")
        print("=" * 80)
        print(f"\nOperator Request: {user_request}\n")
        print("=" * 80)

        # Security validation
        print("\n[Security] Validating operator input...")
        validation_result = validate_and_prepare_input(user_request)

        if not validation_result["is_valid"]:
            print(f"[Security] Input rejected - {validation_result['rejection_reason']}")
            return {
                "success": False,
                "error": f"Security validation failed: {validation_result['rejection_reason']}",
                "user_request": user_request,
                "actions_taken": [],
                "iterations": 0,
                "total_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }

        if validation_result["warnings"]:
            print(f"[Security] Warnings: {', '.join(validation_result['warnings'])}")
        print(f"[Security] Input validated (risk level: {validation_result['risk_level']})")

        prepared_input = validation_result["prepared_input"]

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prepared_input}
        ]

        context = {"user_request": prepared_input}
        total_prompt_tokens = 0
        total_completion_tokens = 0
        actions_taken = []
        iteration_metrics = []
        max_iterations = 15

        print("\n[Aggregator] Starting ReAct workflow...\n")

        result = None
        try:
            for iteration in range(max_iterations):
                print(f"\n{'='*80}")
                print(f"Iteration {iteration + 1}")
                print(f"{'='*80}")

                try:
                    llm_response, usage = self._call_llm(messages)
                    total_prompt_tokens += usage.get('prompt_tokens', 0)
                    total_completion_tokens += usage.get('completion_tokens', 0)
                except requests.exceptions.HTTPError as e:
                    error_msg = f"API Error: {e.response.status_code} - {e.response.text}"
                    print(f"\n[ERROR] {error_msg}")
                    result = {
                        "success": False, "exit_reason": "error", "error": error_msg,
                        "user_request": user_request,
                        "actions_taken": actions_taken,
                        "iteration_metrics": iteration_metrics,
                        "iterations": iteration + 1,
                        "duration_seconds": time.time() - start_time,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result
                except Exception as e:
                    error_msg = f"Unexpected error: {str(e)}"
                    print(f"\n[ERROR] {error_msg}")
                    result = {
                        "success": False, "exit_reason": "error", "error": error_msg,
                        "user_request": user_request,
                        "actions_taken": actions_taken,
                        "iteration_metrics": iteration_metrics,
                        "iterations": iteration + 1,
                        "duration_seconds": time.time() - start_time,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result

                print(f"\n[LLM Thought/Action]:\n{llm_response}\n")

                action = self._parse_action(llm_response)

                if not action:
                    iteration_metrics.append({
                        "iteration": iteration + 1,
                        "prompt_tokens": usage.get('prompt_tokens', 0),
                        "completion_tokens": usage.get('completion_tokens', 0),
                        "latency_seconds": usage.get('latency_seconds', 0),
                        "action_type": None,
                        "has_reasoning": usage.get('has_reasoning', False),
                    })
                    print("[System] No action detected. Prompting LLM to continue...")
                    messages.append({"role": "assistant", "content": llm_response})
                    messages.append({"role": "user", "content": (
                        "No ACTION was detected in your response. You wrote a narrative but did not execute any tools. "
                        "Output exactly ONE action now. For dispatch requests, start with: ACTION: GET_PORTFOLIO_STATUS"
                    )})
                    continue

                observation = self._execute_action(action, context)
                actions_taken.append({
                    "iteration": iteration + 1,
                    "action": action,
                    "observation": observation
                })

                # Record per-iteration metrics
                iteration_metrics.append({
                    "iteration": iteration + 1,
                    "prompt_tokens": usage.get('prompt_tokens', 0),
                    "completion_tokens": usage.get('completion_tokens', 0),
                    "latency_seconds": usage.get('latency_seconds', 0),
                    "action_type": action["type"],
                    "has_reasoning": usage.get('has_reasoning', False),
                })

                print(f"\n[Observation]: {observation}")

                messages.append({"role": "assistant", "content": llm_response})
                messages.append({"role": "user", "content": f"Observation: {observation}\n\nWhat's your next thought and action?"})

                if action["type"] == "FINISH":
                    duration_seconds = time.time() - start_time

                    print("\n" + "=" * 80)
                    print("AGGREGATOR FINAL SUMMARY")
                    print("=" * 80)
                    print(context.get("final_summary", "No summary provided."))
                    print("=" * 80)
                    print(f"\nTotal execution time: {duration_seconds:.2f} seconds")

                    result = {
                        "success": True,
                        "exit_reason": "finish",
                        "user_request": user_request,
                        "final_summary": context.get("final_summary", ""),
                        "actions_taken": actions_taken,
                        "iteration_metrics": iteration_metrics,
                        "dispatched_events": context.get("dispatched_events", []),
                        "iterations": iteration + 1,
                        "duration_seconds": duration_seconds,
                        "total_usage": {
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                            "total_tokens": total_prompt_tokens + total_completion_tokens
                        }
                    }
                    return result

            # Max iterations reached
            duration_seconds = time.time() - start_time
            result = {
                "success": False,
                "exit_reason": "max_iterations",
                "error": "Max iterations reached without FINISH action",
                "user_request": user_request,
                "actions_taken": actions_taken,
                "iteration_metrics": iteration_metrics,
                "duration_seconds": duration_seconds,
                "total_usage": {
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "total_tokens": total_prompt_tokens + total_completion_tokens
                }
            }
            return result
        finally:
            if result:
                self._save_run_data(result, context)


def main():
    """Run the ReAct orchestrator with a test query."""
    # Get user query from command line or use default
    if len(sys.argv) > 1:
        # Check for agent mode flags
        if sys.argv[1] == "--aggregator":
            user_query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Fulfill today's market obligation"
            agent = AggregatorAgentReAct()
            result = agent.run_aggregator(user_query)
        elif sys.argv[1] == "--dr-handler":
            event_id = sys.argv[2] if len(sys.argv) > 2 else None
            if not event_id:
                print("Usage: python orchestrator_agent_react.py --dr-handler <event_id> [--followup <message>]")
                sys.exit(1)
            # Check for --followup flag
            followup_message = None
            if len(sys.argv) > 3 and sys.argv[3] == "--followup":
                followup_message = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else None
            orchestrator = OrchestratorAgentReAct()
            if followup_message:
                result = orchestrator.run_dr_followup(event_id, followup_message)
            else:
                result = orchestrator.run_dr_response(event_id)
        else:
            user_query = " ".join(sys.argv[1:])
            orchestrator = OrchestratorAgentReAct()
            result = orchestrator.run_scheduling(user_query)
    else:
        user_query = "Schedule all flexible loads"
        orchestrator = OrchestratorAgentReAct()
        result = orchestrator.run_scheduling(user_query)

    # Print token usage
    print("\n" + "=" * 80)
    print("TOKEN USAGE")
    print("=" * 80)
    usage = result["total_usage"]
    print(f"  Prompt tokens: {usage['prompt_tokens']}")
    print(f"  Completion tokens: {usage['completion_tokens']}")
    print(f"  Total tokens: {usage['total_tokens']}")
    print(f"  Iterations: {result.get('iterations', 'N/A')}")
    print(f"  Actions taken: {len(result.get('actions_taken', []))}")
    print("=" * 80)

    if result.get("success"):
        print(f"\nWorkflow completed successfully.")


if __name__ == "__main__":
    main()
