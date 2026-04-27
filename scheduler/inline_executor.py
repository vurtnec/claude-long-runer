"""
Inline Task Executor
====================

Executes lightweight tasks defined directly in schedule YAML,
without requiring a full tasks/{name}/ directory.

Reuses the existing client.py create_client() function.
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Add parent directory for imports from the existing codebase
sys.path.insert(0, str(Path(__file__).parent.parent))

from claude_agent_sdk import ClaudeSDKClient

from client import create_client


async def run_inline_task(
    prompt: str,
    project_dir: Path,
    model: str = "claude-sonnet-4-5-20250929",
    max_turns: int = 3,
    effort: str | None = None,
) -> Dict[str, Any]:
    """
    Execute an inline prompt task.

    Args:
        prompt: The prompt to send to Claude
        project_dir: Working directory for the task
        model: Claude model to use
        max_turns: Maximum conversation turns

    Returns:
        Dict with keys: success, response_text, turns_used
    """
    print(f"\n  Inline task: sending prompt ({len(prompt)} chars)")
    print(f"  Model: {model}, Max turns: {max_turns}")

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

    def _flush_current():
        nonlocal final_response, current_text, current_has_tool_use, last_text_seen
        if current_text:
            last_text_seen = current_text
            if not current_has_tool_use:
                # A pure-text assistant message — treat as the latest final
                # answer. Later tool-using messages will not overwrite this
                # unless they too produce a final tool-free message.
                final_response = current_text
        current_text = ""
        current_has_tool_use = False

    try:
        client = create_client(project_dir, model, max_turns=max_turns, effort=effort)

        async with client:
            # Send the initial prompt
            await client.query(prompt)
            turns_used = 1

            # Collect the response and print execution logs
            async for msg in client.receive_response():
                msg_type = type(msg).__name__

                # Handle AssistantMessage (text and tool use)
                if msg_type == "AssistantMessage" and hasattr(msg, "content"):
                    # Close out the previous assistant message before starting a new one
                    _flush_current()
                    for block in msg.content:
                        block_type = type(block).__name__

                        if block_type == "TextBlock" and hasattr(block, "text"):
                            current_text += block.text
                            print(block.text, end="", flush=True)
                        elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                            current_has_tool_use = True
                            print(f"\n[Tool: {block.name}]", flush=True)
                            if hasattr(block, "input"):
                                input_str = str(block.input)
                                if len(input_str) > 200:
                                    print(f"   Input: {input_str[:200]}...", flush=True)
                                else:
                                    print(f"   Input: {input_str}", flush=True)

                # Handle UserMessage (tool results)
                elif msg_type == "UserMessage" and hasattr(msg, "content"):
                    for block in msg.content:
                        block_type = type(block).__name__

                        if block_type == "ToolResultBlock":
                            result_content = getattr(block, "content", "")
                            is_error = getattr(block, "is_error", False)

                            if "blocked" in str(result_content).lower():
                                print(f"   [BLOCKED] {result_content}", flush=True)
                            elif is_error:
                                error_str = str(result_content)[:500]
                                print(f"   [Error] {error_str}", flush=True)
                            else:
                                print("   [Done]", flush=True)

            # Flush whatever was in flight when the stream ended
            _flush_current()
            print("\n" + "-" * 70 + "\n")

        # If we never observed a clean closing message (e.g. run ended on a
        # tool turn), fall back to the most recent text we did see so the
        # notification isn't empty.
        if not final_response:
            final_response = last_text_seen

        return {
            "success": True,
            "response_text": final_response,
            "turns_used": turns_used,
        }

    except Exception as e:
        print(f"  Inline task error: {e}")
        # Even on error, prefer final answer text if any
        _flush_current()
        if not final_response:
            final_response = last_text_seen
        return {
            "success": False,
            "response_text": final_response,
            "turns_used": turns_used,
            "error": str(e),
        }
