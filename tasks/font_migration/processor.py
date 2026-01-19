"""
Font Migration State Processor
==============================

Parses agent JSON output and updates state for the font migration task.
Handles batch completion, skipped files, and prepares next batch.
"""

import json
import re
from typing import Any, Dict, List


def process(response: str, state: Any) -> None:
    """
    Parse agent response and update state accordingly.

    Args:
        response: The agent's text response
        state: StateManager instance to update
    """
    # Check if this is the first iteration and needs batch initialization
    current_batch = state.get("current_batch", [])
    if not current_batch and state.get("phase") == "migrating":
        _initialize_first_batch(state)
        # Don't return here - continue to process agent's JSON output if present

    # Extract JSON block from response
    json_data = _extract_json(response)
    if not json_data:
        print("No JSON output found in agent response")
        return

    action = json_data.get("action")
    print(f"Processing action: {action}")

    if action == "batch_complete":
        _handle_batch_complete(json_data, state)
    elif action == "migration_complete":
        _handle_migration_complete(state)
    else:
        print(f"Unknown action: {action}")


def _extract_json(response: str) -> Dict[str, Any] | None:
    """Extract JSON block from agent response."""
    # Try to find JSON in code block
    json_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', response)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON from code block: {e}")

    # Try to find raw JSON object with nested structures
    # This pattern handles nested objects like fail_reasons and skip_reasons
    json_match = re.search(r'\{\s*"action"\s*:\s*"[^"]+"\s*,[\s\S]*?\}\s*\}', response)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError as e:
            print(f"Failed to parse raw JSON: {e}")

    # Fallback: try simpler pattern for flat JSON
    json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', response)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError as e:
            print(f"Failed to parse flat JSON: {e}")

    return None


def _initialize_first_batch(state: Any) -> None:
    """Initialize the first batch from pending files."""
    pending_files = state.get("pending_files", [])
    batch_size = state.get("batch_size", 3)

    if not pending_files:
        state.update(phase="completed")
        print("No pending files, marking as completed")
        return

    first_batch = pending_files[:batch_size]
    state.update(
        current_batch=first_batch,
        current_batch_display=_format_batch_display(first_batch),
        pending_count=len(pending_files),
    )
    print(f"Initialized first batch with {len(first_batch)} files")


def _handle_batch_complete(data: Dict[str, Any], state: Any) -> None:
    """Handle batch_complete action."""
    processed = data.get("processed", [])
    succeeded = data.get("succeeded", [])
    failed = data.get("failed", [])
    skipped = data.get("skipped", [])

    # Normalize file paths (handle with/without leading slash)
    processed = [_normalize_path(f) for f in processed]
    succeeded = [_normalize_path(f) for f in succeeded]
    failed = [_normalize_path(f) for f in failed]
    skipped = [_normalize_path(f) for f in skipped]

    # Get current lists
    pending_files = state.get("pending_files", [])
    completed_files = state.get("completed_files", [])
    failed_files = state.get("failed_files", [])
    skipped_files = state.get("skipped_files", [])

    # Update lists
    completed_files.extend(succeeded)
    failed_files.extend(failed)
    skipped_files.extend(skipped)

    # Remove processed files from pending (normalize for comparison)
    processed_normalized = set(processed)
    pending_files = [f for f in pending_files if _normalize_path(f) not in processed_normalized]

    # Prepare next batch
    batch_size = state.get("batch_size", 3)
    next_batch = pending_files[:batch_size]

    # Update state
    state.update(
        pending_files=pending_files,
        completed_files=completed_files,
        failed_files=failed_files,
        skipped_files=skipped_files,
        current_batch=next_batch,
        current_batch_display=_format_batch_display(next_batch),
        pending_count=len(pending_files),
        completed_count=len(completed_files),
        failed_count=len(failed_files),
        skipped_count=len(skipped_files),
    )

    # Log fail/skip reasons if provided
    fail_reasons = data.get("fail_reasons", {})
    skip_reasons = data.get("skip_reasons", {})

    if fail_reasons:
        print(f"Fail reasons: {fail_reasons}")
    if skip_reasons:
        print(f"Skip reasons: {skip_reasons}")

    # Check if migration is complete
    if len(pending_files) == 0:
        state.update(phase="completed")
        print("Font migration complete! All files processed.")
    else:
        print(f"Batch complete. Remaining: {len(pending_files)} files, next batch: {len(next_batch)} files")


def _handle_migration_complete(state: Any) -> None:
    """Handle migration_complete action."""
    state.update(phase="completed")
    print("Font migration marked as complete by agent")


def _normalize_path(path: str) -> str:
    """Normalize file path for consistent comparison."""
    # Remove leading slash if present
    if path.startswith("/"):
        path = path[1:]
    return path


def _format_batch_display(files: List[str]) -> str:
    """Format file list for display in prompt."""
    if not files:
        return "(No files remaining)"

    lines = []
    for i, f in enumerate(files, 1):
        lines.append(f"{i}. `{f}`")
    return "\n".join(lines)
