"""
Agent Protocol — Unified interface for LLM agent SDK clients.
==============================================================

Defines a backend-agnostic protocol so the rest of the codebase
(feishu_bot, long_run_executor, inline_executor) can work with
any agent SDK without knowing which one is behind the scenes.

Supported backends:
  - claude  : Claude Agent SDK (ClaudeSDKClient)
  - codex   : OpenAI Codex Python SDK (AsyncCodex + app-server)

Design principles:
  1. Minimal surface — only methods the callers actually use.
  2. Unified events — callers iterate AgentEvent, never raw SDK types.
  3. Optional capabilities — `supports()` lets callers degrade gracefully
     when a backend lacks a feature (e.g. Codex has no dynamic mode switch).
  4. SDK isolation — all SDK-specific imports live in the backend modules
     (claude_agent.py / codex_agent.py), never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Protocol, runtime_checkable

# ── Unified Event Types ──────────────────────────────────────────────────────


class EventType(str, Enum):
    """All event types that callers may receive during a response stream."""

    TEXT = "text"  # Assistant text content (delta or full)
    TOOL_USE = "tool_use"  # Agent is invoking a tool
    TOOL_RESULT = "tool_result"  # Tool execution finished
    SYSTEM = "system"  # Session metadata (session_id, mode, …)
    RESULT = "result"  # End-of-response signal
    ERROR = "error"  # Non-fatal error or warning


@dataclass(slots=True)
class AgentEvent:
    """
    A single event from the agent response stream.

    Callers switch on `event.type` and read the relevant fields.
    Unknown / unused fields default to None so forward-compat is free.
    """

    type: EventType

    # TEXT
    text: str | None = None

    # TOOL_USE
    tool_name: str | None = None
    tool_input: Any | None = None

    # TOOL_RESULT
    result_content: str | None = None
    is_error: bool = False

    # SYSTEM / RESULT  (bag of metadata — keeps the dataclass stable when
    # backends add new fields; callers do `event.metadata.get("session_id")`)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Capability Feature Keys ──────────────────────────────────────────��───────


class Feature(str, Enum):
    """
    Feature flags exposed via `AgentClient.supports()`.

    When a backend lacks a capability, the caller can skip or show a
    user-friendly "not supported" message instead of crashing.
    """

    PERMISSION_MODE = "permission_mode"  # dynamic mode switch
    SESSION_RESUME = "session_resume"  # resume by session/thread id
    STREAMING = "streaming"  # event-by-event streaming
    INTERRUPT = "interrupt"  # cancel in-flight request
    MCP_SERVERS = "mcp_servers"  # SDK-managed MCP servers
    SECURITY_HOOKS = "security_hooks"  # pre-tool-use hooks


# ── Agent Client Protocol ────────────────────────────────────────────────────


@runtime_checkable
class AgentClient(Protocol):
    """
    Structural interface that every backend must satisfy.

    Why Protocol and not ABC?
      - Structural subtyping — implementations don't need to inherit.
      - Works with isinstance() checks at runtime (runtime_checkable).
      - Plays nicely with static type checkers (mypy / pyright).
    """

    # ── Identity ─────────────────────────────────────────────────────────

    @property
    def backend_name(self) -> str:
        """Return 'claude' or 'codex' (or future backend names)."""
        ...

    @property
    def session_id(self) -> str | None:
        """Current session / thread id, or None before first message."""
        ...

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Establish connection to the underlying agent runtime."""
        ...

    async def disconnect(self) -> None:
        """Gracefully close the connection and release resources."""
        ...

    # ── Conversation ─────────────────────────────────────────────────────

    async def send_message(self, prompt: str) -> None:
        """
        Send a user message.  After calling this, iterate
        `receive_events()` to consume the response stream.
        """
        ...

    def receive_events(self) -> AsyncIterator[AgentEvent]:
        """
        Async iterator that yields AgentEvent objects until the agent
        finishes its turn.  The last event has type == EventType.RESULT.
        """
        ...

    # ── Control ──────────────────────────────────────────────────────────

    def interrupt(self) -> None:
        """
        Interrupt the current in-flight request.
        No-op if nothing is running.
        """
        ...

    # ── Capabilities ─────────────────────────────────────────────────────

    def supports(self, feature: Feature | str) -> bool:
        """
        Check whether this backend supports a given capability.

        Callers use this to degrade gracefully:

            if client.supports(Feature.PERMISSION_MODE):
                await client.set_permission_mode("plan")
            else:
                print("This backend does not support mode switching.")
        """
        ...

    # ── Optional: Permission Mode ────────────────────────────────────────

    async def set_permission_mode(self, mode: str) -> None:
        """
        Switch the permission / approval mode on an active session.

        Raises NotImplementedError if the backend does not support this.
        Callers should check `supports(Feature.PERMISSION_MODE)` first.
        """
        ...


# ── Factory ──────────────────────────────────────────────────────────────────


def create_agent_client(
    backend: str,
    *,
    project_dir: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    resume: str | None = None,
    restricted: bool = False,
    effort: str | None = None,
    max_turns: int = 1000,
    browser_tool: str = "playwright",
    system_prompt: str | None = None,
    **extra,
) -> AgentClient:
    """
    Create an agent client for the requested backend.

    Parameters mirror the union of what each backend needs.  Unknown
    kwargs are forwarded so new backend-specific options don't require
    changes here.

    Raises:
        ValueError   — unknown backend name
        ImportError  — backend SDK not installed
    """
    backend = backend.lower().strip()

    if backend == "claude":
        from claude_agent import ClaudeAgentClient

        return ClaudeAgentClient(
            project_dir=project_dir,
            model=model or "claude-opus-4-7",
            permission_mode=permission_mode,
            resume=resume,
            restricted=restricted,
            effort=effort,
            max_turns=max_turns,
            browser_tool=browser_tool,
            system_prompt=system_prompt,
        )

    elif backend == "codex":
        from codex_agent import CodexAgentClient

        return CodexAgentClient(
            project_dir=project_dir,
            model=model or "o3",
            approval_policy=permission_mode,
            resume_thread_id=resume,
            effort=effort,
            max_turns=max_turns,
            **extra,
        )

    else:
        raise ValueError(f"Unknown backend: {backend!r}.  Supported: 'claude', 'codex'")
