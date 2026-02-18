"""
Execution Log
=============

Tracks schedule execution history with JSON file persistence.
Follows the same pattern as state_manager.py.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class ExecutionLog:
    """Track schedule execution history in a JSON file."""

    def __init__(self, history_file: str, max_entries: int = 1000):
        self.history_file = Path(history_file)
        self.max_entries = max_entries
        self._records: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if self.history_file.exists():
            try:
                with open(self.history_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return []
        return []

    def _save(self):
        # Truncate to max_entries
        if len(self._records) > self.max_entries:
            self._records = self._records[-self.max_entries :]

        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "w") as f:
            json.dump(self._records, f, indent=2, default=str)

    def record_start(self, schedule_name: str, trigger_time: datetime) -> int:
        """Record a task execution start. Returns the record index."""
        record = {
            "schedule_name": schedule_name,
            "trigger_time": trigger_time.isoformat(),
            "start_time": datetime.now().isoformat(),
            "end_time": None,
            "success": None,
            "iterations": 0,
            "error": None,
        }
        self._records.append(record)
        self._save()
        return len(self._records) - 1

    def record_end(
        self,
        index: int,
        success: bool,
        iterations: int = 0,
        error: Optional[str] = None,
    ):
        """Record a task execution end."""
        if 0 <= index < len(self._records):
            self._records[index]["end_time"] = datetime.now().isoformat()
            self._records[index]["success"] = success
            self._records[index]["iterations"] = iterations
            self._records[index]["error"] = error
            self._save()

    def is_running(self, schedule_name: str) -> bool:
        """Check if a schedule has an active (unfinished) execution."""
        for record in reversed(self._records):
            if record["schedule_name"] == schedule_name:
                if record["end_time"] is None:
                    return True
                return False
        return False

    def get_last_run(self, schedule_name: str) -> Optional[Dict[str, Any]]:
        """Get the most recent completed execution for a schedule."""
        for record in reversed(self._records):
            if (
                record["schedule_name"] == schedule_name
                and record["end_time"] is not None
            ):
                return record
        return None

    def get_all_records(self) -> List[Dict[str, Any]]:
        """Return all execution records."""
        return list(self._records)
