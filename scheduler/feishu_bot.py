"""
Feishu Bot Server (飞书应用机器人)
==================================

通过飞书开放平台的 WebSocket 长连接模式接收群消息，
使用 ClaudeSDKClient 的多轮对话能力保持上下文，
并将结果回复到群聊。

每个群聊（chat_id）维护一个长驻的 ClaudeSDKClient，
效果等同于在 Claude Code CLI 中持续对话。

前置条件：
1. 在 open.feishu.cn 创建企业自建应用，启用机器人能力
2. 添加权限：im:message
3. 事件订阅选「长连接」模式，添加 im.message.receive_v1
4. 创建版本并发布，将机器人添加到群聊
5. 在 scheduler_config.yaml 中配置 app_id 和 app_secret

用法：
    python -m scheduler.feishu_bot                     # 独立运行
    python -m scheduler.feishu_bot --config config.yaml # 指定配置
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
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

# Add parent directory for imports from the existing codebase
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from client import create_client
from claude_agent_sdk import ClaudeSDKClient

from .schedule_loader import load_all_schedules, resolve_env_vars


# Session timeout: auto-disconnect after 30 minutes of inactivity
SESSION_TIMEOUT_SECONDS = 6 * 60 * 60

# Mode aliases: user-friendly names → SDK permission_mode values
MODE_ALIASES = {
    "plan": "plan",
    "auto": "acceptEdits",
    "default": "default",
    "bypass": "bypassPermissions",
}

# Reverse mapping for display: SDK permission_mode → user-friendly name
MODE_DISPLAY = {v: k for k, v in MODE_ALIASES.items()}

# Model aliases: user-friendly names → model IDs
MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}
MODEL_DISPLAY = {v: k for k, v in MODEL_ALIASES.items()}

# Session history persistence
SESSION_HISTORY_FILE = Path.home() / ".claude-long-runner" / "feishu_sessions.json"
SESSION_HISTORY_MAX_PER_CHAT = 10

# Claude Code CLI sessions directory
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"


class ChatSession:
    """
    管理单个群聊的 Claude 会话。

    每个 chat_id 对应一个 ChatSession，内部维护一个长驻的 ClaudeSDKClient。
    通过 asyncio.Lock 确保同一群聊的消息串行处理。
    """

    def __init__(self, chat_id: str, client: ClaudeSDKClient, project_dir: Path):
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
        self.model: str = "claude-opus-4-6"

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
    飞书应用机器人服务。

    通过 WebSocket 长连接接收群消息，支持多轮对话：
    - 每个群聊维护一个长驻 ClaudeSDKClient（per-chat session）
    - 用户发的每条消息追加到同一个对话，Claude 保持完整上下文
    - 支持 /new（重置对话）、/stop（停止会话）、/run（触发 schedule）等命令
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
            config.get("defaults", {}).get("model", "claude-sonnet-4-5-20250929"),
        )
        # Projects: alias → absolute path
        self.projects: Dict[str, Path] = {}
        for alias, path_str in bot_config.get("projects", {}).items():
            self.projects[alias] = Path(path_str).resolve()

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
        self.lark_client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        # Per-chat sessions and project selection
        self._sessions: Dict[str, ChatSession] = {}
        self._chat_project_dirs: Dict[str, Path] = {}  # chat_id → selected project_dir
        self._chat_models: Dict[str, str] = {}  # chat_id → model ID

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
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message_received) \
            .build()

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
        if self.projects:
            print(f"  Projects:")
            for alias, path in self.projects.items():
                default_mark = " (default)" if path == self.default_project_dir else ""
                print(f"    {alias}: {path}{default_mark}")
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

            # Only handle text messages
            if message.message_type != "text":
                return

            # Extract sender info
            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"

            # Check whitelist
            if self.allowed_user_ids and sender_id not in self.allowed_user_ids:
                print(f"  Ignoring message from non-whitelisted user: {sender_id}")
                return

            # Parse message content
            content = json.loads(message.content)
            raw_text = content.get("text", "").strip()

            # Remove @mention prefix (飞书会在文本前加 @bot_name)
            text = raw_text
            if hasattr(message, "mentions") and message.mentions:
                for mention in message.mentions:
                    mention_key = mention.key
                    text = text.replace(mention_key, "").strip()

            if not text:
                return

            chat_id = message.chat_id

            print(f"\n[Feishu Bot] Received: \"{text}\" (from {sender_id}, chat {chat_id[:8]}...)")

            # Route the message
            if text.startswith("/"):
                self._handle_command(text, chat_id, message_id)
            else:
                self._handle_free_prompt(text, chat_id, message_id)

        except Exception as e:
            print(f"[Feishu Bot] Error handling message: {e}")
            traceback.print_exc()

    def _handle_command(self, text: str, chat_id: str, message_id: str):
        """Handle slash commands like /new, /stop, /run, /help, /list."""
        parts = text.split(None, 2)
        command = parts[0].lower()

        if command == "/help":
            self._send_help(chat_id, message_id)
        elif command == "/list":
            self._send_schedule_list(chat_id, message_id)
        elif command == "/new":
            self._handle_new_session(chat_id, message_id)
        elif command == "/stop":
            self._handle_stop_session(chat_id, message_id)
        elif command == "/project":
            alias = parts[1] if len(parts) >= 2 else None
            self._handle_project(alias, chat_id, message_id)
        elif command == "/mode":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_mode(arg, chat_id, message_id)
        elif command == "/model":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_model(arg, chat_id, message_id)
        elif command == "/resume":
            arg = parts[1] if len(parts) >= 2 else None
            self._handle_resume(arg, chat_id, message_id)
        elif command == "/run":
            if len(parts) < 2:
                self._reply_text(message_id, "Usage: /run <schedule_name>")
                return
            schedule_name = parts[1]
            self._trigger_schedule(schedule_name, chat_id, message_id)
        else:
            self._reply_text(
                message_id,
                f"Unknown command: {command}\nType /help for available commands.",
            )

    def _handle_new_session(self, chat_id: str, message_id: str):
        """Handle /new command: archive current session and reset."""
        session = self._sessions.get(chat_id)
        if session and session.session_id:
            # Archive current session to history before closing
            self._save_session_to_history(chat_id, session)

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._close_session(chat_id), self._loop
            )
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
            # Show current project and available list
            current_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
            current_alias = None
            for a, p in self.projects.items():
                if p == current_dir:
                    current_alias = a
                    break

            lines = [f"Current project: {current_alias or current_dir}\n"]
            lines.append("Available projects:")
            for a, p in self.projects.items():
                marker = " <--" if p == current_dir else ""
                lines.append(f"  {a}: {p}{marker}")
            lines.append(f"\nUsage: /project <alias>")
            self._reply_text(message_id, "\n".join(lines))
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

    def _handle_free_prompt(self, text: str, chat_id: str, message_id: str):
        """Handle free-form prompt — send to persistent Claude session."""
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
        """Wrapper that adds a 10-minute timeout to _execute_and_reply."""
        try:
            await asyncio.wait_for(
                self._execute_and_reply(text, chat_id, message_id),
                timeout=600,  # 10 minutes
            )
        except asyncio.TimeoutError:
            self._send_message(
                chat_id,
                "Response timed out after 10 minutes.\n"
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

        # Determine project dir and model for this chat
        project_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
        model = self._chat_models.get(chat_id, self.default_model)

        # Create new session
        print(f"  [Session] Creating new session for chat {chat_id[:8]}... (project: {project_dir}, model: {model})")
        client = create_client(
            project_dir=project_dir,
            model=model,
        )

        session = ChatSession(chat_id=chat_id, client=client, project_dir=project_dir)
        session.model = model
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
        stale_ids = [
            cid for cid, s in self._sessions.items() if s.is_stale()
        ]
        for cid in stale_ids:
            print(f"  [Session] Cleaning up stale session: {cid[:8]}...")
            await self._close_session(cid)

    async def _execute_and_reply(self, prompt: str, chat_id: str, message_id: str):
        """Send prompt to persistent Claude session and reply with result."""
        start_time = datetime.now()
        try:
            session = await self._get_or_create_session(chat_id)

            async with session.lock:
                # Send the prompt to the existing conversation
                await session.client.query(prompt)

                # Collect response (stops automatically at ResultMessage)
                response_text = ""
                async for msg in session.client.receive_response():
                    msg_type = type(msg).__name__

                    if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                        for block in msg.content:
                            block_type = type(block).__name__

                            if block_type == "TextBlock" and hasattr(block, "text"):
                                response_text += block.text
                                print(block.text, end="", flush=True)
                            elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                                print(f"\n[Tool: {block.name}]", flush=True)
                                if hasattr(block, "input"):
                                    input_str = str(block.input)
                                    if len(input_str) > 200:
                                        print(f"   Input: {input_str[:200]}...", flush=True)
                                    else:
                                        print(f"   Input: {input_str}", flush=True)

                    elif msg_type == "UserMessage" and hasattr(msg, "content"):
                        for block in msg.content:
                            block_type = type(block).__name__
                            if block_type == "ToolResultBlock":
                                result_content = getattr(block, "content", "")
                                is_error = getattr(block, "is_error", False)
                                if "blocked" in str(result_content).lower():
                                    print(f"   [BLOCKED] {result_content}", flush=True)
                                elif is_error:
                                    print(f"   [Error] {str(result_content)[:500]}", flush=True)
                                else:
                                    print("   [Done]", flush=True)

                    elif msg_type == "SystemMessage":
                        # Capture permission_mode and session_id from init message
                        if hasattr(msg, "data") and isinstance(msg.data, dict):
                            init_mode = msg.data.get("permission_mode")
                            if init_mode:
                                session.permission_mode = init_mode
                            init_session_id = msg.data.get("session_id")
                            if init_session_id:
                                session.session_id = init_session_id
                            print(f"  [System] mode={init_mode}, session={init_session_id and init_session_id[:8]}...")

                    elif msg_type == "ResultMessage":
                        # ResultMessage signals end of response
                        num_turns = getattr(msg, "num_turns", "?")
                        is_error = getattr(msg, "is_error", False)
                        result_session_id = getattr(msg, "session_id", None)
                        if result_session_id:
                            session.session_id = result_session_id
                        print(f"\n  [Result] turns={num_turns}, error={is_error}, session={session.session_id and session.session_id[:8]}...")

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
            mode_display = MODE_DISPLAY.get(session.permission_mode, session.permission_mode)

            if response_text:
                # Truncate for Feishu's message size limit
                if len(response_text) > 25000:
                    response_text = response_text[:25000] + "\n\n... (truncated)"
                reply = f"{response_text}\n\n({duration_str} | mode: {mode_display})"
            else:
                reply = f"Done ({duration_str} | mode: {mode_display})"

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
            timeout = schedule.timeout_minutes or 15
            max_turns = schedule.task.max_turns or 5

            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._execute_schedule_and_reply(
                        prompt, project_dir, model, chat_id, schedule_name,
                        timeout, max_turns
                    ),
                    self._loop,
                )
            else:
                thread = threading.Thread(
                    target=lambda: asyncio.run(
                        self._execute_schedule_and_reply(
                            prompt, project_dir, model, chat_id, schedule_name,
                            timeout, max_turns
                        )
                    ),
                    daemon=True,
                )
                thread.start()
        elif schedule.task.task_type == "standard" and schedule.task.name:
            model = schedule.task.model or self.default_model
            project_dir = Path(schedule.task.project_dir).resolve()
            max_iters = schedule.task.max_iterations or 10
            timeout = schedule.timeout_minutes or 30

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
                        schedule.task.name, resolved_params, project_dir,
                        model, max_iters, chat_id, schedule_name, timeout
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
                summary = last_response[:3000] if last_response else "No response captured"
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
            "/mode [plan|auto|default] — Show or switch permission mode\n"
            "/model [opus|sonnet|haiku] — Show or switch model\n"
            "/resume [number] — List recent sessions / resume by number\n"
            "/list  — List available schedules\n"
            "/run <name> — Run a schedule (inline or standard task)\n"
            "/new   — Reset conversation (start fresh)\n"
            "/stop  — Stop and disconnect current session\n\n"
            "Or just send a message to chat with Claude.\n"
            "Multi-turn conversation is supported — Claude remembers context.\n"
            "Each reply shows the current mode automatically."
        )
        self._reply_text(message_id, help_text)

    def _send_schedule_list(self, chat_id: str, message_id: str):
        """Send list of available schedules."""
        if not self.schedules:
            self._reply_text(message_id, "No schedules loaded.")
            return

        lines = ["Available schedules:\n"]
        for name, sched in self.schedules.items():
            lines.append(f"  {name} - {sched.description}")
        lines.append(f"\nUsage: /run <name>")

        self._reply_text(message_id, "\n".join(lines))

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
        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(self._build_interactive_card(text))
                .build()
            ).build()

        response = self.lark_client.im.v1.message.reply(request)
        if not response.success():
            print(f"  [Feishu Bot] Reply failed: {response.code} - {response.msg}")
            self._reply_plain_text(message_id, text)

    def _reply_plain_text(self, message_id: str, text: str):
        """Fallback: reply as plain text when interactive card fails."""
        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            ).build()
        response = self.lark_client.im.v1.message.reply(request)
        if not response.success():
            print(f"  [Feishu Bot] Plain text reply also failed: {response.code} - {response.msg}")

    def _send_message(self, chat_id: str, text: str):
        """Send a new message to a chat with interactive card (Markdown rendered)."""
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(self._build_interactive_card(text))
                .build()
            ).build()

        response = self.lark_client.im.v1.message.create(request)
        if not response.success():
            print(f"  [Feishu Bot] Send failed: {response.code} - {response.msg}")
            self._send_plain_text(chat_id, text)

    def _send_plain_text(self, chat_id: str, text: str):
        """Fallback: send as plain text when interactive card fails."""
        request = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            ).build()
        response = self.lark_client.im.v1.message.create(request)
        if not response.success():
            print(f"  [Feishu Bot] Plain text send also failed: {response.code} - {response.msg}")

    # ── Mode detection ──────────────────────────────────────────────────

    def _handle_mode(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /mode command: show or switch permission mode."""
        session = self._sessions.get(chat_id)

        if arg is None:
            # Show current mode
            if session:
                mode_display = MODE_DISPLAY.get(session.permission_mode, session.permission_mode)
                self._reply_text(message_id, f"Current mode: {mode_display} ({session.permission_mode})")
            else:
                self._reply_text(message_id, "No active session. Start a conversation first.")
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
            self._reply_text(message_id, "No active session. Start a conversation first, then switch mode.")
            return

        # Switch mode asynchronously
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._switch_mode(session, sdk_mode, arg, chat_id, message_id),
                self._loop,
            )
        else:
            self._reply_text(message_id, "Event loop not available.")

    async def _switch_mode(self, session: ChatSession, sdk_mode: str, display_name: str, chat_id: str, message_id: str):
        """Switch permission mode on an active session."""
        try:
            await session.client.set_permission_mode(sdk_mode)
            session.permission_mode = sdk_mode
            self._send_message(chat_id, f"Mode switched to: {display_name} ({sdk_mode})")
        except Exception as e:
            self._send_message(chat_id, f"Failed to switch mode: {e}")

    # ── Model switching ──────────────────────────────────────────────────

    def _handle_model(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /model command: show or switch model."""
        session = self._sessions.get(chat_id)

        if arg is None:
            # Show current model
            if session:
                display = MODEL_DISPLAY.get(session.model, session.model)
                self._reply_text(message_id, f"Current model: {display} ({session.model})")
            else:
                current = self._chat_models.get(chat_id, self.default_model)
                display = MODEL_DISPLAY.get(current, current)
                self._reply_text(message_id, f"Current model: {display} (no active session)")
            return

        arg = arg.lower().strip()
        if arg not in MODEL_ALIASES:
            available = ", ".join(MODEL_ALIASES.keys())
            self._reply_text(
                message_id,
                f"Unknown model: {arg}\nAvailable models: {available}",
            )
            return

        new_model = MODEL_ALIASES[arg]
        self._chat_models[chat_id] = new_model

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
                f"Model switched to: {arg} ({new_model})\nSession reset — send a message to start.",
            )
        else:
            self._send_message(
                chat_id,
                f"Model set to: {arg} ({new_model})\nWill take effect on next session.",
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
                "summary": (session.first_message or "")[:30] or "(no message)",
                "permission_mode": session.permission_mode,
                "project_alias": session.project_alias or self._get_project_alias(session.project_dir),
                "project_dir": str(session.project_dir),
                "created_at": session.created_at.isoformat(),
                "last_active": session.last_active.isoformat(),
                "model": session.model,
                "source": "bot",
            }
            chat_history.insert(0, entry)  # newest first
        else:
            # Update existing entry
            entry["last_active"] = session.last_active.isoformat()
            entry["permission_mode"] = session.permission_mode
            entry["model"] = session.model
            if session.first_message and entry.get("summary") == "(no message)":
                entry["summary"] = session.first_message[:30]

        # Trim to max history size
        chat_history = chat_history[:SESSION_HISTORY_MAX_PER_CHAT]
        history[chat_id] = chat_history
        self._save_session_history(history)

    def _get_chat_history(self, chat_id: str) -> list:
        """Get session history for a specific chat."""
        history = self._load_session_history()
        return history.get(chat_id, [])

    def _scan_cli_sessions(self, project_dir: Path, exclude_ids: set | None = None) -> list:
        """
        Scan Claude Code CLI session files for a given project.

        Reads sessions-index.json (CLI's authoritative metadata) first,
        falls back to scanning .jsonl files directly if index is unavailable.
        """
        encoded_dir = str(project_dir.resolve()).replace("/", "-")
        sessions_path = CLAUDE_SESSIONS_DIR / encoded_dir
        if not sessions_path.is_dir():
            return []

        exclude_ids = exclude_ids or set()
        results = []

        # Try sessions-index.json first (CLI's authoritative metadata)
        index_file = sessions_path / "sessions-index.json"
        if index_file.exists():
            try:
                with open(index_file) as f:
                    index_data = json.load(f)

                # sessions-index.json is { "version": N, "entries": [...] }
                entries = index_data.get("entries", []) if isinstance(index_data, dict) else index_data

                for entry in entries:
                    session_id = entry.get("sessionId", "")
                    if session_id in exclude_ids:
                        continue
                    # Only include if the jsonl file actually exists
                    jsonl_path = sessions_path / f"{session_id}.jsonl"
                    if not jsonl_path.exists():
                        continue

                    summary = entry.get("summary") or entry.get("firstPrompt", "")[:30] or "(no message)"
                    modified = entry.get("modified", "")
                    created = entry.get("created", "")
                    project_alias = self._get_project_alias(project_dir)

                    results.append({
                        "session_id": session_id,
                        "summary": summary[:50],
                        "permission_mode": "default",
                        "project_alias": project_alias,
                        "project_dir": str(project_dir),
                        "created_at": created,
                        "last_active": modified or created,
                        "source": "cli",
                    })

                results.sort(key=lambda x: x["last_active"], reverse=True)
                return results
            except (json.JSONDecodeError, IOError, KeyError):
                pass  # Fall through to legacy scan

        # Fallback: scan .jsonl files directly (legacy behavior)
        for jsonl_file in sessions_path.glob("*.jsonl"):
            session_id = jsonl_file.stem
            if session_id in exclude_ids:
                continue

            mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime)

            summary = ""
            try:
                with open(jsonl_file) as f:
                    for line in f:
                        obj = json.loads(line)
                        if obj.get("type") == "user":
                            content = obj.get("message", {}).get("content", [])
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block["text"].strip()
                                    if not text.startswith("<"):
                                        summary = text[:30]
                                        break
                            break
            except (json.JSONDecodeError, IOError, KeyError, AttributeError, TypeError):
                pass

            if not summary:
                summary = "(no message)"

            project_alias = self._get_project_alias(project_dir)
            results.append({
                "session_id": session_id,
                "summary": summary,
                "permission_mode": "default",
                "project_alias": project_alias,
                "project_dir": str(project_dir),
                "created_at": mtime.isoformat(),
                "last_active": mtime.isoformat(),
                "source": "cli",
            })

        results.sort(key=lambda x: x["last_active"], reverse=True)
        return results

    def _get_merged_sessions(self, chat_id: str) -> list:
        """Get merged session list: bot sessions (current project) + CLI sessions, deduped and sorted."""
        current_project_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
        current_project_str = str(current_project_dir.resolve())

        # 1. Get bot sessions filtered by current project
        all_bot_sessions = self._get_chat_history(chat_id)
        bot_sessions = [
            {**entry, "source": entry.get("source", "bot")}
            for entry in all_bot_sessions
            if str(Path(entry.get("project_dir", "")).resolve()) == current_project_str
        ]
        bot_session_ids = {e["session_id"] for e in bot_sessions}

        # 2. Get CLI sessions, excluding those already in bot history
        cli_sessions = self._scan_cli_sessions(current_project_dir, exclude_ids=bot_session_ids)

        # 3. Merge and sort by last_active descending
        merged = bot_sessions + cli_sessions
        merged.sort(key=lambda x: x.get("last_active", ""), reverse=True)
        return merged[:10]

    def _handle_resume(self, arg: str | None, chat_id: str, message_id: str):
        """Handle /resume command: list history or resume a session."""
        merged = self._get_merged_sessions(chat_id)

        if not merged:
            project_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
            alias = self._get_project_alias(project_dir) or str(project_dir)
            self._reply_text(message_id, f"No sessions found for project: {alias}")
            return

        if arg is None:
            # List recent sessions for current project
            project_dir = self._chat_project_dirs.get(chat_id, self.default_project_dir)
            alias = self._get_project_alias(project_dir) or str(project_dir)
            lines = [f"Sessions for {alias}:\n"]
            for i, entry in enumerate(merged, 1):
                last_active = entry.get("last_active", "")
                try:
                    dt = datetime.fromisoformat(last_active)
                    time_str = dt.strftime("%-m/%-d %H:%M")
                except (ValueError, TypeError):
                    time_str = "?"
                summary = entry.get("summary", "?")
                source = entry.get("source", "bot")
                source_tag = "cli" if source == "cli" else "bot"
                lines.append(f'{i}. [{time_str}] "{summary}" [{source_tag}]')
            lines.append("\nUsage: /resume <number>")
            self._reply_text(message_id, "\n".join(lines))
            return

        # Resume specific session by number
        try:
            idx = int(arg.strip()) - 1
        except ValueError:
            self._reply_text(message_id, "Usage: /resume <number>\nExample: /resume 1")
            return

        if idx < 0 or idx >= len(merged):
            self._reply_text(message_id, f"Invalid number. Choose between 1 and {len(merged)}.")
            return

        entry = merged[idx]
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

        # Auto-switch project and model if different from current
        self._chat_project_dirs[chat_id] = project_dir
        model = entry.get("model", self.default_model)
        self._chat_models[chat_id] = model

        # Resume asynchronously
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._resume_session(chat_id, session_id, project_dir, project_alias, entry),
                self._loop,
            )

    async def _resume_session(self, chat_id: str, session_id: str, project_dir: Path, project_alias: str | None, entry: dict):
        """Resume a previous session by session_id."""
        try:
            # Close current session if any
            await self._close_session(chat_id)

            model = entry.get("model", self.default_model)
            print(f"  [Session] Resuming session {session_id[:8]}... for chat {chat_id[:8]}... (project: {project_dir}, model: {model})")
            client = create_client(
                project_dir=project_dir,
                model=model,
                resume=session_id,
            )

            session = ChatSession(chat_id=chat_id, client=client, project_dir=project_dir)
            session.session_id = session_id
            session.model = model
            session.project_alias = project_alias
            session.first_message = entry.get("summary")
            session.permission_mode = entry.get("permission_mode", "default")
            await session.connect()
            self._sessions[chat_id] = session

            mode_display = MODE_DISPLAY.get(session.permission_mode, session.permission_mode)
            summary = entry.get("summary", "?")
            self._send_message(
                chat_id,
                f'Session resumed: "{summary}" ({project_alias or "?"} | {mode_display})\n\n'
                "You can continue the conversation now.",
            )

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
        description="Feishu Bot for Claude Long-Runner",
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
