"""
Claude Agent Client — AgentClient implementation backed by Claude Agent SDK.
=============================================================================

Wraps the existing `client.py` (create_client / ClaudeSDKClient) to expose
the unified AgentClient protocol.  The original `client.py` is NOT modified;
this module composes on top of it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator

from agent_protocol import AgentClient, AgentEvent, EventType, Feature

# Existing project modules — these remain unchanged
from client import create_client  # the original factory

logger = logging.getLogger(__name__)


class ClaudeAgentClient:
    """
    AgentClient backed by the Claude Agent SDK (ClaudeSDKClient).

    Translates Claude SDK message types into unified AgentEvent objects:

        AssistantMessage / TextBlock      →  EventType.TEXT
        AssistantMessage / ToolUseBlock   →  EventType.TOOL_USE
        UserMessage / ToolResultBlock     →  EventType.TOOL_RESULT
        SystemMessage                     →  EventType.SYSTEM
        ResultMessage                     →  EventType.RESULT
    """

    # ── Features supported by this backend ───────────────────────────────

    _SUPPORTED_FEATURES = frozenset({
        Feature.PERMISSION_MODE,
        Feature.SESSION_RESUME,
        Feature.STREAMING,
        Feature.INTERRUPT,
        Feature.MCP_SERVERS,
        Feature.SECURITY_HOOKS,
    })

    def __init__(
        self,
        project_dir: str | None = None,
        model: str = "claude-sonnet-4-5-20250929",
        permission_mode: str | None = None,
        resume: str | None = None,
        restricted: bool = False,
        effort: str | None = None,
        max_turns: int = 1000,
        browser_tool: str = "playwright",
        system_prompt: str | None = None,
    ):
        self._project_dir = Path(project_dir) if project_dir else Path.cwd()
        self._model = model
        self._permission_mode = permission_mode
        self._resume = resume
        self._restricted = restricted
        self._effort = effort
        self._max_turns = max_turns
        self._browser_tool = browser_tool
        self._system_prompt = system_prompt

        # The underlying SDK client — created eagerly so the caller can
        # pass it around before calling connect().
        self._sdk_client = create_client(
            project_dir=self._project_dir,
            model=self._model,
            browser_tool=self._browser_tool,
            system_prompt=self._system_prompt,
            max_turns=self._max_turns,
            permission_mode=self._permission_mode,
            resume=self._resume,
            restricted=self._restricted,
            effort=self._effort,
        )

        self._session_id: str | None = None
        self._connected: bool = False

    # ── Identity ─────────────────────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        return "claude"

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        await self._sdk_client.connect()
        self._connected = True

    async def disconnect(self) -> None:
        if self._connected:
            try:
                await self._sdk_client.disconnect()
            except Exception as e:
                logger.warning("Claude disconnect error: %s", e)
            self._connected = False

    # ── Conversation ─────────────────────────────────────────────────────

    async def send_message(self, prompt: str) -> None:
        await self._sdk_client.query(prompt)

    async def receive_events(self) -> AsyncIterator[AgentEvent]:
        """
        Map Claude SDK messages to AgentEvent.

        The mapping is intentionally verbose (one if-branch per type)
        so that future SDK changes surface as explicit KeyErrors rather
        than silent data loss.
        """
        async for msg in self._sdk_client.receive_response():
            msg_type = type(msg).__name__

            # ── AssistantMessage ─────────────────────────────────────
            if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                actual_model = getattr(msg, "model", None)
                for block in msg.content:
                    block_type = type(block).__name__

                    if block_type == "TextBlock" and hasattr(block, "text"):
                        yield AgentEvent(
                            type=EventType.TEXT,
                            text=block.text,
                            metadata={"model": actual_model} if actual_model else {},
                        )
                    elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                        yield AgentEvent(
                            type=EventType.TOOL_USE,
                            tool_name=block.name,
                            tool_input=getattr(block, "input", None),
                        )

            # ── UserMessage (tool results) ───────────────────────────
            elif msg_type == "UserMessage" and hasattr(msg, "content"):
                for block in msg.content:
                    block_type = type(block).__name__
                    if block_type == "ToolResultBlock":
                        yield AgentEvent(
                            type=EventType.TOOL_RESULT,
                            result_content=str(getattr(block, "content", "")),
                            is_error=getattr(block, "is_error", False),
                        )

            # ── SystemMessage (metadata) ─────────────────────────────
            elif msg_type == "SystemMessage":
                meta: dict[str, Any] = {}
                if hasattr(msg, "data") and isinstance(msg.data, dict):
                    if msg.data.get("permission_mode"):
                        meta["permission_mode"] = msg.data["permission_mode"]
                    if msg.data.get("session_id"):
                        meta["session_id"] = msg.data["session_id"]
                        self._session_id = msg.data["session_id"]
                yield AgentEvent(type=EventType.SYSTEM, metadata=meta)

            # ── ResultMessage (end of response) ──────────────────────
            elif msg_type == "ResultMessage":
                result_session_id = getattr(msg, "session_id", None)
                if result_session_id:
                    self._session_id = result_session_id
                yield AgentEvent(
                    type=EventType.RESULT,
                    metadata={
                        "num_turns": getattr(msg, "num_turns", None),
                        "is_error": getattr(msg, "is_error", False),
                        "session_id": self._session_id,
                    },
                )

    # ── Control ──────────────────────────────────────────────────��───────

    def interrupt(self) -> None:
        self._sdk_client.interrupt()

    # ── Capabilities ─────────────────────────────────────────────────────

    def supports(self, feature: Feature | str) -> bool:
        feat = Feature(feature) if isinstance(feature, str) else feature
        return feat in self._SUPPORTED_FEATURES

    async def set_permission_mode(self, mode: str) -> None:
        await self._sdk_client.set_permission_mode(mode)
        self._permission_mode = mode
