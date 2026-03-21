"""
Schedule Data Models
====================

Dataclass-based models for schedule definitions, triggers,
notifications, and execution records.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class TriggerType(Enum):
    CRON = "cron"
    FILE_CHANGED = "file_changed"
    HTTP_CONDITION = "http_condition"
    COMPOSITE = "composite"


class OverlapPolicy(Enum):
    SKIP = "skip"
    QUEUE = "queue"
    CANCEL_PREVIOUS = "cancel_previous"


@dataclass
class TriggerConfig:
    """Unified trigger configuration supporting all trigger types."""

    type: TriggerType
    # Cron fields
    cron: Optional[str] = None
    timezone: Optional[str] = None
    # File change fields
    paths: List[str] = field(default_factory=list)
    debounce_seconds: int = 0
    # HTTP condition fields
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    condition: Optional[Dict[str, Any]] = None
    # Composite fields
    operator: Optional[str] = None  # "and" | "or"
    triggers: List["TriggerConfig"] = field(default_factory=list)


@dataclass
class TaskRef:
    """Reference to a task to execute. Supports both directory-based and inline tasks."""

    name: Optional[str] = None  # tasks/{name}/ directory (for standard tasks)
    task_type: str = "standard"  # "standard" | "inline"
    params: Dict[str, Any] = field(default_factory=dict)
    project_dir: str = "."
    model: Optional[str] = None
    effort: Optional[str] = None
    max_iterations: Optional[int] = None
    # Inline task fields
    prompt: Optional[str] = None
    max_turns: Optional[int] = None


@dataclass
class NotificationConfig:
    """Notification channel configuration."""

    type: str  # "wechat" | "webhook" | "email"
    settings: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetryConfig:
    """Retry policy for failed tasks."""

    max_retries: int = 0
    retry_delay_minutes: int = 5


@dataclass
class ConcurrencyConfig:
    """Concurrency control for scheduled tasks."""

    max_concurrent: int = 1
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP


@dataclass
class ScheduleDefinition:
    """Complete schedule definition loaded from YAML."""

    name: str
    description: str
    enabled: bool
    trigger: TriggerConfig
    task: TaskRef
    timeout_minutes: Optional[int] = None
    notifications_on_success: List[NotificationConfig] = field(default_factory=list)
    notifications_on_failure: List[NotificationConfig] = field(default_factory=list)
    retry: RetryConfig = field(default_factory=RetryConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)


@dataclass
class ExecutionRecord:
    """Record of a single schedule execution."""

    schedule_name: str
    trigger_time: str  # ISO format
    start_time: str  # ISO format
    end_time: Optional[str] = None
    success: Optional[bool] = None
    iterations: int = 0
    error: Optional[str] = None
