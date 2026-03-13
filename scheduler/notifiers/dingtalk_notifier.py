"""
DingTalk Push Notification
===========================

Sends messages to a DingTalk group chat via a custom bot webhook.

Setup steps:
1. Open DingTalk group > Settings > Smart Group Assistant > Add Bot > Custom
2. For security settings, choose "Custom Keywords" and enter a keyword (e.g. "notification")
3. Copy the Webhook URL (format: https://oapi.dingtalk.com/robot/send?access_token=xxx)
4. Configure it in scheduler_config.yaml or the schedule YAML file

Docs: https://open.dingtalk.com/document/orgapp/custom-robot-access
"""

import asyncio
import json
from typing import Any, Dict

from .base import BaseNotifier


class DingTalkNotifier(BaseNotifier):
    """Send notifications to DingTalk group via custom bot webhook."""

    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        dingtalk_config = self.global_config.get("dingtalk", {})
        webhook_url = settings.get("webhook_url") or dingtalk_config.get("webhook_url", "")

        if not webhook_url:
            print("  DingTalk webhook URL not configured")
            return False

        title = self.render_template(settings.get("title", ""), context)
        body = self.render_template(settings.get("body", ""), context)
        msg_type = settings.get("msg_type", "text")

        if msg_type == "markdown":
            payload = self._build_markdown(title, body)
        else:
            payload = self._build_text(title, body)

        cmd = [
            "curl", "-s", "-X", "POST",
            webhook_url,
            "-H", "Content-Type: application/json",
            "-d", json.dumps(payload, ensure_ascii=False),
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                print("  DingTalk: request timed out")
                return False

            response = stdout.decode().strip()

            try:
                resp_json = json.loads(response)
                if resp_json.get("errcode") == 0:
                    print(f"  DingTalk: sent [{title}]")
                    return True
                else:
                    print(f"  DingTalk error: {resp_json.get('errmsg', response[:200])}")
                    return False
            except json.JSONDecodeError:
                print(f"  DingTalk: unexpected response: {response[:200]}")
                return False

        except Exception as e:
            print(f"  DingTalk send failed: {e}")
            return False

    def _build_text(self, title: str, body: str) -> dict:
        """Build plain text message payload."""
        text = f"{title}\n\n{body}" if title else body
        return {
            "msgtype": "text",
            "text": {
                "content": text,
            },
        }

    def _build_markdown(self, title: str, body: str) -> dict:
        """Build markdown message payload."""
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n{body}",
            },
        }
