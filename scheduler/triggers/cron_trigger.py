"""
Cron-based time trigger using croniter.

Fires when the current time matches a cron slot that hasn't been handled yet.
"""

from datetime import datetime, timedelta
from typing import Optional

from croniter import croniter

from .base import BaseTrigger, TriggerResult


class CronTrigger(BaseTrigger):
    """
    Evaluates a standard 5-field cron expression against the current time.

    Example cron expressions:
        "0 8 * * *"       - Every day at 8:00 AM
        "30 7 * * 1-5"    - Weekdays at 7:30 AM
        "*/30 9-18 * * *" - Every 30 min from 9AM to 6PM
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.cron_expr = config["cron"]
        self.timezone_str = config.get("timezone")
        self._last_scheduled_time: Optional[datetime] = None

    def evaluate(self) -> TriggerResult:
        now = datetime.now()

        # Get the most recent scheduled time that is <= now
        cron = croniter(self.cron_expr, now - timedelta(seconds=1))
        prev_scheduled = cron.get_prev(datetime)

        # Fire if we haven't fired for this scheduled slot yet
        if self._last_scheduled_time is None or prev_scheduled > self._last_scheduled_time:
            # Only fire if within 2 minutes of the scheduled time
            # (avoids firing for old slots on daemon startup)
            age = (now - prev_scheduled).total_seconds()
            if age < 120:
                self._last_scheduled_time = prev_scheduled
                return TriggerResult(
                    fired=True,
                    trigger_data={
                        "scheduled_time": prev_scheduled.isoformat(),
                        "trigger_type": "cron",
                        "cron_expression": self.cron_expr,
                    },
                )

        return TriggerResult(fired=False)
