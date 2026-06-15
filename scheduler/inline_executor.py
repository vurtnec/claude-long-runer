"""
Inline Task Executor
====================

Executes lightweight tasks defined directly in schedule YAML,
without requiring a full tasks/{name}/ directory.

Uses the unified AgentClient protocol so the same executor works for
both Claude and Codex backends.
"""

import sys
from pathlib import Path
from typing import Any, Dict

# Add parent directory for imports from the existing codebase
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_protocol import EventType, create_agent_client


async def run_inline_task(
    prompt: str,
    project_dir: Path,
    model: str | None = None,
    max_turns: int = 3,
    effort: str | None = None,
    backend: str = "codex",
) -> Dict[str, Any]:
    """
    Execute an inline prompt task.

    Args:
        prompt: The prompt to send to the agent
        project_dir: Working directory for the task
        model: Model to use (backend default if None)
        max_turns: Maximum conversation turns
        effort: Reasoning effort level (low/medium/high/xhigh/max)
        backend: "claude" or "codex"

    Returns:
        Dict with keys: success, response_text, turns_used
    """
    print(f"\n  Inline task: sending prompt ({len(prompt)} chars)")
    print(f"  Backend: {backend}, Model: {model or '(default)'}, Max turns: {max_turns}")

    # Track only the FINAL assistant answer (the tool-free closing message),
    # not the running concatenation of every intermediate thought. Without
    # this, downstream notifications get flooded with "Let me check…" /
    # "Now I'll run…" prose and the actual answer is truncated off.
    final_response = ""
    # Buffer for the message currently being streamed
    current_text = ""
    current_has_tool_use = False
    # Fallback: keep the last non-empty text we saw, in case the run ends
    # without a clean tool-free message (e.g. max_turns exhausted mid-flight).
    last_text_seen = ""
    turns_used = 0
    run_error = ""

    def _flush_current():
        nonlocal final_response, current_text, current_has_tool_use, last_text_seen
        if current_text:
            last_text_seen = current_text
            if not current_has_tool_use:
                # A pure-text assistant message — treat as the latest final
                # answer. Tool-using messages do not overwrite this; only
                # subsequent tool-free messages do.
                final_response = current_text
        current_text = ""
        current_has_tool_use = False

    client = None
    try:
        client_kwargs: Dict[str, Any] = {
            "project_dir": str(project_dir),
            "max_turns": max_turns,
            "effort": effort,
        }
        if model:
            client_kwargs["model"] = model
        client = create_agent_client(backend, **client_kwargs)

        await client.connect()
        await client.send_message(prompt)
        turns_used = 1

        async for event in client.receive_events():
            if event.type == EventType.TEXT:
                current_text += event.text or ""
                print(event.text or "", end="", flush=True)

            elif event.type == EventType.TOOL_USE:
                current_has_tool_use = True
                print(f"\n[Tool: {event.tool_name}]", flush=True)
                if event.tool_input is not None:
                    input_str = str(event.tool_input)
                    if len(input_str) > 200:
                        print(f"   Input: {input_str[:200]}...", flush=True)
                    else:
                        print(f"   Input: {input_str}", flush=True)

            elif event.type == EventType.TOOL_RESULT:
                # An assistant turn that ended with a tool call is done;
                # flush so subsequent text starts a fresh assistant message.
                _flush_current()

                result_content = event.result_content or ""
                if "blocked" in result_content.lower():
                    print(f"   [BLOCKED] {result_content}", flush=True)
                elif event.is_error:
                    print(f"   [Error] {result_content[:500]}", flush=True)
                else:
                    print("   [Done]", flush=True)

            elif event.type == EventType.ERROR:
                run_error = event.metadata.get("error", "Unknown error")
                print(f"\n  [Error] {run_error}", flush=True)

            elif event.type == EventType.RESULT:
                if event.metadata.get("is_error"):
                    run_error = (
                        event.metadata.get("error")
                        or current_text
                        or final_response
                        or last_text_seen
                        or "Agent returned an error"
                    )

        # Flush whatever was in flight when the stream ended
        _flush_current()
        print("\n" + "-" * 70 + "\n")

        # If we never observed a clean closing message (e.g. run ended on a
        # tool turn), fall back to the most recent text we did see so the
        # notification isn't empty.
        if not final_response:
            final_response = last_text_seen

        if run_error:
            return {
                "success": False,
                "response_text": final_response,
                "turns_used": turns_used,
                "error": run_error,
            }

        return {
            "success": True,
            "response_text": final_response,
            "turns_used": turns_used,
        }

    except Exception as e:
        print(f"  Inline task error: {e}")
        _flush_current()
        if not final_response:
            final_response = last_text_seen
        return {
            "success": False,
            "response_text": final_response,
            "turns_used": turns_used,
            "error": str(e),
        }

    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                print(f"  Inline task disconnect error: {e}")
