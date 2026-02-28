"""
Tools for the HEMS agent to interact with electricity pricing and appliance scheduling.
These tools are registered with the Claude Agent SDK.
"""

import json
import requests
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

# Google Calendar imports (optional - graceful degradation if not installed)
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    CALENDAR_AVAILABLE = True
except ImportError:
    CALENDAR_AVAILABLE = False


def get_electricity_prices(date: Optional[str] = None, use_cached_prices: bool = False) -> Dict[str, Any]:
    """
    Retrieves day-ahead electricity prices for the specified date.

    Args:
        date: Date in YYYY-MM-DD format. If None, returns tomorrow's prices.
        use_cached_prices: If True, loads from test_prices_reference.json (for benchmarking)

    Returns:
        Dictionary containing:
        - date: The date for these prices
        - unit: Price unit (EUR/kWh)
        - resolution_minutes: Time resolution (15)
        - timeslots: List of time labels (e.g., ["00:00", "00:15", ...])
        - prices: List of 96 price values
    """
    # Use cached reference prices if requested (for systematic evaluation)
    if use_cached_prices:
        cache_path = Path(__file__).parent / "test_prices_reference.json"
        if cache_path.exists():
            with open(cache_path, 'r') as f:
                price_data = json.load(f)
            print(f"✓ Loaded cached reference prices from {cache_path.name}")
            print(f"  Date: {price_data['date']}, {len(price_data['prices'])} price points")
            return price_data
        else:
            print(f"⚠ Cached prices not found at {cache_path}, falling back to API fetch")

    # Try to fetch from ENTSO-E API
    try:
        from entsoe_client import fetch_entsoe_prices
        from config import ENTSOE_API_KEY, BIDDING_ZONE

        if ENTSOE_API_KEY:
            # If no date specified, fetch tomorrow's prices (day-ahead)
            if date is None:
                tomorrow = datetime.now() + timedelta(days=1)
                date = tomorrow.strftime("%Y-%m-%d")
                print(f"Attempting to fetch from ENTSO-E API for date: {date} (tomorrow - day-ahead)...")
            else:
                print(f"Attempting to fetch from ENTSO-E API for date: {date}...")

            result = fetch_entsoe_prices(ENTSOE_API_KEY, date, BIDDING_ZONE)
            print(f"✓ Successfully fetched {len(result['prices'])} price points from ENTSO-E")
            return result
    except Exception as e:
        # Fallback to mock data if API fails
        print(f"⚠ ENTSO-E API failed: {type(e).__name__}: {e}")
        print("Falling back to mock data...")

    # Fallback: Read from mock file
    data_path = Path(__file__).parent / "data" / "prices_sample.json"
    with open(data_path, 'r') as f:
        price_data = json.load(f)

    return price_data


def get_weather_forecast(
    location: str = "Vienna",
    date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieves hourly outdoor temperature forecast for the specified date.

    Args:
        location: City name (default: "Vienna")
        date: Date in YYYY-MM-DD format. If None, returns tomorrow's forecast.

    Returns:
        Dictionary containing:
        - date: The date for this forecast
        - location: Location name
        - temps_hourly: List of 24 hourly temperatures in Celsius
        - temps_min: Minimum temperature
        - temps_max: Maximum temperature
    """
    # Vienna, Austria coordinates
    LOCATIONS = {
        "Vienna": {"lat": 48.2082, "lon": 16.3738}
    }

    if location not in LOCATIONS:
        location = "Vienna"

    coords = LOCATIONS[location]

    # Parse target date
    if date is None:
        target_date = datetime.now() + timedelta(days=1)
    else:
        target_date = datetime.strptime(date, "%Y-%m-%d")

    date_str = target_date.strftime("%Y-%m-%d")

    try:
        # Use Open-Meteo API (free, no API key required)
        # Request 15-minute resolution to match electricity price resolution
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": coords["lat"],
            "longitude": coords["lon"],
            "minutely_15": "temperature_2m",
            "start_date": date_str,
            "end_date": date_str,
            "timezone": "Europe/Vienna"
        }

        print(f"Fetching weather forecast (15-min resolution) for {location} on {date_str}...")
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()

        data = response.json()

        # Extract 15-minute temperatures (should be 96 values for 24 hours)
        temps_15min = data["minutely_15"]["temperature_2m"]

        # Ensure we have 96 values (24h * 4 per hour)
        if len(temps_15min) < 96:
            temps_15min.extend([temps_15min[-1]] * (96 - len(temps_15min)))
        temps_15min = temps_15min[:96]

        print(f"✓ Fetched 96 temperature values (15-min): {min(temps_15min):.1f}°C - {max(temps_15min):.1f}°C")

        return {
            "date": date_str,
            "location": location,
            "temps_15min": temps_15min,
            "temps_min": min(temps_15min),
            "temps_max": max(temps_15min),
            "unit": "Celsius",
            "resolution_minutes": 15
        }

    except Exception as e:
        print(f"⚠ Weather API failed: {type(e).__name__}: {e}")
        print("Using fallback temperature data...")

        # Fallback: typical winter day in Vienna (96 values at 15-min resolution)
        # Pattern: gradual cooling at night, warming during day, cooling in evening
        hourly_pattern = [
            2, 1, 0, 0, -1, -1, 0, 1,  # 00:00 - 07:00 (night, coldest)
            3, 5, 7, 8, 9, 10, 10, 9,  # 08:00 - 15:00 (day, warming)
            7, 5, 4, 3, 2, 2, 2, 1     # 16:00 - 23:00 (evening, cooling)
        ]

        # Expand to 15-minute resolution (repeat each hourly value 4 times)
        fallback_temps = []
        for temp in hourly_pattern:
            fallback_temps.extend([temp] * 4)

        return {
            "date": date_str,
            "location": location,
            "temps_15min": fallback_temps,
            "temps_min": min(fallback_temps),
            "temps_max": max(fallback_temps),
            "unit": "Celsius",
            "resolution_minutes": 15,
            "fallback": True
        }


def calculate_heating_requirement(
    outdoor_temps: List[float],
    comfort_min: float = 20.0,
    comfort_max: float = 22.0,
    building_type: str = "old",
    initial_temp: float = 19.0
) -> Dict[str, Any]:
    """
    Calculates heating requirement using RC thermal model.

    Args:
        outdoor_temps: List of 96 outdoor temperatures (15-min resolution) in Celsius
        comfort_min: Minimum comfort temperature in Celsius
        comfort_max: Maximum comfort temperature in Celsius
        building_type: Building type ('old', 'modern', 'passive')
        initial_temp: Initial indoor temperature in Celsius

    Returns:
        Dictionary containing:
        - building_type: The building type used
        - comfort_range: [min, max] comfort temperatures
        - outdoor_temp_range: [min, max] outdoor temperatures
        - heating_slots_needed: Number of 15-min slots heating is needed
        - heating_hours_needed: Total hours of heating needed
        - slots_requiring_heat: List of slot indices where heating is needed
        - estimated_total_power_kwh: Total energy needed in kWh
    """
    # Building parameters from DERMOT validated model
    BUILDING_PARAMS = {
        # "old": {
        #     "wall_resistance": 1.0,      # R-value (m²K/W), U = 1.0 W/m²K
        #     "window_resistance": 0.2,    # U = 5.0 W/m²K
        #     "roof_resistance": 1.5,      # U = 0.67 W/m²K
        #     "floor_resistance": 1.0,     # U = 1.0 W/m²K
        #     "wall_capacitance": 2000000, # 2.0 MJ/K
        #     "air_capacitance": 210000,   # 0.21 MJ/K (DERMOT validated)
        #     "wall_area": 100,            # m²
        #     "window_area": 20,           # m²
        #     "roof_area": 70,             # m²
        #     "floor_area": 70,            # m²
        #     "infiltration_rate": 1.5     # ACH
        # },
        # "modern": {
        #     "wall_resistance": 3.5,
        #     "window_resistance": 0.7,
        #     "roof_resistance": 5.0,
        #     "floor_resistance": 4.0,
        #     "wall_capacitance": 1500000,
        #     "air_capacitance": 210000,
        #     "wall_area": 100,
        #     "window_area": 20,
        #     "roof_area": 70,
        #     "floor_area": 70,
        #     "infiltration_rate": 0.5
        # },
        "passive": {
            "wall_resistance": 8.0,
            "window_resistance": 1.5,
            "roof_resistance": 10.0,
            "floor_resistance": 8.0,
            "wall_capacitance": 2500000,
            "air_capacitance": 210000,
            "wall_area": 100,
            "window_area": 20,
            "roof_area": 70,
            "floor_area": 70,
            "infiltration_rate": 0.3
        }
    }

    params = BUILDING_PARAMS.get(building_type, BUILDING_PARAMS["passive"])

    # Physical constants
    AIR_DENSITY = 1.2  # kg/m³
    SPECIFIC_HEAT_AIR = 1005  # J/(kg·K)
    HEAT_PUMP_POWER = 6.0  # kW (typical heat pump capacity)

    def calculate_heat_loss(indoor_temp: float, outdoor_temp: float) -> float:
        """Calculate heat loss through building envelope in Watts."""
        temp_diff = indoor_temp - outdoor_temp

        # Conduction through each component: Q = ΔT / R * A
        wall_loss = (temp_diff / params["wall_resistance"]) * params["wall_area"]
        window_loss = (temp_diff / params["window_resistance"]) * params["window_area"]
        roof_loss = (temp_diff / params["roof_resistance"]) * params["roof_area"]
        floor_loss = (temp_diff / params["floor_resistance"]) * params["floor_area"]

        # Infiltration: Q = ACH * V * ρ * cp * ΔT / 3600
        # Building volume from DERMOT: (wallArea + roofArea) * ceiling_height
        building_volume = (params["wall_area"] + params["roof_area"]) * 2.5  # m³
        infiltration_loss = (
            params["infiltration_rate"] *
            building_volume *
            AIR_DENSITY *
            SPECIFIC_HEAT_AIR *
            temp_diff
        ) / 3600

        total_loss = wall_loss + window_loss + roof_loss + floor_loss + infiltration_loss
        return total_loss  # Watts

    def calculate_next_temp(current_indoor: float, outdoor: float, heating_power_kw: float) -> float:
        """Calculate next temperature using RC model."""
        total_capacitance = params["wall_capacitance"] + params["air_capacitance"]  # J/K
        heat_loss = calculate_heat_loss(current_indoor, outdoor)  # Watts
        heating_power_w = heating_power_kw * 1000  # Convert kW to W

        # Energy balance: ΔT = (Q_heating - Q_loss) * Δt / C
        time_step = 15 * 60  # 15 minutes in seconds
        net_heat_flow = heating_power_w - heat_loss
        temp_change = (net_heat_flow * time_step) / total_capacitance

        return current_indoor + temp_change

    # Simulate 24-hour period to determine heating needs
    indoor_temp = initial_temp
    slots_requiring_heat = []
    total_energy_kwh = 0.0

    print(f"Simulating thermal dynamics ({building_type} building)...")
    print(f"  Comfort range: {comfort_min}°C - {comfort_max}°C")
    print(f"  Outdoor range: {min(outdoor_temps):.1f}°C - {max(outdoor_temps):.1f}°C")

    for slot in range(96):
        outdoor = outdoor_temps[slot]

        # Check if heating is needed to maintain comfort
        # Project temperature without heating
        temp_without_heating = calculate_next_temp(indoor_temp, outdoor, 0.0)

        # If temperature would drop below comfort minimum, heating is needed
        if temp_without_heating < comfort_min:
            slots_requiring_heat.append(slot)
            # Apply heating to bring temperature to comfort_max
            indoor_temp = calculate_next_temp(indoor_temp, outdoor, HEAT_PUMP_POWER)
            total_energy_kwh += HEAT_PUMP_POWER * 0.25  # 15 min = 0.25 hours

            # Cap at comfort_max
            if indoor_temp > comfort_max:
                indoor_temp = comfort_max
        else:
            # No heating, temperature evolves naturally
            indoor_temp = temp_without_heating

            # Cap at comfort_max (from solar gains, etc.)
            if indoor_temp > comfort_max:
                indoor_temp = comfort_max

    heating_hours = len(slots_requiring_heat) * 0.25

    print(f"  ✓ {len(slots_requiring_heat)} slots need heating ({heating_hours:.1f} hours)")
    print(f"  ✓ Estimated energy: {total_energy_kwh:.2f} kWh")

    return {
        "building_type": building_type,
        "comfort_range": [comfort_min, comfort_max],
        "outdoor_temp_range": [min(outdoor_temps), max(outdoor_temps)],
        "heating_slots_needed": len(slots_requiring_heat),
        "heating_hours_needed": heating_hours,
        "slots_requiring_heat": slots_requiring_heat,
        "estimated_total_power_kwh": total_energy_kwh,
        "heat_pump_power_kw": HEAT_PUMP_POWER,
        "building_params": {
            "type": building_type,
            "total_u_value_avg": round(
                (params["wall_area"] / params["wall_resistance"] +
                 params["window_area"] / params["window_resistance"] +
                 params["roof_area"] / params["roof_resistance"] +
                 params["floor_area"] / params["floor_resistance"]) /
                (params["wall_area"] + params["window_area"] +
                 params["roof_area"] + params["floor_area"]), 2
            ),
            "infiltration_rate": params["infiltration_rate"]
        }
    }


def schedule_appliance(
    appliance_id: str,
    start_slot: int,
    duration_slots: int,
    user_info: Optional[str] = None
) -> Dict[str, Any]:
    """
    Schedules an appliance to run starting at the specified timeslot.
    Optionally sends API commands to actual devices if configured.

    Args:
        appliance_id: Identifier for the appliance (e.g., "washing_machine")
        start_slot: Starting timeslot index (0-95, where 0 = 00:00)
        duration_slots: Number of 15-minute slots the appliance will run
        user_info: Optional context about why this schedule was chosen

    Returns:
        Dictionary containing:
        - success: Boolean indicating if scheduling succeeded
        - appliance_id: The appliance that was scheduled
        - start_slot: Starting timeslot index
        - start_time: Human-readable start time
        - end_slot: Ending timeslot index
        - end_time: Human-readable end time
        - duration_minutes: Total duration in minutes
        - schedule: 96-element binary array (0 = off, 1 = on for each 15-min slot)
        - message: Confirmation message
        - api_response: API response if device control was attempted
    """
    from config import AVAILABLE_APPLIANCES

    # Validate inputs
    if not 0 <= start_slot < 96:
        return {
            "success": False,
            "error": f"Invalid start_slot {start_slot}. Must be between 0 and 95."
        }

    end_slot = start_slot + duration_slots - 1

    if end_slot >= 96:
        return {
            "success": False,
            "error": f"Schedule exceeds 24-hour period. Start slot {start_slot} + duration {duration_slots} exceeds slot 95."
        }

    # Convert slot indices to human-readable times
    def slot_to_time(slot: int) -> str:
        hours = (slot * 15) // 60
        minutes = (slot * 15) % 60
        return f"{hours:02d}:{minutes:02d}"

    start_time = slot_to_time(start_slot)
    end_time = slot_to_time(end_slot + 1)  # +1 because end_slot is inclusive
    duration_minutes = duration_slots * 15

    # Build schedule record
    schedule_record = {
        "timestamp": datetime.now().isoformat(),
        "appliance_id": appliance_id,
        "start_slot": start_slot,
        "start_time": start_time,
        "end_slot": end_slot,
        "end_time": end_time,
        "duration_slots": duration_slots,
        "duration_minutes": duration_minutes,
        "user_info": user_info
    }

    # Save schedule to file for record keeping
    schedule_path = Path(__file__).parent / "data" / "schedules.json"
    schedules = []

    if schedule_path.exists():
        with open(schedule_path, 'r') as f:
            schedules = json.load(f)

    schedules.append(schedule_record)

    with open(schedule_path, 'w') as f:
        json.dump(schedules, f, indent=2)

    # Attempt device control via API if configured
    api_response = None
    if appliance_id in AVAILABLE_APPLIANCES:
        appliance_config = AVAILABLE_APPLIANCES[appliance_id]
        api_config = appliance_config.get("api_config", {})

        if api_config.get("enabled", False):
            try:
                # Prepare template variables for substitution
                template_vars = {
                    "start_time": start_time,
                    "end_time": end_time,
                    "start_slot": start_slot,
                    "end_slot": end_slot,
                    "duration_minutes": duration_minutes,
                    "power_rating_kw": appliance_config.get("power_rating_kw", 0),
                    "comfort_max": appliance_config.get("comfort_max", 22),
                    "comfort_min": appliance_config.get("comfort_min", 20)
                }

                # Build payload by substituting template variables
                payload = {}
                for key, value in api_config["payload_template"].items():
                    if isinstance(value, str) and "{" in value:
                        # Replace template variable
                        for var_name, var_value in template_vars.items():
                            value = value.replace(f"{{{var_name}}}", str(var_value))
                    payload[key] = value

                # Build headers by substituting variables
                headers = {}
                for key, value in api_config["headers"].items():
                    if isinstance(value, str) and "{" in value:
                        # Replace environment variables or tokens
                        # Note: Users should replace {HASS_TOKEN} with actual token in config
                        headers[key] = value
                    else:
                        headers[key] = value

                # Send API request
                print(f"     → Sending API command to {appliance_id}...")
                response = requests.request(
                    method=api_config["method"],
                    url=api_config["endpoint"],
                    headers=headers,
                    json=payload,
                    timeout=10
                )

                api_response = {
                    "status_code": response.status_code,
                    "success": response.status_code in [200, 201, 202],
                    "response": response.text[:200]  # Truncate for brevity
                }

                if api_response["success"]:
                    print(f"     ✓ API command successful (status {response.status_code})")
                else:
                    print(f"     ⚠ API command failed (status {response.status_code})")

            except requests.exceptions.RequestException as e:
                api_response = {
                    "success": False,
                    "error": str(e)
                }
                print(f"     ✗ API request failed: {e}")
            except Exception as e:
                api_response = {
                    "success": False,
                    "error": f"Unexpected error: {e}"
                }
                print(f"     ✗ Unexpected error during API call: {e}")

    # Generate 96-element binary schedule array (0 = off, 1 = on)
    schedule_array = [0] * 96
    for slot in range(start_slot, end_slot + 1):
        schedule_array[slot] = 1

    result = {
        "success": True,
        "appliance_id": appliance_id,
        "start_slot": start_slot,
        "start_time": start_time,
        "end_slot": end_slot,
        "end_time": end_time,
        "duration_minutes": duration_minutes,
        "schedule": schedule_array,  # The actual 96-element binary schedule
        "message": f"{appliance_id} scheduled to run from {start_time} to {end_time} ({duration_minutes} minutes)"
    }

    if api_response:
        result["api_response"] = api_response

    return result


def get_calendar_ev_constraint(hours_ahead: int = 24) -> Optional[Dict[str, Any]]:
    """
    Fetch calendar events and extract EV charging deadline using LLM reasoning.

    Args:
        hours_ahead: How many hours ahead to fetch events

    Returns:
        Dict with EV constraint if found, None otherwise:
        {
            "deadline_time": "07:30",
            "deadline_slot": 30,
            "event_title": "Work",
            "event_time": "07:30",
            "reasoning": "User has work at 7:30am, EV must be charged before departure"
        }
    """
    if not CALENDAR_AVAILABLE:
        print("   ℹ Google Calendar not configured - using default EV deadline")
        return None

    # Check if credentials exist
    credentials_path = Path(__file__).parent / 'credentials.json'
    token_path = Path(__file__).parent / 'token.json'

    if not credentials_path.exists():
        print("   ℹ Google Calendar credentials not found - using default EV deadline")
        return None

    try:
        # Authenticate
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path),
                ['https://www.googleapis.com/auth/calendar.readonly'])

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # Silent skip if OAuth not completed
                return None

            with open(token_path, 'w') as token:
                token.write(creds.to_json())

        # Build calendar service
        service = build('calendar', 'v3', credentials=creds)

        # Fetch events
        now = datetime.utcnow()
        time_min = now.isoformat() + 'Z'
        time_max = (now + timedelta(hours=hours_ahead)).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            maxResults=10,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        events = events_result.get('items', [])

        if not events:
            return None

        # Format events for LLM (title + time only)
        events_text = "Calendar events:\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'Untitled')
            events_text += f"- {summary} at {start}\n"

        # Use LLM to extract EV constraint
        import os
        from config import CEREBRAS_API_KEY, CEREBRAS_MODEL, TEMPERATURE, REASONING_EFFORT, REASONING_FORMAT

        # Use model override if set (from orchestrator)
        model = os.environ.get('CEREBRAS_MODEL_OVERRIDE', CEREBRAS_MODEL)

        system_prompt = """You are analyzing calendar events to determine when an electric vehicle (EV) must be fully charged.

Rules:
1. If there are morning events (before 10am), the EV should be charged before the earliest event
2. Use a 30-minute buffer before the event time for the charging deadline
3. Only respond if there's a clear need for EV charging
4. Respond ONLY with valid JSON, no additional text

Output format (JSON only):
{
  "needs_charging": true/false,
  "deadline_time": "HH:MM",
  "event_title": "Event name",
  "event_time": "HH:MM",
  "reasoning": "Brief explanation"
}"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": events_text}
        ]

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CEREBRAS_API_KEY}"
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": 200,
            "reasoning_effort": REASONING_EFFORT,
            "reasoning_format": REASONING_FORMAT,
        }

        response = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        response.raise_for_status()

        result = response.json()
        msg = result['choices'][0]['message']

        # Display reasoning if present
        reasoning = msg.get('reasoning', '')
        if reasoning:
            print(f"\n[Reasoning - Calendar Constraint]\n{reasoning}\n")

        llm_response = msg.get('content', '').strip()

        # Parse JSON response
        # Remove markdown code blocks if present
        if '```json' in llm_response:
            llm_response = llm_response.split('```json')[1].split('```')[0].strip()
        elif '```' in llm_response:
            llm_response = llm_response.split('```')[1].split('```')[0].strip()

        constraint_data = json.loads(llm_response)

        if not constraint_data.get('needs_charging', False):
            return None

        # Convert deadline time to slot index
        deadline_time = constraint_data['deadline_time']
        hours, minutes = map(int, deadline_time.split(':'))
        deadline_slot = (hours * 60 + minutes) // 15

        return {
            "deadline_time": deadline_time,
            "deadline_slot": deadline_slot,
            "event_title": constraint_data['event_title'],
            "event_time": constraint_data['event_time'],
            "reasoning": constraint_data['reasoning']
        }

    except Exception as e:
        print(f"   ⚠ Calendar constraint extraction failed: {e}")
        return None


def get_battery_state() -> Dict[str, Any]:
    """
    Reads the current battery state from data/battery_state.json.

    Returns:
        Dictionary containing:
        - battery_id: Battery identifier
        - capacity_kwh: Total battery capacity
        - current_soc_kwh: Current state of charge in kWh
        - current_soc_pct: Current state of charge in percent
        - max_charge_kw: Maximum charging power
        - max_discharge_kw: Maximum discharging power
        - min_soc_pct: Minimum allowed state of charge
        - round_trip_efficiency: Round-trip efficiency (0-1)
        - pv_forecast_kwh: Forecasted PV generation in kWh
        - available_energy_kwh: Energy available above min SoC
    """
    state_path = Path(__file__).parent / "data" / "battery_state.json"

    if not state_path.exists():
        return {
            "success": False,
            "error": "Battery state file not found at data/battery_state.json"
        }

    with open(state_path, 'r') as f:
        state = json.load(f)

    # Calculate available energy above minimum SoC
    min_soc_kwh = state["capacity_kwh"] * (state["min_soc_pct"] / 100)
    available_energy = state["current_soc_kwh"] - min_soc_kwh

    state["success"] = True
    state["available_energy_kwh"] = round(available_energy, 2)
    state["min_soc_kwh"] = round(min_soc_kwh, 2)

    return state


def calculate_window_sums(prices: List[float], window_size: int, start_slot: int = 0, end_slot: Optional[int] = None) -> Dict[str, Any]:
    """
    Calculates sums for all consecutive price windows of a given size.
    This enables agents to find optimal scheduling windows without arithmetic errors.

    Args:
        prices: List of 96 electricity prices (EUR/MWh)
        window_size: Number of consecutive slots per window
        start_slot: Starting slot index (default 0)
        end_slot: Ending slot index (default: len(prices) - window_size)

    Returns:
        Dictionary containing:
        - window_sums: List of sums for each window
        - window_count: Number of windows calculated
        - min_window_index: Index of window with minimum sum
        - min_window_sum: The minimum sum value
        - success: Boolean indicating if calculation succeeded
    """
    try:
        if not prices or len(prices) < window_size:
            return {
                "success": False,
                "error": f"Invalid input: need at least {window_size} prices, got {len(prices)}"
            }

        if window_size <= 0:
            return {
                "success": False,
                "error": f"Invalid window_size: {window_size}"
            }

        # Default end_slot to allow last valid window
        if end_slot is None:
            end_slot = len(prices) - window_size

        # Calculate sums for all consecutive windows
        window_sums = []
        for i in range(start_slot, end_slot + 1):
            window = prices[i:i+window_size]
            if len(window) == window_size:
                window_sums.append(round(sum(window), 4))

        if not window_sums:
            return {
                "success": False,
                "error": "No valid windows to calculate"
            }

        # Find minimum
        min_sum = min(window_sums)
        min_index = window_sums.index(min_sum) + start_slot

        return {
            "success": True,
            "window_sums": window_sums,
            "window_count": len(window_sums),
            "min_window_index": min_index,
            "min_window_sum": min_sum,
            "window_size": window_size,
            "start_slot": start_slot
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Calculation error: {str(e)}"
        }


def call_appliance_agent(
    agent_name: str,
    prices_data: Dict[str, Any] = None,
    user_request: str = ""
) -> Dict[str, Any]:
    """
    Delegates to a specialist appliance agent for scheduling optimization.

    Args:
        agent_name: Name of the appliance agent (e.g., "washing_machine_agent")
        prices_data: Electricity price data from get_electricity_prices() (optional for DR events)
        user_request: User's scheduling request with constraints

    Returns:
        Dictionary containing:
        - recommended_slot: Optimal start slot
        - duration_slots: Duration in 15-min slots
        - cost: Estimated cost in EUR
        - reasoning: Agent's explanation
        - agent_response: Full agent response text
    """
    import os
    from config import AVAILABLE_APPLIANCES, CEREBRAS_API_KEY, CEREBRAS_MODEL, TEMPERATURE, REASONING_EFFORT, REASONING_FORMAT

    # Use model override if set (from orchestrator)
    model = os.environ.get('CEREBRAS_MODEL_OVERRIDE', CEREBRAS_MODEL)

    # Validate agent exists
    appliance_id = agent_name.replace("_agent", "")
    if appliance_id not in AVAILABLE_APPLIANCES:
        return {
            "error": f"Agent '{agent_name}' not found. Available: {list(AVAILABLE_APPLIANCES.keys())}"
        }

    # Load agent's system prompt
    agent_file = AVAILABLE_APPLIANCES[appliance_id]["agent_file"]
    prompt_path = Path(__file__).parent.parent / "prompts" / agent_file

    if not prompt_path.exists():
        return {
            "error": f"Agent prompt file not found: {agent_file}"
        }

    with open(prompt_path, 'r') as f:
        system_prompt = f.read()

    # Inject battery specs from config if placeholder present
    if "{BATTERY_SPECS}" in system_prompt and appliance_id == "battery":
        spec = AVAILABLE_APPLIANCES["battery"]
        cap = spec.get("capacity_kwh", "?")
        pwr = spec.get("power_rating_kw", "?")
        min_soc = spec.get("min_soc_pct", 20)
        min_soc_kwh = round(cap * min_soc / 100, 1) if isinstance(cap, (int, float)) else "?"
        eff = int(spec.get("round_trip_efficiency", 0.9) * 100)
        battery_specs = (
            f"- **Capacity**: {cap} kWh\n"
            f"- **Max charge power**: {pwr} kW\n"
            f"- **Max discharge power**: {pwr} kW\n"
            f"- **Minimum SoC**: {min_soc}% ({min_soc_kwh} kWh) -- never discharge below this\n"
            f"- **Round-trip efficiency**: {eff}%"
        )
        system_prompt = system_prompt.replace("{BATTERY_SPECS}", battery_specs)

    # Format price data for agent (optional for DR events)
    if prices_data:
        prices_text = (
            f"Date: {prices_data['date']}\n"
            f"Unit: {prices_data['unit']}\n"
            f"Resolution: {prices_data['resolution_minutes']} minutes\n"
            f"Prices (96 slots, slot 0=00:00):\n{prices_data['prices']}\n"
            f"Min: {min(prices_data['prices']):.4f} at slot {prices_data['prices'].index(min(prices_data['prices']))}\n"
            f"Max: {max(prices_data['prices']):.4f} at slot {prices_data['prices'].index(max(prices_data['prices']))}"
        )
    else:
        prices_text = "No price data available (DR event -- compensation provided by aggregator)."

    # Special handling for heat pump agent - needs weather and thermal model
    additional_context = ""
    if appliance_id == "heat_pump" and prices_data:
        print(f"     → Heat pump agent requires weather and thermal analysis...")

        # Get weather forecast
        weather_data = get_weather_forecast(location="Vienna", date=prices_data['date'])

        # Get building parameters from config
        building_type = AVAILABLE_APPLIANCES[appliance_id].get("building_type", "old")
        comfort_min = AVAILABLE_APPLIANCES[appliance_id].get("comfort_min", 20.0)
        comfort_max = AVAILABLE_APPLIANCES[appliance_id].get("comfort_max", 22.0)

        # Calculate heating requirements using thermal model
        thermal_data = calculate_heating_requirement(
            outdoor_temps=weather_data['temps_15min'],
            comfort_min=comfort_min,
            comfort_max=comfort_max,
            building_type=building_type
        )

        # Format thermal and weather data for agent
        additional_context = (
            f"\n\n{'='*60}\n"
            f"WEATHER FORECAST:\n"
            f"{'='*60}\n"
            f"Location: {weather_data['location']}\n"
            f"Date: {weather_data['date']}\n"
            f"Temperature range: {weather_data['temps_min']:.1f}°C - {weather_data['temps_max']:.1f}°C\n"
            f"Outdoor temperatures (96 slots, 15-min resolution):\n{weather_data['temps_15min']}\n"
            f"\n{'='*60}\n"
            f"THERMAL MODEL ANALYSIS:\n"
            f"{'='*60}\n"
            f"Building type: {thermal_data['building_type']}\n"
            f"Comfort range: {thermal_data['comfort_range'][0]}°C - {thermal_data['comfort_range'][1]}°C\n"
            f"Heating slots needed: {thermal_data['heating_slots_needed']} slots ({thermal_data['heating_hours_needed']:.1f} hours)\n"
            f"Estimated energy requirement: {thermal_data['estimated_total_power_kwh']:.2f} kWh\n"
            f"Heat pump power: {thermal_data['heat_pump_power_kw']} kW\n"
            f"\nSlots requiring heating (slot indices):\n{thermal_data['slots_requiring_heat']}\n"
            f"\nBuilding parameters:\n"
            f"  - Average U-value: {thermal_data['building_params']['total_u_value_avg']} W/m²K\n"
            f"  - Infiltration rate: {thermal_data['building_params']['infiltration_rate']} ACH\n"
        )

    # Call LLM with agent's system prompt
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CEREBRAS_API_KEY}"
    }

    if prices_data:
        user_content = f"{user_request}\n\nElectricity Price Data:\n{prices_text}{additional_context}"
    else:
        user_content = user_request

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]

    # All appliances need 15k tokens to show all window calculations
    # This ensures agents don't get truncated mid-calculation
    max_tokens = 15000

    # Define tools for agent use
    tools = [
        {
            "type": "function",
            "function": {
                "name": "calculate_window_sums",
                "description": "Calculates sums for all consecutive price windows. Returns array of sums and identifies minimum. Use this once at the start to get all window sums.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prices": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "List of 96 electricity prices in EUR/MWh"
                        },
                        "window_size": {
                            "type": "integer",
                            "description": "Number of consecutive slots per window (e.g., 8 for 2-hour cycle)"
                        },
                        "start_slot": {
                            "type": "integer",
                            "description": "Starting slot index (default 0)"
                        },
                        "end_slot": {
                            "type": "integer",
                            "description": "Ending slot index (optional, defaults to last valid window)"
                        }
                    },
                    "required": ["prices", "window_size"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "assess_dr_feasibility",
                "description": "Solves a MILP optimization to determine if the battery can provide DR flexibility while meeting household energy needs. Compares baseline (no DR) vs DR-committed schedules. Returns feasibility (full/partial/infeasible), max deliverable kW, opportunity cost, compensation, net benefit, and SoC trajectory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dr_target_kw": {
                            "type": "number",
                            "description": "Requested discharge power during DR window (kW)"
                        },
                        "dr_start_slot": {
                            "type": "integer",
                            "description": "First slot of DR window (inclusive), e.g. 68 for 17:00"
                        },
                        "dr_end_slot": {
                            "type": "integer",
                            "description": "Last slot of DR window (exclusive), e.g. 76 for 19:00"
                        },
                        "compensation_eur_kwh": {
                            "type": "number",
                            "description": "Aggregator compensation rate in EUR/kWh"
                        }
                    },
                    "required": ["dr_target_kw", "dr_start_slot", "dr_end_slot"]
                }
            }
        }
    ]

    payload = {
        "model": model,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "tools": tools,
        "stream": False,
        "reasoning_effort": REASONING_EFFORT,
        "reasoning_format": REASONING_FORMAT,
    }

    # Make API call with tool support
    response = requests.post(
        "https://api.cerebras.ai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=60
    )
    response.raise_for_status()

    result = response.json()

    # Handle tool calls if present (with loop limit to prevent infinite calls)
    message = result['choices'][0]['message']

    # Display reasoning from initial call if present
    reasoning = message.get('reasoning', '')
    if reasoning:
        print(f"\n[Reasoning - Sub-agent]\n{reasoning}\n")
    tool_call_rounds = 0
    max_tool_rounds = 2

    # If agent requested tool calls, execute them and continue
    while message.get('tool_calls') and tool_call_rounds < max_tool_rounds:
        tool_call_rounds += 1
        print(f"     → Agent requested {len(message['tool_calls'])} tool call(s) (round {tool_call_rounds})")

        # Add assistant message with tool calls to conversation
        messages.append(message)

        # Execute each tool call
        for tool_call in message['tool_calls']:
            function_name = tool_call['function']['name']
            function_args = json.loads(tool_call['function']['arguments'])

            if function_name == "calculate_window_sums":
                tool_result = calculate_window_sums(**function_args)
                result_content = json.dumps(tool_result)
                if tool_result.get('success'):
                    print(f"       calculate_window_sums(window_size={function_args['window_size']}) -> {tool_result['window_count']} windows, min at slot {tool_result['min_window_index']} = {tool_result['min_window_sum']}")
                else:
                    print(f"       calculate_window_sums() ERROR: {tool_result.get('error')}")
            elif function_name == "assess_dr_feasibility":
                from battery_optimizer import assess_dr_feasibility
                tool_result = assess_dr_feasibility(**function_args)
                result_content = json.dumps(tool_result)
                if tool_result.get('success'):
                    print(f"       assess_dr_feasibility({function_args['dr_target_kw']}kW, slots {function_args['dr_start_slot']}-{function_args['dr_end_slot']}) -> {tool_result['feasibility']}, max={tool_result['max_deliverable_kw']}kW")
                else:
                    print(f"       assess_dr_feasibility() ERROR: {tool_result.get('reason', 'unknown')}")
            else:
                result_content = json.dumps({"error": f"Unknown tool: {function_name}"})

            # Add tool result to messages
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call['id'],
                "content": result_content
            })

        # Re-call LLM with tool results (reasoning params already in payload)
        payload['messages'] = messages
        response = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        result = response.json()
        message = result['choices'][0]['message']
        print(f"       Agent response after tool call received")

        # Display reasoning from follow-up call if present
        reasoning = message.get('reasoning', '')
        if reasoning:
            print(f"\n[Reasoning - Sub-agent (after tool call)]\n{reasoning}\n")

    agent_response = message.get('content', '')
    if not agent_response:
        # If agent didn't provide text response but we have tool results, construct response
        if tool_call_rounds > 0:
            # Agent called the tool but didn't formulate final text response
            # Extract the tool result from conversation history and use it
            print(f"       ⚠ Agent didn't provide final text after {tool_call_rounds} tool calls")
            print(f"       → Constructing response from tool results")

            # Find the last tool result in messages
            for msg in reversed(messages):
                if msg.get('role') == 'tool':
                    tool_data = json.loads(msg['content'])
                    # Handle assess_dr_feasibility results
                    if 'feasibility' in tool_data:
                        feas = tool_data['feasibility']
                        max_kw = tool_data.get('max_deliverable_kw', '?')
                        net = tool_data.get('net_benefit_eur', '?')
                        opp = tool_data.get('opportunity_cost_eur', '?')
                        comp = tool_data.get('compensation_eur', '?')
                        agent_response = (
                            f"**Feasibility**: {feas}\n\n"
                            f"**Solver Results**:\n"
                            f"- Max deliverable: {max_kw} kW\n"
                            f"- Opportunity cost: {opp} EUR\n"
                            f"- Compensation: {comp} EUR\n"
                            f"- Net benefit: {net} EUR\n\n"
                            f"**Reasoning**: Result from MILP optimizer (assess_dr_feasibility)."
                        )
                        print(f"       ✓ Synthetic response created from DR feasibility result")
                        break
                    # Handle calculate_window_sums results
                    if tool_data.get('success') and 'min_window_index' in tool_data:
                        agent_response = f"""Based on calculate_window_sums analysis:

**Recommended Timeslot**: **Slot {tool_data['min_window_index']}**
**Duration**: {tool_data.get('window_size', 'N/A')} slots
**Sum of Prices**: {tool_data['min_window_sum']} EUR/MWh

This window has the minimum sum across all {tool_data['window_count']} evaluated windows.

**Reasoning**: Optimal schedule found via exhaustive search of all valid windows."""
                        print(f"       ✓ Synthetic response created from tool result")
                        break

        if not agent_response:
            return {
                "error": "Agent did not return text response after tool call",
                "message": message
            }

    # Battery/variable-control assets: return raw feasibility assessment (no scheduling parse)
    appliance_config = AVAILABLE_APPLIANCES.get(appliance_id, {})
    if appliance_config.get("control_type") == "variable":
        reasoning_match = re.search(r"\*{0,2}[Rr]easoning\*{0,2}[:\s]+(.+?)(?:\n\n|$)", agent_response, re.DOTALL)
        reasoning = reasoning_match.group(1).strip() if reasoning_match else agent_response[:300]
        return {
            "recommended_slot": None,
            "duration_slots": None,
            "cost": None,
            "reasoning": reasoning,
            "agent_response": agent_response,
            "tool_call_rounds": tool_call_rounds,
            "usage": result.get('usage', {})
        }

    # Parse agent response for recommended slot
    patterns = [
        # Pattern for structured JSON output (most reliable)
        r'"start_slot"\s*:\s*(\d+)',
        # Pattern for bold markdown recommendations (qwen-32b style)
        r'\*\*[Ss]lot\s+(\d+)\s*\(',
        # Pattern for "Recommended Timeslot: Slot X"
        r'[Rr]ecommended\s+[Tt]imeslot[:\s]+.*?[Ss]lot\s+(\d+)',
        # Original patterns
        r"recommended start time.*?[Ss]lot\s+(\d+)",
        r"start.*?[Ss]lot\s+(\d+)\s*\(",
        r"[Oo]ptimal.*?[Ss]lot\s+(\d+)",
        r"[Ss]chedule.*?[Ss]lot\s+(\d+)",
        r"[Mm]inimum cost.*?[Ww]indow\s+\[(\d+)-",
        r"[Ww]indow\s+\[(\d+)-\d+\]\s*\([^)]*MINIMUM[^)]*\)",  # Window [X-Y] (MINIMUM)
    ]

    slot_match = None
    for pattern in patterns:
        match = re.search(pattern, agent_response, re.IGNORECASE | re.DOTALL)
        if match:
            slot_match = match
            break

    # Fallback 1: Parse all windows and find minimum cost
    if not slot_match:
        window_pattern = r"[Ww]indow\s+\[(\d+)-\d+\]:\s+[\d\.\s\+]+\s*=\s*([\d\.]+)"
        windows = re.findall(window_pattern, agent_response)

        if windows:
            # Find window with minimum cost
            min_window = min(windows, key=lambda x: float(x[1]))
            recommended_slot = int(min_window[0])
            print(f"     ℹ Extracted slot {recommended_slot} from minimum window calculation")
        else:
            # Fallback 2: Look for any "Slot X" mention near end of response (last 500 chars)
            tail_match = re.search(r'[Ss]lot\s+(\d+)', agent_response[-500:])
            if tail_match:
                recommended_slot = int(tail_match.group(1))
                print(f"     ⚠ Extracted slot {recommended_slot} from response tail (fallback)")
            else:
                return {
                    "error": "Could not parse recommended slot from agent response",
                    "agent_response": agent_response
                }
    else:
        recommended_slot = int(slot_match.group(1))

    # Determine duration from config (PRIORITY 2 FIX: Always use config defaults, don't extract from user_request)
    # For heat pump, duration comes from thermal model
    if appliance_id == "heat_pump":
        # Heat pump duration is variable based on thermal model
        duration_slots = thermal_data.get("heating_slots_needed", 24)  # Default 6 hours
    else:
        # For all other appliances, ALWAYS use config defaults
        # This ensures consistent durations across runs (e.g., dishwasher always 90 min, washing_machine always 120 min)
        duration_slots = AVAILABLE_APPLIANCES[appliance_id]["default_duration_minutes"] // 15

    # Validate and correct 24-hour boundary constraint
    if recommended_slot + duration_slots > 96:
        original_slot = recommended_slot
        recommended_slot = 96 - duration_slots  # Shift back to fit within day
        print(f"     ⚠ Adjusted start slot from {original_slot} to {recommended_slot} to fit within 24h boundary")

    # Extract cost - look for "total cost for this schedule" specifically to avoid window calculations
    cost_match = re.search(r"total cost for this schedule is\s*€?\s*(\d+\.\d+)", agent_response.lower())
    if not cost_match:
        # Fallback: look for any "total cost is X" pattern
        cost_match = re.search(r"total cost[^.]*?(?:is|:|=)\s*€?\s*(\d+\.\d+)", agent_response.lower())
    if not cost_match:
        # Fallback: look for LaTeX boxed format (e.g., $\boxed{0.8076}$)
        cost_match = re.search(r"\$\\boxed\{(\d+\.\d+)\}\$", agent_response)
    if not cost_match:
        # Final fallback: calculate cost from prices directly
        if appliance_id != "heat_pump":
            try:
                window_prices = prices_data['prices'][recommended_slot:recommended_slot + duration_slots]
                power_kw = AVAILABLE_APPLIANCES[appliance_id]["power_rating_kw"]
                # Cost = sum of (price * power * 0.25 hours per slot) / 1000
                # Divide by 1000 to convert EUR/MWh to EUR
                cost = sum(price * power_kw * 0.25 / 1000 for price in window_prices)
                print(f"     ℹ Calculated cost from prices: €{cost:.4f}")
            except Exception as e:
                print(f"     ⚠ Could not calculate cost: {e}")
                cost = None
        else:
            cost = None
    else:
        cost = float(cost_match.group(1))

    # Extract reasoning (look for "Reasoning:" section)
    reasoning_match = re.search(r"\*{0,2}[Rr]easoning\*{0,2}[:\s]+(.+?)(?:\n\n|$)", agent_response, re.DOTALL)
    reasoning = reasoning_match.group(1).strip() if reasoning_match else "Cost-optimized schedule"

    return {
        "recommended_slot": recommended_slot,
        "duration_slots": duration_slots,
        "cost": cost,
        "reasoning": reasoning,
        "agent_response": agent_response,
        "tool_call_rounds": tool_call_rounds,
        "usage": result.get('usage', {})
    }


# Tool metadata for Claude SDK registration
TOOL_DEFINITIONS = [
    {
        "name": "calculate_window_sums",
        "description": "Calculates sums for all consecutive price windows of a given size. Returns array of window sums and identifies the minimum. Use this to get precise window calculations for scheduling optimization.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prices": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "List of 96 electricity prices in EUR/MWh"
                },
                "window_size": {
                    "type": "integer",
                    "description": "Number of consecutive slots per window (e.g., 8 for 2-hour cycle)"
                },
                "start_slot": {
                    "type": "integer",
                    "description": "Starting slot index (default 0)"
                },
                "end_slot": {
                    "type": "integer",
                    "description": "Ending slot index (optional)"
                }
            },
            "required": ["prices", "window_size"]
        }
    },
    {
        "name": "get_battery_state",
        "description": "Reads the current battery state including SoC, capacity, power limits, and available energy above minimum SoC.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_electricity_prices",
        "description": "Retrieves day-ahead electricity prices with 15-minute resolution (96 timeslots per day). Returns price data that can be analyzed to find the most cost-effective time window for running appliances.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format. Optional, defaults to today."
                }
            }
        }
    },
    {
        "name": "schedule_appliance",
        "description": "Schedules an appliance to start at a specific timeslot. The appliance will run continuously for the specified duration. Use this after analyzing prices to execute the optimal schedule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "appliance_id": {
                    "type": "string",
                    "description": "Identifier for the appliance (e.g., 'washing_machine', 'dishwasher')"
                },
                "start_slot": {
                    "type": "integer",
                    "description": "Starting timeslot index (0-95, where 0 = 00:00, 1 = 00:15, etc.)"
                },
                "duration_slots": {
                    "type": "integer",
                    "description": "Number of consecutive 15-minute slots the appliance will run"
                },
                "user_info": {
                    "type": "string",
                    "description": "Optional explanation of the scheduling decision for user transparency"
                }
            },
            "required": ["appliance_id", "start_slot", "duration_slots"]
        }
    }
]
