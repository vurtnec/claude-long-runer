"""
Feishu (飞书) Push Notification
================================

通过飞书自定义机器人 Webhook 发送消息到群聊。

设置步骤：
1. 打开飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人
2. 复制 Webhook URL (格式: https://open.feishu.cn/open-apis/bot/v2/hook/xxx)
3. 配置到 scheduler_config.yaml 或 schedule YAML 中

文档：https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

import asyncio
import json
from typing import Any, Dict

from .base import BaseNotifier


class FeishuNotifier(BaseNotifier):
    """Send notifications to Feishu (飞书) group via custom bot webhook."""

    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        # Webhook URL: per-notification or global config
        feishu_config = self.global_config.get("feishu", {})
        webhook_url = settings.get("webhook_url") or feishu_config.get("webhook_url", "")

        if not webhook_url:
            print("  Feishu webhook URL not configured")
            return False

        title = self.render_template(settings.get("title", ""), context)
        body = self.render_template(settings.get("body", ""), context)
        msg_type = settings.get("msg_type", "text")

        if msg_type == "markdown":
            payload = self._build_markdown_card(title, body)
        elif msg_type == "rich":
            payload = self._build_rich_text(title, body)
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
                print("  Feishu: request timed out")
                return False

            response = stdout.decode().strip()

            try:
                resp_json = json.loads(response)
                if resp_json.get("code") == 0 or resp_json.get("StatusCode") == 0:
                    print(f"  Feishu: sent [{title}]")
                    return True
                else:
                    print(f"  Feishu error: {resp_json.get('msg', response[:200])}")
                    return False
            except json.JSONDecodeError:
                print(f"  Feishu: unexpected response: {response[:200]}")
                return False

        except Exception as e:
            print(f"  Feishu send failed: {e}")
            return False

    def _build_text(self, title: str, body: str) -> dict:
        """Build plain text message payload."""
        text = f"{title}\n\n{body}" if title else body
        return {
            "msg_type": "text",
            "content": {
                "text": text,
            },
        }

    def _build_markdown_card(self, title: str, body: str) -> dict:
        """Build interactive card using schema 2.0 with ``markdown`` tag.

        Schema 2.0 natively renders headings, code blocks, tables,
        blockquotes, etc. — no manual Markdown-to-lark_md conversion needed.
        """
        card: dict = {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": body,
                    }
                ],
            },
        }
        if title:
            card["header"] = {
                "title": {
                    "content": title,
                    "tag": "plain_text",
                },
                "template": "blue",
            }
        return {
            "msg_type": "interactive",
            "card": card,
        }

    def _build_rich_text(self, title: str, body: str) -> dict:
        """Build rich text (post) message payload with title and body."""
        # Split body into lines, each becomes a text element
        lines = []
        for line in body.split("\n"):
            lines.append([{"tag": "text", "text": line}])

        return {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": lines,
                    }
                }
            },
        }
