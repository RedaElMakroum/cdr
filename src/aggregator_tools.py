"""
Tools for the Aggregator agent to manage DR events, portfolio, and household communications.
Mirrors the structure and patterns of tools.py for consistency.
"""

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

import requests

from config import CEREBRAS_API_KEY, CEREBRAS_MODEL
from event_logger import log_event


# Base data directory
DATA_DIR = Path(__file__).parent.parent / "data"


def _load_aggregator_settings() -> Dict[str, Any]:
    """Load aggregator settings from data/aggregator_settings.json."""
    settings_path = DATA_DIR / "aggregator_settings.json"
    with open(settings_path, 'r') as f:
        return json.load(f)


def _slot_to_time(slot: int) -> str:
    """Convert a 15-minute slot index (0-95) to HH:MM format."""
    hours = (slot * 15) // 60
    minutes = (slot * 15) % 60
    return f"{hours:02d}:{minutes:02d}"


def _time_to_slot(time_str: str) -> int:
    """Convert HH:MM format to a 15-minute slot index (0-95)."""
    parts = time_str.split(":")
    hours = int(parts[0])
    minutes = int(parts[1])
    return (hours * 60 + minutes) // 15


def get_market_obligation(obligation_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Retrieves market obligation details (day-ahead flexibility commitments).

    Args:
        obligation_id: Specific obligation ID to load. If None, loads the latest.

    Returns:
        Dictionary containing:
        - success: Boolean
        - obligation_id: The obligation identifier
        - date: Date for this obligation
        - events: List of flexibility events with time windows, targets, compensation
        - portfolio: Summary of registered household capacity
    """
    obligations_dir = DATA_DIR / "market_obligations"

    try:
        if obligation_id:
            # Load specific obligation
            obligation_path = obligations_dir / f"{obligation_id}.json"
            if not obligation_path.exists():
                return {
                    "success": False,
                    "error": f"Obligation {obligation_id} not found"
                }
            with open(obligation_path, 'r') as f:
                obligation = json.load(f)
        else:
            # Load the latest obligation file
            obligation_files = sorted(obligations_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not obligation_files:
                return {
                    "success": False,
                    "error": "No market obligations found. Create one in data/market_obligations/"
                }
            with open(obligation_files[0], 'r') as f:
                obligation = json.load(f)

        print(f"[AGGREGATOR] Loaded market obligation: {obligation.get('obligation_id', 'unknown')}")
        print(f"  Date: {obligation.get('date', 'N/A')}")
        print(f"  Events: {len(obligation.get('events', []))}")

        return {
            "success": True,
            **obligation
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load obligation: {str(e)}"
        }


def get_portfolio_status(sandbox: bool = False) -> Dict[str, Any]:
    """
    Retrieves the current status of registered households and their flexible assets.

    Args:
        sandbox: If True, read from portfolio_sandbox.json instead of portfolio.json.

    Returns:
        Dictionary containing:
        - success: Boolean
        - households: List of household details with appliances
        - total_households: Count
        - total_flexible_capacity_kw: Aggregate capacity
    """
    portfolio_path = _get_portfolio_path(sandbox)

    try:
        if not portfolio_path.exists():
            return {
                "success": False,
                "error": "Portfolio file not found at data/portfolio.json"
            }

        with open(portfolio_path, 'r') as f:
            portfolio = json.load(f)

        households = portfolio.get("households", [])
        total_capacity = sum(h.get("total_flexible_capacity_kw", 0) for h in households)

        print(f"[AGGREGATOR] Portfolio status: {len(households)} households, {total_capacity} kW total capacity")

        return {
            "success": True,
            "households": households,
            "total_households": len(households),
            "total_flexible_capacity_kw": total_capacity
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to load portfolio: {str(e)}"
        }


def dispatch_dr_event(
    household_id: str,
    window_start: str,
    window_end: str,
    target_kw: float,
    compensation_eur_kwh: float = None
) -> Dict[str, Any]:
    """
    Dispatches a battery discharge DR event to a household.
    Saves the event to data/dr_events/ for the HEMS agent to pick up.

    Args:
        household_id: Target household (e.g., "HH-001")
        window_start: Start time in HH:MM format
        window_end: End time in HH:MM format
        target_kw: Requested discharge power in kW
        compensation_eur_kwh: Compensation rate, loaded from aggregator_settings.json if not provided

    Returns:
        Dictionary with event_id, household_id, status, event_details
    """
    if compensation_eur_kwh is None:
        settings = _load_aggregator_settings()
        compensation_eur_kwh = settings["default_compensation_eur_kwh"]
    event_type = "battery_discharge"
    events_dir = DATA_DIR / "dr_events"
    events_dir.mkdir(parents=True, exist_ok=True)

    # Validate inputs
    try:
        start_slot = _time_to_slot(window_start)
        end_slot = _time_to_slot(window_end)
    except (ValueError, IndexError):
        return {
            "success": False,
            "error": f"Invalid time format. Use HH:MM. Got start={window_start}, end={window_end}"
        }

    if not 0 <= start_slot < 96 or not 0 < end_slot <= 96:
        return {
            "success": False,
            "error": f"Time window out of range. Slots must be 0-96."
        }

    if start_slot >= end_slot:
        return {
            "success": False,
            "error": f"Start time must be before end time. Got {window_start} -> {window_end}"
        }

    if target_kw <= 0:
        return {
            "success": False,
            "error": f"Target kW must be positive. Got {target_kw}"
        }

    if compensation_eur_kwh < 0:
        return {
            "success": False,
            "error": f"Compensation must be non-negative. Got {compensation_eur_kwh}"
        }

    # Generate event
    date_str = datetime.now().strftime("%Y-%m-%d")
    event_id = f"DR-{date_str}-{uuid.uuid4().hex[:6].upper()}"

    duration_slots = end_slot - start_slot
    duration_hours = duration_slots * 15 / 60
    max_compensation = target_kw * duration_hours * compensation_eur_kwh

    event = {
        "event_id": event_id,
        "household_id": household_id,
        "event_type": event_type,
        "window_start": window_start,
        "window_start_slot": start_slot,
        "window_end": window_end,
        "window_end_slot": end_slot,
        "duration_slots": duration_slots,
        "target_kw": target_kw,
        "compensation_eur_kwh": compensation_eur_kwh,
        "max_compensation_eur": round(max_compensation, 4),
        "status": "dispatched",
        "created_at": datetime.now().isoformat(),
        "responded_at": None,
        "response": None
    }

    # Save event
    event_path = events_dir / f"{event_id}.json"
    with open(event_path, 'w') as f:
        json.dump(event, f, indent=2)

    # Log lifecycle entry
    log_event(event_id, source="aggregator", action="dispatched", details={
        "household_id": household_id,
        "event_type": event_type,
        "target_kw": target_kw,
        "window": f"{window_start}-{window_end}",
        "compensation_eur_kwh": compensation_eur_kwh,
    })

    print(f"[AGGREGATOR] DR event dispatched: {event_id}")
    print(f"  Household: {household_id}")
    print(f"  Type: {event_type}")
    print(f"  Window: {window_start} - {window_end} ({duration_slots} slots)")
    print(f"  Target: {target_kw} kW")
    print(f"  Compensation: {compensation_eur_kwh} EUR/kWh (max {max_compensation:.2f} EUR)")

    return {
        "success": True,
        "event_id": event_id,
        "household_id": household_id,
        "status": "dispatched",
        "event_details": event
    }


def collect_response(event_id: str) -> Dict[str, Any]:
    """
    Checks the household response for a dispatched DR event.

    Args:
        event_id: The DR event identifier

    Returns:
        Dictionary containing:
        - success: Boolean
        - event_id: The event identifier
        - status: Current status (dispatched, accepted, rejected, negotiating)
        - response: Response details if available
    """
    # Check event exists
    event_path = DATA_DIR / "dr_events" / f"{event_id}.json"
    if not event_path.exists():
        return {
            "success": False,
            "error": f"Event {event_id} not found"
        }

    with open(event_path, 'r') as f:
        event = json.load(f)

    # Check for response
    response_path = DATA_DIR / "dr_responses" / f"{event_id}.json"
    if response_path.exists():
        with open(response_path, 'r') as f:
            response = json.load(f)

        # Update event status from response
        event["status"] = response.get("status", event["status"])
        event["responded_at"] = response.get("responded_at")
        event["response"] = response

        # Save updated event
        with open(event_path, 'w') as f:
            json.dump(event, f, indent=2)

        print(f"[AGGREGATOR] Response for {event_id}: {response.get('status', 'unknown')}")
        if response.get("status") == "accepted":
            print(f"  Committed: {response.get('commitment_kw', 0)} kW")
            print(f"  Appliances: {', '.join(response.get('accepted_appliances', []))}")

        return {
            "success": True,
            "event_id": event_id,
            "status": response.get("status", "unknown"),
            "response": response,
            "event_details": event
        }
    else:
        print(f"[AGGREGATOR] No response yet for {event_id}. Status: {event['status']}")
        return {
            "success": True,
            "event_id": event_id,
            "status": event.get("status", "dispatched"),
            "response": None,
            "event_details": event
        }


def handle_household_request(request_id: str) -> Dict[str, Any]:
    """
    Processes a bottom-up message from a household.
    Triages the request as auto-handleable or requiring human escalation.

    Args:
        request_id: The household request identifier

    Returns:
        Dictionary containing:
        - success: Boolean
        - request_id: The request identifier
        - request: Full request details
        - triage: "auto_handle" or "escalate"
        - suggested_action: What should be done
    """
    requests_dir = DATA_DIR / "household_requests"

    request_path = requests_dir / f"{request_id}.json"
    if not request_path.exists():
        return {
            "success": False,
            "error": f"Request {request_id} not found"
        }

    with open(request_path, 'r') as f:
        request_data = json.load(f)

    # Triage logic based on request type
    request_type = request_data.get("type", "unknown")

    auto_handle_types = {
        "asset_update": "Update portfolio with new asset information",
        "preference_change": "Update prosumer preference profile",
        "availability_update": "Update appliance availability schedule",
        "spec_change": "Update appliance specifications"
    }

    escalate_types = {
        "contract_change": "Requires human review -- contract modification requested",
        "complaint": "Requires human review -- prosumer complaint",
        "opt_out": "Requires human review -- prosumer wants to leave program",
        "unknown": "Requires human review -- unrecognized request type"
    }

    if request_type in auto_handle_types:
        triage = "auto_handle"
        suggested_action = auto_handle_types[request_type]

        # Auto-handle: update portfolio if it's an asset update
        if request_type == "asset_update":
            _apply_asset_update(request_data)

        request_data["triage"] = triage
        request_data["triage_action"] = suggested_action
        request_data["triaged_at"] = datetime.now().isoformat()

    else:
        triage = "escalate"
        suggested_action = escalate_types.get(request_type, escalate_types["unknown"])

        request_data["triage"] = triage
        request_data["triage_action"] = suggested_action
        request_data["triaged_at"] = datetime.now().isoformat()

    # Save updated request
    with open(request_path, 'w') as f:
        json.dump(request_data, f, indent=2)

    print(f"[AGGREGATOR] Household request {request_id}: {triage}")
    print(f"  Type: {request_type}")
    print(f"  Action: {suggested_action}")

    return {
        "success": True,
        "request_id": request_id,
        "request": request_data,
        "triage": triage,
        "suggested_action": suggested_action
    }


def submit_dr_response(
    event_id: str,
    accepted: bool,
    commitment_kw: float = 0.0,
    accepted_appliances: Optional[List[str]] = None,
    reasoning: str = "",
    conversation_summary: str = ""
) -> Dict[str, Any]:
    """
    Submits the prosumer's response to a DR event.
    Called by the HEMS DR event handler after prosumer conversation.

    Args:
        event_id: The DR event identifier
        accepted: Whether the prosumer accepted
        commitment_kw: kW the household commits to deliver
        accepted_appliances: List of appliance IDs that will participate
        reasoning: Prosumer's reasoning or conversation context
        conversation_summary: Summary of the conversation for aggregator records

    Returns:
        Dictionary containing:
        - success: Boolean
        - event_id: The event identifier
        - status: "accepted" or "rejected"
        - commitment_kw: Committed flexibility
    """
    responses_dir = DATA_DIR / "dr_responses"
    responses_dir.mkdir(parents=True, exist_ok=True)

    # Verify event exists
    event_path = DATA_DIR / "dr_events" / f"{event_id}.json"
    if not event_path.exists():
        return {
            "success": False,
            "error": f"Event {event_id} not found"
        }

    status = "accepted" if accepted else "rejected"

    response = {
        "event_id": event_id,
        "status": status,
        "commitment_kw": commitment_kw if accepted else 0.0,
        "accepted_appliances": accepted_appliances or [],
        "reasoning": reasoning,
        "conversation_summary": conversation_summary,
        "responded_at": datetime.now().isoformat()
    }

    # Save response
    response_path = responses_dir / f"{event_id}.json"
    with open(response_path, 'w') as f:
        json.dump(response, f, indent=2)

    # Update event status
    with open(event_path, 'r') as f:
        event = json.load(f)
    event["status"] = status
    event["responded_at"] = response["responded_at"]
    with open(event_path, 'w') as f:
        json.dump(event, f, indent=2)

    # Log lifecycle entry
    log_event(event_id, source="prosumer", action="approved" if accepted else "rejected", details={
        "commitment_kw": commitment_kw if accepted else 0.0,
        "appliances": accepted_appliances or [],
        "reasoning": reasoning,
    })

    print(f"[HEMS] DR response submitted for {event_id}: {status}")
    if accepted:
        print(f"  Committed: {commitment_kw} kW")
        print(f"  Appliances: {', '.join(accepted_appliances or [])}")
    else:
        print(f"  Reason: {reasoning}")

    return {
        "success": True,
        "event_id": event_id,
        "status": status,
        "commitment_kw": commitment_kw if accepted else 0.0
    }


def create_household_request(
    household_id: str,
    request_type: str,
    message: str,
    details: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Creates a bottom-up request from a household to the aggregator.

    Args:
        household_id: The household sending the request
        request_type: Type of request (asset_update, preference_change, complaint, etc.)
        message: Human-readable message describing the request
        details: Additional structured data (e.g., new EV specs)

    Returns:
        Dictionary containing:
        - success: Boolean
        - request_id: Generated request identifier
        - status: "pending"
    """
    requests_dir = DATA_DIR / "household_requests"
    requests_dir.mkdir(parents=True, exist_ok=True)

    request_id = f"REQ-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    request_data = {
        "request_id": request_id,
        "household_id": household_id,
        "type": request_type,
        "message": message,
        "details": details or {},
        "status": "pending",
        "triage": None,
        "triage_action": None,
        "created_at": datetime.now().isoformat(),
        "triaged_at": None
    }

    request_path = requests_dir / f"{request_id}.json"
    with open(request_path, 'w') as f:
        json.dump(request_data, f, indent=2)

    print(f"[HEMS] Household request created: {request_id}")
    print(f"  Type: {request_type}")
    print(f"  Message: {message}")

    return {
        "success": True,
        "request_id": request_id,
        "status": "pending"
    }


def get_pending_dr_events(household_id: str) -> Dict[str, Any]:
    """
    Retrieves pending DR events for a specific household.
    Used by the prosumer-side to poll for incoming events.

    Args:
        household_id: The household to check for pending events

    Returns:
        Dictionary containing:
        - success: Boolean
        - pending_events: List of DR events with status "dispatched"
    """
    events_dir = DATA_DIR / "dr_events"

    if not events_dir.exists():
        return {
            "success": True,
            "pending_events": []
        }

    pending = []
    for event_file in sorted(events_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        with open(event_file, 'r') as f:
            event = json.load(f)

        if event.get("household_id") == household_id and event.get("status") == "dispatched":
            pending.append(event)

    return {
        "success": True,
        "pending_events": pending
    }


def get_active_dr_events() -> Dict[str, Any]:
    """
    Get all active (unresolved) DR events across the portfolio.
    Scans dr_events/ for dispatched events and checks dr_responses/ for resolution.

    Returns:
        Dictionary containing:
        - success: Boolean
        - active_events: List of events still awaiting response or unresolved
    """
    events_dir = DATA_DIR / "dr_events"
    responses_dir = DATA_DIR / "dr_responses"

    if not events_dir.exists():
        return {"success": True, "active_events": []}

    active = []
    for event_file in sorted(events_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        with open(event_file, 'r') as f:
            event = json.load(f)

        event_id = event.get("event_id", event_file.stem)

        # Check if a response exists
        response_path = responses_dir / f"{event_id}.json"
        if response_path.exists():
            with open(response_path, 'r') as f:
                resp = json.load(f)
            resp_status = resp.get("status", "")
            if resp_status in ("accepted", "rejected"):
                continue  # Resolved, skip

        active.append({
            "event_id": event_id,
            "household_id": event.get("household_id"),
            "event_type": event.get("event_type"),
            "window_start": event.get("window_start"),
            "window_end": event.get("window_end"),
            "target_kw": event.get("target_kw"),
            "status": event.get("status", "dispatched"),
            "dispatched_at": event.get("dispatched_at", event.get("created_at"))
        })

    return {"success": True, "active_events": active}


def _apply_asset_update(request_data: Dict[str, Any]) -> None:
    """
    Applies an asset update from a household request to the portfolio.
    Internal helper for auto-handled requests.
    """
    portfolio_path = DATA_DIR / "portfolio.json"
    if not portfolio_path.exists():
        return

    with open(portfolio_path, 'r') as f:
        portfolio = json.load(f)

    household_id = request_data.get("household_id")
    details = request_data.get("details", {})

    for household in portfolio.get("households", []):
        if household.get("household_id") == household_id:
            # Add or update appliance
            new_appliance = details.get("appliance")
            if new_appliance:
                existing = [a for a in household["appliances"] if a["appliance_id"] != new_appliance["appliance_id"]]
                existing.append(new_appliance)
                household["appliances"] = existing
                household["total_flexible_capacity_kw"] = sum(a.get("power_kw", 0) for a in existing)
                print(f"  [AUTO] Updated portfolio for {household_id}: added/updated {new_appliance['appliance_id']}")
            break

    with open(portfolio_path, 'w') as f:
        json.dump(portfolio, f, indent=2)


# =============================================================================
# SANDBOX & PROSUMER MESSAGE PROCESSING
# =============================================================================

def _get_portfolio_path(sandbox: bool = False) -> Path:
    """Return the appropriate portfolio file path.

    When sandbox=True, returns data/portfolio_sandbox.json and auto-copies
    from the base portfolio.json on first use.
    """
    if not sandbox:
        return DATA_DIR / "portfolio.json"

    sandbox_path = DATA_DIR / "portfolio_sandbox.json"
    if not sandbox_path.exists():
        base_path = DATA_DIR / "portfolio.json"
        if base_path.exists():
            shutil.copy2(base_path, sandbox_path)
            print("[SANDBOX] Created portfolio_sandbox.json from portfolio.json")
        else:
            return base_path  # fallback
    return sandbox_path


def reset_sandbox_portfolio() -> Dict[str, Any]:
    """Copy portfolio.json -> portfolio_sandbox.json, resetting sandbox state."""
    base_path = DATA_DIR / "portfolio.json"
    sandbox_path = DATA_DIR / "portfolio_sandbox.json"

    if not base_path.exists():
        return {"success": False, "error": "Base portfolio.json not found"}

    shutil.copy2(base_path, sandbox_path)
    print("[SANDBOX] Reset portfolio_sandbox.json from portfolio.json")
    return {"success": True, "message": "Sandbox portfolio reset to base state"}


def _apply_portfolio_changes(
    portfolio_path: Path,
    household_id: str,
    changes: List[Dict[str, Any]]
) -> List[str]:
    """Apply a list of structured changes to the portfolio file.

    Each change dict has a 'type' key:
      - add_asset: adds or replaces an asset by asset_id
      - update_asset: partial update of an existing asset
      - remove_asset: removes an asset by asset_id
      - update_preference: updates prosumer_profile fields
      - update_availability: updates household-level availability info

    Returns a list of human-readable descriptions of applied changes.
    """
    with open(portfolio_path, 'r') as f:
        portfolio = json.load(f)

    descriptions = []
    household = None
    for h in portfolio.get("households", []):
        if h.get("household_id") == household_id:
            household = h
            break

    if household is None:
        return [f"Household {household_id} not found in portfolio"]

    assets = household.get("assets", [])

    for change in changes:
        ctype = change.get("type", "")

        if ctype == "add_asset":
            asset = change.get("asset", {})
            aid = asset.get("asset_id", "")
            # Ensure numeric fields are valid
            asset["power_kw"] = float(asset.get("power_kw", 0) or 0)
            asset["capacity_kwh"] = float(asset.get("capacity_kwh", 0) or 0)
            # Replace if exists, otherwise append
            assets = [a for a in assets if a.get("asset_id") != aid]
            assets.append(asset)
            descriptions.append(f"Added asset: {aid} ({asset.get('type', 'unknown')}, {asset['power_kw']} kW, {asset['capacity_kwh']} kWh)")

        elif ctype == "update_asset":
            aid = change.get("asset_id", "")
            updates = change.get("updates", {})
            for a in assets:
                if a.get("asset_id") == aid:
                    a.update(updates)
                    descriptions.append(f"Updated asset {aid}: {updates}")
                    break
            else:
                descriptions.append(f"Asset {aid} not found for update")

        elif ctype == "remove_asset":
            aid = change.get("asset_id", "")
            before = len(assets)
            assets = [a for a in assets if a.get("asset_id") != aid]
            if len(assets) < before:
                descriptions.append(f"Removed asset: {aid}")
            else:
                descriptions.append(f"Asset {aid} not found for removal")

        elif ctype == "update_preference":
            updates = change.get("updates", {})
            profile = household.get("prosumer_profile", {})
            profile.update(updates)
            household["prosumer_profile"] = profile
            descriptions.append(f"Updated preferences: {updates}")

        elif ctype == "update_availability":
            updates = change.get("updates", {})
            household.update(updates)
            descriptions.append(f"Updated availability: {updates}")

    household["assets"] = assets
    household["total_flexible_capacity_kw"] = sum(
        float(a.get("power_kw", 0) or 0) for a in assets
    )

    with open(portfolio_path, 'w') as f:
        json.dump(portfolio, f, indent=2)

    return descriptions


def process_prosumer_message(
    household_id: str,
    message: str,
    sandbox: bool = True
) -> Dict[str, Any]:
    """Process a free-text prosumer message via a single LLM call.

    The LLM classifies the message, extracts structured portfolio changes,
    and generates a confirmation. No multi-step reasoning -- one call.

    Args:
        household_id: The household sending the message.
        message: Free-text prosumer message.
        sandbox: If True, apply changes to sandbox portfolio only.

    Returns:
        Dictionary with classification, changes_applied, confirmation_message, request_id.
    """
    portfolio_path = _get_portfolio_path(sandbox)

    # Load current portfolio context
    try:
        with open(portfolio_path, 'r') as f:
            portfolio = json.load(f)
    except Exception as e:
        return {"success": False, "error": f"Could not load portfolio: {e}"}

    # Find household info
    hh_info = None
    for h in portfolio.get("households", []):
        if h.get("household_id") == household_id:
            hh_info = h
            break

    if hh_info is None:
        return {"success": False, "error": f"Household {household_id} not found in portfolio"}

    # Build the classification prompt
    system_prompt = f"""You are an energy aggregator's automated message processor. A prosumer from household {household_id} has sent a message. Your job:

1. Classify the message into one of these types:
   - asset_update: Adding, removing, or changing an energy asset (EV charger, battery, PV, heat pump, etc.). This includes plugging in an EV, connecting a new device, or announcing a new asset is available. If the prosumer mentions a device not currently in the portfolio, classify as asset_update even if they also mention a charging/usage target.
   - preference_change: Changing comfort preferences, participation willingness, contact preferences
   - availability_update: Reporting absence, schedule changes, seasonal availability
   - spec_change: Updating specs of existing assets (capacity, power rating, etc.)
   - general_inquiry: Questions or informational messages that need no portfolio changes. Only use this if the message truly asks a question or shares info without implying any asset, preference, or availability change.
   - complaint: Expressing dissatisfaction
   - opt_out: Wanting to leave the flexibility program

2. Extract any portfolio changes needed as structured operations.

3. Write a short, friendly confirmation message to send back to the prosumer.

Current household portfolio:
{json.dumps(hh_info, indent=2)}

Respond with ONLY a valid JSON object (no markdown, no extra text):
{{
  "request_type": "<one of the types above>",
  "summary": "<one-line summary of what the prosumer wants>",
  "portfolio_changes": [
    {{
      "type": "add_asset|update_asset|remove_asset|update_preference|update_availability",
      "asset": {{"asset_id": "...", "type": "...", "capacity_kwh": ..., "power_kw": ...}},
      "asset_id": "...",
      "updates": {{...}}
    }}
  ],
  "confirmation_message": "<friendly response to the prosumer>"
}}

If no portfolio changes are needed, set "portfolio_changes" to an empty list [].
Only include fields relevant to each change type. Use snake_case for asset IDs (e.g. "ev_charger", "heat_pump").

IMPORTANT asset modeling rules:
- Each physical device = exactly ONE asset entry. Never split a device into multiple assets.
- An EV is ONE asset (asset_id: "ev_charger", type: "ev") with BOTH power_kw (charger rating) AND capacity_kwh (onboard battery size).
- A home battery is ONE asset with power_kw (inverter rating) and capacity_kwh (storage capacity).
- A PV system is ONE asset with power_kw (inverter/peak output).
- A heat pump is ONE asset with power_kw (electrical input power).
- power_kw and capacity_kwh must always be numbers (never null)."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message}
    ]

    # Call LLM (same pattern as orchestrator _call_llm)
    import time as _time
    api_key = CEREBRAS_API_KEY
    base_url = "https://api.cerebras.ai/v1"
    model = CEREBRAS_MODEL

    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 2000,
            "stream": False
        }
        call_start = _time.time()
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        llm_latency = round(_time.time() - call_start, 3)
        result = response.json()
        llm_content = result['choices'][0]['message'].get('content', '')
        llm_usage = result.get('usage', {})
        llm_usage['latency_seconds'] = llm_latency
    except Exception as e:
        return {"success": False, "error": f"LLM call failed: {e}"}

    # Parse JSON from LLM response
    try:
        # Strip markdown code fences if present
        cleaned = llm_content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "success": False,
            "error": "Failed to parse LLM response as JSON",
            "raw_response": llm_content
        }

    request_type = parsed.get("request_type", "general_inquiry")
    summary = parsed.get("summary", "")
    portfolio_changes = parsed.get("portfolio_changes", [])
    confirmation_message = parsed.get("confirmation_message", "Message received.")

    # Create a household request record
    req_result = create_household_request(
        household_id=household_id,
        request_type=request_type,
        message=message,
        details={"llm_parsed": parsed}
    )
    request_id = req_result.get("request_id", "unknown")

    # Apply portfolio changes if any
    change_descriptions = []
    if portfolio_changes:
        change_descriptions = _apply_portfolio_changes(
            portfolio_path, household_id, portfolio_changes
        )
        print(f"[PROSUMER-MSG] Applied {len(change_descriptions)} changes for {household_id}")

    print(f"[PROSUMER-MSG] Processed message from {household_id}: {request_type}")
    print(f"  Summary: {summary}")
    print(f"  Changes: {len(portfolio_changes)}")

    return {
        "success": True,
        "request_id": request_id,
        "request_type": request_type,
        "summary": summary,
        "portfolio_changes": portfolio_changes,
        "changes_applied": change_descriptions,
        "confirmation_message": confirmation_message,
        "sandbox": sandbox,
        "llm_usage": llm_usage
    }
