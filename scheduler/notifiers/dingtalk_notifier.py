"""
DingTalk (钉钉) Push Notification
===================================

通过钉钉自定义机器人 Webhook 发送消息到群聊。

设置步骤：
1. 打开钉钉群 → 设置 → 智能群助手 → 添加机器人 → 自定义
2. 安全设置选「自定义关键词」，填入一个关键词（如「通知」）
3. 复制 Webhook URL (格式: https://oapi.dingtalk.com/robot/send?access_token=xxx)
4. 配置到 scheduler_config.yaml 或 schedule YAML 中

文档：https://open.dingtalk.com/document/orgapp/custom-robot-access
"""

import json
import subprocess
from typing import Any, Dict

from .base import BaseNotifier


class DingTalkNotifier(BaseNotifier):
    """Send notifications to DingTalk (钉钉) group via custom bot webhook."""

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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            response = result.stdout.strip()

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
