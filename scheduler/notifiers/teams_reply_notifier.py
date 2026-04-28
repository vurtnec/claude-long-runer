"""
Teams Reply Notification
========================

Reply back into the same Teams chat that triggered the schedule, but only
when the original sender is on a configured whitelist. Designed to pair
with a ``teams_message`` trigger — relies on ``chat_id`` / ``sender_name``
/ ``sender_id`` being present in the notification context (which
``TeamsMessageTrigger`` populates automatically).

Whitelist semantics:

- Each entry is matched (case-insensitive) against BOTH the sender's
  Teams displayName AND their AAD object id; either match qualifies.
  This lets you write the friendly name in YAML without losing the
  option to fall back to a stable id when display names collide or get
  renamed.
- An empty / missing whitelist means "never reply" — the notifier logs
  a clear skip message so a misconfiguration doesn't silently swallow
  every send.
- Non-whitelisted senders are skipped silently (returns False) without
  raising. This is intentional so the notifier can sit alongside a
  Feishu notifier in the same ``on_success`` block: every trigger
  produces the Feishu push, but only whitelisted senders also get the
  Teams reply.

YAML usage::

    notifications:
      on_success:
        - type: feishu
          ...
        - type: teams_reply
          whitelist:
            - "Jane Doe"                       # displayName (case-insensitive)
            - "12345678-aaaa-bbbb-cccc-..."   # AAD user id also works
          # Optional. Defaults to "{{last_response}}" if omitted.
          body: |
            {{last_response}}
          # Optional prefix; useful when the chat is busy and the AI
          # output needs an obvious header.
          # title: "Auto-analysis"
          # Optional. "text" (default) or "html".
          # content_type: text

Setup (one-time after upgrading from the Chat.Read-only release)::

    python teams_probe.py

This re-issues the OAuth token cache with the ``ChatMessage.Send`` scope.
Without it the daemon will log an auth error on the first whitelisted send.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

# Reach the project root so we can import the top-level scheduler package's
# teams_client module from inside the notifiers/ subpackage. (Same trick
# inline_executor.py uses to reach client.py at the project root.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scheduler.teams_client import TeamsAuthError, get_teams_client  # noqa: E402

from .base import BaseNotifier


# Soft cap on outbound reply size. Teams' hard limit on chat-message body
# is ~28KB, but pasting a 28KB wall of text into a chat is hostile. 3500
# chars is roughly a screenful on desktop; long answers get truncated with
# a clear marker so the recipient knows there's more in Feishu.
_MAX_REPLY_CHARS = 3500


class TeamsReplyNotifier(BaseNotifier):
    """Send a Teams reply for whitelisted senders only; skip otherwise."""

    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        whitelist = self._normalise_whitelist(settings.get("whitelist"))
        if whitelist is None:
            # Malformed (not a list) — already logged by _normalise_whitelist.
            return False
        if not whitelist:
            print(
                "  TeamsReply: whitelist is empty — skipping "
                "(set 'whitelist:' in the schedule YAML to enable)"
            )
            return False

        sender_name = str(context.get("sender_name") or "").strip()
        sender_id = str(context.get("sender_id") or "").strip()
        chat_id = str(context.get("chat_id") or "").strip()

        if not chat_id:
            # No chat_id means the trigger that fired wasn't teams_message
            # (or the schema changed). Nothing useful we can do here.
            print(
                "  TeamsReply: no chat_id in context — this notifier is only "
                "meaningful for teams_message triggers; skipping"
            )
            return False

        if not self._is_whitelisted(sender_name, sender_id, whitelist):
            shown_id = (sender_id[:8] + "...") if sender_id else "no-id"
            print(
                f"  TeamsReply: sender '{sender_name}' ({shown_id}) "
                f"not whitelisted — skipping"
            )
            return False

        text = self._render_body(settings, context)
        if not text:
            print("  TeamsReply: rendered body is empty — skipping")
            return False
        if len(text) > _MAX_REPLY_CHARS:
            # Reserve room for the truncation marker; rstrip avoids the
            # marker landing right after a trailing space.
            text = text[: _MAX_REPLY_CHARS - 16].rstrip() + "\n\n…(truncated)"

        content_type = settings.get("content_type", "text")
        if content_type not in ("text", "html"):
            content_type = "text"

        try:
            client = get_teams_client()
            # send_chat_message uses the synchronous `requests` library;
            # offload it so a slow Graph round-trip doesn't stall the
            # daemon's event loop (and by extension Ctrl+C handling).
            await asyncio.to_thread(
                client.send_chat_message, chat_id, text, content_type
            )
        except TeamsAuthError as e:
            # Most common cause after upgrade: cached token only has the
            # old Chat.Read scope. Surface the fix prominently.
            print(
                f"  TeamsReply: auth failed — {e}\n"
                f"  → Re-run `python teams_probe.py` once to re-consent "
                f"with the ChatMessage.Send scope."
            )
            return False
        except Exception as e:
            # Don't propagate — we deliberately want Feishu (or other
            # notifiers in the same on_success block) to keep working
            # even if this Teams reply fails.
            print(f"  TeamsReply: send failed — {e}")
            return False

        print(
            f"  TeamsReply: sent to chat {chat_id[:30]}... "
            f"(sender '{sender_name}', {len(text)} chars)"
        )
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_whitelist(raw: Any) -> List[str] | None:
        """
        Lower-case the whitelist for case-insensitive comparison and drop
        empty entries. Returns ``None`` (NOT empty list) when the value is
        the wrong shape, so the caller can distinguish "configured but
        empty" from "misconfigured".
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            print("  TeamsReply: 'whitelist' must be a list — skipping")
            return None
        return [str(item).strip().lower() for item in raw if str(item).strip()]

    @staticmethod
    def _is_whitelisted(
        sender_name: str, sender_id: str, whitelist_lower: List[str]
    ) -> bool:
        # Either a displayName match OR an id match qualifies. Both are
        # compared case-insensitively (AAD ids are GUIDs so casing doesn't
        # matter; displayNames in practice keep stable casing too).
        candidates: List[str] = []
        if sender_name:
            candidates.append(sender_name.lower())
        if sender_id:
            candidates.append(sender_id.lower())
        return any(c in whitelist_lower for c in candidates)

    def _render_body(
        self, settings: Dict[str, Any], context: Dict[str, Any]
    ) -> str:
        """
        Build the message body:
          - ``body`` template (default ``{{last_response}}``) is rendered
            via the inherited ``render_template``.
          - ``title`` is optional; when present it's prepended on its own
            line so the recipient sees a heading before the AI output.
        """
        title = self.render_template(settings.get("title", ""), context).strip()
        body_template = settings.get("body") or "{{last_response}}"
        body = self.render_template(body_template, context).strip()

        if title and body:
            return f"{title}\n\n{body}"
        return title or body
