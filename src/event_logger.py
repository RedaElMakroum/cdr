"""
Per-event lifecycle logging for DR events.
Appends timestamped JSONL entries to data/event_logs/{event_id}.jsonl.
"""

import json
import os
from datetime import datetime
from pathlib import Path


LOG_DIR = Path(__file__).parent / "data" / "event_logs"


def log_event(event_id: str, source: str, action: str, model: str = None, details: dict = None):
    """
    Append a log entry to data/event_logs/{event_id}.jsonl.

    Args:
        event_id: DR event identifier (e.g. "DR-2026-02-16-A1B2C3")
        source: Origin of the action ("aggregator", "hems", "prosumer")
        action: What happened ("dispatched", "evaluation_started", "evaluation_paused",
                "evaluation_finished", "followup", "approved", "rejected")
        model: LLM model used, if applicable
        details: Arbitrary dict with action-specific data
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source,
        "action": action,
    }
    if model:
        entry["model"] = model
    if details:
        entry["details"] = details

    log_path = LOG_DIR / f"{event_id}.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"[EventLog] {event_id} | {source}/{action}")
