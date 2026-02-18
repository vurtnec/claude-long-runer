"""
Abstract base class for all trigger types.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class TriggerResult:
    """Result of a trigger evaluation."""

    fired: bool
    trigger_data: Dict[str, Any] = field(default_factory=dict)


class BaseTrigger(ABC):
    """Abstract base class for triggers."""

    def __init__(self, config: dict):
        self.config = config
        self._last_fired: Optional[datetime] = None

    @abstractmethod
    def evaluate(self) -> TriggerResult:
        """
        Evaluate whether this trigger should fire.

        Returns:
            TriggerResult with fired=True if the trigger condition is met.
        """
        ...

    @property
    def last_fired(self) -> Optional[datetime]:
        return self._last_fired

    def mark_fired(self):
        self._last_fired = datetime.now()
