"""
Abstract base class for notification delivery.
"""

import re
from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseNotifier(ABC):
    """Abstract notifier with template rendering support."""

    def __init__(self, global_config: Dict[str, Any]):
        """
        Args:
            global_config: Global notification settings from scheduler_config.yaml
        """
        self.global_config = global_config

    @abstractmethod
    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        """
        Send a notification.

        Args:
            settings: Per-notification settings from the schedule YAML
            context: Template variables (task_name, duration, iterations, last_response, etc.)

        Returns:
            True if sent successfully.
        """
        ...

    def render_template(self, template: str, context: Dict[str, Any]) -> str:
        """Render a template string by replacing {{key}} with context values."""
        result = template
        for key, value in context.items():
            result = result.replace(f"{{{{{key}}}}}", str(value))
        return result
