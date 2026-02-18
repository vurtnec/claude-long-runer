"""
Scheduler Daemon
================

Main entry point for the scheduled task trigger framework.
Runs as a long-lived process that evaluates triggers and dispatches tasks.

Usage:
    python -m scheduler.daemon                                # Run in foreground
    python -m scheduler.daemon --config scheduler_config.yaml # Custom config
    python -m scheduler.daemon --once                         # Evaluate once and exit
    python -m scheduler.daemon --run daily_hk_ipo             # Run specific schedule now
"""

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Add parent directory for imports from the existing codebase
sys.path.insert(0, str(Path(__file__).parent.parent))

from long_run_executor import run_long_task

from .execution_log import ExecutionLog
from .inline_executor import run_inline_task
from .models import OverlapPolicy, ScheduleDefinition
from .notifiers.base import BaseNotifier
from .notifiers.dingtalk_notifier import DingTalkNotifier
from .notifiers.email_notifier import EmailNotifier
from .notifiers.feishu_notifier import FeishuNotifier
from .notifiers.webhook_notifier import WebhookNotifier
from .notifiers.wechat_notifier import WeChatNotifier
from .schedule_loader import load_all_schedules, resolve_env_vars
from .trigger_engine import TriggerEngine


class SchedulerDaemon:
    """
    Main scheduler daemon.

    Runs an asyncio event loop that:
    1. Loads schedule definitions from schedules/ directory
    2. Evaluates triggers on a configurable polling interval
    3. Dispatches tasks via run_long_task() or run_inline_task()
    4. Sends notifications on completion/failure
    """

    def __init__(self, config_path: str = "scheduler_config.yaml"):
        self.base_dir = Path(__file__).parent.parent  # claude-long-runner root
        self.config = self._load_config(config_path)

        daemon_cfg = self.config.get("daemon", {})
        self.poll_interval = daemon_cfg.get("poll_interval_seconds", 30)
        schedules_dir_name = daemon_cfg.get("schedules_dir", "schedules")
        self.schedules_dir = self.base_dir / schedules_dir_name

        history_cfg = self.config.get("history", {})
        self.execution_log = ExecutionLog(
            history_file=str(
                self.base_dir
                / history_cfg.get("history_file", "scheduler_history.json")
            ),
            max_entries=history_cfg.get("max_entries", 1000),
        )

        self.trigger_engine = TriggerEngine()
        self.schedules: List[ScheduleDefinition] = []
        self._running = False
        self._active_tasks: Dict[str, asyncio.Task] = {}

        # Initialize notifiers
        notif_config = self.config.get("notifications", {})
        self._notifiers: Dict[str, BaseNotifier] = {
            "wechat": WeChatNotifier(notif_config),
            "feishu": FeishuNotifier(notif_config),
            "dingtalk": DingTalkNotifier(notif_config),
            "webhook": WebhookNotifier(notif_config),
            "email": EmailNotifier(notif_config),
        }

        # Defaults
        self.defaults = self.config.get("defaults", {})

    def _load_config(self, config_path: str) -> dict:
        """Load global scheduler configuration."""
        # Try absolute path first, then relative to base_dir
        path = Path(config_path)
        if not path.is_absolute():
            path = self.base_dir / config_path

        if not path.exists():
            print(f"Warning: Config file {config_path} not found, using defaults")
            return {}

        with open(path) as f:
            raw = yaml.safe_load(f)
        return resolve_env_vars(raw) if raw else {}

    def load_schedules(self):
        """Load all schedule definitions and register triggers."""
        if not self.schedules_dir.exists():
            print(f"Schedules directory not found: {self.schedules_dir}")
            print(f"Creating it at: {self.schedules_dir}")
            self.schedules_dir.mkdir(parents=True, exist_ok=True)
            return

        print(f"Loading schedules from {self.schedules_dir}:")
        self.schedules = load_all_schedules(self.schedules_dir)

        for schedule in self.schedules:
            self.trigger_engine.register(schedule)

        print(f"Loaded {len(self.schedules)} active schedule(s)\n")

    def _find_schedule(self, name: str) -> Optional[ScheduleDefinition]:
        """Find a schedule by name."""
        for s in self.schedules:
            if s.name == name:
                return s
        return None

    def _start_feishu_bot(self, loop: asyncio.AbstractEventLoop):
        """Optionally start the Feishu bot alongside the daemon."""
        bot_config = self.config.get("feishu_bot", {})
        if not bot_config.get("enabled", False):
            return None

        try:
            from .feishu_bot import FeishuBotServer

            bot = FeishuBotServer(self.config, self.base_dir)
            bot.start(loop=loop)
            return bot
        except ValueError as e:
            print(f"Warning: Feishu bot not started: {e}")
            return None
        except ImportError as e:
            print(f"Warning: Feishu bot requires lark-oapi: {e}")
            return None

    async def run(self, once: bool = False):
        """Main daemon loop."""
        self._running = True
        print(f"Scheduler daemon started (poll interval: {self.poll_interval}s)")
        print(f"Press Ctrl+C to stop\n")

        # Handle graceful shutdown via signals
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown)

        # Optionally start Feishu bot
        self._start_feishu_bot(loop)

        while self._running:
            await self._poll_cycle()

            if once:
                break

            await asyncio.sleep(self.poll_interval)

        # Wait for any running tasks to complete
        if self._active_tasks:
            print(
                f"\nWaiting for {len(self._active_tasks)} active task(s) to complete..."
            )
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)

        print("Scheduler daemon stopped.")

    async def run_schedule_now(self, schedule_name: str):
        """Immediately execute a specific schedule (for testing/manual runs)."""
        schedule = self._find_schedule(schedule_name)
        if not schedule:
            print(f"Schedule not found: {schedule_name}")
            print(f"Available schedules: {[s.name for s in self.schedules]}")
            return

        print(f"Immediately executing schedule: {schedule_name}")
        await self._execute_schedule(schedule, {"trigger_type": "manual"})

    async def _poll_cycle(self):
        """Single poll cycle: evaluate all triggers and dispatch as needed."""
        now = datetime.now()

        for schedule in self.schedules:
            if not schedule.enabled:
                continue

            # Check concurrency: skip if task is still running
            if schedule.concurrency.overlap_policy == OverlapPolicy.SKIP:
                if schedule.name in self._active_tasks:
                    continue

            # Evaluate trigger
            result = self.trigger_engine.evaluate(schedule.name)

            if result.fired:
                print(
                    f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"Trigger fired: {schedule.name}"
                )

                # Dispatch task as async task
                task = asyncio.create_task(
                    self._execute_schedule(schedule, result.trigger_data)
                )
                self._active_tasks[schedule.name] = task
                task.add_done_callback(
                    lambda t, name=schedule.name: self._active_tasks.pop(name, None)
                )

    async def _execute_schedule(
        self, schedule: ScheduleDefinition, trigger_data: Dict[str, Any]
    ):
        """Execute a scheduled task and handle notifications."""
        start_time = datetime.now()
        record_idx = self.execution_log.record_start(schedule.name, start_time)

        # Build template variables
        template_vars = {
            "today": datetime.now().strftime("%Y-%m-%d"),
            "now": datetime.now().isoformat(),
            "trigger_time": start_time.isoformat(),
        }
        # Add trigger data with "trigger." prefix for template access
        for key, value in trigger_data.items():
            template_vars[f"trigger.{key}"] = value
            template_vars[key] = value  # Also available without prefix

        # Resolve template variables in task params
        resolved_params = {}
        for key, value in schedule.task.params.items():
            if isinstance(value, str):
                for tvar, tval in template_vars.items():
                    value = value.replace(f"{{{{{tvar}}}}}", str(tval))
            resolved_params[key] = value

        # Determine model and max_iterations (schedule > defaults > hardcoded)
        model = (
            schedule.task.model
            or self.defaults.get("model", "claude-sonnet-4-5-20250929")
        )

        success = False
        error_msg = None
        iterations = 0
        last_response = ""
        retries = 0

        while retries <= schedule.retry.max_retries:
            try:
                if schedule.task.task_type == "inline":
                    # Inline task: direct prompt execution
                    result = await self._execute_inline(schedule, model, template_vars)
                    success = result["success"]
                    last_response = result.get("response_text", "")
                    iterations = result.get("turns_used", 0)
                    if not success:
                        error_msg = result.get("error", "Inline task failed")
                else:
                    # Standard task: use existing run_long_task()
                    result = await self._execute_standard(
                        schedule, model, resolved_params
                    )
                    success = result["success"]
                    last_response = result.get("last_response", "")
                    iterations = result.get("iterations", 0)
                    if not success:
                        error_msg = "Task completed but success conditions not met"

                if success:
                    break

            except Exception as e:
                error_msg = str(e)
                print(f"  Task {schedule.name} failed: {e}")

            retries += 1
            if retries <= schedule.retry.max_retries:
                delay = schedule.retry.retry_delay_minutes * 60
                print(f"  Retrying in {schedule.retry.retry_delay_minutes} minutes...")
                await asyncio.sleep(delay)

        # Calculate duration
        end_time = datetime.now()
        duration = end_time - start_time
        duration_str = str(duration).split(".")[0]  # Remove microseconds

        # Record execution
        self.execution_log.record_end(
            record_idx, success=success, iterations=iterations, error=error_msg
        )

        # Build notification context
        today_str = datetime.now().strftime("%Y-%m-%d")
        notification_context = {
            "task_name": schedule.task.name or "inline_task",
            "schedule_name": schedule.name,
            "duration": duration_str,
            "iterations": iterations,
            "last_response": last_response[:5000],  # Truncate for notifications
            "status": "SUCCESS" if success else "FAILED",
            "error": error_msg or "",
            "date": today_str,
            "today": today_str,
            **resolved_params,
        }
        # Add trigger data to notification context
        for key, value in trigger_data.items():
            notification_context[key] = str(value)

        # Send notifications
        if success:
            print(f"  Task completed successfully. Sending success notifications...")
            await self._send_notifications(
                schedule.notifications_on_success, notification_context
            )
        else:
            print(f"  Task failed. Sending failure notifications...")
            await self._send_notifications(
                schedule.notifications_on_failure, notification_context
            )

    async def _execute_standard(
        self,
        schedule: ScheduleDefinition,
        model: str,
        resolved_params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a standard task via run_long_task()."""
        max_iters = schedule.task.max_iterations or self.defaults.get(
            "max_iterations", 10
        )
        project_dir = Path(schedule.task.project_dir).resolve()
        project_dir.mkdir(parents=True, exist_ok=True)

        print(f"  Executing standard task: {schedule.task.name}")
        print(f"  Project dir: {project_dir}")
        print(f"  Model: {model}, Max iterations: {max_iters}")

        success = await run_long_task(
            task_name=schedule.task.name,
            task_params=resolved_params,
            project_dir=project_dir,
            model=model,
            max_iterations=max_iters,
            resume=False,
        )

        # Read state file for last_response
        last_response = ""
        iterations = 0
        task_name = Path(schedule.task.name).name
        state_file = project_dir / f"{task_name}_state.json"
        if state_file.exists():
            try:
                with open(state_file) as f:
                    state_data = json.load(f)
                last_response = state_data.get("last_response", "")
                iterations = state_data.get("iteration", 0)
            except (json.JSONDecodeError, IOError):
                pass

        return {
            "success": success,
            "last_response": last_response,
            "iterations": iterations,
        }

    async def _execute_inline(
        self,
        schedule: ScheduleDefinition,
        model: str,
        template_vars: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute an inline prompt task."""
        prompt = schedule.task.prompt or ""

        # Resolve template variables in the prompt
        for key, value in template_vars.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))

        max_turns = schedule.task.max_turns or 3
        project_dir = Path(schedule.task.project_dir).resolve()
        project_dir.mkdir(parents=True, exist_ok=True)

        return await run_inline_task(
            prompt=prompt,
            project_dir=project_dir,
            model=model,
            max_turns=max_turns,
        )

    async def _send_notifications(
        self, notifications, context: Dict[str, Any]
    ):
        """Send all configured notifications."""
        for notif in notifications:
            notifier = self._notifiers.get(notif.type)
            if notifier:
                try:
                    await notifier.send(notif.settings, context)
                except Exception as e:
                    print(f"  Notification error ({notif.type}): {e}")
            else:
                print(f"  Unknown notifier type: {notif.type}")

    def _shutdown(self):
        """Handle graceful shutdown signal."""
        print("\nShutdown signal received...")
        self._running = False


def main():
    parser = argparse.ArgumentParser(
        description="Claude Long-Runner Scheduler Daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start the scheduler daemon
  python -m scheduler.daemon

  # Use custom config file
  python -m scheduler.daemon --config /path/to/config.yaml

  # Run one poll cycle and exit (for testing)
  python -m scheduler.daemon --once

  # Immediately run a specific schedule
  python -m scheduler.daemon --run daily_hk_ipo
        """,
    )
    parser.add_argument(
        "--config",
        default="scheduler_config.yaml",
        help="Path to scheduler config file (default: scheduler_config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll cycle and exit (for testing)",
    )
    parser.add_argument(
        "--run",
        type=str,
        metavar="SCHEDULE_NAME",
        help="Immediately run a specific schedule by name",
    )

    args = parser.parse_args()

    daemon = SchedulerDaemon(config_path=args.config)
    daemon.load_schedules()

    if args.run:
        asyncio.run(daemon.run_schedule_now(args.run))
    else:
        asyncio.run(daemon.run(once=args.once))


if __name__ == "__main__":
    main()
