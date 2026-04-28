"""
Teams Message Trigger
=====================

Polls Microsoft Teams chats via Microsoft Graph and fires when a new message
matches one or more of the configured filters:

- ``chat_topic_contains``  — only consider chats whose topic contains this string
- ``chat_id``              — explicit chat to monitor (skips topic resolution)
- ``sender_displayname``   — only fire on messages from this exact display name
- ``content_pattern``      — regex that must match the message text/HTML
- ``capture_groups``       — when ``content_pattern`` has named groups, lift them
                             into trigger_data (e.g. ``pr_id`` from the PR URL)

If multiple matching messages are found in one evaluate() call, the trigger
fires for the OLDEST unfired one and advances the watermark past it. The next
poll cycle will pick up the next match. This keeps each Claude run scoped to
a single triggering message.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from ..teams_client import TeamsClient, TeamsMessage, get_teams_client
from .base import BaseTrigger, TriggerResult


# Module-level executor reused across triggers/poll-cycles to avoid the
# per-cycle ThreadPoolExecutor setup/teardown cost. 10 concurrent Graph calls
# is well under Microsoft's 4 RPS-per-app limit (each request ≤ 1s typical),
# but high enough to cut a 30-chat scan from ~10s serial to ~1-2s.
_FETCH_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="teams-fetch")


class TeamsMessageTrigger(BaseTrigger):
    """Fires on incoming Teams messages matching configured filters."""

    def __init__(self, config: dict, client: Optional[TeamsClient] = None):
        super().__init__(config)
        self.chat_topic_substring: Optional[str] = config.get("chat_topic_contains")
        self.explicit_chat_id: Optional[str] = config.get("chat_id")
        self.sender_displayname: Optional[str] = config.get("sender_displayname")

        pattern = config.get("content_pattern")
        self.content_re: Optional[re.Pattern] = (
            re.compile(pattern, re.IGNORECASE | re.DOTALL) if pattern else None
        )

        # If true, regex is matched against raw HTML body (so links are visible);
        # otherwise it's matched against HTML-stripped plain text. Default: HTML
        # so PR URLs inside <a href="..."> are easily caught.
        self.match_html: bool = bool(config.get("match_html", True))

        # Default True: production semantics is "someone sent ME a message",
        # so messages I send myself should not fire. Set False to test by
        # sending to yourself in any chat.
        self.exclude_self: bool = bool(config.get("exclude_self", True))

        # Skip messages whose stripped plain-text body is shorter than this.
        # Avoids spinning up Claude for "OK" / "收到" acknowledgements.
        self.min_message_length: int = int(config.get("min_message_length", 0))

        self._client = client or get_teams_client()
        self._resolved_chat_id: Optional[str] = self.explicit_chat_id
        self._chat_resolution_attempted = False
        # Set by TriggerEngine.register() — used to namespace watermarks so
        # different schedules monitoring overlapping chats don't interfere.
        self.owner_name: str = "_default"

    # ------------------------------------------------------------------
    # Chat resolution
    # ------------------------------------------------------------------

    def _resolve_chat_id(self) -> Optional[str]:
        if self._resolved_chat_id:
            return self._resolved_chat_id
        if self._chat_resolution_attempted and not self.chat_topic_substring:
            return None
        self._chat_resolution_attempted = True

        if not self.chat_topic_substring:
            # No topic filter and no explicit chat → scan all recent chats each
            # time. We treat _resolved_chat_id as None to indicate "all".
            return None

        try:
            chat = self._client.find_chat_by_topic(self.chat_topic_substring)
        except Exception as e:
            print(
                f"  [teams_trigger] chat lookup failed for "
                f"'{self.chat_topic_substring}': {e}"
            )
            return None

        if not chat:
            print(
                f"  [teams_trigger] no chat found matching "
                f"'{self.chat_topic_substring}' yet — will retry next cycle"
            )
            return None

        self._resolved_chat_id = chat["id"]
        topic = chat.get("topic") or "(no topic)"
        print(
            f"  [teams_trigger:{self.owner_name}] resolved chat '{topic}' → "
            f"{self._resolved_chat_id[:40]}..."
        )
        # Seed the watermark to the latest existing message so we don't fire
        # on historical content.
        self._client.initialize_watermark_to_now(
            self.owner_name, self._resolved_chat_id
        )
        return self._resolved_chat_id

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def _matches(self, msg: TeamsMessage) -> Optional[Dict[str, Any]]:
        """Return regex match groups (or {}) if msg matches; None otherwise."""
        if self.exclude_self:
            my_id, my_name = self._client.get_my_identity()
            is_self_sender = (
                (bool(my_id) and bool(msg.sender_id) and msg.sender_id == my_id)
                or (bool(my_name) and bool(msg.sender_name) and msg.sender_name == my_name)
            )
            # In a self-chat (chat with only yourself) you ARE both sender and
            # the sole recipient, so the "exclude self" intent doesn't apply —
            # let those messages through. Anywhere else with you as sender
            # (group / 1-on-1 with someone else) is filtered as before.
            if is_self_sender and not self._client.is_self_chat(msg.chat_id):
                return None

        if self.sender_displayname and msg.sender_name != self.sender_displayname:
            return None

        # Length filter on plain-text body — measured AFTER HTML strip so that
        # `<at>...</at>` tags don't pad the count. A short ack like "OK" stays
        # short whether the source body is HTML or plain text.
        if self.min_message_length > 0 and len(msg.body_text) < self.min_message_length:
            return None

        if self.content_re:
            haystack = msg.body_html if self.match_html else msg.body_text
            m = self.content_re.search(haystack or "")
            if not m:
                return None
            # Surface named groups; numbered groups go under match_1, match_2, ...
            captures: Dict[str, Any] = {}
            for name, val in (m.groupdict() or {}).items():
                if val is not None:
                    captures[name] = val
            for i, val in enumerate(m.groups(), start=1):
                if val is not None and f"match_{i}" not in captures:
                    captures[f"match_{i}"] = val
            captures["matched_text"] = m.group(0)
            return captures

        # No content_pattern set → any sender-allowed message matches
        return {}

    # ------------------------------------------------------------------
    # Evaluate
    # ------------------------------------------------------------------

    def _candidate_messages(self) -> List[TeamsMessage]:
        chat_id = self._resolve_chat_id()
        if chat_id:
            since = self._client.get_watermark(self.owner_name, chat_id)
            return self._client.get_chat_messages(chat_id, since=since)

        # No specific chat — scan all recent chats. Useful for the "PR URL in
        # any chat" rule. Watermarks are tracked per-(schedule, chat) so
        # different schedules never disturb each other.
        out: List[TeamsMessage] = []
        try:
            # 15 most-recent chats covers active conversations without
            # scanning every dormant chat (steady-state poll latency).
            chats = self._client.list_chats(top=15)
        except Exception as e:
            print(f"  [teams_trigger:{self.owner_name}] list_chats failed: {e}")
            return []

        # Split into "first-sight" chats (need watermark seeding) and
        # "watched" chats (need a real messages fetch). We seed serially —
        # it's a one-shot cost when a chat first becomes visible — and
        # parallelise the steady-state message fetches.
        targets: List[str] = []
        for chat in chats:
            cid = chat["id"]
            since = self._client.get_watermark(self.owner_name, cid)
            if since is None:
                self._client.initialize_watermark_to_now(self.owner_name, cid)
                continue
            targets.append(cid)

        if not targets:
            return out

        def _fetch(cid: str) -> List[TeamsMessage]:
            since_ts = self._client.get_watermark(self.owner_name, cid)
            # Small top: in steady state we just want anything posted since
            # last poll (typically 0-2 messages). Bigger top inflates response
            # payload without adding signal — server still returns history
            # we'll filter out client-side via `since`.
            return self._client.get_chat_messages(cid, since=since_ts, top=10)

        future_to_cid = {
            _FETCH_POOL.submit(_fetch, cid): cid for cid in targets
        }
        # Cap each fetch at 15s so a single hung Graph call can't stall the
        # whole poll cycle (and by extension Ctrl+C responsiveness).
        for fut in as_completed(future_to_cid, timeout=20):
            cid = future_to_cid[fut]
            try:
                out.extend(fut.result(timeout=15))
            except Exception as e:
                print(
                    f"  [teams_trigger:{self.owner_name}] fetch failed "
                    f"for {cid[:30]}: {e}"
                )
        return out

    def evaluate(self) -> TriggerResult:
        try:
            messages = self._candidate_messages()
        except Exception as e:
            print(f"  [teams_trigger] evaluate error: {e}")
            return TriggerResult(fired=False)

        if not messages:
            return TriggerResult(fired=False)

        # Process in chronological order. First match wins; we advance the
        # watermark on every message we look at (matched or not) so that
        # non-matching messages aren't re-evaluated on the next poll.
        messages.sort(key=lambda m: m.created_at)

        fired_data: Optional[Dict[str, Any]] = None
        for msg in messages:
            captures = self._matches(msg)
            if captures is not None and fired_data is None:
                data = msg.as_trigger_data()
                data.update(captures)
                fired_data = data
                # Advance watermark past this message and stop — leave the
                # rest for the next poll cycle (one schedule run per match).
                self._client.set_watermark(self.owner_name, msg.chat_id, msg.created_at)
                break
            # Non-matching: still advance the watermark so we don't re-scan
            self._client.set_watermark(self.owner_name, msg.chat_id, msg.created_at)

        if fired_data:
            return TriggerResult(fired=True, trigger_data=fired_data)
        return TriggerResult(fired=False)
