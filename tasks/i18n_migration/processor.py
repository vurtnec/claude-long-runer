"""
i18n Migration State Processor
==============================

Parses agent JSON output and updates state for the i18n migration task.
"""

import json
import re
from typing import Any, Dict


def process(response: str, state: Any) -> None:
    """
    Parse agent response and update state accordingly.

    Args:
        response: The agent's text response
        state: StateManager instance to update
    """
    # Extract JSON block from response
    json_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', response)
    if not json_match:
        # Try to find raw JSON object (without code block)
        json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', response)
        if not json_match:
            print("No JSON output found in agent response")
            return

    try:
        if json_match.lastindex:
            data = json.loads(json_match.group(1))
        else:
            data = json.loads(json_match.group(0))
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON: {e}")
        return

    action = data.get("action")
    print(f"Processing action: {action}")

    if action == "set_pending_files":
        # Initialization: set the list of files to process
        pending_files = data.get("pending_files", [])
        total_files = data.get("total_files", len(pending_files))

        state.update(
            pending_files=pending_files,
            total_files=total_files,
            pending_count=len(pending_files),
            phase="migrating"
        )

        # Prepare first batch
        batch_size = state.get("batch_size", 5)
        first_batch = pending_files[:batch_size]
        state.update(
            current_batch=first_batch,
            current_batch_display=_format_batch_display(first_batch)
        )

        print(f"Initialized with {total_files} files, first batch: {len(first_batch)} files")

    elif action == "batch_complete":
        # Batch completed: update file lists
        processed = data.get("processed", [])
        succeeded = data.get("succeeded", [])
        failed = data.get("failed", [])
        fail_reasons = data.get("fail_reasons", {})

        # Get current lists
        pending_files = state.get("pending_files", [])
        completed_files = state.get("completed_files", [])
        failed_files = state.get("failed_files", [])

        # Update lists
        completed_files.extend(succeeded)
        failed_files.extend(failed)

        # Remove processed files from pending
        pending_files = [f for f in pending_files if f not in processed]

        # Prepare next batch
        batch_size = state.get("batch_size", 5)
        next_batch = pending_files[:batch_size]

        state.update(
            pending_files=pending_files,
            completed_files=completed_files,
            failed_files=failed_files,
            current_batch=next_batch,
            current_batch_display=_format_batch_display(next_batch),
            pending_count=len(pending_files),
            completed_count=len(completed_files),
            failed_count=len(failed_files)
        )

        # Check if migration is complete
        if len(pending_files) == 0:
            state.update(phase="completed")
            print("Migration complete! All files processed.")
        else:
            print(f"Batch complete. Remaining: {len(pending_files)} files")

    elif action == "migration_complete":
        # Final completion
        state.update(phase="completed")
        print("Migration marked as complete by agent")

    else:
        print(f"Unknown action: {action}")


def _format_batch_display(files: list) -> str:
    """Format file list for display in prompt."""
    if not files:
        return "(No files remaining)"

    lines = []
    for i, f in enumerate(files, 1):
        lines.append(f"{i}. `{f}`")
    return "\n".join(lines)
