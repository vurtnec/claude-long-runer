"""
HTTP condition trigger.

Polls an HTTP endpoint and evaluates a condition on the response.
Uses subprocess + curl to keep dependencies minimal.
"""

import json
import subprocess

from .base import BaseTrigger, TriggerResult


class HttpConditionTrigger(BaseTrigger):
    """
    Fires when an HTTP endpoint returns a response matching a condition.

    Condition evaluation supports:
    - jq expressions (if jq is installed)
    - Simple built-in checks as fallback
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.url = config["url"]
        self.headers = config.get("headers", {})
        self.condition = config.get("condition", {})

    def evaluate(self) -> TriggerResult:
        try:
            # Build curl command
            cmd = ["curl", "-s", "-w", "\n%{http_code}", self.url]
            for key, value in self.headers.items():
                cmd.extend(["-H", f"{key}: {value}"])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout.strip()

            # Split response body and status code
            lines = output.rsplit("\n", 1)
            body = lines[0] if len(lines) > 1 else ""
            status_code = int(lines[-1]) if lines[-1].isdigit() else 0

            if status_code < 200 or status_code >= 300:
                return TriggerResult(fired=False)

            # Evaluate condition
            fired = self._evaluate_condition(body)

            if fired:
                try:
                    response_data = json.loads(body)
                except json.JSONDecodeError:
                    response_data = body

                return TriggerResult(
                    fired=True,
                    trigger_data={
                        "http_response": response_data,
                        "status_code": status_code,
                        "trigger_type": "http_condition",
                    },
                )

        except Exception as e:
            print(f"  HTTP trigger error for {self.url}: {e}")

        return TriggerResult(fired=False)

    def _evaluate_condition(self, body: str) -> bool:
        """Evaluate condition against response body."""
        jq_expr = self.condition.get("jq_expression")
        if not jq_expr:
            return True  # No condition = always fire on 2xx

        # Try jq subprocess first
        try:
            result = subprocess.run(
                ["jq", "-e", jq_expr],
                input=body,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except FileNotFoundError:
            # jq not installed, try simple fallback
            return self._simple_condition_eval(body, jq_expr)

    def _simple_condition_eval(self, body: str, jq_expr: str) -> bool:
        """Fallback condition evaluation without jq."""
        try:
            data = json.loads(body)
            # Handle common patterns
            if "length > 0" in jq_expr and isinstance(data, list):
                return len(data) > 0
            if "length == 0" in jq_expr and isinstance(data, list):
                return len(data) == 0
        except json.JSONDecodeError:
            pass
        return False
