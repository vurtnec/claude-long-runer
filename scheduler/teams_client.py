"""
Teams Graph Client
==================

Wraps Microsoft Graph API for Teams chat-message access using delegated
permissions (OAuth device code flow) so that no Azure app registration is
required from the user — we borrow the public ``Microsoft Graph Command Line
Tools`` client_id (14d82eec-204b-4c2f-b7e8-296a70dab67e).

Design notes:

- ``TeamsClient`` is a process-level singleton. Multiple TeamsMessageTrigger
  instances share the same authenticated client and per-chat watermark
  state, so every poll cycle hits Graph at most once per (active) chat.
- High-water marks are persisted at
  ``~/.claude-long-runner/teams_watermarks.json`` to survive daemon restarts.
- Token cache is at ``~/.claude-long-runner/teams_token_cache.json``. After
  the first device-code login the daemon refreshes silently.

Personal Microsoft accounts (outlook.com / hotmail / Teams Free) are NOT
supported by the underlying Graph chat APIs — work or school accounts only.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import msal
import requests


# Public Microsoft client id for "Microsoft Graph Command Line Tools".
# Using this avoids requiring users to register their own Entra ID app for
# basic delegated Graph access.
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"
# Chat.Read         — list chats / read messages (trigger side)
# ChatMessage.Send  — post a new top-level message into a chat (reply side)
# User.Read         — resolve the signed-in user's id/displayName so
#                     TeamsMessageTrigger can filter out self-sent messages.
#
# NOTE: Adding ChatMessage.Send to an existing install REQUIRES re-running
# `python teams_probe.py` once. MSAL's silent-acquire keys cached tokens by
# scope, so the previous Chat.Read-only token is unusable here and the user
# must re-consent via device-code flow.
DEFAULT_SCOPES = ["Chat.Read", "ChatMessage.Send", "User.Read"]

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

STATE_DIR = Path.home() / ".claude-long-runner"
TOKEN_CACHE_FILE = STATE_DIR / "teams_token_cache.json"
WATERMARK_FILE = STATE_DIR / "teams_watermarks.json"


@dataclass
class TeamsMessage:
    """Lightweight view of a Graph chatMessage relevant for trigger matching."""

    id: str
    chat_id: str
    chat_topic: str
    chat_type: str
    created_at: str  # ISO 8601 UTC, e.g. "2026-04-27T06:08:16.029Z"
    sender_name: str
    sender_id: str
    body_html: str
    body_text: str  # HTML-stripped plain text
    raw: Dict[str, Any]

    def as_trigger_data(self) -> Dict[str, Any]:
        return {
            "trigger_type": "teams_message",
            "message_id": self.id,
            "chat_id": self.chat_id,
            "chat_topic": self.chat_topic,
            "chat_type": self.chat_type,
            "sender_name": self.sender_name,
            "sender_id": self.sender_id,
            "message_text": self.body_text,
            "message_html": self.body_html,
            "created_at": self.created_at,
        }


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    return _HTML_TAG_RE.sub("", html or "").strip()


def _parse_iso(ts: str) -> datetime:
    """Parse Graph's ISO timestamps (always UTC, may end with 'Z')."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


class TeamsAuthError(RuntimeError):
    """Raised when authentication with Microsoft Graph fails."""


class TeamsClient:
    """
    Authenticated wrapper around the Microsoft Graph Teams chat APIs.

    Use ``get_teams_client()`` rather than constructing directly so that
    triggers in the same process share a single authenticated client.
    """

    def __init__(
        self,
        client_id: str = DEFAULT_CLIENT_ID,
        authority: str = DEFAULT_AUTHORITY,
        scopes: Optional[List[str]] = None,
    ):
        self.client_id = client_id
        self.authority = authority
        self.scopes = list(scopes or DEFAULT_SCOPES)
        self._lock = threading.Lock()
        self._token_cache = msal.SerializableTokenCache()

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if TOKEN_CACHE_FILE.exists():
            try:
                self._token_cache.deserialize(TOKEN_CACHE_FILE.read_text())
            except Exception:
                # Corrupt cache — start fresh; user will re-login on next call
                pass

        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=self.authority,
            token_cache=self._token_cache,
        )

        # Identity of the signed-in user, used by triggers to filter out
        # self-sent messages. Lazily resolved on first call to get_my_identity().
        self._my_user_id: Optional[str] = None
        self._my_display_name: Optional[str] = None

        # chat_id → is this a self-chat (only-me member)? Cached to avoid
        # querying /chats/{id}/members on every poll.
        self._self_chat_cache: Dict[str, bool] = {}

        # chat_id → {"topic": ..., "chatType": ...} cached during list_chats
        # so get_chat_messages doesn't need a second GET /me/chats/{id} round
        # trip per poll cycle. Halves Graph load when scanning many chats.
        self._chat_meta_cache: Dict[str, Dict[str, str]] = {}

        # Watermarks: {owner_name: {chat_id: last_seen_iso_ts}}
        # The owner is the schedule name — different schedules monitoring the
        # same chat must not advance each other's progress, otherwise one
        # schedule's non-matching messages would hide them from another.
        self._watermarks: Dict[str, Dict[str, str]] = {}
        if WATERMARK_FILE.exists():
            try:
                raw = json.loads(WATERMARK_FILE.read_text())
            except json.JSONDecodeError:
                raw = {}
            # Migrate legacy flat format ({chat_id: ts}) into a "_legacy"
            # namespace so existing users don't lose their progress.
            if raw and all(isinstance(v, str) for v in raw.values()):
                raw = {"_legacy": raw}
            self._watermarks = raw

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def get_access_token(self, interactive: bool = True) -> str:
        """
        Return a valid access token. Tries silent acquisition first; falls back
        to device code flow only if ``interactive=True`` (set False inside the
        scheduler daemon to avoid blocking on user input — pre-login via
        ``teams_probe.py`` instead).
        """
        with self._lock:
            accounts = self._app.get_accounts()
            if accounts:
                result = self._app.acquire_token_silent(
                    self.scopes, account=accounts[0]
                )
                if result and "access_token" in result:
                    self._save_cache()
                    return result["access_token"]

            if not interactive:
                raise TeamsAuthError(
                    "No cached Teams credentials. Run `python teams_probe.py` "
                    "once to log in via device code flow."
                )

            flow = self._app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise TeamsAuthError(
                    f"Device flow init failed: {json.dumps(flow)[:300]}"
                )
            print("\n" + "=" * 60)
            print(flow["message"])
            print("=" * 60 + "\n")

            result = self._app.acquire_token_by_device_flow(flow)
            if "access_token" not in result:
                raise TeamsAuthError(
                    f"Device flow login failed: {json.dumps(result)[:300]}"
                )
            self._save_cache()
            return result["access_token"]

    def _save_cache(self) -> None:
        if self._token_cache.has_state_changed:
            TOKEN_CACHE_FILE.write_text(self._token_cache.serialize())

    # ------------------------------------------------------------------
    # Graph calls
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        token = self.get_access_token(interactive=False)
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(
                f"Graph GET {path} failed: {r.status_code} {r.text[:300]}"
            )
        return r.json()

    def _post(self, path: str, json_body: Dict[str, Any]) -> Dict[str, Any]:
        token = self.get_access_token(interactive=False)
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=json_body,
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(
                f"Graph POST {path} failed: {r.status_code} {r.text[:300]}"
            )
        # Graph returns 201 + the created resource for chat-message sends; for
        # other endpoints a successful write may return 204 with no body.
        return r.json() if r.text else {}

    def send_chat_message(
        self,
        chat_id: str,
        text: str,
        content_type: str = "text",
    ) -> Dict[str, Any]:
        """
        Post a new top-level message into an existing chat.

        Teams 1:1 and group chats have no thread/reply hierarchy — every
        send is a standalone message in the chat, attributed to the
        signed-in user. So "reply to the person who sent us a message" is
        modelled as "send a new message into the chat that contained their
        message", which is what TeamsReplyNotifier does.

        Requires the ``ChatMessage.Send`` scope (see ``DEFAULT_SCOPES``).
        Re-run ``teams_probe.py`` once after upgrading if the cached token
        only has the older ``Chat.Read``-only scope set.
        """
        if content_type not in ("text", "html"):
            content_type = "text"
        body = {
            "body": {
                "contentType": content_type,
                "content": text,
            }
        }
        return self._post(f"/me/chats/{chat_id}/messages", body)

    def get_my_identity(self) -> Tuple[Optional[str], Optional[str]]:
        """Return (user_id, displayName) of the signed-in user, cached."""
        if self._my_user_id is None:
            try:
                me = self._get("/me")
                self._my_user_id = me.get("id")
                self._my_display_name = me.get("displayName")
            except Exception as e:
                print(f"  [teams] get_my_identity failed: {e}")
        return self._my_user_id, self._my_display_name

    def is_self_chat(self, chat_id: str) -> bool:
        """
        True iff the chat has exactly one member who is the signed-in user
        (Teams "Notes to self" / chat-with-yourself). Cached per chat.

        Used by TeamsMessageTrigger so that ``exclude_self`` skips messages
        you send in group/colleague chats but still fires for messages in
        your own self-chat (where you are simultaneously sender and the
        only recipient).
        """
        if chat_id in self._self_chat_cache:
            return self._self_chat_cache[chat_id]

        my_id, _ = self.get_my_identity()
        try:
            data = self._get(f"/me/chats/{chat_id}/members")
        except Exception as e:
            print(f"  [teams] is_self_chat check failed for {chat_id[:30]}: {e}")
            # Fail-safe: treat as NOT self-chat so default exclude behaviour wins
            self._self_chat_cache[chat_id] = False
            return False

        members = data.get("value", [])
        is_self = (
            len(members) == 1
            and bool(my_id)
            and members[0].get("userId") == my_id
        )
        self._self_chat_cache[chat_id] = is_self
        return is_self

    def list_chats(self, top: int = 50) -> List[Dict[str, Any]]:
        """
        List recent chats the signed-in user is a member of.

        Microsoft Graph caps `$top` at 50 per request for /me/chats. When
        ``top > 50`` we transparently follow ``@odata.nextLink`` to gather up
        to ``top`` chats across multiple round-trips.
        """
        out: List[Dict[str, Any]] = []
        page_size = min(top, 50)

        data = self._get(
            "/me/chats",
            params={
                "$top": str(page_size),
                "$orderby": "lastMessagePreview/createdDateTime desc",
                "$expand": "lastMessagePreview",
            },
        )
        out.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")

        while next_url and len(out) < top:
            # next_url is a full URL with its own query string already
            data = self._get(next_url)
            out.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")

        # Populate metadata cache so get_chat_messages doesn't need a second
        # GET per chat per poll just to learn topic/chatType.
        for ch in out:
            self._chat_meta_cache[ch["id"]] = {
                "topic": ch.get("topic") or "",
                "chatType": ch.get("chatType") or "",
            }

        return out[:top]

    def find_chat_by_topic(self, topic_substring: str) -> Optional[Dict[str, Any]]:
        """
        Find the first chat whose topic contains ``topic_substring`` (case-insensitive).
        Useful for filtering by a stable group-chat name (substring match,
        e.g. ``"Backend Team Standup"``).
        """
        needle = topic_substring.lower()
        for chat in self.list_chats(top=200):
            topic = (chat.get("topic") or "").lower()
            if needle in topic:
                return chat
        return None

    def get_chat_messages(
        self,
        chat_id: str,
        since: Optional[str] = None,
        top: int = 50,
    ) -> List[TeamsMessage]:
        """
        Fetch recent messages from a chat. If ``since`` is provided, only
        messages strictly newer than that ISO timestamp are returned.
        Results are returned in ascending createdDateTime order.
        """
        # Graph returns newest first; we filter client-side for >since
        params: Dict[str, str] = {"$top": str(top)}
        data = self._get(f"/me/chats/{chat_id}/messages", params=params)

        # Prefer cached metadata (populated by list_chats / find_chat_by_topic).
        # Only fetch chat metadata explicitly if we've never seen this chat —
        # rare in steady state, common only on first sight of a new chat.
        meta = self._chat_meta_cache.get(chat_id)
        if meta is None:
            try:
                chat_meta = self._get(f"/me/chats/{chat_id}")
                meta = {
                    "topic": chat_meta.get("topic") or "",
                    "chatType": chat_meta.get("chatType") or "",
                }
                self._chat_meta_cache[chat_id] = meta
            except Exception:
                meta = {"topic": "", "chatType": ""}
        topic = meta["topic"]
        chat_type = meta["chatType"]

        cutoff: Optional[datetime] = _parse_iso(since) if since else None

        msgs: List[TeamsMessage] = []
        for raw in data.get("value", []):
            ts = raw.get("createdDateTime")
            if not ts:
                continue
            if cutoff is not None and _parse_iso(ts) <= cutoff:
                continue
            # Skip system messages with no body
            body = raw.get("body") or {}
            html = body.get("content") or ""
            sender_user = (raw.get("from") or {}).get("user") or {}
            msgs.append(
                TeamsMessage(
                    id=raw.get("id", ""),
                    chat_id=chat_id,
                    chat_topic=topic,
                    chat_type=chat_type,
                    created_at=ts,
                    sender_name=sender_user.get("displayName") or "",
                    sender_id=sender_user.get("id") or "",
                    body_html=html,
                    body_text=_strip_html(html),
                    raw=raw,
                )
            )

        msgs.sort(key=lambda m: m.created_at)
        return msgs

    # ------------------------------------------------------------------
    # Watermarks (per-schedule, per-chat)
    # ------------------------------------------------------------------

    def get_watermark(self, owner: str, chat_id: str) -> Optional[str]:
        return self._watermarks.get(owner, {}).get(chat_id)

    def set_watermark(self, owner: str, chat_id: str, ts: str) -> None:
        with self._lock:
            ns = self._watermarks.setdefault(owner, {})
            current = ns.get(chat_id)
            if current is None or ts > current:
                ns[chat_id] = ts
                WATERMARK_FILE.write_text(json.dumps(self._watermarks, indent=2))

    def initialize_watermark_to_now(self, owner: str, chat_id: str) -> None:
        """
        Seed THIS owner's watermark with the latest message currently in a
        chat so that the trigger only fires for messages arriving AFTER it
        starts watching. Safe to call repeatedly; only sets if absent.
        """
        if self.get_watermark(owner, chat_id) is not None:
            return
        try:
            messages = self.get_chat_messages(chat_id, since=None, top=1)
            if messages:
                self.set_watermark(owner, chat_id, messages[-1].created_at)
            else:
                # Empty chat — use current UTC time as the floor
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                self.set_watermark(owner, chat_id, now)
        except Exception as e:
            print(f"  [teams] watermark init failed for {chat_id[:30]}: {e}")


# ----------------------------------------------------------------------
# Singleton accessor
# ----------------------------------------------------------------------

_INSTANCE: Optional[TeamsClient] = None
_INSTANCE_LOCK = threading.Lock()


def get_teams_client() -> TeamsClient:
    """Return the process-wide ``TeamsClient`` instance (lazy-init)."""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = TeamsClient()
    return _INSTANCE
