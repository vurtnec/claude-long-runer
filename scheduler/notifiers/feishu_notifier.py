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
import re
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

    @staticmethod
    def _markdown_to_lark_elements(text: str) -> list:
        """Convert standard Markdown into a list of Feishu card elements.

        Same conversion logic used by feishu_bot.py — headings, code blocks,
        horizontal rules, blockquotes, and tables are mapped to appropriate
        card element types that Feishu can actually render.
        """
        elements: list = []
        lines = text.split("\n")
        i = 0
        buf: list[str] = []

        def _flush():
            if not buf:
                return
            content = "\n".join(buf).strip()
            if content:
                elements.append({
                    "tag": "div",
                    "text": {"content": content, "tag": "lark_md"},
                })
            buf.clear()

        while i < len(lines):
            line = lines[i]

            # fenced code block
            if line.strip().startswith("```"):
                _flush()
                lang = line.strip().removeprefix("```").strip()
                code_lines: list[str] = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                i += 1
                code_text = "\n".join(code_lines)
                elements.append({
                    "tag": "div",
                    "text": {
                        "content": f"```{lang}\n{code_text}\n```",
                        "tag": "lark_md",
                    },
                })
                continue

            # horizontal rule
            if re.match(r"^\s*([-*_])\s*\1\s*\1[\s\-*_]*$", line):
                _flush()
                elements.append({"tag": "hr"})
                i += 1
                continue

            # heading
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                _flush()
                heading_text = m.group(2).strip()
                elements.append({
                    "tag": "div",
                    "text": {"content": f"**{heading_text}**", "tag": "lark_md"},
                })
                i += 1
                continue

            # blockquote
            if line.strip().startswith("> "):
                _flush()
                quote_lines: list[str] = []
                while i < len(lines) and lines[i].strip().startswith(">"):
                    quote_lines.append(lines[i].strip().removeprefix(">").strip())
                    i += 1
                elements.append({
                    "tag": "note",
                    "elements": [{"tag": "lark_md", "content": "\n".join(quote_lines)}],
                })
                continue

            # table
            if "|" in line and re.match(r"^\s*\|", line):
                _flush()
                table_lines: list[str] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    table_lines.append(lines[i])
                    i += 1
                rows: list[list[str]] = []
                for tl in table_lines:
                    cells = [c.strip() for c in tl.strip().strip("|").split("|")]
                    if all(re.match(r"^[-:]+$", c) for c in cells if c):
                        continue
                    rows.append(cells)
                if rows:
                    formatted = "**" + "  |  ".join(rows[0]) + "**"
                    for row in rows[1:]:
                        formatted += "\n" + "  |  ".join(row)
                    elements.append({
                        "tag": "div",
                        "text": {"content": formatted, "tag": "lark_md"},
                    })
                continue

            buf.append(line)
            i += 1

        _flush()
        return elements

    def _build_markdown_card(self, title: str, body: str) -> dict:
        """Build interactive card with lark_md for Markdown rendering."""
        elements = self._markdown_to_lark_elements(body)
        if not elements:
            elements = [
                {
                    "tag": "div",
                    "text": {"content": body, "tag": "lark_md"},
                }
            ]
        card: dict = {
            "config": {
                "wide_screen_mode": True,
            },
            "elements": elements,
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
