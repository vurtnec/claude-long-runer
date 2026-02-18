"""
Composite trigger: combines multiple sub-triggers with AND/OR logic.
"""

from typing import List

from .base import BaseTrigger, TriggerResult


class CompositeTrigger(BaseTrigger):
    """
    Combines multiple sub-triggers with AND or OR logic.

    For AND: all sub-triggers must fire.
    For OR: at least one sub-trigger must fire.
    """

    def __init__(self, config: dict, sub_triggers: List[BaseTrigger]):
        super().__init__(config)
        self.operator = config.get("operator", "and")
        self.sub_triggers = sub_triggers

    def evaluate(self) -> TriggerResult:
        results = [t.evaluate() for t in self.sub_triggers]
        merged_data = {}

        if self.operator == "and":
            fired = all(r.fired for r in results)
        else:  # "or"
            fired = any(r.fired for r in results)

        if fired:
            for r in results:
                if r.fired:
                    merged_data.update(r.trigger_data)

        return TriggerResult(fired=fired, trigger_data=merged_data)
