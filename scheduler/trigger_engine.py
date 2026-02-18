"""
Trigger Engine
==============

Factory for creating trigger instances and engine for evaluating them.
"""

from typing import Dict, List

from .models import ScheduleDefinition, TriggerConfig, TriggerType
from .triggers.base import BaseTrigger, TriggerResult
from .triggers.composite_trigger import CompositeTrigger
from .triggers.cron_trigger import CronTrigger
from .triggers.file_trigger import FileChangeTrigger
from .triggers.http_trigger import HttpConditionTrigger


def create_trigger(config: TriggerConfig) -> BaseTrigger:
    """Factory function to create a trigger instance from config."""
    config_dict = {
        "cron": config.cron,
        "timezone": config.timezone,
        "paths": config.paths,
        "debounce_seconds": config.debounce_seconds,
        "url": config.url,
        "headers": config.headers,
        "condition": config.condition,
        "operator": config.operator,
    }

    if config.type == TriggerType.CRON:
        return CronTrigger(config_dict)
    elif config.type == TriggerType.FILE_CHANGED:
        return FileChangeTrigger(config_dict)
    elif config.type == TriggerType.HTTP_CONDITION:
        return HttpConditionTrigger(config_dict)
    elif config.type == TriggerType.COMPOSITE:
        sub_triggers = [create_trigger(tc) for tc in config.triggers]
        return CompositeTrigger(config_dict, sub_triggers)
    else:
        raise ValueError(f"Unknown trigger type: {config.type}")


class TriggerEngine:
    """
    Manages triggers for all loaded schedules.
    Provides evaluate() method called by the daemon's poll loop.
    """

    def __init__(self):
        self._triggers: Dict[str, BaseTrigger] = {}

    def register(self, schedule: ScheduleDefinition):
        """Register a schedule's trigger for evaluation."""
        trigger = create_trigger(schedule.trigger)
        self._triggers[schedule.name] = trigger

    def evaluate(self, schedule_name: str) -> TriggerResult:
        """Evaluate a single schedule's trigger."""
        trigger = self._triggers.get(schedule_name)
        if not trigger:
            return TriggerResult(fired=False)

        result = trigger.evaluate()
        if result.fired:
            trigger.mark_fired()
        return result

    def registered_names(self) -> List[str]:
        """Return all registered schedule names."""
        return list(self._triggers.keys())
