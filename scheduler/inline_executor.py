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

    response_text = ""
    turns_used = 0

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

            print("\n" + "-" * 70 + "\n")

        return {
            "success": True,
            "response_text": response_text,
            "turns_used": turns_used,
        }

    except Exception as e:
        print(f"  Inline task error: {e}")
        return {
            "success": False,
            "response_text": response_text,
            "turns_used": turns_used,
            "error": str(e),
        }
