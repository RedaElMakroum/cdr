"""
ENTSO-E Transparency Platform API Client
Fetches day-ahead electricity prices for European bidding zones.
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import pytz


class ENTSOEClient:
    """Client for ENTSO-E Transparency Platform API."""

    BASE_URL = "https://web-api.tp.entsoe.eu/api"

    # Common bidding zone codes
    BIDDING_ZONES = {
        "AT": "10YAT-APG------L",  # Austria
        "DE": "10Y1001A1001A83F",  # Germany
        "NL": "10YNL----------L",  # Netherlands
        "BE": "10YBE----------2",  # Belgium
    }

    def __init__(self, api_key: str, bidding_zone: str = "AT"):
        """
        Initialize ENTSO-E client.

        Args:
            api_key: ENTSO-E API key
            bidding_zone: Two-letter country code (default: AT for Austria)
        """
        self.api_key = api_key
        self.zone_code = self.BIDDING_ZONES.get(bidding_zone, bidding_zone)
        ZONE_TIMEZONES = {
            "AT": "Europe/Vienna", "DE": "Europe/Berlin",
            "NL": "Europe/Amsterdam", "BE": "Europe/Brussels",
        }
        self.timezone = pytz.timezone(ZONE_TIMEZONES.get(bidding_zone, "Europe/Vienna"))

    def get_day_ahead_prices(
        self,
        date: Optional[str] = None,
        target_resolution_minutes: int = 15
    ) -> Dict[str, Any]:
        """
        Fetch day-ahead prices for specified date.

        Args:
            date: Target date in YYYY-MM-DD format (default: today)
            target_resolution_minutes: Desired time resolution (default: 15)

        Returns:
            Dictionary with price data in standardized format
        """
        # Parse target date
        if date is None:
            # Default to today (prices usually available after 13:00 CET)
            target_date = datetime.now()
        else:
            target_date = datetime.strptime(date, "%Y-%m-%d")

        # ENTSO-E day runs from 22:00 UTC (D-1) to 22:00 UTC (D)
        period_start = (target_date - timedelta(days=1)).replace(
            hour=22, minute=0, second=0, microsecond=0
        )
        period_end = target_date.replace(
            hour=22, minute=0, second=0, microsecond=0
        )

        # Format dates for API (UTC)
        start_str = period_start.strftime("%Y%m%d%H%M")
        end_str = period_end.strftime("%Y%m%d%H%M")

        # Build request parameters
        params = {
            "securityToken": self.api_key,
            "documentType": "A44",  # Price document
            "contract_MarketAgreement.type": "A01",  # Day-ahead
            "periodStart": start_str,
            "periodEnd": end_str,
            "out_Domain": self.zone_code,
            "in_Domain": self.zone_code,
        }

        # Make API request (increased timeout to handle slow API responses)
        response = requests.get(self.BASE_URL, params=params, timeout=60)
        response.raise_for_status()

        # Check if response is an error acknowledgement
        if "Acknowledgement_MarketDocument" in response.text:
            # Extract error message
            root = ET.fromstring(response.text)
            reason = root.find(".//{*}Reason/{*}text")
            error_msg = reason.text if reason is not None else "Unknown error"

            # If no data found and we requested default date, try yesterday
            if "No matching data" in error_msg and date is None:
                print(f"⚠ Today's prices not yet available. Trying yesterday...")
                yesterday = target_date - timedelta(days=1)
                return self.get_day_ahead_prices(yesterday.strftime("%Y-%m-%d"), target_resolution_minutes)
            else:
                raise ValueError(f"ENTSO-E API error: {error_msg}")

        # Parse XML response
        prices_raw = self._parse_xml_response(response.text)

        # Process to target resolution
        prices_processed = self._process_prices(
            prices_raw,
            target_resolution_minutes,
            target_date
        )

        return prices_processed

    def _parse_xml_response(self, xml_content: str) -> List[Dict[str, Any]]:
        """
        Parse XML response from ENTSO-E API.

        Args:
            xml_content: Raw XML response

        Returns:
            List of price points with timestamps
        """
        root = ET.fromstring(xml_content)

        # Define XML namespaces (handle both versions)
        # Extract namespace from root element
        if root.tag.startswith("{"):
            namespace = root.tag.split("}")[0].strip("{")
            ns = {"ns": namespace}
        else:
            # Fallback to common namespace
            ns = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}

        prices = []

        # Find TimeSeries elements
        for timeseries in root.findall(".//ns:TimeSeries", ns):
            period = timeseries.find(".//ns:Period", ns)

            if period is None:
                continue

            # Get time interval
            time_interval = period.find("ns:timeInterval", ns)
            start_time = time_interval.find("ns:start", ns).text
            resolution = period.find("ns:resolution", ns).text

            # Parse start time (ISO 8601 format)
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))

            # Determine interval duration
            if resolution == "PT60M":
                interval_minutes = 60
            elif resolution == "PT15M":
                interval_minutes = 15
            else:
                # Parse PTxxM format
                import re
                match = re.search(r"PT(\d+)M", resolution)
                interval_minutes = int(match.group(1)) if match else 60

            # Extract price points
            for point in period.findall("ns:Point", ns):
                position = int(point.find("ns:position", ns).text)
                price_mwh = float(point.find("ns:price.amount", ns).text)

                # Calculate timestamp for this point
                point_time = start_dt + timedelta(minutes=(position - 1) * interval_minutes)

                prices.append({
                    "timestamp": point_time,
                    "price_mwh": price_mwh,
                    "position": position
                })

        return sorted(prices, key=lambda x: x["timestamp"])

    def _process_prices(
        self,
        prices_raw: List[Dict[str, Any]],
        target_resolution_minutes: int,
        target_date: datetime
    ) -> Dict[str, Any]:
        """
        Process raw prices to target resolution and format.

        Args:
            prices_raw: Raw price data from API
            target_resolution_minutes: Desired resolution (e.g., 15 minutes)
            target_date: Target date for output

        Returns:
            Standardized price data dictionary
        """
        # Convert to local timezone
        prices_local = []
        for p in prices_raw:
            local_time = p["timestamp"].astimezone(self.timezone)
            # Keep native EUR/MWh format for better LLM arithmetic
            prices_local.append({
                "timestamp": local_time,
                "price_mwh": p["price_mwh"]
            })

        # Interpolate to target resolution if needed
        if target_resolution_minutes == 15:
            num_slots = 96  # 24 hours * 4 slots per hour
        elif target_resolution_minutes == 60:
            num_slots = 24
        else:
            num_slots = int(24 * 60 / target_resolution_minutes)

        # Generate target timeslots
        start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        start_of_day_local = self.timezone.localize(start_of_day)

        timeslots = []
        prices = []

        for i in range(num_slots):
            slot_time = start_of_day_local + timedelta(minutes=i * target_resolution_minutes)
            timeslots.append(slot_time.strftime("%H:%M"))

            # Find corresponding price (interpolate if necessary)
            price = self._interpolate_price(prices_local, slot_time)
            prices.append(round(price, 4))

        return {
            "date": target_date.strftime("%Y-%m-%d"),
            "unit": "EUR/MWh",
            "resolution_minutes": target_resolution_minutes,
            "timeslots": timeslots,
            "prices": prices
        }

    def _interpolate_price(
        self,
        prices_local: List[Dict[str, Any]],
        target_time: datetime
    ) -> float:
        """
        Interpolate price for target time from available data.

        Args:
            prices_local: List of price points with timestamps
            target_time: Target timestamp

        Returns:
            Interpolated price in EUR/MWh
        """
        # Find nearest price points
        before = None
        after = None

        for p in prices_local:
            if p["timestamp"] <= target_time:
                before = p
            elif p["timestamp"] > target_time and after is None:
                after = p
                break

        # If exact match
        if before and before["timestamp"] == target_time:
            return before["price_mwh"]

        # If only one side available, use it
        if before and not after:
            return before["price_mwh"]
        if after and not before:
            return after["price_mwh"]

        # Linear interpolation
        if before and after:
            time_diff = (after["timestamp"] - before["timestamp"]).total_seconds()
            target_diff = (target_time - before["timestamp"]).total_seconds()
            ratio = target_diff / time_diff

            price = before["price_mwh"] + ratio * (after["price_mwh"] - before["price_mwh"])
            return price

        # Fallback (should not reach here)
        return 100.0  # Default fallback price


def fetch_entsoe_prices(
    api_key: str,
    date: Optional[str] = None,
    bidding_zone: str = "AT"
) -> Dict[str, Any]:
    """
    Convenience function to fetch ENTSO-E day-ahead prices.

    Args:
        api_key: ENTSO-E API key
        date: Target date in YYYY-MM-DD format (default: tomorrow)
        bidding_zone: Bidding zone code (default: AT for Austria)

    Returns:
        Price data dictionary
    """
    client = ENTSOEClient(api_key, bidding_zone)
    return client.get_day_ahead_prices(date)


if __name__ == "__main__":
    # Example usage
    from config import ENTSOE_API_KEY, BIDDING_ZONE

    if not ENTSOE_API_KEY:
        print("Error: ENTSOE_API_KEY not set in config.py")
        print("Register at: https://transparency.entsoe.eu/")
        exit(1)

    # Fetch prices (auto-fallback to yesterday if today not available)
    try:
        client = ENTSOEClient(ENTSOE_API_KEY, BIDDING_ZONE)

        # Use default date (with auto-fallback)
        prices = client.get_day_ahead_prices()

        print(f"Date: {prices['date']}")
        print(f"Resolution: {prices['resolution_minutes']} minutes")
        print(f"Number of slots: {len(prices['prices'])}")
        print(f"Min price: {min(prices['prices']):.2f} EUR/MWh at slot {prices['prices'].index(min(prices['prices']))}")
        print(f"Max price: {max(prices['prices']):.2f} EUR/MWh at slot {prices['prices'].index(max(prices['prices']))}")
        print(f"\nFirst 5 slots:")
        for i in range(5):
            print(f"  {prices['timeslots'][i]}: {prices['prices'][i]:.2f} EUR/MWh")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
