"""
Codex Agent Client — AgentClient implementation backed by OpenAI Codex Python SDK.
====================================================================================

Wraps the Codex Python SDK (codex-app-server-sdk) to expose the unified
AgentClient protocol.

Installation (SDK is not yet on PyPI — install from source):

    git clone https://github.com/openai/codex.git
    cd codex/sdk/python
    pip install -e .

Known issues & workarounds (as of 2026-04):
  - Issue #16554: 64 KiB stdio crash → avoid prompts > 60 KB
  - Issue #17829: FileChangeItem.status rejects "in_progress"
                  → caught below with try/except on each notification
  - Issue #19348: Unrecognised notification types → logged, not crashed

Upgrade strategy:
  - ALL Codex SDK imports are in this file.  No other module imports codex_*.
  - Notification handling is defensive (unknown types → warning, not crash).
  - When the SDK publishes to PyPI, just change the install command.
  - When APIs change, only this file needs updating.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

from agent_protocol import AgentClient, AgentEvent, EventType, Feature

logger = logging.getLogger(__name__)

# ── Lazy SDK import ──────────────────────────────────────────────────────────
# The Codex SDK is optional.  If not installed, the class can still be
# *defined* (for type-checking), but instantiation will raise ImportError
# with a helpful message.

_CODEX_SDK_AVAILABLE = False
_CODEX_IMPORT_ERROR: str | None = None

try:
    from codex_app_server import AsyncCodex
    from codex_app_server._inputs import TextInput
    from codex_app_server.client import AppServerConfig

    _CODEX_SDK_AVAILABLE = True
except ImportError as exc:
    _CODEX_IMPORT_ERROR = (
        f"Codex SDK not installed ({exc}).\n"
        "\n"
        "Install from source:\n"
        "  pip install git+https://github.com/openai/codex.git#subdirectory=sdk/python\n"
        "\n"
        "You also need the codex CLI binary installed (npm install -g @openai/codex,\n"
        "or brew install codex).\n"
        "\n"
        "Or, when the SDK is published to PyPI with bundled binary:\n"
        "  pip install codex-app-server-sdk"
    )


def codex_available() -> bool:
    """Check whether the Codex SDK is importable."""
    return _CODEX_SDK_AVAILABLE


# Effort level mapping: Claude/bot vocabulary → Codex ReasoningEffort
# Codex valid values: none, minimal, low, medium, high, xhigh
# Claude/bot values:  low, medium, high, xhigh, max
_EFFORT_MAP = {
    "max": "xhigh",      # Claude's "max" → Codex's highest "xhigh"
    "xhigh": "xhigh",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "minimal": "minimal",
    "none": "none",
}


def _normalize_effort(value: str | None) -> str | None:
    """Translate effort value to a Codex-valid one, or None if unknown."""
    if value is None:
        return None
    mapped = _EFFORT_MAP.get(value.lower().strip())
    if mapped is None:
        logger.warning("Unknown effort value %r, dropping", value)
    return mapped


async def list_codex_threads(project_dir: str, limit: int = 10) -> list[dict]:
    """
    List Codex threads for a project directory.

    Returns a list of dicts with the same shape as Claude CLI session entries
    (so the bot can merge / display them uniformly):
        {session_id, summary, permission_mode, project_dir,
         created_at, last_active, model, backend}

    Spawns a temporary AsyncCodex instance — does NOT interfere with any
    chat session's running app-server process.
    """
    if not _CODEX_SDK_AVAILABLE:
        return []

    from datetime import datetime as _dt

    codex_bin = _resolve_codex_bin()
    config = AppServerConfig(codex_bin=codex_bin) if codex_bin else None
    codex = AsyncCodex(config=config) if config else AsyncCodex()

    try:
        await codex.__aenter__()
        result = await codex.thread_list(cwd=project_dir, limit=limit)
    except Exception as e:
        logger.warning("Codex thread_list failed for %s: %s", project_dir, e)
        try:
            await codex.__aexit__(None, None, None)
        except Exception:
            pass
        return []

    threads: list[dict] = []
    for t in getattr(result, "data", []) or []:
        try:
            created = _dt.fromtimestamp(t.created_at).isoformat() if t.created_at else ""
            updated = _dt.fromtimestamp(t.updated_at).isoformat() if t.updated_at else ""
            preview = (getattr(t, "preview", "") or getattr(t, "name", "") or "(no preview)")
            threads.append({
                "session_id": t.id,
                "summary": preview[:50],
                "permission_mode": "default",  # Codex has no Claude-style modes
                "project_alias": None,         # filled in by caller
                "project_dir": getattr(t, "cwd", project_dir),
                "created_at": created,
                "last_active": updated,
                "model": getattr(t, "model_provider", "") or "",
                "backend": "codex",
                "source": "codex",
            })
        except Exception as e:
            logger.warning("Skipping malformed Codex thread entry: %s", e)
            continue

    try:
        await codex.__aexit__(None, None, None)
    except Exception:
        pass

    return threads


def _resolve_codex_bin() -> str | None:
    """
    Find the Codex CLI binary.

    Priority:
      1. CODEX_BIN environment variable (explicit override)
      2. `codex` on PATH (e.g. installed via npm or homebrew)
      3. None → let the SDK use its bundled runtime (if available)

    The SDK published to PyPI bundles `openai-codex-cli-bin`, but the
    install-from-source path does NOT include the binary.  This resolver
    bridges the gap.
    """
    explicit = os.environ.get("CODEX_BIN")
    if explicit and Path(explicit).is_file():
        return explicit

    on_path = shutil.which("codex")
    if on_path:
        return on_path

    return None


# ── Notification → AgentEvent mapping ────────────────────────────────────────
#
# The Codex SDK streams `Notification(method: str, payload: BaseModel)`.
# We map the most common method prefixes to AgentEvent types.
# Unknown methods are logged at DEBUG and skipped — this is how we stay
# forward-compatible when the SDK adds new notification types.

# Method prefix → handler name (looked up on the class)
_NOTIFICATION_HANDLERS: dict[str, str] = {
    "item/agentMessage/delta":           "_on_text_delta",
    "item/started":                      "_on_item_started",
    "item/completed":                    "_on_item_completed",
    "item/commandExecution/outputDelta": "_on_tool_output",
    "item/fileChange/outputDelta":       "_on_tool_output",
    "item/mcpToolCall/progress":         "_on_tool_output",
    "item/plan/delta":                   "_on_text_delta",
    "item/reasoning/textDelta":          "_on_text_delta",
    "turn/started":                      "_on_turn_started",
    "turn/completed":                    "_on_turn_completed",
    "hook/started":                      "_on_hook",
    "hook/completed":                    "_on_hook",
    "thread/tokenUsage/updated":         "_on_usage",
}


class CodexAgentClient:
    """
    AgentClient backed by the OpenAI Codex Python SDK.

    Each instance manages:
      - One `AsyncCodex` context  (≈ one codex app-server subprocess)
      - One `AsyncThread`         (≈ one conversation)
      - One `AsyncTurnHandle`     per send_message() call (≈ one turn)

    Concurrency: Because of the single-consumer limitation (SDK issue),
    each FeishuBot ChatSession gets its own CodexAgentClient, so there
    is never more than one active stream per instance.
    """

    # ── Features supported by this backend ───────────────────────────────

    _SUPPORTED_FEATURES = frozenset({
        Feature.SESSION_RESUME,
        Feature.STREAMING,
        Feature.INTERRUPT,
        # NOT supported:
        #   Feature.PERMISSION_MODE  — no dynamic mode switch
        #   Feature.MCP_SERVERS      — managed by Rust runtime, not SDK
        #   Feature.SECURITY_HOOKS   — no pre-tool-use hooks
    })

    def __init__(
        self,
        project_dir: str | None = None,
        model: str = "o3",
        approval_policy: str | None = None,
        resume_thread_id: str | None = None,
        effort: str | None = None,
        max_turns: int = 1000,
        **extra,
    ):
        if not _CODEX_SDK_AVAILABLE:
            raise ImportError(_CODEX_IMPORT_ERROR)

        self._project_dir = str(Path(project_dir).resolve()) if project_dir else None
        self._model = model
        self._approval_policy = approval_policy
        self._resume_thread_id = resume_thread_id
        self._effort = _normalize_effort(effort)  # map Claude vocab → Codex
        self._max_turns = max_turns
        self._extra = extra

        # SDK objects — initialized in connect()
        self._codex: AsyncCodex | None = None
        self._thread: Any = None          # AsyncThread
        self._turn_handle: Any = None     # AsyncTurnHandle
        self._session_id: str | None = resume_thread_id
        self._connected: bool = False

    # ── Identity ─────────────────────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        return "codex"

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Start the Codex app-server subprocess and open (or resume) a thread.

        Parameter mapping (Codex SDK separates thread-level vs turn-level):
          - thread_start(): model, cwd, approval_policy   (set once)
          - thread.turn():  effort, model override        (per-message)
        So `effort` is stored on self and applied in send_message().
        """
        # Build AppServerConfig — explicitly point to the codex binary if
        # the bundled runtime isn't available (source-install workaround).
        codex_bin = _resolve_codex_bin()
        if codex_bin:
            logger.info("Using codex binary at %s", codex_bin)
            config = AppServerConfig(codex_bin=codex_bin)
            self._codex = AsyncCodex(config=config)
        else:
            self._codex = AsyncCodex()
        await self._codex.__aenter__()

        # Thread-level kwargs only — see thread_start signature
        thread_kwargs: dict[str, Any] = {"model": self._model}
        if self._project_dir:
            thread_kwargs["cwd"] = self._project_dir
        if self._approval_policy:
            thread_kwargs["approval_policy"] = self._approval_policy

        # Forward any extra kwargs the caller provided that match thread_start's API
        # (unknown kwargs would crash thread_start, so callers must know the schema)
        thread_kwargs.update(self._extra)

        if self._resume_thread_id:
            logger.info("Resuming Codex thread %s", self._resume_thread_id[:8])
            self._thread = await self._codex.thread_resume(
                self._resume_thread_id,
                **thread_kwargs,
            )
        else:
            self._thread = await self._codex.thread_start(**thread_kwargs)

        self._session_id = self._thread.id
        self._connected = True
        logger.info("Codex connected — thread %s", self._session_id[:8] if self._session_id else "?")

    async def disconnect(self) -> None:
        if self._connected and self._codex:
            try:
                await self._codex.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("Codex disconnect error: %s", e)
            self._codex = None
            self._thread = None
            self._turn_handle = None
            self._connected = False

    # ── Conversation ─────────────────────────────────────────────────────

    async def send_message(self, prompt: str) -> None:
        """
        Start a new turn.  The TurnHandle is stored so receive_events()
        can stream from it.

        Per-turn options (effort, model override) are applied here, not
        at thread_start time, because the Codex SDK separates them.
        """
        if not self._thread:
            raise RuntimeError("Not connected — call connect() first")

        turn_kwargs: dict[str, Any] = {}
        if self._effort:
            turn_kwargs["effort"] = self._effort

        self._turn_handle = await self._thread.turn(TextInput(prompt), **turn_kwargs)

    async def receive_events(self) -> AsyncIterator[AgentEvent]:
        """
        Stream Codex notifications and map them to AgentEvent.

        Defensive handling:
          - Unknown notification methods → logged, skipped
          - Pydantic ValidationError on a notification → logged, skipped
            (workaround for SDK issue #17829)
        """
        if not self._turn_handle:
            return

        try:
            async for notification in self._turn_handle.stream():
                try:
                    event = self._map_notification(notification)
                    if event is not None:
                        yield event
                except Exception as e:
                    # Issue #17829: FileChangeItem.status "in_progress"
                    # causes Pydantic ValidationError.  Don't let it
                    # kill the entire stream.
                    logger.warning(
                        "Skipping malformed Codex notification (%s): %s",
                        getattr(notification, "method", "?"),
                        e,
                    )
                    continue
        except Exception as e:
            # Stream-level error — yield as ERROR event so the caller can
            # decide how to handle it (e.g. show message to user)
            logger.error("Codex stream error: %s", e)
            yield AgentEvent(
                type=EventType.ERROR,
                metadata={"error": str(e)},
            )

        # Always emit a RESULT at the end so the caller knows we're done
        yield AgentEvent(
            type=EventType.RESULT,
            metadata={"session_id": self._session_id},
        )

    # ── Control ──────────────────────────────────────────────────────────

    def interrupt(self) -> None:
        if self._turn_handle:
            try:
                self._turn_handle.interrupt()
            except Exception as e:
                logger.warning("Codex interrupt error: %s", e)

    # ── Capabilities ─────────────────────────────────────────────────────

    def supports(self, feature: Feature | str) -> bool:
        feat = Feature(feature) if isinstance(feature, str) else feature
        return feat in self._SUPPORTED_FEATURES

    async def set_permission_mode(self, mode: str) -> None:
        raise NotImplementedError(
            "Codex does not support dynamic permission mode switching.  "
            "Set approval_policy at thread creation time instead."
        )

    # ── Notification Mapping (private) ───────────────────────────────────

    def _map_notification(self, notification: Any) -> AgentEvent | None:
        """
        Map a single Codex Notification to an AgentEvent.

        Returns None for notifications we intentionally skip (e.g. usage updates).
        """
        method: str = getattr(notification, "method", "")
        payload: Any = getattr(notification, "payload", None)

        # Look up handler by exact method match first, then by prefix
        handler_name = _NOTIFICATION_HANDLERS.get(method)
        if handler_name is None:
            # Try prefix match for forward-compat
            for prefix, name in _NOTIFICATION_HANDLERS.items():
                if method.startswith(prefix):
                    handler_name = name
                    break

        if handler_name is None:
            logger.debug("Unknown Codex notification: %s", method)
            return None

        handler = getattr(self, handler_name, None)
        if handler is None:
            return None

        return handler(method, payload)

    # ── Individual notification handlers ─────────────────────────────────

    def _on_text_delta(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle text delta notifications (agent message, plan, reasoning)."""
        text = None
        if payload:
            # AgentMessageDeltaNotification has .delta or .text
            text = getattr(payload, "delta", None) or getattr(payload, "text", None)
            # Some payloads nest it deeper
            if text is None and hasattr(payload, "content"):
                text = str(payload.content)
        if text:
            return AgentEvent(type=EventType.TEXT, text=text)
        return None

    def _on_item_started(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle item/started — usually a tool invocation beginning."""
        item = getattr(payload, "item", payload)
        item_type = getattr(item, "type", "")

        # Detect tool use from item type
        if item_type in ("command_execution", "file_change", "mcp_tool_call"):
            tool_name = (
                getattr(item, "name", None)
                or getattr(item, "command", None)
                or item_type
            )
            tool_input = getattr(item, "input", None) or getattr(item, "args", None)
            return AgentEvent(
                type=EventType.TOOL_USE,
                tool_name=str(tool_name),
                tool_input=tool_input,
            )
        return None

    def _on_item_completed(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle item/completed — tool execution result."""
        item = getattr(payload, "item", payload)
        item_type = getattr(item, "type", "")

        if item_type in ("command_execution", "file_change", "mcp_tool_call"):
            status = getattr(item, "status", "completed")
            output = getattr(item, "output", None) or getattr(item, "result", None)
            return AgentEvent(
                type=EventType.TOOL_RESULT,
                result_content=str(output)[:500] if output else "",
                is_error=(status == "failed"),
            )
        elif item_type == "agent_message":
            # Final message text
            text = getattr(item, "text", None) or getattr(item, "content", None)
            if text:
                return AgentEvent(type=EventType.TEXT, text=str(text))
        return None

    def _on_tool_output(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle incremental tool output (command, file change, MCP)."""
        delta = getattr(payload, "delta", None) or getattr(payload, "output", None)
        if delta:
            return AgentEvent(
                type=EventType.TOOL_RESULT,
                result_content=str(delta),
                is_error=False,
            )
        return None

    def _on_turn_started(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle turn/started — session metadata."""
        return AgentEvent(
            type=EventType.SYSTEM,
            metadata={"turn_started": True},
        )

    def _on_turn_completed(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle turn/completed — signals end of turn."""
        # We emit RESULT in receive_events() after the stream ends,
        # so this is just metadata.
        return AgentEvent(
            type=EventType.SYSTEM,
            metadata={
                "turn_completed": True,
                "session_id": self._session_id,
            },
        )

    def _on_hook(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle hook lifecycle — informational only."""
        hook_name = getattr(payload, "name", "unknown")
        is_start = "started" in method
        logger.debug("Codex hook %s: %s", "started" if is_start else "completed", hook_name)
        return None  # skip — hooks are internal to the runtime

    def _on_usage(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle token usage updates — skip for now."""
        return None
