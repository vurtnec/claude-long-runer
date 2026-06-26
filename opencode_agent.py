"""
OpenCode Agent Client -- AgentClient implementation backed by the OpenCode CLI.
================================================================================

This adapter uses `opencode run --format json` as a JSONL event stream.  It keeps
the integration Python-native: no Node bridge, no JS SDK dependency, and no
long-lived OpenCode server process to supervise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from agent_protocol import AgentClient, AgentEvent, EventType, Feature

logger = logging.getLogger(__name__)

DEFAULT_OPENCODE_BIN = "opencode"

_VARIANT_MAP = {
    "max": "max",
    "xhigh": "max",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "minimal": "minimal",
    "none": "none",
}

_SKIP_PERMISSION_VALUES = {
    "bypass",
    "bypasspermissions",
    "never",
    "dangerouslyskippermissions",
}

_RUNNING_TOOL_STATES = {"pending", "running", "processing", "queued", "started"}
_ERROR_TOOL_STATES = {"error", "failed", "cancelled", "canceled"}


def _resolve_opencode_bin() -> str | None:
    """Resolve the OpenCode CLI binary from OPENCODE_BIN or PATH."""
    explicit = os.environ.get("OPENCODE_BIN")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        logger.warning("OPENCODE_BIN is set but not executable: %s", explicit)

    return shutil.which(DEFAULT_OPENCODE_BIN)


def opencode_available() -> bool:
    """Return whether the OpenCode CLI is available."""
    return _resolve_opencode_bin() is not None


def _normalize_variant(value: str | None) -> str | None:
    if not value:
        return None
    key = value.lower().strip()
    mapped = _VARIANT_MAP.get(key)
    if mapped is None:
        logger.warning("Unknown OpenCode variant/effort value %r, dropping", value)
    return mapped


def _should_skip_permissions(value: str | None) -> bool:
    if not value:
        return False
    key = value.lower().strip().replace("_", "").replace("-", "")
    return key in _SKIP_PERMISSION_VALUES


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _get_nested(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _event_type(value: Any) -> str:
    return str(value or "").replace("-", "_").replace(".", "_")


def _extract_session_id(data: dict[str, Any]) -> str | None:
    part = data.get("part") if isinstance(data.get("part"), dict) else {}
    for source in (data, part):
        value = _get_nested(
            source,
            "sessionID",
            "sessionId",
            "session_id",
            "session",
        )
        if value:
            return str(value)
    return None


def _path_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    root = getattr(value, "root", None)
    if isinstance(root, str):
        return root
    return str(value)


def _format_time(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds).isoformat()
        except (OSError, OverflowError, ValueError):
            return ""
    return str(value)


def _extract_model(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        provider = value.get("providerID") or value.get("provider") or value.get("provider_id")
        model = value.get("modelID") or value.get("model") or value.get("model_id")
        if provider and model:
            return f"{provider}/{model}"
        return _stringify(value)
    return str(value)


def _load_sessions_json(stdout: str) -> list[Any]:
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed OpenCode session list line: %s", line[:120])
        return items

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("sessions", "data", "items", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    return []


def _normalize_session_entry(entry: Any, project_dir: str) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None

    session_id = _get_nested(entry, "id", "sessionID", "sessionId", "session_id")
    if not session_id:
        return None

    entry_project = _path_str(
        _get_nested(entry, "cwd", "projectDir", "project_dir", "directory", "root")
    )
    if entry_project:
        try:
            if Path(entry_project).resolve() != Path(project_dir).resolve():
                return None
        except OSError:
            pass

    title = (
        _get_nested(entry, "title", "name", "summary", "description")
        or _get_nested(entry, "prompt", "preview")
        or str(session_id)
    )
    created = _format_time(
        _get_nested(entry, "createdAt", "created_at", "created", "timeCreated")
    )
    updated = _format_time(
        _get_nested(entry, "updatedAt", "updated_at", "updated", "timeUpdated", "lastActive")
    )

    return {
        "session_id": str(session_id),
        "summary": str(title)[:50],
        "custom_title": entry.get("title") or entry.get("name"),
        "permission_mode": "default",
        "project_alias": None,
        "project_dir": entry_project or project_dir,
        "created_at": created or updated,
        "last_active": updated or created,
        "model": _extract_model(entry.get("model")),
        "backend": "opencode",
        "source": "opencode",
    }


async def list_opencode_sessions(project_dir: str, limit: int = 10) -> list[dict[str, Any]]:
    """List OpenCode sessions, normalized to the bot's common session shape."""
    opencode_bin = _resolve_opencode_bin()
    if not opencode_bin:
        return []

    cmd = [
        opencode_bin,
        "session",
        "list",
        "--format",
        "json",
        "--max-count",
        str(limit),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
    except OSError as exc:
        logger.warning("OpenCode session list failed: %s", exc)
        return []

    if proc.returncode != 0:
        stderr = stderr_b.decode(errors="replace").strip()
        logger.warning("OpenCode session list exited %s: %s", proc.returncode, stderr)
        return []

    raw_entries = _load_sessions_json(stdout_b.decode(errors="replace"))
    sessions: list[dict[str, Any]] = []
    for raw in raw_entries:
        normalized = _normalize_session_entry(raw, project_dir)
        if normalized:
            sessions.append(normalized)

    sessions.sort(key=lambda item: item.get("last_active") or "", reverse=True)
    return sessions[:limit]


class OpenCodeAgentClient:
    """AgentClient backed by `opencode run --format json`."""

    _SUPPORTED_FEATURES = frozenset(
        {
            Feature.SESSION_RESUME,
            Feature.STREAMING,
            Feature.INTERRUPT,
            Feature.MCP_SERVERS,
        }
    )

    def __init__(
        self,
        project_dir: str | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        resume_session_id: str | None = None,
        effort: str | None = None,
        max_turns: int = 1000,
        **extra: Any,
    ):
        self._project_dir = Path(project_dir).resolve() if project_dir else Path.cwd()
        self._model = model or None
        self._permission_mode = permission_mode
        self._resume_session_id = resume_session_id
        self._variant = _normalize_variant(effort)
        self._max_turns = max_turns
        self._agent = extra.get("agent")
        self._title = extra.get("title")
        self._attach = extra.get("attach")

        self._opencode_bin: str | None = None
        self._session_id: str | None = resume_session_id
        self._connected = False
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_chunks: list[str] = []
        self._stderr_task: asyncio.Task | None = None

    @property
    def backend_name(self) -> str:
        return "opencode"

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def connect(self) -> None:
        opencode_bin = _resolve_opencode_bin()
        if not opencode_bin:
            raise ImportError(
                "OpenCode CLI not found. Install it from https://opencode.ai/ "
                "or set OPENCODE_BIN to the executable path."
            )
        self._opencode_bin = opencode_bin
        self._connected = True

    async def disconnect(self) -> None:
        if self._process and self._process.returncode is None:
            self.interrupt()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None
        self._connected = False

    async def send_message(self, prompt: str) -> None:
        if not self._connected:
            raise RuntimeError("Not connected -- call connect() first")
        if self._process and self._process.returncode is None:
            raise RuntimeError("OpenCode is already processing a message")

        cmd = self._build_command(prompt)
        self._stderr_chunks = []
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self._project_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._process.stderr:
            self._stderr_task = asyncio.create_task(
                self._collect_stderr(self._process.stderr)
            )

    def _build_command(self, prompt: str) -> list[str]:
        if not self._opencode_bin:
            self._opencode_bin = _resolve_opencode_bin() or DEFAULT_OPENCODE_BIN

        cmd = [
            self._opencode_bin,
            "run",
            "--format",
            "json",
            "--dir",
            str(self._project_dir),
        ]

        if self._session_id:
            cmd.extend(["--session", self._session_id])
        if self._model:
            cmd.extend(["--model", self._model])
        if self._variant:
            cmd.extend(["--variant", self._variant])
        if self._agent:
            cmd.extend(["--agent", str(self._agent)])
        if self._title:
            cmd.extend(["--title", str(self._title)])
        if self._attach:
            cmd.extend(["--attach", str(self._attach)])
        if _should_skip_permissions(self._permission_mode):
            cmd.append("--dangerously-skip-permissions")

        cmd.extend(["--", prompt])
        return cmd

    async def _collect_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.readline()
            if not chunk:
                break
            self._stderr_chunks.append(chunk.decode(errors="replace"))

    async def receive_events(self) -> AsyncIterator[AgentEvent]:
        if not self._process:
            return

        proc = self._process
        if proc.stdout:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    yield AgentEvent(
                        type=EventType.ERROR,
                        metadata={"error": f"Malformed OpenCode JSON event: {text[:200]}"},
                    )
                    continue

                event = self._map_json_event(data)
                if event is not None:
                    yield event

        returncode = await proc.wait()
        if self._stderr_task:
            try:
                await self._stderr_task
            except Exception as exc:
                logger.warning("OpenCode stderr reader failed: %s", exc)

        stderr = "".join(self._stderr_chunks).strip()
        if returncode != 0:
            error = stderr or f"OpenCode exited with code {returncode}"
            yield AgentEvent(type=EventType.ERROR, metadata={"error": error})

        yield AgentEvent(
            type=EventType.RESULT,
            metadata={
                "session_id": self._session_id,
                "is_error": returncode != 0,
                "error": stderr if returncode != 0 else None,
            },
        )

    def _map_json_event(self, data: dict[str, Any]) -> AgentEvent | None:
        session_id = _extract_session_id(data)
        if session_id:
            self._session_id = session_id

        raw_type = _event_type(data.get("type"))
        part = data.get("part") if isinstance(data.get("part"), dict) else {}
        part_type = _event_type(part.get("type"))

        if raw_type in {"step_start", "step_finish", "session_status", "message_updated"}:
            return None

        if raw_type == "error" or part_type == "error":
            message = (
                _get_nested(data, "message", "error")
                or _get_nested(part, "message", "error", "text")
                or "OpenCode reported an error"
            )
            return AgentEvent(type=EventType.ERROR, metadata={"error": _stringify(message)})

        if raw_type in {"text", "message_part_updated"} or part_type == "text":
            text = (
                _get_nested(part, "text", "delta", "content")
                or _get_nested(data, "text", "delta", "content")
            )
            if text:
                return AgentEvent(type=EventType.TEXT, text=str(text))
            return None

        if raw_type in {"tool_use", "tool"} or part_type == "tool":
            return self._map_tool_event(raw_type, data, part)

        logger.debug("Skipping unknown OpenCode JSON event type: %s", raw_type)
        return None

    def _map_tool_event(
        self, raw_type: str, data: dict[str, Any], part: dict[str, Any]
    ) -> AgentEvent | None:
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool_name = (
            _get_nested(data, "tool", "name", "toolName")
            or _get_nested(part, "tool", "name", "toolName")
            or _get_nested(state, "tool", "name", "toolName")
            or "tool"
        )
        tool_input = (
            _get_nested(data, "input", "args", "arguments")
            or _get_nested(part, "input", "args", "arguments")
            or _get_nested(state, "input", "args", "arguments")
        )
        status = str(
            _get_nested(data, "status")
            or _get_nested(part, "status")
            or _get_nested(state, "status")
            or ""
        ).lower()

        if raw_type == "tool_use" or status in _RUNNING_TOOL_STATES:
            return AgentEvent(
                type=EventType.TOOL_USE,
                tool_name=str(tool_name),
                tool_input=tool_input,
            )

        output = (
            _get_nested(data, "output", "result", "error", "text")
            or _get_nested(part, "output", "result", "error", "text")
            or _get_nested(state, "output", "result", "error", "text")
            or ""
        )
        return AgentEvent(
            type=EventType.TOOL_RESULT,
            tool_name=str(tool_name),
            result_content=_stringify(output),
            is_error=status in _ERROR_TOOL_STATES or bool(_get_nested(data, "error") or _get_nested(part, "error")),
        )

    def interrupt(self) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()

    def supports(self, feature: Feature | str) -> bool:
        feat = Feature(feature) if isinstance(feature, str) else feature
        return feat in self._SUPPORTED_FEATURES

    async def set_permission_mode(self, mode: str) -> None:
        raise NotImplementedError(
            "OpenCode does not support dynamic permission mode switching. "
            "Set bypass mode before creating the session if needed."
        )
