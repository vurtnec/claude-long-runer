"""
Generic Webhook Notification
============================

Sends HTTP POST/PUT requests to any webhook URL.
Works with Slack, Discord, Feishu, and custom endpoints.
"""

import subprocess
from typing import Any, Dict

from .base import BaseNotifier


class WebhookNotifier(BaseNotifier):
    """Send notifications via HTTP webhook (Slack, Discord, Feishu, custom, etc.)."""

    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        url = settings.get("url", "")
        method = settings.get("method", "POST")
        headers = settings.get("headers", {"Content-Type": "application/json"})
        body_template = settings.get("body", "")

        if not url:
            print("  Webhook URL not configured")
            return False

        body = self.render_template(body_template, context)

        cmd = ["curl", "-s", "-X", method, url]
        for key, value in headers.items():
            cmd.extend(["-H", f"{key}: {value}"])
        cmd.extend(["-d", body])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            print(f"  Webhook sent to {url} (exit code: {result.returncode})")
            return result.returncode == 0
        except Exception as e:
            print(f"  Webhook send failed to {url}: {e}")
            return False
