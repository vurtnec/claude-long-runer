"""
Teams Reply Notification
========================

Reply into Teams after a schedule fires. By default this sends back into the
same Teams chat that triggered the schedule. Set ``target_chat_id`` to route
the message to a fixed Teams chat instead.

By default it only sends when the original sender is on a configured
whitelist. For schedules that already restrict the source chat tightly, set
``allow_any_sender: true`` to send regardless of sender.

Designed to pair with a ``teams_message`` trigger — relies on ``chat_id`` /
``sender_name`` / ``sender_id`` being present in the notification context
(which ``TeamsMessageTrigger`` populates automatically).

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
- ``allow_any_sender: true`` bypasses the sender whitelist. Use it only when
  the trigger is already scoped to trusted ``allowed_chat_ids`` or
  ``allowed_chat_topic_contains`` values.
- ``target_chat_id`` sends to a fixed Teams chat instead of the chat that
  triggered the schedule. This is useful when trusted senders can trigger the
  schedule from multiple chats, but results should always land in one review
  group.

YAML usage::

    notifications:
      on_success:
        - type: feishu
          ...
        - type: teams_reply
          whitelist:
            - "Jane Doe"                       # displayName (case-insensitive)
            - "12345678-aaaa-bbbb-cccc-..."   # AAD user id also works
          # Optional. Bypass sender whitelist when the trigger chat itself
          # is trusted and tightly scoped.
          # allow_any_sender: true
          # Optional. Defaults to the triggering chat_id.
          # target_chat_id: "19:...@thread.v2"
          # Optional. Defaults to "{{last_response}}" if omitted.
          body: |
            {{last_response}}
          # Optional prefix; useful when the chat is busy and the AI
          # output needs an obvious header.
          # title: "Auto-analysis"
          # Optional. "text" (default) or "html".
          # content_type: text
          # Optional. Defaults to 3500; max supported here is 27000 to stay
          # below Microsoft Teams' chat-message body limit.
          # max_chars: 20000

Setup (one-time after upgrading from the Chat.Read-only release)::

    python teams_probe.py

This re-issues the OAuth token cache with the ``ChatMessage.Send`` scope.
Without it the daemon will log an auth error on the first Teams send.
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


# Soft default on outbound reply size. Teams' hard limit on chat-message body
# is ~28KB, so callers can raise this per schedule when a full review belongs
# in the chat. Keep a small safety margin below the hard limit.
_DEFAULT_REPLY_CHARS = 3500
_MAX_TEAMS_REPLY_CHARS = 27000
_TRUNCATION_MARKER = "\n\n…(truncated)"


class TeamsReplyNotifier(BaseNotifier):
    """Send a Teams reply when the configured sender policy allows it."""

    async def send(self, settings: Dict[str, Any], context: Dict[str, Any]) -> bool:
        allow_any_sender = self._as_bool(settings.get("allow_any_sender", False))
        whitelist: List[str] = []
        if not allow_any_sender:
            normalised = self._normalise_whitelist(settings.get("whitelist"))
            if normalised is None:
                # Malformed (not a list) — already logged by _normalise_whitelist.
                return False
            whitelist = normalised
            if not whitelist:
                print(
                    "  TeamsReply: whitelist is empty — skipping "
                    "(set 'whitelist:' or 'allow_any_sender: true' in the "
                    "schedule YAML to enable)"
                )
                return False

        sender_name = str(context.get("sender_name") or "").strip()
        sender_id = str(context.get("sender_id") or "").strip()
        source_chat_id = str(context.get("chat_id") or "").strip()
        target_chat_id = str(settings.get("target_chat_id") or "").strip()
        chat_id = target_chat_id or source_chat_id

        if not chat_id:
            # No source or fixed target chat id means the trigger that fired
            # wasn't teams_message (or the schema changed) and the schedule did
            # not provide an explicit destination.
            print(
                "  TeamsReply: no chat_id in context and no target_chat_id "
                "configured — skipping"
            )
            return False

        if not allow_any_sender and not self._is_whitelisted(
            sender_name, sender_id, whitelist
        ):
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
        max_reply_chars = self._reply_char_limit(settings.get("max_chars"))
        if len(text) > max_reply_chars:
            # Reserve room for the truncation marker; rstrip avoids the
            # marker landing right after a trailing space.
            text = (
                text[: max_reply_chars - len(_TRUNCATION_MARKER)].rstrip()
                + _TRUNCATION_MARKER
            )

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
            f"(sender '{sender_name}', {len(text)} chars, "
            f"policy={'any-sender' if allow_any_sender else 'whitelist'})"
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

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    @staticmethod
    def _reply_char_limit(value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return _DEFAULT_REPLY_CHARS
        if limit <= 0:
            return _DEFAULT_REPLY_CHARS
        return max(100, min(limit, _MAX_TEAMS_REPLY_CHARS))

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
