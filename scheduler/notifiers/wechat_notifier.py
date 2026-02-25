"""
WeChat Push Notification
========================

Supports two free services:
- Server酱 (ServerChan): https://sct.ftqq.com/
  Free tier: 5 messages/day. Register to get a SendKey.

- WxPusher: https://wxpusher.zjiecode.com/
  Free, unlimited. Register to get appToken and UID.
"""

import asyncio
import json
from typing import Any, Dict

from .base import BaseNotifier


class WeChatNotifier(BaseNotifier):
    """Send push notifications to WeChat via ServerChan or WxPusher."""

    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        channel = settings.get("channel", "serverchan")
        title = self.render_template(settings.get("title", ""), context)
        body = self.render_template(settings.get("body", ""), context)

        if channel == "serverchan":
            return await self._send_serverchan(title, body)
        elif channel == "wxpusher":
            return await self._send_wxpusher(title, body)
        else:
            print(f"  Unknown WeChat channel: {channel}")
            return False

    async def _send_serverchan(self, title: str, body: str) -> bool:
        """
        Send via Server酱 (ServerChan).

        API: POST https://sctapi.ftqq.com/{SendKey}.send
        Params: title, desp (description, supports Markdown)
        """
        wechat_config = self.global_config.get("wechat", {})
        send_key = wechat_config.get("serverchan_key", "")

        if not send_key:
            print("  ServerChan SendKey not configured")
            return False

        cmd = [
            "curl",
            "-s",
            "-X",
            "POST",
            f"https://sctapi.ftqq.com/{send_key}.send",
            "-d",
            f"title={title}&desp={body}",
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
                print("  ServerChan: request timed out")
                return False

            response = stdout.decode().strip()

            # Check for success
            try:
                resp_json = json.loads(response)
                if resp_json.get("code") == 0:
                    print(f"  ServerChan: sent to WeChat [{title}]")
                    return True
                else:
                    print(
                        f"  ServerChan error: {resp_json.get('message', 'unknown error')}"
                    )
                    return False
            except json.JSONDecodeError:
                print(f"  ServerChan: unexpected response: {response[:200]}")
                return False

        except Exception as e:
            print(f"  ServerChan send failed: {e}")
            return False

    async def _send_wxpusher(self, title: str, body: str) -> bool:
        """
        Send via WxPusher.

        API: POST https://wxpusher.zjiecode.com/api/send/message
        Body: JSON with appToken, content, summary, contentType, uids
        """
        wechat_config = self.global_config.get("wechat", {})
        app_token = wechat_config.get("wxpusher_token", "")
        uid = wechat_config.get("wxpusher_uid", "")

        if not app_token or not uid:
            print("  WxPusher token/uid not configured")
            return False

        payload = json.dumps(
            {
                "appToken": app_token,
                "content": body,
                "summary": title[:100],  # WxPusher summary limit
                "contentType": 1,  # 1=text, 2=html, 3=markdown
                "uids": [uid],
            }
        )

        cmd = [
            "curl",
            "-s",
            "-X",
            "POST",
            "https://wxpusher.zjiecode.com/api/send/message",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
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
                print("  WxPusher: request timed out")
                return False

            response = stdout.decode().strip()

            try:
                resp_json = json.loads(response)
                if resp_json.get("success"):
                    print(f"  WxPusher: sent to WeChat [{title}]")
                    return True
                else:
                    print(f"  WxPusher error: {resp_json.get('msg', 'unknown error')}")
                    return False
            except json.JSONDecodeError:
                print(f"  WxPusher: unexpected response: {response[:200]}")
                return False

        except Exception as e:
            print(f"  WxPusher send failed: {e}")
            return False
