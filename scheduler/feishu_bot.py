"""
Feishu Bot Server
==================

Receives group messages via the Feishu Open Platform WebSocket long-connection mode,
uses AgentClient's multi-turn conversation capability to maintain context,
and replies results back to the group chat.

Each group chat (chat_id) maintains a persistent AgentClient (Claude or Codex),
equivalent to an ongoing conversation in the Claude Code CLI or Codex CLI.

Prerequisites:
1. Create an enterprise app at open.feishu.cn and enable bot capabilities
2. Add permission: im:message
3. For event subscriptions, choose "Long Connection" mode and add im.message.receive_v1
4. Create a version, publish it, and add the bot to a group chat
5. Configure app_id and app_secret in scheduler_config.yaml

Usage:
    python -m scheduler.feishu_bot                     # Run standalone
    python -m scheduler.feishu_bot --config config.yaml # Specify config
"""

import argparse
import asyncio
import json
import sys
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

# Add parent directory for imports from the existing codebase
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from agent_protocol import (
    AgentClient,
    AgentEvent,
    EventType,
    Feature,
    create_agent_client,
)
from client import create_client  # kept for backward compat (schedule execution)

from .feishu_cards import (
    ACTION_BACKEND,
    ACTION_EFFORT,
    ACTION_KEY,
    ACTION_MODE,
    ACTION_MODEL,
    ACTION_PROJECT,
    ACTION_RESUME,
    ACTION_SCHEDULE,
    build_select_card,
    truncate,
)
from .schedule_loader import load_all_schedules, resolve_env_vars

# Session timeout: auto-disconnect after prolonged inactivity (only resets on restart)
SESSION_TIMEOUT_SECONDS = 50 * 60 * 60

# Mode aliases: user-friendly names → SDK permission_mode values
MODE_ALIASES = {
    "plan": "plan",  # Plan — suggest only, no execution
    "ask": "default",  # Ask before edits
    "auto": "auto",  # Auto-determine permissions (new in SDK 0.1.60)
    "edits": "acceptEdits",  # Auto-accept file edits (previously named 'auto')
}

# Reverse mapping for display: SDK permission_mode → user-friendly name
MODE_DISPLAY = {v: k for k, v in MODE_ALIASES.items()}

# Model aliases: user-friendly names → model IDs
MODEL_ALIASES = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}
MODEL_DISPLAY = {v: k for k, v in MODEL_ALIASES.items()}

# Backend aliases and per-backend model maps
BACKEND_ALIASES = {"claude", "codex"}

CODEX_MODEL_ALIASES = {
    # Verified via codex.models() — 2026-04
    "gpt-5.5": "gpt-5.5",  # default frontier
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.2": "gpt-5.2",
}
CODEX_MODEL_DISPLAY = {v: k for k, v in CODEX_MODEL_ALIASES.items()}

# Default models per backend
BACKEND_DEFAULT_MODELS = {
    "claude": "claude-opus-4-7",
    "codex": "gpt-5.5",
}

# Session history persistence
SESSION_HISTORY_FILE = Path.home() / ".claude-long-runner" / "feishu_sessions.json"
SESSION_HISTORY_MAX_PER_CHAT = 10

# Claude Code CLI sessions directory
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"


class ChatSession:
    """
    Manages a single group chat's agent session.

    Each chat_id maps to one ChatSession, which internally maintains a persistent
    AgentClient (either Claude or Codex).
    An asyncio.Lock ensures messages within the same chat are processed serially.
    """

    def __init__(self, chat_id: str, client: AgentClient, project_dir: Path):
        self.chat_id = chat_id
        self.client = client
        self.project_dir = project_dir
        self.connected = False
        self.created_at = datetime.now()
        self.last_active = datetime.now()
        self.lock = asyncio.Lock()
        # Mode detection and resume support
        self.session_id: str | None = None
        self.permission_mode: str = "default"
        self.first_message: str | None = None
        self.project_alias: str | None = None
        self.model: str = "claude-opus-4-7"
        self.backend: str = "claude"  # "claude" or "codex"
        self.custom_title: str | None = None
        # Progress tracking for /status command
        self.working_since: datetime | None = None  # set when agent starts processing
        self.tool_count: int = 0
        self.recent_tools: list[
            dict
        ] = []  # last 5: [{"name": "Edit", "input": "file.py ..."}]

    async def connect(self):
        """Establish connection to Claude."""
        await self.client.connect()
        self.connected = True
        self.last_active = datetime.now()
        print(f"  [Session {self.chat_id[:8]}] Connected")

    async def disconnect(self):
        """Disconnect from Claude and release resources."""
        if self.connected:
            try:
                await self.client.disconnect()
            except Exception as e:
                print(f"  [Session {self.chat_id[:8]}] Disconnect error: {e}")
            self.connected = False
            print(f"  [Session {self.chat_id[:8]}] Disconnected")

    def is_stale(self) -> bool:
        """Check if session has been inactive for too long."""
        elapsed = (datetime.now() - self.last_active).total_seconds()
        return elapsed > SESSION_TIMEOUT_SECONDS

    def touch(self):
        """Update last activity timestamp."""
        self.last_active = datetime.now()


class FeishuBotServer:
    """
    Feishu app bot server.

    Receives group messages via WebSocket long connection, supporting multi-turn conversations:
    - Each group chat maintains a persistent AgentClient (per-chat session)
    - Each user message is appended to the same conversation; Claude retains full context
    - Supports /new (reset conversation), /stop (stop session), /run (trigger schedule), etc.
    """

    def __init__(self, config: dict, base_dir: Path = None):
        self.base_dir = base_dir or Path(__file__).parent.parent
        self.config = config

        # Feishu app credentials
        feishu_config = config.get("notifications", {}).get("feishu", {})
        self.app_id = feishu_config.get("app_id", "")
        self.app_secret = feishu_config.get("app_secret", "")

        if not self.app_id or not self.app_secret:
            raise ValueError(
                "Feishu app_id and app_secret are required.\n"
                "Configure them in scheduler_config.yaml under notifications.feishu"
            )

        # Bot settings
        bot_config = config.get("feishu_bot", {})
        self.default_model = bot_config.get(
            "model",
            config.get("defaults", {}).get("model", "claude-opus-4-7"),
        )
        self.default_effort: str | None = bot_config.get(
            "effort",
            config.get("defaults", {}).get("effort"),
        )
        self.default_backend: str = bot_config.get("default_backend", "claude")
        # Default permission mode applied to new sessions when the user
        # hasn't called /mode in the chat.  Accepts either the friendly
        # alias (plan/ask/auto/edits) or the raw SDK value
        # (default/acceptEdits/plan/auto/bypassPermissions/dontAsk).
        # See PermissionMode in claude_agent_sdk/types.py.
        _raw_mode = bot_config.get("mode", config.get("defaults", {}).get("mode", "auto"))
        self.default_mode: str = MODE_ALIASES.get(_raw_mode, _raw_mode)
        # Projects: alias → absolute path, with per-project settings
        self.projects: Dict[str, Path] = {}
        self._project_restricted: Dict[str, bool] = {}  # alias → restricted flag
        self._project_models: Dict[str, str] = {}  # alias → default model
        self._project_efforts: Dict[str, str] = {}  # alias → default effort
        self._project_backends: Dict[str, str] = {}  # alias → default backend
        for alias, value in bot_config.get("projects", {}).items():
            if isinstance(value, str):
                # Legacy format: plain path string
                self.projects[alias] = Path(value).resolve()
            elif isinstance(value, dict):
                # New format: dict with path, restricted, model, backend
                self.projects[alias] = Path(value["path"]).resolve()
                if value.get("restricted"):
                    self._project_restricted[alias] = True
                if value.get("model"):
                    self._project_models[alias] = value["model"]
                if value.get("effort"):
                    self._project_efforts[alias] = value["effort"]
                if value.get("backend"):
                    self._project_backends[alias] = value["backend"]

        # Default project
        default_alias = bot_config.get("default_project", "")
        if default_alias and default_alias in self.projects:
            self.default_project_dir = self.projects[default_alias]
        elif self.projects:
            # Use first project as default
            self.default_project_dir = next(iter(self.projects.values()))
        else:
            self.default_project_dir = Path(
                bot_config.get("project_dir", str(self.base_dir))
            ).resolve()

        self.allowed_user_ids: List[str] = bot_config.get("allowed_user_ids", [])

        # Load schedules for /run command
        schedules_dir_name = config.get("daemon", {}).get("schedules_dir", "schedules")
        schedules_dir = self.base_dir / schedules_dir_name
        self.schedules = {}
        if schedules_dir.exists():
            for s in load_all_schedules(schedules_dir):
                self.schedules[s.name] = s

        # Lark API client (for sending messages)
        self.lark_client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        # Per-chat sessions and project selection
        self._sessions: Dict[str, ChatSession] = {}
        self._chat_project_dirs: Dict[str, Path] = {}  # chat_id → selected project_dir
        self._chat_models: Dict[str, str] = {}  # chat_id → model ID
        self._chat_efforts: Dict[str, str] = {}  # chat_id → effort level
        self._chat_modes: Dict[str, str] = {}  # chat_id → permission mode
        self._chat_backends: Dict[
            str, str
        ] = {}  # chat_id → backend ("claude" or "codex")

        # Pending images: buffer images until user sends a text message
        self._pending_images: Dict[str, List[str]] = {}  # chat_id → [image_file_paths]

        # Message dedup: Feishu may deliver the same event multiple times
        self._seen_message_ids: OrderedDict[str, float] = OrderedDict()
        self._seen_max_size = 500

        # asyncio event loop (set when start() is called)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """
        Start the WebSocket client in a background thread.

        Args:
            loop: asyncio event loop for scheduling async tasks
                  (e.g. session management). The lark WebSocket client
                  always creates its own loop internally.
        """
        self._loop = loop or asyncio.new_event_loop()

        # Build event handler
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_received)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )

        # Build WebSocket client
        ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        print(f"Starting Feishu bot (WebSocket long connection)...")
        print(f"  App ID: {self.app_id[:8]}...")
        print(f"  Default model: {self.default_model}")
        print(f"  Default mode:  {MODE_DISPLAY.get(self.default_mode, self.default_mode)} ({self.default_mode})")
        if self.projects:
            print(f"  Projects:")
            for alias, path in self.projects.items():
                tags = []
                if path == self.default_project_dir:
                    tags.append("default")
                if self._project_restricted.get(alias):
                    tags.append("restricted")
                if alias in self._project_models:
                    tags.append(f"model: {self._project_models[alias]}")
                tag_str = f" ({', '.join(tags)})" if tags else ""
                print(f"    {alias}: {path}{tag_str}")
        else:
            print(f"  Project dir: {self.default_project_dir}")
        print(f"  Session timeout: {SESSION_TIMEOUT_SECONDS // 60} minutes")
        print(f"  Loaded {len(self.schedules)} schedule(s) for /run command")
        if self.allowed_user_ids:
            print(f"  Allowed users: {self.allowed_user_ids}")
        else:
            print(f"  Allowed users: all (no whitelist configured)")
        print()

        # ws_client.start() is blocking and uses the module-level event loop
        # from lark_oapi.ws.client. When running inside the daemon's existing
        # asyncio loop, we must patch that module-level loop to a fresh one
        # in the background thread to avoid "This event loop is already running".
        def _run_ws():
            import lark_oapi.ws.client as ws_module

            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            ws_module.loop = new_loop  # Patch the module-level loop

            # macOS system proxy (127.0.0.1:7890) causes SSL handshake
            # failures (BAD_RECORD_MAC) when both requests and websockets
            # route through the HTTP CONNECT tunnel.  Setting no_proxy=*
            # makes all Python HTTP libraries (requests, websockets, urllib)
            # bypass the proxy.  This only affects Python code in this process;
            # curl subprocesses used by notifiers are unaffected.
            import os

            os.environ.setdefault("no_proxy", "*")

            # Also patch the SDK's requests reference to a no-proxy session
            # in case the env var is read too late by urllib.
            import requests as _req

            _no_proxy_session = _req.Session()
            _no_proxy_session.trust_env = False
            ws_module.requests = _no_proxy_session

            ws_client.start()

        thread = threading.Thread(target=_run_ws, daemon=True)
        thread.start()
        return thread

    def _on_message_received(self, data) -> None:
        """
        Handle incoming message event from Feishu.
        Called by lark-oapi SDK when im.message.receive_v1 fires.
        """
        try:
            message = data.event.message
            sender = data.event.sender

            # Dedup: Feishu may deliver the same event multiple times
            message_id = message.message_id
            now = time.time()
            if message_id in self._seen_message_ids:
                print(f"  [Feishu Bot] Duplicate message {message_id}, skipping")
                return
            self._seen_message_ids[message_id] = now
            while len(self._seen_message_ids) > self._seen_max_size:
                self._seen_message_ids.popitem(last=False)

            # Only handle text and image messages
            if message.message_type not in ("text", "image"):
                return

            # Extract sender info
            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"

            # Check whitelist
            if self.allowed_user_ids and sender_id not in self.allowed_user_ids:
                print(f"  Ignoring message from non-whitelisted user: {sender_id}")
                return

            chat_id = message.chat_id

            # Handle image messages: download, buffer, and wait for text
            if message.message_type == "image":
                self._handle_image_message(message, chat_id, message_id, sender_id)
                return

            # Parse text message content
            content = json.loads(message.content)
            raw_text = content.get("text", "").strip()

            # Remove @mention prefix (Feishu prepends @bot_name to the text)
            text = raw_text
            if hasattr(message, "mentions") and message.mentions:
                for mention in message.mentions:
                    mention_key = mention.key
                    text = text.replace(mention_key, "").strip()

            if not text:
                return

            print(
                f'\n[Feishu Bot] Received: "{text}" (from {sender_id}, chat {chat_id[:8]}...)'
            )

            # Route the message
            if text.startswith("/"):
                self._handle_command(text, chat_id, message_id)
            else:
                self._handle_free_prompt(text, chat_id, message_id)

        except Exception as e:
            print(f"[Feishu Bot] Error handling message: {e}")
            traceback.print_exc()

    def _on_card_action(self, event):
        """Handle card action callbacks (dropdown selections etc.).

        Routes by ``event.event.action.name`` to the same handlers used by
        slash commands.  The dropdown's ``option`` (selected value) is
        passed as the ``arg`` parameter, so handlers don't need to know
        whether they were invoked from text or a card click.

        Returns a ``P2CardActionTriggerResponse`` carrying a toast for
        instant feedback; the actual switch / resume / trigger work is
        scheduled async by the routed handler and produces a follow-up
        message in the chat.
        """
        try:
            inner = event.event
            action = getattr(inner, "action", None)
            context = getattr(inner, "context", None)

            # Diagnostic dump of the entire event for debugging field-name
            # drift across lark-oapi versions / card schema 2.0.
            try:
                raw = lark.JSON.marshal(inner)
                print(f"\n[Feishu Bot] Card action raw event: {raw}")
            except Exception:
                pass

            chat_id = getattr(context, "open_chat_id", None) if context else None
            message_id = getattr(context, "open_message_id", None) if context else None
            operator = getattr(inner, "operator", None)
            sender_open_id = getattr(operator, "open_id", None) if operator else None

            # Routing key: card schema 2.0 only echoes back the ``name``
            # attribute when the component sits in a form container, so we
            # read the key from ``action.value[ACTION_KEY]`` (always
            # present — we put it there in build_select_card) and fall
            # back to ``action.name`` for backward compat.
            action_value_dict = getattr(action, "value", None) if action else None
            action_name = ""
            if isinstance(action_value_dict, dict):
                v = action_value_dict.get(ACTION_KEY)
                if v:
                    action_name = str(v).strip()
            if not action_name and action:
                action_name = (getattr(action, "name", "") or "").strip()

            # Selected value: ``action.option`` is the single-select picked
            # value in card schema 2.0.  Fall back to ``input_value`` for
            # input components.
            selected = ""
            if action:
                opt = getattr(action, "option", None)
                if opt:
                    selected = str(opt).strip()
                if not selected:
                    inp = getattr(action, "input_value", None)
                    if inp:
                        selected = str(inp).strip()

            print(
                f"[Feishu Bot] Card action parsed: name='{action_name}' "
                f"option='{selected}' chat={chat_id[:8] if chat_id else '?'}... "
                f"msg={message_id[:8] if message_id else '?'}..."
            )

            # Whitelist check — same rule as text messages.  No-op when no
            # whitelist is configured.
            if (
                self.allowed_user_ids
                and sender_open_id
                and sender_open_id not in self.allowed_user_ids
            ):
                print(
                    f"  Ignoring card action from non-whitelisted user: {sender_open_id}"
                )
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": "Not authorized."}}
                )

            # Diagnose which specific field is missing instead of a generic
            # "invalid payload" — makes future drift easy to spot in logs.
            missing = []
            if not chat_id:
                missing.append("chat_id")
            if not message_id:
                missing.append("message_id")
            if not action_name:
                missing.append("action.name")
            if not selected:
                missing.append("action.option")
            if missing:
                msg = f"Missing card fields: {', '.join(missing)}"
                print(f"  [Feishu Bot] {msg}")
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": msg}}
                )

            # Route by component name — same `name` constants used by
            # build_select_card() in feishu_cards.py.
            if action_name == ACTION_PROJECT:
                self._handle_project(selected, chat_id, message_id)
                toast_text = f"Switching to project: {selected}"
            elif action_name == ACTION_MODE:
                self._handle_mode(selected, chat_id, message_id)
                toast_text = f"Switching mode: {selected}"
            elif action_name == ACTION_RESUME:
                self._handle_resume(selected, chat_id, message_id)
                toast_text = "Resuming session…"
            elif action_name == ACTION_SCHEDULE:
                self._trigger_schedule(selected, chat_id, message_id)
                toast_text = f"Triggering: {selected}"
            elif action_name == ACTION_BACKEND:
                self._handle_backend(selected, chat_id, message_id)
                toast_text = f"Switching backend: {selected}"
            elif action_name == ACTION_MODEL:
                self._handle_model(selected, chat_id, message_id)
                toast_text = f"Switching model: {selected}"
            elif action_name == ACTION_EFFORT:
                self._handle_effort(selected, chat_id, message_id)
                toast_text = f"Switching effort: {selected}"
            else:
                print(f"  [Feishu Bot] Unknown card action name: {action_name}")
                return P2CardActionTriggerResponse(
                    {
                        "toast": {
                            "type": "error",
                            "content": f"Unknown action: {action_name}",
                        }
                    }
                )

            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": toast_text}}
            )

        except Exception as e:
            print(f"[Feishu Bot] Error handling card action: {e}")
            traceback.print_exc()
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": f"Error: {e}"}}
            )

    # Bot's own slash commands — anything else gets forwarded to the agent
    # (so users can run Claude/Codex custom slash commands like /init, /commit, etc.)
    BOT_COMMANDS = frozenset(
        {
            "/help",
            "/list",
            "/new",
            "/stop",
            "/cancel",
            "/status",
            "/project",
            "/mode",
            "/model",
            "/effort",
            "/backend",
            "/rename",
            "/resume",
            "/run",
        }
    )

    def _handle_command(self, text: str, chat_id: str, message_id: str):
        """
        Handle slash commands.

        - Commands in BOT_COMMANDS are handled locally by the bot.
        - Any other /xxx is forwarded to the agent as a prompt, so users
          can invoke Claude's custom slash commands (~/.claude/commands/*.md
          or <project>/.claude/commands/*.md), Codex slash commands, etc.
        """
        parts = text.split(None, 2)
        command = parts[0].lower()

        # Forward unknown commands to the agent — they may be Claude/Codex
        # internal slash commands (e.g. /init, /commit, /compact).
        if command not in self.BOT_COMMANDS:
            print(f"  [Forward] '{command}' is not a bot command — sending to agent")
            self._handle_free_prompt(text, chat_id, message_id)
            return

        if command == "/help":
            self._send_help(chat_id, message_id)
        elif command == "/list":
            self._send_schedule_list(chat_id, message_id)
        elif command == "/new":
            self._handle_new_session(chat_id, message_id)
        elif command == "/stop":
            self._handle_stop_session(chat_id, message_id)
        elif command == "/cancel":
            self._handle_cancel(chat_id, message_id)
        elif command == "/status":
            self._handle_status(chat_id, message_id)
        elif command == "/project":
            alias = parts[1] if len(parts) >= 2 else None
            self._handle_project(alias, chat_id, message_id)
        elif command == "/mode":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_mode(arg, chat_id, message_id)
        elif command == "/model":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_model(arg, chat_id, message_id)
        elif command == "/effort":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_effort(arg, chat_id, message_id)
        elif command == "/backend":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_backend(arg, chat_id, message_id)
        elif command == "/rename":
            # Use split(None, 1) to keep the full title as a single string
            title = text.split(None, 1)[1] if len(text.split(None, 1)) >= 2 else None
            self._handle_rename(title, chat_id, message_id)
        elif command == "/resume":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_resume(arg, chat_id, message_id)
        elif command == "/run":
            if len(parts) < 2:
                self._reply_text(message_id, "Usage: /run <schedule_name>")
                return
            schedule_name = parts[1]
            self._trigger_schedule(schedule_name, chat_id, message_id)
        # No else: unrecognized commands were forwarded to the agent at the
        # top of this method.

    def _handle_new_session(self, chat_id: str, message_id: str):
        """Handle /new command: archive current session and reset."""
        session = self._sessions.get(chat_id)
        if session and session.session_id:
            # Archive current session to history before closing
            self._save_session_to_history(chat_id, session)

        # Clear any buffered images
        self._pending_images.pop(chat_id, None)

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close_session(chat_id), self._loop)
        self._reply_text(
            message_id,
            "Session reset. Next message starts a new conversation.\n"
            "Use /resume to view and restore previous sessions.",
        )

    def _handle_stop_session(self, chat_id: str, message_id: str):
        """Handle /stop command: stop and close the current chat session."""
        if chat_id in self._sessions:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._close_session(chat_id), self._loop
                )
            self._reply_text(message_id, "Session stopped and disconnected.")
        else:
            self._reply_text(message_id, "No active session for this chat.")

    def _handle_cancel(self, chat_id: str, message_id: str):
        """Handle /cancel command: interrupt the current agent request without closing session."""
        session = self._sessions.get(chat_id)
        if not session:
            self._reply_text(message_id, "No active session.")
            return
        if not session.lock.locked():
            self._reply_text(message_id, "Agent is not processing any request.")
            return
        try:
            session.client.interrupt()
            self._reply_text(
                message_id, "Request interrupted. You can send a new message."
            )
        except Exception as e:
            self._reply_text(message_id, f"Failed to interrupt: {e}")

    def _handle_status(self, chat_id: str, message_id: str):
        """Handle /status command: show current session working state."""
        session = self._sessions.get(chat_id)
        if not session:
            self._reply_text(message_id, "No active session.")
            return

        if not session.lock.locked() or not session.working_since:
            mode_display = MODE_DISPLAY.get(
                session.permission_mode, session.permission_mode
            )
            self._reply_text(message_id, f"Idle. ({mode_display} mode)")
            return

        elapsed = (datetime.now() - session.working_since).total_seconds()
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        mode_display = MODE_DISPLAY.get(
            session.permission_mode, session.permission_mode
        )

        lines = [
            f"Working for {mins}m{secs}s | {session.tool_count} tool calls | {mode_display} mode\n"
        ]
        if session.recent_tools:
            lines.append("Recent tools:")
            for t in session.recent_tools:
                name = t.get("name", "?")
                inp = t.get("input", "")
                if inp:
                    lines.append(f"  {name}({inp})")
                else:
                    lines.append(f"  {name}")

        self._reply_text(message_id, "\n".join(lines))

    def _handle_project(self, alias: Optional[str], chat_id: str, message_id: str):
        """Handle /project command: show or switch project."""
        if not self.projects:
            self._reply_text(
                message_id,
                "No projects configured.\n"
                "Add projects in scheduler_config.yaml under feishu_bot.projects",
            )
            return

        if alias is None:
            # Send a dropdown card so the user can pick.  The text-form
            # ``/project <alias>`` flow still works (handled by the
            # branch below).
            current_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
            current_alias = None
            for a, p in self.projects.items():
                if p == current_dir:
                    current_alias = a
                    break

            options = []
            for a, p in self.projects.items():
                marker = "  ← current" if p == current_dir else ""
                options.append({"text": f"{a}{marker}", "value": a})

            intro = (
                f"**Current project:** `{current_alias or current_dir}`\n\n"
                f"Pick a project to switch to:"
            )
            card_json = build_select_card(
                intro_markdown=intro,
                placeholder="Select a project…",
                options=options,
                action_name=ACTION_PROJECT,
                initial_value=current_alias,
            )
            fallback = (
                f"Current project: {current_alias or current_dir}\n"
                + "Available: "
                + ", ".join(self.projects.keys())
            )
            self._reply_card_json(message_id, card_json, fallback_text=fallback)
            return

        if alias not in self.projects:
            available = ", ".join(self.projects.keys())
            self._reply_text(
                message_id,
                f"Unknown project: {alias}\nAvailable: {available}",
            )
            return

        # Switch project: update mapping and close existing session
        new_dir = self.projects[alias]
        self._chat_project_dirs[chat_id] = new_dir

        if chat_id in self._sessions:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._close_session(chat_id), self._loop
                )

        self._reply_text(
            message_id,
            f"Switched to project: {alias}\n{new_dir}\n\nSession reset. Next message uses the new project context.",
        )

    def _handle_image_message(
        self, message, chat_id: str, message_id: str, sender_id: str
    ):
        """Handle image message: download, save locally, and buffer for next text message."""
        try:
            content = json.loads(message.content)
            image_key = content.get("image_key")
            if not image_key:
                print(f"  [Feishu Bot] Image message without image_key, skipping")
                return

            print(
                f"\n[Feishu Bot] Received image (from {sender_id}, chat {chat_id[:8]}...)"
            )

            # Download and save image
            image_path = self._download_and_save_image(message_id, image_key)
            if not image_path:
                self._reply_text(
                    message_id, "Failed to download image. Please try again."
                )
                return

            # Buffer the image path for this chat
            if chat_id not in self._pending_images:
                self._pending_images[chat_id] = []
            self._pending_images[chat_id].append(image_path)

            count = len(self._pending_images[chat_id])
            if count == 1:
                self._reply_text(
                    message_id, "Image received. What would you like me to do with it?"
                )
            else:
                self._reply_text(
                    message_id,
                    f"{count} images received. What would you like me to do with them?",
                )

            print(f"  [Feishu Bot] Image saved: {image_path} (pending: {count})")

        except Exception as e:
            print(f"[Feishu Bot] Error handling image message: {e}")
            traceback.print_exc()
            self._reply_text(message_id, "Failed to process image. Please try again.")

    def _download_and_save_image(
        self, message_id: str, image_key: str
    ) -> Optional[str]:
        """Download image from Feishu message and save to tmp_images/ directory."""
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )

            response = self.lark_client.im.v1.message_resource.get(request)

            if not response.success():
                print(
                    f"  [Feishu Bot] Image download failed: {response.code} - {response.msg}"
                )
                return None

            # Determine file extension from response filename or default to .png
            file_name = getattr(response, "file_name", None) or ""
            ext = Path(file_name).suffix if file_name else ".png"
            if not ext:
                ext = ".png"

            # Save to tmp_images/ under project root
            tmp_dir = self.base_dir / "tmp_images"
            tmp_dir.mkdir(exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            short_id = message_id[-8:] if len(message_id) > 8 else message_id
            save_path = tmp_dir / f"img_{timestamp}_{short_id}{ext}"

            with open(save_path, "wb") as f:
                f.write(response.file.read())

            return str(save_path.resolve())

        except Exception as e:
            print(f"  [Feishu Bot] Error downloading image: {e}")
            traceback.print_exc()
            return None

    def _handle_free_prompt(self, text: str, chat_id: str, message_id: str):
        """Handle free-form prompt — send to persistent Claude session."""
        # Merge pending images into the prompt if any
        pending = self._pending_images.pop(chat_id, [])
        if pending:
            image_lines = []
            for i, path in enumerate(pending, 1):
                if len(pending) == 1:
                    image_lines.append(f"The user sent an image, saved at: {path}")
                else:
                    image_lines.append(f"Image {i}: {path}")
            image_context = "\n".join(image_lines)
            text = (
                f"{image_context}\n"
                f"Use the Read tool to view the image(s) above.\n\n"
                f"User message: {text}"
            )

        # Send "processing" acknowledgment immediately
        self._reply_text(message_id, "Received. Processing...")

        # Run task asynchronously in the event loop
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._execute_with_timeout(text, chat_id, message_id),
                self._loop,
            )
        else:
            thread = threading.Thread(
                target=self._run_async_task,
                args=(text, chat_id, message_id),
                daemon=True,
            )
            thread.start()

    def _run_async_task(self, text: str, chat_id: str, message_id: str):
        """Run async task in a new event loop (fallback for thread mode)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                self._execute_with_timeout(text, chat_id, message_id)
            )
        finally:
            loop.close()

    async def _execute_with_timeout(self, text: str, chat_id: str, message_id: str):
        """Wrapper that adds a 30-minute timeout to _execute_and_reply."""
        try:
            await asyncio.wait_for(
                self._execute_and_reply(text, chat_id, message_id),
                timeout=1800,  # 30 minutes
            )
        except asyncio.TimeoutError:
            self._send_message(
                chat_id,
                "Response timed out after 30 minutes.\n"
                "The session is still active — try sending a shorter request.",
            )

    async def _get_or_create_session(self, chat_id: str) -> ChatSession:
        """Get existing session or create a new one for the chat."""
        # Clean up stale sessions opportunistically
        await self._cleanup_stale_sessions()

        if chat_id in self._sessions:
            session = self._sessions[chat_id]
            if session.connected:
                session.touch()
                return session
            # Session exists but disconnected, remove and recreate
            del self._sessions[chat_id]

        # Determine project dir, model, mode, and restriction for this chat
        project_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
        project_alias = self._get_project_alias(project_dir)

        # Model priority: user /model override > per-project model > global default
        if chat_id in self._chat_models:
            model = self._chat_models[chat_id]
        elif project_alias and project_alias in self._project_models:
            model = self._project_models[project_alias]
        else:
            model = self.default_model

        # Effort priority: user /effort override > per-project effort > global default
        if chat_id in self._chat_efforts:
            effort = self._chat_efforts[chat_id]
        elif project_alias and project_alias in self._project_efforts:
            effort = self._project_efforts[project_alias]
        else:
            effort = self.default_effort

        mode = self._chat_modes.get(chat_id, self.default_mode)
        restricted = self._project_restricted.get(project_alias or "", False)

        # Backend priority: user /backend override > per-project backend > global default
        if chat_id in self._chat_backends:
            backend = self._chat_backends[chat_id]
        elif project_alias and project_alias in self._project_backends:
            backend = self._project_backends[project_alias]
        else:
            backend = self.default_backend

        # If model was never explicitly set and backend changed, use backend's default model
        if chat_id not in self._chat_models and (
            not project_alias or project_alias not in self._project_models
        ):
            model = BACKEND_DEFAULT_MODELS.get(backend, model)

        # Create new session
        restriction_tag = " [RESTRICTED]" if restricted else ""
        effort_tag = f", effort: {effort}" if effort else ""
        print(
            f"  [Session] Creating new session for chat {chat_id[:8]}... (backend: {backend}, project: {project_dir}, model: {model}, mode: {mode or 'default'}{effort_tag}{restriction_tag})"
        )
        client = create_agent_client(
            backend=backend,
            project_dir=str(project_dir),
            model=model,
            permission_mode=mode,
            restricted=restricted,
            effort=effort,
        )

        session = ChatSession(chat_id=chat_id, client=client, project_dir=project_dir)
        session.model = model
        session.backend = backend
        if mode:
            session.permission_mode = mode
        session.project_alias = self._get_project_alias(project_dir)
        await session.connect()
        self._sessions[chat_id] = session
        return session

    async def _close_session(self, chat_id: str):
        """Close and remove a session."""
        session = self._sessions.pop(chat_id, None)
        if session:
            await session.disconnect()

    async def _cleanup_stale_sessions(self):
        """Remove sessions that have been inactive for too long."""
        stale_ids = [cid for cid, s in self._sessions.items() if s.is_stale()]
        for cid in stale_ids:
            print(f"  [Session] Cleaning up stale session: {cid[:8]}...")
            await self._close_session(cid)

    async def _execute_and_reply(self, prompt: str, chat_id: str, message_id: str):
        """Send prompt to persistent agent session and reply with result."""
        start_time = datetime.now()
        try:
            session = await self._get_or_create_session(chat_id)

            async with session.lock:
                # Send the prompt to the existing conversation
                await session.client.send_message(prompt)

                # Reset progress tracking
                session.working_since = datetime.now()
                session.tool_count = 0
                session.recent_tools = []

                # Collect response via unified AgentEvent stream
                response_text = ""
                exit_plan_attempted = False
                async for event in session.client.receive_events():
                    if event.type == EventType.TEXT:
                        response_text += event.text
                        print(event.text, end="", flush=True)
                        # Log actual model if provided
                        actual_model = event.metadata.get("model")
                        if actual_model:
                            print(f"  [Model] Actual model in response: {actual_model}")

                    elif event.type == EventType.TOOL_USE:
                        if event.tool_name == "ExitPlanMode":
                            exit_plan_attempted = True
                        session.tool_count += 1
                        # Track tool name + input summary for /status
                        input_summary = ""
                        if event.tool_input:
                            input_summary = str(event.tool_input)
                            if len(input_summary) > 80:
                                input_summary = input_summary[:80] + "..."
                        session.recent_tools.append(
                            {"name": event.tool_name, "input": input_summary}
                        )
                        if len(session.recent_tools) > 5:
                            session.recent_tools = session.recent_tools[-5:]
                        print(f"\n[Tool: {event.tool_name}]", flush=True)
                        if event.tool_input:
                            input_str = str(event.tool_input)
                            if len(input_str) > 200:
                                print(f"   Input: {input_str[:200]}...", flush=True)
                            else:
                                print(f"   Input: {input_str}", flush=True)

                    elif event.type == EventType.TOOL_RESULT:
                        result_content = event.result_content or ""
                        if "blocked" in result_content.lower():
                            print(f"   [BLOCKED] {result_content}", flush=True)
                        elif event.is_error:
                            print(f"   [Error] {result_content[:500]}", flush=True)
                        else:
                            print("   [Done]", flush=True)

                    elif event.type == EventType.SYSTEM:
                        init_mode = event.metadata.get("permission_mode")
                        if init_mode:
                            session.permission_mode = init_mode
                        init_session_id = event.metadata.get("session_id")
                        if init_session_id:
                            session.session_id = init_session_id
                        print(
                            f"  [System] mode={init_mode}, session={init_session_id and init_session_id[:8]}..."
                        )

                    elif event.type == EventType.RESULT:
                        num_turns = event.metadata.get("num_turns", "?")
                        is_error = event.metadata.get("is_error", False)
                        result_session_id = event.metadata.get("session_id")
                        if result_session_id:
                            session.session_id = result_session_id
                        print(
                            f"\n  [Result] turns={num_turns}, error={is_error}, session={session.session_id and session.session_id[:8]}..."
                        )

                    elif event.type == EventType.ERROR:
                        error_msg = event.metadata.get("error", "Unknown error")
                        print(f"  [Error] {error_msg}", flush=True)

                # Done working
                session.working_since = None

                print("\n" + "-" * 70)

                # Record first message for session summary
                if session.first_message is None:
                    session.first_message = prompt

                # Save session to history after each response
                session.touch()
                self._save_session_to_history(chat_id, session)

            duration = datetime.now() - start_time
            duration_str = str(duration).split(".")[0]

            # Build mode display string
            mode_display = MODE_DISPLAY.get(
                session.permission_mode, session.permission_mode
            )

            if response_text:
                # Truncate for Feishu's message size limit
                if len(response_text) > 25000:
                    response_text = response_text[:25000] + "\n\n... (truncated)"
                reply = f"{response_text}\n\n({duration_str} | mode: {mode_display})"
            else:
                reply = f"Done ({duration_str} | mode: {mode_display})"

            # In plan mode, agent's ExitPlanMode tool call is blocked (no approval
            # channel in the bot). Tell the user to switch modes instead.
            if exit_plan_attempted and session.permission_mode == "plan":
                reply += (
                    "\n\nℹ️ Plan 模式下计划无法自动执行。确认后请发送 "
                    "`/mode auto`（自动编辑）或 `/mode ask`（逐步确认）后再回复继续。"
                )

            self._send_message(chat_id, reply)

        except Exception as e:
            print(f"  [Error] {e}")
            traceback.print_exc()
            # If session is broken, close it so next message creates a fresh one
            await self._close_session(chat_id)
            self._send_message(
                chat_id,
                f"Error: {e}\n\nSession has been reset. Please try again.",
            )

    def _trigger_schedule(self, schedule_name: str, chat_id: str, message_id: str):
        """Trigger a predefined schedule by name."""
        if schedule_name not in self.schedules:
            available = ", ".join(self.schedules.keys()) or "(none)"
            self._reply_text(
                message_id,
                f"Schedule not found: {schedule_name}\nAvailable: {available}",
            )
            return

        schedule = self.schedules[schedule_name]
        self._reply_text(
            message_id,
            f"Received. Executing schedule: {schedule_name}\n{schedule.description}",
        )

        # Execute the schedule's inline task
        if schedule.task.task_type == "inline" and schedule.task.prompt:
            prompt = schedule.task.prompt
            today_str = datetime.now().strftime("%Y-%m-%d")
            prompt = prompt.replace("{{today}}", today_str)
            prompt = prompt.replace("{{now}}", datetime.now().isoformat())

            model = schedule.task.model or self.default_model
            project_dir = Path(schedule.task.project_dir).resolve()
            default_timeout = self.config.get("defaults", {}).get("timeout_minutes", 30)
            timeout = schedule.timeout_minutes or default_timeout
            max_turns = schedule.task.max_turns or 5

            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._execute_schedule_and_reply(
                        prompt,
                        project_dir,
                        model,
                        chat_id,
                        schedule_name,
                        timeout,
                        max_turns,
                    ),
                    self._loop,
                )
            else:
                thread = threading.Thread(
                    target=lambda: asyncio.run(
                        self._execute_schedule_and_reply(
                            prompt,
                            project_dir,
                            model,
                            chat_id,
                            schedule_name,
                            timeout,
                            max_turns,
                        )
                    ),
                    daemon=True,
                )
                thread.start()
        elif schedule.task.task_type == "standard" and schedule.task.name:
            model = schedule.task.model or self.default_model
            project_dir = Path(schedule.task.project_dir).resolve()
            max_iters = schedule.task.max_iterations or 10
            default_timeout = self.config.get("defaults", {}).get("timeout_minutes", 30)
            timeout = schedule.timeout_minutes or default_timeout

            # Build template vars (same as daemon)
            today_str = datetime.now().strftime("%Y-%m-%d")
            template_vars = {"today": today_str, "now": datetime.now().isoformat()}
            resolved_params = {}
            for key, value in schedule.task.params.items():
                if isinstance(value, str):
                    for tvar, tval in template_vars.items():
                        value = value.replace(f"{{{{{tvar}}}}}", str(tval))
                resolved_params[key] = value

            thread = threading.Thread(
                target=lambda: asyncio.run(
                    self._execute_standard_and_reply(
                        schedule.task.name,
                        resolved_params,
                        project_dir,
                        model,
                        max_iters,
                        chat_id,
                        schedule_name,
                        timeout,
                    )
                ),
                daemon=True,
            )
            thread.start()
        else:
            self._send_message(
                chat_id,
                f"Schedule {schedule_name}: unsupported task type.",
            )

    async def _execute_schedule_and_reply(
        self,
        prompt: str,
        project_dir: Path,
        model: str,
        chat_id: str,
        schedule_name: str,
        timeout_minutes: int = 15,
        max_turns: int = 5,
    ):
        """Execute a schedule's inline task (one-shot, no session persistence)."""
        from .inline_executor import run_inline_task

        start_time = datetime.now()
        try:
            result = await asyncio.wait_for(
                run_inline_task(
                    prompt=prompt,
                    project_dir=project_dir,
                    model=model,
                    max_turns=max_turns,
                ),
                timeout=timeout_minutes * 60,
            )

            duration = datetime.now() - start_time
            duration_str = str(duration).split(".")[0]

            if result["success"]:
                response_text = result.get("response_text", "")
                if len(response_text) > 25000:
                    response_text = response_text[:25000] + "\n\n... (truncated)"
                reply = f"[{schedule_name}] Done ({duration_str})\n\n{response_text}"
            else:
                error = result.get("error", "Unknown error")
                reply = f"[{schedule_name}] Failed ({duration_str})\nError: {error}"

            self._send_message(chat_id, reply)

        except asyncio.TimeoutError:
            duration = datetime.now() - start_time
            duration_str = str(duration).split(".")[0]
            self._send_message(
                chat_id,
                f"[{schedule_name}] Timed out after {timeout_minutes} minutes ({duration_str})",
            )
        except Exception as e:
            self._send_message(chat_id, f"[{schedule_name}] Error: {e}")
            traceback.print_exc()

    async def _execute_standard_and_reply(
        self,
        task_name: str,
        task_params: Dict[str, Any],
        project_dir: Path,
        model: str,
        max_iterations: int,
        chat_id: str,
        schedule_name: str,
        timeout_minutes: int = 120,
    ):
        """Execute a standard long-runner task and send result to chat."""
        from long_run_executor import run_long_task

        start_time = datetime.now()
        try:
            project_dir.mkdir(parents=True, exist_ok=True)

            success = await asyncio.wait_for(
                run_long_task(
                    task_name=task_name,
                    task_params=task_params,
                    project_dir=project_dir,
                    model=model,
                    max_iterations=max_iterations,
                    resume=False,
                ),
                timeout=timeout_minutes * 60,
            )

            duration = datetime.now() - start_time
            duration_str = str(duration).split(".")[0]

            # Read state file for final result
            task_dir_name = Path(task_name).name
            state_file = project_dir / f"{task_dir_name}_state.json"
            last_response = ""
            iterations = 0
            if state_file.exists():
                try:
                    with open(state_file) as f:
                        state_data = json.load(f)
                    last_response = state_data.get("last_response", "")
                    iterations = state_data.get("iteration", 0)
                except (json.JSONDecodeError, IOError):
                    pass

            if success:
                summary = (
                    last_response[:3000] if last_response else "No response captured"
                )
                reply = (
                    f"[{schedule_name}] Task completed successfully\n"
                    f"Iterations: {iterations} | Duration: {duration_str}\n\n"
                    f"{summary}"
                )
            else:
                reply = (
                    f"[{schedule_name}] Task failed\n"
                    f"Iterations: {iterations} | Duration: {duration_str}\n\n"
                    f"Success conditions not met.\n"
                    f"{last_response[:2000] if last_response else ''}"
                )

            self._send_message(chat_id, reply)

        except asyncio.TimeoutError:
            duration = datetime.now() - start_time
            duration_str = str(duration).split(".")[0]
            self._send_message(
                chat_id,
                f"[{schedule_name}] Timed out after {timeout_minutes} minutes ({duration_str})",
            )
        except Exception as e:
            duration = datetime.now() - start_time
            duration_str = str(duration).split(".")[0]
            self._send_message(
                chat_id,
                f"[{schedule_name}] Error ({duration_str})\n{e}",
            )
            traceback.print_exc()

    def _send_help(self, chat_id: str, message_id: str):
        """Send help message."""
        help_text = (
            "Available commands:\n\n"
            "/help  — Show this help\n"
            "/project — Show current project / switch project\n"
            "/backend [claude|codex] — Show or switch agent backend\n"
            "/mode [plan|ask|auto|edits] — Show or switch permission mode\n"
            "/model [opus|sonnet|haiku|o3|o4-mini|gpt-5] — Show or switch model\n"
            "/effort [low|medium|high|xhigh|max] — Show or switch effort level\n"
            "/rename <title> — Rename current session\n"
            "/resume [number] — List recent sessions / resume by number\n"
            "/list  — List available schedules\n"
            "/run <name> — Run a schedule (inline or standard task)\n"
            "/new   — Reset conversation (start fresh)\n"
            "/stop  — Stop and disconnect current session\n"
            "/cancel — Interrupt current request (keep session)\n"
            "/status — Check if agent is working and what it's doing\n\n"
            "Send a message to chat with the agent.\n"
            "Multi-turn conversation is supported — context is preserved.\n"
            "Each reply shows the current mode automatically.\n\n"
            "Tip: any /command not listed above (e.g. /init, /commit, /compact)\n"
            "is forwarded to the agent as a prompt, so you can use Claude's\n"
            "or Codex's custom slash commands directly."
        )
        self._reply_text(message_id, help_text)

    def _send_schedule_list(self, chat_id: str, message_id: str):
        """Send a dropdown card listing available schedules.

        Picking an option triggers ``_trigger_schedule`` for that name
        via the card-action callback, replacing the old two-step
        ``/list`` then ``/run <name>`` flow.
        """
        if not self.schedules:
            self._reply_text(message_id, "No schedules loaded.")
            return

        options = []
        for name, sched in self.schedules.items():
            desc = truncate(sched.description or "", 35)
            label = f"{name} — {desc}" if desc else name
            options.append({"text": label, "value": name})

        intro = (
            f"**Schedules** _(loaded: {len(self.schedules)})_\n\nPick one to run now:"
        )
        card_json = build_select_card(
            intro_markdown=intro,
            placeholder="Select a schedule…",
            options=options,
            action_name=ACTION_SCHEDULE,
        )
        fallback = "Available schedules: " + ", ".join(self.schedules.keys())
        self._reply_card_json(message_id, card_json, fallback_text=fallback)

    # ── Card builder (schema 2.0 — full Markdown support) ──────────────

    def _build_interactive_card(self, text: str) -> str:
        """Build a Feishu card JSON using schema 2.0 with the ``markdown`` tag.

        Schema 2.0 uses ``body.elements`` (not top-level ``elements``) and
        the ``"tag": "markdown"`` element, which natively renders headings,
        code blocks, tables, blockquotes, etc. — no manual conversion needed.
        """
        card = {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": text,
                    }
                ],
            },
        }
        return json.dumps(card)

    def _reply_text(self, message_id: str, text: str):
        """Reply to a specific message with interactive card (Markdown rendered)."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(self._build_interactive_card(text))
                .build()
            )
            .build()
        )

        response = self.lark_client.im.v1.message.reply(request)
        if not response.success():
            print(f"  [Feishu Bot] Reply failed: {response.code} - {response.msg}")
            self._reply_plain_text(message_id, text)

    def _reply_card_json(
        self, message_id: str, card_json: str, fallback_text: str = ""
    ):
        """Reply with a prebuilt interactive card JSON (e.g. from feishu_cards).

        Falls back to plain text if the card send fails.
        """
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(card_json)
                .build()
            )
            .build()
        )

        response = self.lark_client.im.v1.message.reply(request)
        if not response.success():
            print(f"  [Feishu Bot] Card reply failed: {response.code} - {response.msg}")
            if fallback_text:
                self._reply_plain_text(message_id, fallback_text)

    def _reply_plain_text(self, message_id: str, text: str):
        """Fallback: reply as plain text when interactive card fails."""
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        response = self.lark_client.im.v1.message.reply(request)
        if not response.success():
            print(
                f"  [Feishu Bot] Plain text reply also failed: {response.code} - {response.msg}"
            )

    def _send_message(self, chat_id: str, text: str):
        """Send a new message to a chat with interactive card (Markdown rendered)."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(self._build_interactive_card(text))
                .build()
            )
            .build()
        )

        response = self.lark_client.im.v1.message.create(request)
        if not response.success():
            print(f"  [Feishu Bot] Send failed: {response.code} - {response.msg}")
            self._send_plain_text(chat_id, text)

    def _send_plain_text(self, chat_id: str, text: str):
        """Fallback: send as plain text when interactive card fails."""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        response = self.lark_client.im.v1.message.create(request)
        if not response.success():
            print(
                f"  [Feishu Bot] Plain text send also failed: {response.code} - {response.msg}"
            )

    # ── Mode detection ──────────────────────────────────────────────────

    def _handle_mode(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /mode command: show or switch permission mode."""
        session = self._sessions.get(chat_id)

        if arg is None:
            # Send a dropdown card listing the four modes.  Without an
            # active session, switching is impossible — show a hint
            # instead of a card the user can't act on.
            if not session:
                self._reply_text(
                    message_id, "No active session. Start a conversation first."
                )
                return

            current_display = MODE_DISPLAY.get(
                session.permission_mode, session.permission_mode
            )
            options = []
            for display_name, sdk_mode in MODE_ALIASES.items():
                marker = "  ← current" if sdk_mode == session.permission_mode else ""
                options.append(
                    {"text": f"{display_name}{marker}", "value": display_name}
                )

            intro = (
                f"**Current mode:** `{current_display}` ({session.permission_mode})\n\n"
                f"Pick a permission mode:"
            )
            card_json = build_select_card(
                intro_markdown=intro,
                placeholder="Select mode…",
                options=options,
                action_name=ACTION_MODE,
                initial_value=current_display,
            )
            fallback = f"Current mode: {current_display} ({session.permission_mode})"
            self._reply_card_json(message_id, card_json, fallback_text=fallback)
            return

        arg = arg.lower().strip()
        if arg not in MODE_ALIASES:
            available = ", ".join(MODE_ALIASES.keys())
            self._reply_text(
                message_id,
                f"Unknown mode: {arg}\nAvailable modes: {available}",
            )
            return

        sdk_mode = MODE_ALIASES[arg]

        if not session or not session.connected:
            self._reply_text(
                message_id,
                "No active session. Start a conversation first, then switch mode.",
            )
            return

        # Switch mode asynchronously
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._switch_mode(session, sdk_mode, arg, chat_id, message_id),
                self._loop,
            )
        else:
            self._reply_text(message_id, "Event loop not available.")

    async def _switch_mode(
        self,
        session: ChatSession,
        sdk_mode: str,
        display_name: str,
        chat_id: str,
        message_id: str,
    ):
        """Switch permission mode on an active session."""
        if not session.client.supports(Feature.PERMISSION_MODE):
            self._send_message(
                chat_id,
                f"Backend '{session.backend}' does not support dynamic mode switching.\n"
                "Mode is set at session creation time.",
            )
            return
        try:
            await session.client.set_permission_mode(sdk_mode)
            session.permission_mode = sdk_mode
            self._chat_modes[chat_id] = sdk_mode
            self._send_message(
                chat_id, f"Mode switched to: {display_name} ({sdk_mode})"
            )
        except Exception as e:
            self._send_message(chat_id, f"Failed to switch mode: {e}")

    # ── Session rename ────────────────────────────────────────────────────

    def _handle_rename(self, title: str | None, chat_id: str, message_id: str):
        """Handle /rename command: rename the current session's custom title."""
        session = self._sessions.get(chat_id)

        if title is None:
            # Show current title
            if session and session.custom_title:
                self._reply_text(message_id, f'Current title: "{session.custom_title}"')
            elif session and session.first_message:
                self._reply_text(
                    message_id,
                    f'No custom title. Auto-summary: "{session.first_message[:30]}"\n\nUsage: /rename <new title>',
                )
            else:
                self._reply_text(
                    message_id, "No active session.\nUsage: /rename <new title>"
                )
            return

        if not session or not session.connected:
            self._reply_text(
                message_id,
                "No active session. Start a conversation first, then rename.",
            )
            return

        if not session.session_id:
            self._reply_text(
                message_id,
                "Session not yet initialized. Send a message first, then rename.",
            )
            return

        title = title.strip()
        if not title:
            self._reply_text(
                message_id, "Title cannot be empty.\nUsage: /rename <new title>"
            )
            return

        # Rename asynchronously
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._rename_session(session, title, chat_id, message_id),
                self._loop,
            )
        else:
            self._reply_text(message_id, "Event loop not available.")

    async def _rename_session(
        self, session: ChatSession, title: str, chat_id: str, message_id: str
    ):
        """Rename session: update in-memory, write to .jsonl, update history."""
        try:
            old_title = session.custom_title or session.first_message or "(untitled)"

            # 1. Update in-memory
            session.custom_title = title

            # 2. Write custom-title record to .jsonl session file
            encoded_dir = str(session.project_dir.resolve()).replace("/", "-")
            jsonl_path = (
                CLAUDE_SESSIONS_DIR / encoded_dir / f"{session.session_id}.jsonl"
            )
            if jsonl_path.exists():
                record = json.dumps({"type": "custom-title", "customTitle": title})
                with open(jsonl_path, "a") as f:
                    f.write(record + "\n")

            # 3. Update history entry in feishu_sessions.json
            self._save_session_to_history(chat_id, session)

            self._send_message(chat_id, f'Session renamed: "{old_title}" → "{title}"')

        except Exception as e:
            print(f"  [Error] Failed to rename session: {e}")
            traceback.print_exc()
            self._send_message(chat_id, f"Failed to rename: {e}")

    # ── Backend switching ────────────────────────────────────────────────

    def _handle_backend(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /backend command: show or switch agent backend."""
        session = self._sessions.get(chat_id)

        if arg is None:
            # Send a dropdown card listing the two backends.
            if session:
                current = session.backend
                model_display = (
                    CODEX_MODEL_DISPLAY if session.backend == "codex" else MODEL_DISPLAY
                ).get(session.model, session.model)
                intro = f"**Current backend:** `{current}` _(model: {model_display})_\n\nPick a backend:"
            else:
                current = self._chat_backends.get(chat_id, self.default_backend)
                intro = f"**Current backend:** `{current}` _(no active session)_\n\nPick a backend:"

            options = []
            for name in sorted(BACKEND_ALIASES):
                marker = "  ← current" if name == current else ""
                options.append({"text": f"{name}{marker}", "value": name})

            card_json = build_select_card(
                intro_markdown=intro,
                placeholder="Select backend…",
                options=options,
                action_name=ACTION_BACKEND,
                initial_value=current,
            )
            self._reply_card_json(
                message_id, card_json, fallback_text=f"Current backend: {current}"
            )
            return

        arg = arg.lower().strip()
        if arg not in BACKEND_ALIASES:
            available = ", ".join(sorted(BACKEND_ALIASES))
            self._reply_text(
                message_id,
                f"Unknown backend: {arg}\nAvailable: {available}",
            )
            return

        old_backend = self._chat_backends.get(chat_id, self.default_backend)
        self._chat_backends[chat_id] = arg

        # Reset model to the new backend's default (unless user explicitly set one)
        default_model = BACKEND_DEFAULT_MODELS.get(arg, "")
        model_display = (CODEX_MODEL_DISPLAY if arg == "codex" else MODEL_DISPLAY).get(
            default_model, default_model
        )

        # Backend requires new session — close current one
        if session and session.connected:
            if session.session_id:
                self._save_session_to_history(chat_id, session)
            # Clear model override so the new backend's default kicks in
            self._chat_models.pop(chat_id, None)
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._close_session(chat_id), self._loop
                )
            self._send_message(
                chat_id,
                f"Backend switched to: {arg} (default model: {model_display})\n"
                "Session reset — send a message to start.",
            )
        else:
            self._chat_models.pop(chat_id, None)
            self._send_message(
                chat_id,
                f"Backend set to: {arg} (default model: {model_display})\n"
                "Will take effect on next session.",
            )

    # ── Model switching ──────────────────────────────────────────────────

    def _handle_model(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /model command: show or switch model (backend-aware)."""
        session = self._sessions.get(chat_id)
        backend = self._chat_backends.get(chat_id, self.default_backend)
        if session:
            backend = session.backend

        # Pick the right alias map based on current backend
        aliases = CODEX_MODEL_ALIASES if backend == "codex" else MODEL_ALIASES
        display_map = CODEX_MODEL_DISPLAY if backend == "codex" else MODEL_DISPLAY

        if arg is None:
            # Send a dropdown card with the models for the current backend.
            # The dropdown values are the user-friendly aliases (opus/sonnet/
            # gpt-5.5 etc.); the existing _handle_model already accepts those.
            if session:
                current_id = session.model
            else:
                current_id = self._chat_models.get(
                    chat_id, BACKEND_DEFAULT_MODELS.get(backend, self.default_model)
                )
            current_display = display_map.get(current_id, current_id)

            options = []
            for display_name, model_id in aliases.items():
                marker = "  ← current" if model_id == current_id else ""
                options.append(
                    {"text": f"{display_name}{marker}", "value": display_name}
                )

            session_tag = "" if session else " _(no active session)_"
            intro = (
                f"**Current model:** `{current_display}` _({backend})_{session_tag}\n\n"
                f"Pick a model:"
            )
            card_json = build_select_card(
                intro_markdown=intro,
                placeholder="Select model…",
                options=options,
                action_name=ACTION_MODEL,
                initial_value=current_display,
            )
            fallback = f"Current model: {current_display} [{backend}]"
            self._reply_card_json(message_id, card_json, fallback_text=fallback)
            return

        arg = arg.lower().strip()
        # Try current backend's aliases first, then the other
        all_aliases = {**MODEL_ALIASES, **CODEX_MODEL_ALIASES}
        if arg not in all_aliases:
            available = ", ".join(aliases.keys())
            self._reply_text(
                message_id,
                f"Unknown model: {arg}\nAvailable models ({backend}): {available}",
            )
            return

        new_model = all_aliases[arg]
        self._chat_models[chat_id] = new_model

        # If user picks a Codex model while on Claude (or vice versa), auto-switch backend
        if arg in CODEX_MODEL_ALIASES and backend != "codex":
            self._chat_backends[chat_id] = "codex"
            backend = "codex"
        elif arg in MODEL_ALIASES and backend != "claude":
            self._chat_backends[chat_id] = "claude"
            backend = "claude"

        # Model is embedded in client — need to close and recreate session
        if session and session.connected:
            if session.session_id:
                self._save_session_to_history(chat_id, session)
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._close_session(chat_id), self._loop
                )
            self._send_message(
                chat_id,
                f"Model switched to: {arg} ({new_model}) [{backend}]\nSession reset — send a message to start.",
            )
        else:
            self._send_message(
                chat_id,
                f"Model set to: {arg} ({new_model}) [{backend}]\nWill take effect on next session.",
            )

    # ── Effort switching ────────────────────────────────────────────────

    EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}

    def _handle_effort(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /effort command: show or switch effort level."""
        session = self._sessions.get(chat_id)

        if arg is None:
            # Send a dropdown card with the 5 effort levels in ascending
            # intensity.  Pre-select the current one if set.
            current = self._chat_efforts.get(chat_id, self.default_effort)
            display_current = current or "default (not set)"
            ordered = ["low", "medium", "high", "xhigh", "max"]

            options = []
            for level in ordered:
                marker = "  ← current" if level == current else ""
                options.append({"text": f"{level}{marker}", "value": level})

            intro = f"**Current effort:** `{display_current}`\n\nPick an effort level:"
            card_json = build_select_card(
                intro_markdown=intro,
                placeholder="Select effort…",
                options=options,
                action_name=ACTION_EFFORT,
                initial_value=current if current in ordered else None,
            )
            fallback = f"Current effort: {display_current}"
            self._reply_card_json(message_id, card_json, fallback_text=fallback)
            return

        arg = arg.lower().strip()
        if arg not in self.EFFORT_LEVELS:
            self._reply_text(
                message_id,
                f"Unknown effort level: {arg}\nAvailable: low, medium, high, xhigh, max",
            )
            return

        self._chat_efforts[chat_id] = arg

        # Effort is embedded in client — need to close and recreate session
        if session and session.connected:
            if session.session_id:
                self._save_session_to_history(chat_id, session)
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._close_session(chat_id), self._loop
                )
            self._send_message(
                chat_id,
                f"Effort switched to: {arg}\nSession reset — send a message to start.",
            )
        else:
            self._send_message(
                chat_id,
                f"Effort set to: {arg}\nWill take effect on next session.",
            )

    # ── Resume / session history ────────────────────────────────────────

    def _get_project_alias(self, project_dir: Path) -> str | None:
        """Resolve project directory back to its alias."""
        resolved = project_dir.resolve()
        for alias, path in self.projects.items():
            if path.resolve() == resolved:
                return alias
        return None

    def _load_session_history(self) -> dict:
        """Load session history from disk."""
        if SESSION_HISTORY_FILE.exists():
            try:
                with open(SESSION_HISTORY_FILE) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def _save_session_history(self, history: dict):
        """Save session history to disk."""
        SESSION_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSION_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

    def _save_session_to_history(self, chat_id: str, session: ChatSession):
        """Save or update a session entry in the history for a chat."""
        if not session.session_id:
            return

        history = self._load_session_history()
        chat_history = history.get(chat_id, [])

        # Check if this session_id already exists (update it)
        entry = None
        for existing in chat_history:
            if existing.get("session_id") == session.session_id:
                entry = existing
                break

        if entry is None:
            entry = {
                "session_id": session.session_id,
                "summary": session.custom_title
                or (session.first_message or "")[:30]
                or "(no message)",
                "permission_mode": session.permission_mode,
                "project_alias": session.project_alias
                or self._get_project_alias(session.project_dir),
                "project_dir": str(session.project_dir),
                "created_at": session.created_at.isoformat(),
                "last_active": session.last_active.isoformat(),
                "model": session.model,
                "backend": session.backend,
                "source": "bot",
            }
            chat_history.insert(0, entry)  # newest first
        else:
            # Update existing entry
            entry["last_active"] = session.last_active.isoformat()
            entry["permission_mode"] = session.permission_mode
            entry["model"] = session.model
            entry["backend"] = session.backend
            if session.custom_title:
                entry["summary"] = session.custom_title
            elif session.first_message and entry.get("summary") == "(no message)":
                entry["summary"] = session.first_message[:30]

        # Trim to max history size
        chat_history = chat_history[:SESSION_HISTORY_MAX_PER_CHAT]
        history[chat_id] = chat_history
        self._save_session_history(history)

    def _get_chat_history(self, chat_id: str) -> list:
        """Get session history for a specific chat."""
        history = self._load_session_history()
        return history.get(chat_id, [])

    def _scan_cli_sessions(
        self, project_dir: Path, exclude_ids: set | None = None
    ) -> list:
        """
        Scan Claude Code CLI session files for a given project.

        Always scans .jsonl files on disk for real-time accuracy.
        Uses sessions-index.json as metadata enrichment (summary, timestamps)
        when available, but never relies on it as the sole source of truth.
        """
        encoded_dir = str(project_dir.resolve()).replace("/", "-")
        sessions_path = CLAUDE_SESSIONS_DIR / encoded_dir
        if not sessions_path.is_dir():
            return []

        exclude_ids = exclude_ids or set()
        results = []

        # Load sessions-index.json as metadata enrichment (not sole source)
        index_lookup: dict[str, dict] = {}
        index_file = sessions_path / "sessions-index.json"
        if index_file.exists():
            try:
                with open(index_file) as f:
                    index_data = json.load(f)
                entries = (
                    index_data.get("entries", [])
                    if isinstance(index_data, dict)
                    else index_data
                )
                for entry in entries:
                    sid = entry.get("sessionId", "")
                    if sid:
                        index_lookup[sid] = entry
            except (json.JSONDecodeError, IOError, KeyError):
                pass

        # Always scan .jsonl files on disk (source of truth)
        project_alias = self._get_project_alias(project_dir)
        for jsonl_file in sessions_path.glob("*.jsonl"):
            session_id = jsonl_file.stem
            if session_id in exclude_ids:
                continue

            index_entry = index_lookup.get(session_id)

            # Always use file mtime for sorting accuracy
            mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime)
            last_active = mtime.isoformat()
            if index_entry:
                created = index_entry.get("created", last_active)
            else:
                created = last_active

            # Extract customTitle, permissionMode, and first user message from .jsonl
            # Note: custom_title always takes the LAST occurrence (user may rename multiple times)
            custom_title = ""
            first_prompt = ""
            permission_mode = ""
            try:
                with open(jsonl_file) as f:
                    for line in f:
                        obj = json.loads(line)
                        if obj.get("type") == "custom-title":
                            custom_title = obj.get("customTitle", "")
                        if obj.get("type") == "user" and not first_prompt:
                            permission_mode = obj.get("permissionMode", "")
                            content = obj.get("message", {}).get("content", [])
                            # Handle string content (newer Claude Code CLI format)
                            if isinstance(content, str):
                                text = content.strip()
                                if not text.startswith("<"):
                                    first_prompt = text[:30]
                                continue
                            # Skip synthetic messages (e.g. "Tool loaded." after MCP init)
                            has_tool_result = any(
                                isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in content
                            )
                            if has_tool_result:
                                continue
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    text = block["text"].strip()
                                    if not text.startswith("<"):
                                        first_prompt = text[:30]
                                        break
            except (json.JSONDecodeError, IOError, KeyError, AttributeError, TypeError):
                pass

            # Summary priority: customTitle > index summary > first_prompt > fallback
            summary = custom_title
            if not summary and index_entry:
                summary = (
                    index_entry.get("summary")
                    or index_entry.get("firstPrompt", "")[:30]
                    or ""
                )
            if not summary:
                summary = first_prompt
            if not summary:
                continue

            results.append(
                {
                    "session_id": session_id,
                    "summary": summary[:50],
                    "custom_title": custom_title or None,
                    "permission_mode": permission_mode or "acceptEdits",
                    "project_alias": project_alias,
                    "project_dir": str(project_dir),
                    "created_at": created,
                    "last_active": last_active,
                    "backend": "claude",
                    "source": "cli",
                }
            )

        results.sort(key=lambda x: x["last_active"], reverse=True)
        return results

    def _read_last_assistant_response(self, session_id: str, project_dir: Path) -> str:
        """Read the last assistant text response from a session's .jsonl file."""
        encoded_dir = str(project_dir.resolve()).replace("/", "-")
        jsonl_path = CLAUDE_SESSIONS_DIR / encoded_dir / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return ""

        last_text = ""
        try:
            with open(jsonl_path) as f:
                for line in f:
                    obj = json.loads(line)
                    if obj.get("type") == "assistant":
                        content = obj.get("message", {}).get("content", [])
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                last_text = block["text"]
                                break
        except (json.JSONDecodeError, IOError, KeyError, AttributeError, TypeError):
            pass

        # Truncate to avoid oversized messages
        if len(last_text) > 2000:
            last_text = last_text[:2000] + "\n\n... (truncated)"
        return last_text

    def _get_merged_sessions(self, chat_id: str) -> list:
        """
        Get session list for the current chat, filtered by current backend.

        - claude: scan ~/.claude/projects/ jsonl files (existing logic)
        - codex:  call codex.thread_list() via SDK (sync wrapper around async)
        """
        backend = self._chat_backends.get(chat_id, self.default_backend)
        # If a session is currently active, its backend wins (avoids confusing
        # state where /backend was set but session not yet recreated)
        session = self._sessions.get(chat_id)
        if session:
            backend = session.backend

        current_project_dir = self._chat_project_dirs.get(
            chat_id, self.default_project_dir
        )

        if backend == "codex":
            return self._scan_codex_threads_sync(current_project_dir)[:10]
        else:
            return self._scan_cli_sessions(current_project_dir)[:10]

    def _scan_codex_threads_sync(self, project_dir: Path) -> list:
        """
        Sync wrapper around the async codex thread_list call.

        Schedules the coroutine on the bot's event loop and blocks the
        caller thread (the message handler thread) until it returns.
        """
        try:
            from codex_agent import codex_available, list_codex_threads
        except ImportError:
            return []

        if not codex_available():
            return []

        if not (self._loop and self._loop.is_running()):
            return []

        try:
            future = asyncio.run_coroutine_threadsafe(
                list_codex_threads(str(project_dir.resolve()), limit=10),
                self._loop,
            )
            threads = future.result(timeout=15)
            # Fill in project alias for display
            alias = self._get_project_alias(project_dir)
            for t in threads:
                t["project_alias"] = alias
            return threads
        except Exception as e:
            print(f"  [Codex] Failed to list threads: {e}")
            return []

    def _handle_resume(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /resume command: list history or resume a session.

        ``arg`` accepts:
        - ``None``  → send a dropdown card listing recent sessions (UI mode)
        - ``"3"``   → numeric index from the (legacy) text listing
        - session_id (UUID-ish) → resume by id (used by the dropdown callback)
        """
        merged = self._get_merged_sessions(chat_id)

        if not merged:
            project_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
            alias = self._get_project_alias(project_dir) or str(project_dir)
            self._reply_text(message_id, f"No sessions found for project: {alias}")
            return

        if arg is None:
            # Send a dropdown card.  Each option's label shows the title
            # (custom_title preferred, summary as fallback) plus the time;
            # the option's value is the session_id so the callback can
            # resume it directly without index arithmetic.
            project_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
            alias = self._get_project_alias(project_dir) or str(project_dir)
            session = self._sessions.get(chat_id)
            backend = (
                session.backend
                if session
                else self._chat_backends.get(chat_id, self.default_backend)
            )

            options = []
            seen_values: set[str] = set()
            for entry in merged:
                session_id = entry.get("session_id")
                if not session_id or session_id in seen_values:
                    continue
                seen_values.add(session_id)

                last_active = entry.get("last_active", "")
                try:
                    dt = datetime.fromisoformat(last_active)
                    time_str = dt.strftime("%-m/%-d %H:%M")
                except (ValueError, TypeError):
                    time_str = "?"

                title = (
                    entry.get("custom_title") or entry.get("summary") or "(no title)"
                )
                label = f"[{time_str}] {truncate(title, 40)}"
                options.append({"text": label, "value": session_id})

            if not options:
                self._reply_text(
                    message_id, f"No resumable sessions for project: {alias}"
                )
                return

            intro = (
                f"**Sessions for `{alias}`** _(backend: {backend})_\n\n"
                f"Pick a session to resume:"
            )
            card_json = build_select_card(
                intro_markdown=intro,
                placeholder="Select a session…",
                options=options,
                action_name=ACTION_RESUME,
            )
            fallback = f"Sessions for {alias} [backend: {backend}]: " + str(
                len(options)
            )
            self._reply_card_json(message_id, card_json, fallback_text=fallback)
            return

        # Resume by session_id (from card click) OR by 1-based index (legacy CLI).
        arg_clean = arg.strip()
        entry = None
        if arg_clean.isdigit():
            idx = int(arg_clean) - 1
            if idx < 0 or idx >= len(merged):
                self._reply_text(
                    message_id, f"Invalid number. Choose between 1 and {len(merged)}."
                )
                return
            entry = merged[idx]
        else:
            for e in merged:
                if e.get("session_id") == arg_clean:
                    entry = e
                    break
            if entry is None:
                self._reply_text(
                    message_id,
                    f"Session not found: {arg_clean[:8]}…\n"
                    "It may have been removed since the card was sent.",
                )
                return
        session_id = entry.get("session_id")
        project_dir_str = entry.get("project_dir")
        project_alias = entry.get("project_alias")

        if not session_id or not project_dir_str:
            self._reply_text(message_id, "Session data is incomplete. Cannot resume.")
            return

        self._reply_text(
            message_id,
            f'Resuming: "{entry.get("summary", "?")}" ({project_alias or "?"})...',
        )

        project_dir = Path(project_dir_str)

        # Auto-switch project, backend, and model based on the entry
        self._chat_project_dirs[chat_id] = project_dir
        entry_backend = entry.get("backend") or self._chat_backends.get(
            chat_id, self.default_backend
        )
        self._chat_backends[chat_id] = entry_backend
        # Use entry's model if present and looks valid for the backend; else
        # fall back to the backend's default
        model = entry.get("model") or BACKEND_DEFAULT_MODELS.get(
            entry_backend, self.default_model
        )
        self._chat_models[chat_id] = model

        # Resume asynchronously
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._resume_session(
                    chat_id, session_id, project_dir, project_alias, entry
                ),
                self._loop,
            )

    async def _resume_session(
        self,
        chat_id: str,
        session_id: str,
        project_dir: Path,
        project_alias: str | None,
        entry: dict,
    ):
        """Resume a previous session by session_id."""
        try:
            # Close current session if any
            await self._close_session(chat_id)

            # The entry's backend is authoritative — a Claude session_id can't
            # be resumed by Codex and vice versa.  Auto-switch if needed.
            backend = entry.get("backend") or self._chat_backends.get(
                chat_id, self.default_backend
            )
            self._chat_backends[chat_id] = backend

            model = entry.get("model") or BACKEND_DEFAULT_MODELS.get(
                backend, self.default_model
            )
            mode = entry.get("permission_mode", "acceptEdits")
            print(
                f"  [Session] Resuming session {session_id[:8]}... for chat {chat_id[:8]}... (backend: {backend}, project: {project_dir}, model: {model}, mode: {mode})"
            )
            client = create_agent_client(
                backend=backend,
                project_dir=str(project_dir),
                model=model,
                permission_mode=mode,
                resume=session_id,
            )

            session = ChatSession(
                chat_id=chat_id, client=client, project_dir=project_dir
            )
            session.session_id = session_id
            session.model = model
            session.backend = backend
            session.project_alias = project_alias
            session.custom_title = entry.get("custom_title")
            session.first_message = entry.get("summary")
            session.permission_mode = mode
            self._chat_modes[chat_id] = mode
            await session.connect()
            self._sessions[chat_id] = session

            mode_display = MODE_DISPLAY.get(
                session.permission_mode, session.permission_mode
            )
            summary = entry.get("summary", "?")

            # Read last assistant response from .jsonl
            last_response = self._read_last_assistant_response(session_id, project_dir)

            resume_msg = f'**Session resumed:** "{summary}" ({project_alias or "?"} | {mode_display})'
            if last_response:
                resume_msg += f"\n\n---\n\n**Last response:**\n\n{last_response}"
            resume_msg += "\n\n---\n*You can continue the conversation now.*"

            self._send_message(chat_id, resume_msg)

        except Exception as e:
            print(f"  [Error] Failed to resume session: {e}")
            traceback.print_exc()
            self._send_message(
                chat_id,
                f"Failed to resume session: {e}\n\nPlease start a new conversation.",
            )


def _load_config(config_path: str) -> dict:
    """Load scheduler config file."""
    base_dir = Path(__file__).parent.parent
    path = Path(config_path)
    if not path.is_absolute():
        path = base_dir / config_path

    if not path.exists():
        print(f"Warning: Config file {config_path} not found, using defaults")
        return {}

    with open(path) as f:
        raw = yaml.safe_load(f)
    return resolve_env_vars(raw) if raw else {}


def main():
    parser = argparse.ArgumentParser(
        description="Feishu Bot for Vurtnec Loom",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start the Feishu bot
  python -m scheduler.feishu_bot

  # Use custom config file
  python -m scheduler.feishu_bot --config /path/to/config.yaml
        """,
    )
    parser.add_argument(
        "--config",
        default="scheduler_config.yaml",
        help="Path to scheduler config file (default: scheduler_config.yaml)",
    )

    args = parser.parse_args()
    config = _load_config(args.config)

    try:
        bot = FeishuBotServer(config)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Create event loop for async task execution
    loop = asyncio.new_event_loop()

    # Start bot WebSocket in background thread
    ws_thread = bot.start(loop=loop)

    print("Feishu bot is running. Press Ctrl+C to stop.\n")

    # Run the event loop in main thread (for async task execution)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("\nShutting down Feishu bot...")
        # Close all active sessions
        for chat_id in list(bot._sessions.keys()):
            try:
                loop.run_until_complete(bot._close_session(chat_id))
            except Exception:
                pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
