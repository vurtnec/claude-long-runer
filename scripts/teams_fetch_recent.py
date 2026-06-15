#!/usr/bin/env python3
"""
Teams Recent-Messages Fetch (read-only)
=======================================

Dumps Teams chat messages from the last N hours (default 24) as plain text
grouped by chat, suitable for piping into an LLM prompt.

Used by `schedules/teams_daily_todo.yaml` to gather context before the
inline task generates a daily TODO list.

Strictly read-only: only calls Chat.Read endpoints via
`scheduler.teams_client`. Never invokes `send_chat_message` or any other
write API.

Usage:
    python scripts/teams_fetch_recent.py                  # last 24h
    python scripts/teams_fetch_recent.py --hours 48
    python scripts/teams_fetch_recent.py --max-chats 100 --per-chat 200
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scheduler.teams_client import TeamsAuthError, get_teams_client


# Per-message body cap so a single mega-message can't blow out the prompt.
MAX_MSG_CHARS = 1200


def _truncate(text: str, limit: int = MAX_MSG_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f" …[truncated {len(text) - limit} chars]"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="Window size in hours")
    parser.add_argument(
        "--max-chats",
        type=int,
        default=50,
        help="Max number of recent chats to scan",
    )
    parser.add_argument(
        "--per-chat",
        type=int,
        default=50,
        help=(
            "Max messages to pull per chat (Graph caps /me/chats/{id}/messages "
            "$top at 50; values above 50 are clamped). If a chat returns the "
            "max and the oldest returned message is still inside the window, "
            "the chat is flagged as possibly truncated."
        ),
    )
    args = parser.parse_args()

    # Graph rejects $top > 50 on /me/chats/{id}/messages with HTTP 400. Clamp
    # silently so callers don't have to know the limit.
    per_chat = min(args.per_chat, 50)

    client = get_teams_client()

    # Surface auth problems before touching Graph — we run unattended in the
    # daemon, so device-code prompts would just hang the task.
    try:
        client.get_access_token(interactive=False)
    except TeamsAuthError as e:
        print(f"[teams] auth failed: {e}", file=sys.stderr)
        print(
            "Run `python teams_probe.py` once to refresh the device-code login.",
            file=sys.stderr,
        )
        return 2

    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    my_id, my_name = client.get_my_identity()

    try:
        chats = client.list_chats(top=args.max_chats)
    except Exception as e:
        print(f"[teams] list_chats failed: {e}", file=sys.stderr)
        return 3

    blocks: list[str] = []
    skipped_old = 0
    failed_reads = 0

    for chat in chats:
        chat_id = chat["id"]
        topic = (chat.get("topic") or "").strip()
        chat_type = chat.get("chatType") or "unknown"

        # Cheap skip: if Graph's lastMessagePreview is older than the cutoff,
        # don't bother fetching the full message list for this chat.
        preview = chat.get("lastMessagePreview") or {}
        preview_ts = preview.get("createdDateTime")
        if preview_ts and preview_ts <= cutoff_iso:
            skipped_old += 1
            continue

        try:
            messages = client.get_chat_messages(
                chat_id, since=cutoff_iso, top=per_chat
            )
        except Exception as e:
            failed_reads += 1
            print(
                f"[teams] read failed for chat {chat_id[:30]}: {e}",
                file=sys.stderr,
            )
            continue

        # Drop empty / system messages that strip to nothing.
        messages = [m for m in messages if (m.body_text or "").strip()]
        if not messages:
            continue

        # If Graph returned its hard cap AND every returned message is still
        # within the cutoff window, we may have older in-window messages we
        # never saw. Flag it so the LLM can mention "可能被截断" rather than
        # silently miss context.
        possibly_truncated = len(messages) >= per_chat

        display_topic = topic or f"(no topic, type={chat_type})"
        lines = [
            f"### Chat: {display_topic}",
            f"chat_id: {chat_id}",
            f"type: {chat_type}",
            f"message_count: {len(messages)}"
            + ("  [WARNING: hit per-chat fetch cap, older in-window messages may be missing]" if possibly_truncated else ""),
            "",
        ]
        for msg in messages:
            sender = msg.sender_name or "(system)"
            self_marker = " (me)" if my_id and msg.sender_id == my_id else ""
            text = _truncate(msg.body_text.strip())
            lines.append(f"[{msg.created_at}] {sender}{self_marker}: {text}")
        lines.append("")
        blocks.append("\n".join(lines))

    header = [
        f"# Teams 消息汇总 · 最近 {args.hours} 小时",
        f"cutoff_utc: {cutoff_iso}",
        f"account: {my_name or '(unknown)'}",
        f"chats_scanned: {len(chats)}",
        f"chats_with_new_msgs: {len(blocks)}",
        f"chats_skipped_old: {skipped_old}",
        f"chats_failed: {failed_reads}",
        "",
    ]

    print("\n".join(header))
    if not blocks:
        print("(过去 24 小时内没有新消息)")
        return 0
    print("\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
