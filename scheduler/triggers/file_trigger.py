"""
File change trigger.

Monitors files for modification time changes using os.stat polling.
Supports debounce to avoid firing on rapid successive changes.
"""

import os
from datetime import datetime
from typing import Dict, Optional

from .base import BaseTrigger, TriggerResult


class FileChangeTrigger(BaseTrigger):
    """
    Fires when monitored files are modified.

    Uses os.stat to check mtime - simple polling approach
    that works on all platforms without extra dependencies.
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.paths = config.get("paths", [])
        self.debounce_seconds = config.get("debounce_seconds", 0)
        self._mtimes: Dict[str, float] = {}
        self._last_change_time: Optional[datetime] = None
        self._changed_file: Optional[str] = None
        self._pending_fire = False

        # Initialize mtimes for existing files
        for path in self.paths:
            if os.path.exists(path):
                self._mtimes[path] = os.stat(path).st_mtime

    def evaluate(self) -> TriggerResult:
        # Check each monitored path for changes
        for path in self.paths:
            if not os.path.exists(path):
                continue
            current_mtime = os.stat(path).st_mtime
            prev_mtime = self._mtimes.get(path, 0)
            if current_mtime > prev_mtime:
                self._mtimes[path] = current_mtime
                self._changed_file = path
                self._last_change_time = datetime.now()
                self._pending_fire = True

        # Handle debounce: wait until debounce period has elapsed since last change
        if self._pending_fire and self._last_change_time:
            elapsed = (datetime.now() - self._last_change_time).total_seconds()
            if elapsed >= self.debounce_seconds:
                self._pending_fire = False
                changed = self._changed_file or "unknown"
                self._changed_file = None
                return TriggerResult(
                    fired=True,
                    trigger_data={
                        "changed_file": changed,
                        "trigger_type": "file_changed",
                    },
                )

        return TriggerResult(fired=False)
