"""
Codex Agent Client — AgentClient implementation backed by OpenAI Codex Python SDK.
====================================================================================

Wraps the official OpenAI Codex Python SDK (openai-codex) to expose the unified
AgentClient protocol.

Installation:

    pip install openai-codex

Known issues & workarounds (as of 2026-04):
  - Issue #16554: 64 KiB stdio crash → avoid prompts > 60 KB
  - Issue #17829: FileChangeItem.status rejects "in_progress"
                  → caught below with try/except on each notification
  - Issue #19348: Unrecognised notification types → logged, not crashed
  - The 0.1.0b3 generated schema predates GPT-5.6 max/ultra effort values
                  → raw notifications are normalized below for newer CLIs

Upgrade strategy:
  - ALL Codex SDK imports are in this file.  No other module imports codex_*.
  - Notification handling is defensive (unknown types → warning, not crash).
  - When APIs change, only this file needs updating.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

from agent_protocol import AgentEvent, EventType, Feature

logger = logging.getLogger(__name__)

# ── Lazy SDK import ──────────────────────────────────────────────────────────
# The Codex SDK is optional.  If not installed, the class can still be
# *defined* (for type-checking), but instantiation will raise ImportError
# with a helpful message.

_CODEX_SDK_AVAILABLE = False
_CODEX_IMPORT_ERROR: str | None = None

try:
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, TextInput

    _CODEX_SDK_AVAILABLE = True
except ImportError as exc:
    _CODEX_IMPORT_ERROR = (
        f"Codex SDK not installed ({exc}).\n"
        "\n"
        "Install the official OpenAI package from PyPI:\n"
        "  pip install openai-codex\n"
        "\n"
        "The package installs a bundled Codex CLI fallback automatically; "
        "preview models may require a newer standalone Codex CLI."
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

DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_EFFORT = "high"


def _normalize_effort(value: str | None) -> str | None:
    """Translate effort value to a Codex-valid one, or None if unknown."""
    if value is None:
        return None
    mapped = _EFFORT_MAP.get(value.lower().strip())
    if mapped is None:
        logger.warning("Unknown effort value %r, dropping", value)
    return mapped


# The published SDK exposes two high-level approval modes.  Keep accepting the
# bot's legacy Claude/Codex vocabulary, but translate it at this boundary.
_DENY_ALL_APPROVAL_VALUES = {
    "bypass",
    "bypasspermissions",
    "dontask",
    "never",
}
_AUTO_REVIEW_APPROVAL_VALUES = {
    "acceptedits",
    "auto",
    "autoreview",
    "default",
    "onfailure",
    "onrequest",
    "plan",
    "untrusted",
}


def _normalize_approval_policy(value: str | None) -> str | None:
    """Translate a Claude/bot permission_mode to an SDK ApprovalMode value."""
    if value is None:
        return None
    key = value.lower().strip().replace("_", "").replace("-", "")
    if key in _DENY_ALL_APPROVAL_VALUES:
        return "deny_all"
    if key in _AUTO_REVIEW_APPROVAL_VALUES:
        return "auto_review"
    logger.warning(
        "Unknown approval_policy value %r, dropping (Codex will use default)",
        value,
    )
    return None


def _unwrap_thread_item(item: Any) -> Any:
    """
    ItemStarted/ItemCompleted notifications carry `item: ThreadItem`, which is
    a Pydantic RootModel discriminated union.  Unwrap to the inner typed item
    (AgentMessageThreadItem, CommandExecutionThreadItem, etc.) so attribute
    lookups like `.type`, `.text`, `.command` work directly.

    Falls back to the original object if it's already unwrapped — keeps the
    callers defensive against minor SDK shape changes.
    """
    if item is None:
        return None
    inner = getattr(item, "root", None)
    return inner if inner is not None else item


def _namespace_from_json(value: Any) -> Any:
    """Convert raw forward-compatible SDK payload dictionaries to objects."""
    if isinstance(value, dict):
        return SimpleNamespace(
            **{
                str(key): _namespace_from_json(item)
                for key, item in value.items()
                if str(key).isidentifier()
            }
        )
    if isinstance(value, list):
        return [_namespace_from_json(item) for item in value]
    return value


def _get_item_id(*objects: Any) -> str | None:
    """
    Best-effort item id extraction across Codex SDK model shapes.

    Notification payloads and thread items have changed names across SDK
    versions (`item_id`, `itemId`, nested `item.id`, RootModel-wrapped item).
    The id is only used for de-duplicating streamed agent text, so failing to
    find one should degrade to current-message comparison instead of raising.
    """
    for obj in objects:
        if obj is None:
            continue
        for attr in ("item_id", "itemId", "id"):
            value = getattr(obj, attr, None)
            if value:
                return str(value)

        nested = getattr(obj, "item", None)
        if nested is not None:
            nested = _unwrap_thread_item(nested)
            for attr in ("item_id", "itemId", "id"):
                value = getattr(nested, attr, None)
                if value:
                    return str(value)

    return None


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
    config = CodexConfig(codex_bin=codex_bin) if codex_bin else None
    codex = AsyncCodex(config=config) if config else AsyncCodex()

    # Codex's thread_list defaults to "interactive sources" only — which
    # excludes threads tagged `unknown`.  Sessions started via the
    # app-server SDK (i.e. our Feishu bot) come back as `unknown`, so the
    # default filter silently hides them.  Pass an explicit list that
    # includes the kinds a user would want to resume from the bot.
    source_kinds = ["cli", "vscode", "exec", "appServer", "unknown"]

    try:
        await codex.__aenter__()
        result = await codex.thread_list(
            cwd=project_dir,
            limit=limit,
            source_kinds=source_kinds,
            sort_key="updated_at",
            sort_direction="desc",
        )
    except Exception as e:
        logger.warning("Codex thread_list failed for %s: %s", project_dir, e)
        try:
            await codex.__aexit__(None, None, None)
        except Exception:
            pass
        return []

    def _path_str(value, fallback: str) -> str:
        # Codex SDK returns `cwd` as an `AbsolutePathBuf` RootModel; unwrap
        # to a plain str so callers can pass it to `pathlib.Path(...)`.
        if value is None:
            return fallback
        if isinstance(value, str):
            return value
        root = getattr(value, "root", None)
        if isinstance(root, str):
            return root
        return str(value)

    def _thread_model(thread: Any) -> str:
        model = getattr(thread, "model", None) or getattr(thread, "model_id", None)
        if isinstance(model, str) and model:
            return model

        path = _path_str(getattr(thread, "path", None), "")
        if not path:
            return ""

        last_model = ""
        try:
            with open(path) as f:
                for line in f:
                    if '"turn_context"' not in line:
                        continue
                    obj = json.loads(line)
                    model = obj.get("payload", {}).get("model")
                    if isinstance(model, str) and model:
                        last_model = model
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            return ""
        return last_model

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
                "project_dir": _path_str(getattr(t, "cwd", None), project_dir),
                "created_at": created,
                "last_active": updated,
                # ThreadList exposes model_provider (e.g. "openai"), not the
                # model id. Do not persist provider names as resumable models.
                "model": _thread_model(t),
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
      2. `codex` on PATH (kept current by the operator)
      3. None → the SDK's bundled `openai-codex-cli-bin` fallback

    Preview models can require a CLI newer than the Python SDK's bundled
    runtime, so an up-to-date standalone CLI takes precedence when available.
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
        model: str = DEFAULT_CODEX_MODEL,
        approval_policy: str | None = None,
        resume_thread_id: str | None = None,
        effort: str | None = DEFAULT_CODEX_EFFORT,
        max_turns: int = 1000,
        **extra,
    ):
        if not _CODEX_SDK_AVAILABLE:
            raise ImportError(_CODEX_IMPORT_ERROR)

        self._project_dir = str(Path(project_dir).resolve()) if project_dir else None
        self._model = model
        self._approval_policy = _normalize_approval_policy(approval_policy)
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
        self._streamed_agent_text: str = ""
        self._streamed_agent_text_by_item: dict[str, str] = {}
        self._current_streamed_agent_item_id: str | None = None

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
          - thread_start(): model, cwd, approval_mode     (set once)
          - thread.turn():  effort, model override        (per-message)
        So `effort` is stored on self and applied in send_message().
        """
        # Prefer an explicit/current standalone CLI because preview models can
        # require a newer runtime than the SDK bundle. Fall back to the bundle.
        codex_bin = _resolve_codex_bin()
        if codex_bin:
            logger.info("Using codex binary at %s", codex_bin)
            config = CodexConfig(codex_bin=codex_bin)
            self._codex = AsyncCodex(config=config)
        else:
            self._codex = AsyncCodex()
        await self._codex.__aenter__()

        # Thread-level kwargs only — see thread_start signature
        thread_kwargs: dict[str, Any] = {"model": self._model}
        if self._project_dir:
            thread_kwargs["cwd"] = self._project_dir
        if self._approval_policy:
            thread_kwargs["approval_mode"] = ApprovalMode(self._approval_policy)

        # Enable web browsing by default so the model can search / fetch URLs.
        # WebSearchMode: disabled | cached | live  — "live" hits the network.
        # Caller can override via the `config` extra kwarg.
        thread_kwargs["config"] = {"web_search": "live"}

        # Forward any extra kwargs the caller provided that match thread_start's API
        # (unknown kwargs would crash thread_start, so callers must know the schema).
        # If caller passes their own `config`, shallow-merge so web_search stays on
        # unless they explicitly override it.
        extra = dict(self._extra)
        if isinstance(extra.get("config"), dict):
            thread_kwargs["config"] = {**thread_kwargs["config"], **extra.pop("config")}
        thread_kwargs.update(extra)

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

        self._streamed_agent_text = ""
        self._streamed_agent_text_by_item = {}
        self._current_streamed_agent_item_id = None
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
                turn_completed = getattr(notification, "method", "") == "turn/completed"
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
                    if turn_completed:
                        break
                    continue
                # A newer CLI may add fields/enums that the published SDK's
                # generated TurnCompletedNotification does not yet know. The
                # SDK then exposes a raw UnknownNotification and its own
                # stream cannot recognize the terminator, so stop by method.
                if turn_completed:
                    break
        except Exception as e:
            # Stream-level error — yield as ERROR event so the caller can
            # decide how to handle it (e.g. show message to user). Skip the
            # trailing RESULT to keep the event contract symmetric with the
            # Claude backend, which only emits RESULT on a real ResultMessage.
            logger.error("Codex stream error: %s", e)
            yield AgentEvent(
                type=EventType.ERROR,
                metadata={"error": str(e)},
            )
            return

        yield AgentEvent(
            type=EventType.RESULT,
            metadata={"session_id": self._session_id},
        )

    # ── Control ──────────────────────────────────────────────────────────

    def interrupt(self) -> None:
        if self._turn_handle:
            try:
                asyncio.get_running_loop().create_task(self._turn_handle.interrupt())
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
        raw_params = getattr(payload, "params", None)
        if isinstance(raw_params, dict):
            payload = _namespace_from_json(raw_params)

        # Diagnostic — every notification, with payload type + key fields, so we
        # can see why a turn produced no TEXT events.  Set CODEX_DEBUG_NOTIFS=0
        # to silence after debugging.
        if os.environ.get("CODEX_DEBUG_NOTIFS", "1") != "0":
            payload_type = type(payload).__name__
            extra = ""
            item = getattr(payload, "item", None)
            if item is not None:
                inner = getattr(item, "root", item)
                extra = f" item.type={getattr(inner, 'type', '?')!r}"
            elif hasattr(payload, "delta"):
                d = getattr(payload, "delta", "")
                if isinstance(d, str) and len(d) > 60:
                    extra = f" delta={d[:60]!r}…"
                else:
                    extra = f" delta={d!r}"
            logger.info("[codex notif] %s payload=%s%s", method, payload_type, extra)

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
            text = str(text)
            if method.startswith("item/agentMessage/delta"):
                item_id = _get_item_id(payload)
                if item_id and item_id != self._current_streamed_agent_item_id:
                    self._streamed_agent_text = ""
                    self._current_streamed_agent_item_id = item_id
                self._streamed_agent_text += text
                if item_id:
                    self._streamed_agent_text_by_item[item_id] = (
                        self._streamed_agent_text_by_item.get(item_id, "") + text
                    )
            return AgentEvent(type=EventType.TEXT, text=text)
        return None

    def _on_item_started(self, method: str, payload: Any) -> AgentEvent | None:
        """Handle item/started — usually a tool invocation beginning."""
        item = _unwrap_thread_item(getattr(payload, "item", payload))
        item_type = getattr(item, "type", "")

        # Item types use camelCase in the SDK schema (agentMessage,
        # commandExecution, fileChange, mcpToolCall, etc.).
        if item_type in ("commandExecution", "fileChange", "mcpToolCall"):
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
        """Handle item/completed — final assistant message or tool result."""
        item = _unwrap_thread_item(getattr(payload, "item", payload))
        item_type = getattr(item, "type", "")

        if item_type in ("commandExecution", "fileChange", "mcpToolCall"):
            status = getattr(item, "status", "completed")
            output = getattr(item, "output", None) or getattr(item, "result", None)
            return AgentEvent(
                type=EventType.TOOL_RESULT,
                result_content=str(output)[:500] if output else "",
                is_error=(status == "failed"),
            )
        elif item_type == "agentMessage":
            # Final assistant message — codex often emits only the completed
            # item (no per-token deltas), so this is what users actually see.
            # When deltas were already emitted, completed is a full snapshot
            # of the same message.  Returning it again makes short replies
            # look doubled in Feishu (e.g. "1" -> "11").
            text = getattr(item, "text", None) or getattr(item, "content", None)
            if text:
                text = str(text)
                item_id = _get_item_id(payload, item)
                streamed = (
                    self._streamed_agent_text_by_item.get(item_id, "")
                    if item_id
                    else ""
                )
                if not streamed:
                    streamed = self._streamed_agent_text

                if streamed:
                    if text == streamed:
                        if item_id:
                            self._streamed_agent_text_by_item.pop(item_id, None)
                        self._streamed_agent_text = ""
                        self._current_streamed_agent_item_id = None
                        return None
                    if text.startswith(streamed):
                        suffix = text[len(streamed):]
                        if item_id:
                            self._streamed_agent_text_by_item.pop(item_id, None)
                        self._streamed_agent_text = ""
                        self._current_streamed_agent_item_id = None
                        if suffix:
                            return AgentEvent(type=EventType.TEXT, text=suffix)
                        return None

                    self._streamed_agent_text = ""
                    self._current_streamed_agent_item_id = None

                return AgentEvent(type=EventType.TEXT, text=text)
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
        """Handle turn/completed — signals end of turn.  Surfaces failure
        info because codex sometimes ends a turn with status=failed and no
        item notifications (e.g. expired auth token), which would otherwise
        look like "silent nothing" to the bot user."""
        turn = getattr(payload, "turn", None)
        status = getattr(turn, "status", None)
        error = getattr(turn, "error", None)

        if status is not None and str(status) not in ("completed", "TurnStatus.completed"):
            logger.warning("Codex turn ended status=%s error=%s", status, error)
            error_msg = ""
            if error is not None:
                error_msg = (
                    getattr(error, "message", None)
                    or getattr(error, "detail", None)
                    or str(error)
                )
            return AgentEvent(
                type=EventType.ERROR,
                metadata={
                    "error": f"Codex turn {status}: {error_msg}" if error_msg else f"Codex turn {status}",
                    "turn_completed": True,
                    "session_id": self._session_id,
                },
            )

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
