"""
Schedule Loader
===============

Loads schedule definitions from YAML files in the schedules/ directory.
Supports environment variable resolution and template variables.
"""

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
from dotenv import load_dotenv

# Auto-load .env from project root (no-op if file doesn't exist)
_project_root = Path(__file__).parent.parent
load_dotenv(_project_root / ".env")

from .models import (
    ConcurrencyConfig,
    NotificationConfig,
    OverlapPolicy,
    RetryConfig,
    ScheduleDefinition,
    TaskRef,
    TriggerConfig,
    TriggerType,
)


def resolve_env_vars(value: Any) -> Any:
    """
    Resolve {{env.VAR_NAME}} template variables in strings.
    Recursively processes dicts and lists.
    """
    if isinstance(value, str):

        def replacer(match):
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))

        return re.sub(r"\{\{env\.(\w+)\}\}", replacer, value)
    elif isinstance(value, dict):
        return {k: resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    return value


def parse_trigger_config(data: dict) -> TriggerConfig:
    """Parse a trigger configuration dict into a TriggerConfig."""
    trigger_type = TriggerType(data["type"])

    sub_triggers = []
    if trigger_type == TriggerType.COMPOSITE:
        sub_triggers = [parse_trigger_config(t) for t in data.get("triggers", [])]

    return TriggerConfig(
        type=trigger_type,
        cron=data.get("cron"),
        timezone=data.get("timezone"),
        paths=data.get("paths", []),
        debounce_seconds=data.get("debounce_seconds", 0),
        url=data.get("url"),
        headers=data.get("headers", {}),
        condition=data.get("condition"),
        operator=data.get("operator"),
        triggers=sub_triggers,
    )


def parse_notifications(
    data: dict,
) -> Tuple[List[NotificationConfig], List[NotificationConfig]]:
    """Parse notification configurations from schedule data."""
    on_success = []
    on_failure = []

    notif_data = data.get("notifications", {})

    for item in notif_data.get("on_success", []):
        item_copy = copy.deepcopy(item)
        notif_type = item_copy.pop("type")
        on_success.append(NotificationConfig(type=notif_type, settings=item_copy))

    for item in notif_data.get("on_failure", []):
        item_copy = copy.deepcopy(item)
        notif_type = item_copy.pop("type")
        on_failure.append(NotificationConfig(type=notif_type, settings=item_copy))

    return on_success, on_failure


def parse_task_ref(data: dict) -> TaskRef:
    """Parse task reference from schedule data."""
    task_data = data.get("task", {})
    task_type = task_data.get("type", "standard")

    return TaskRef(
        name=task_data.get("name"),
        task_type=task_type,
        params=task_data.get("params", {}),
        project_dir=task_data.get("project_dir", "."),
        model=task_data.get("model"),
        max_iterations=task_data.get("max_iterations"),
        prompt=task_data.get("prompt"),
        max_turns=task_data.get("max_turns"),
    )


def load_schedule(filepath: Path) -> ScheduleDefinition:
    """Load a single schedule definition from a YAML file."""
    with open(filepath) as f:
        raw = yaml.safe_load(f)

    data = resolve_env_vars(raw)

    on_success, on_failure = parse_notifications(data)

    retry_data = data.get("retry", {})
    concurrency_data = data.get("concurrency", {})

    return ScheduleDefinition(
        name=data["name"],
        description=data.get("description", ""),
        enabled=data.get("enabled", True),
        trigger=parse_trigger_config(data["trigger"]),
        task=parse_task_ref(data),
        timeout_minutes=data.get("timeout_minutes"),
        notifications_on_success=on_success,
        notifications_on_failure=on_failure,
        retry=RetryConfig(
            max_retries=retry_data.get("max_retries", 0),
            retry_delay_minutes=retry_data.get("retry_delay_minutes", 5),
        ),
        concurrency=ConcurrencyConfig(
            max_concurrent=concurrency_data.get("max_concurrent", 1),
            overlap_policy=OverlapPolicy(
                concurrency_data.get("overlap_policy", "skip")
            ),
        ),
    )


def load_all_schedules(schedules_dir: Path) -> List[ScheduleDefinition]:
    """Load all schedule definitions from a directory (excluding _examples/)."""
    schedules = []

    for filepath in sorted(schedules_dir.glob("*.yaml")):
        try:
            schedule = load_schedule(filepath)
            if schedule.enabled:
                schedules.append(schedule)
                print(f"  Loaded schedule: {schedule.name} ({filepath.name})")
            else:
                print(f"  Skipped disabled schedule: {schedule.name}")
        except Exception as e:
            print(f"  Error loading {filepath.name}: {e}")

    return schedules
