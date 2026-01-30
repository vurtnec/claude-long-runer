"""
State processor for batch processing tasks.

This processor:
1. On first run: discovers files and creates initial batch
2. On subsequent runs: parses JSON response, updates state, creates next batch
"""

import json
import re
import subprocess
from pathlib import Path


def process_state(state: dict, response: str, params: dict) -> dict:
    """
    Process agent response and update state.

    Args:
        state: Current task state
        response: Agent's response text
        params: Task parameters (e.g., project_dir, file_pattern)

    Returns:
        Updated state dictionary
    """
    phase = state.get("phase", "initializing")

    if phase == "initializing":
        return _initialize_files(state, params)

    return _process_batch_response(state, response)


def _initialize_files(state: dict, params: dict) -> dict:
    """Discover files and set up initial batch."""
    project_dir = params.get("project_dir", ".")
    file_pattern = params.get("file_pattern", "*.py")
    exclude_patterns = params.get("exclude_patterns", ["node_modules", "__pycache__", ".git", "dist", "build"])

    # Build find command
    exclude_args = " ".join([f'-not -path "*/{p}/*"' for p in exclude_patterns])
    cmd = f'find {project_dir} -name "{file_pattern}" {exclude_args} -type f'

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        files = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
    except Exception as e:
        print(f"Error discovering files: {e}")
        files = []

    if not files:
        return {
            **state,
            "phase": "completed",
            "pending_files": [],
            "total_files": 0,
            "current_batch": [],
            "current_batch_display": "No files to process."
        }

    batch_size = state.get("batch_size", 3)
    first_batch = files[:batch_size]
    remaining = files[batch_size:]

    return {
        **state,
        "phase": "processing",
        "pending_files": remaining,
        "completed_files": [],
        "failed_files": [],
        "skipped_files": [],
        "current_batch": first_batch,
        "total_files": len(files),
        "pending_count": len(remaining),
        "completed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "current_batch_display": "\n".join([f"- {f}" for f in first_batch])
    }


def _process_batch_response(state: dict, response: str) -> dict:
    """Parse agent response and update state."""
    # Extract JSON from response
    json_match = re.search(r'```json\s*(.*?)\s*```', response, re.DOTALL)
    if not json_match:
        json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', response, re.DOTALL)

    if not json_match:
        # No valid JSON found, keep current batch
        return state

    try:
        json_str = json_match.group(1) if '```' in json_match.group(0) else json_match.group(0)
        data = json.loads(json_str)
    except json.JSONDecodeError:
        return state

    if data.get("action") != "batch_complete":
        return state

    # Process results
    results = data.get("results", [])
    completed = list(state.get("completed_files", []))
    failed = list(state.get("failed_files", []))
    skipped = list(state.get("skipped_files", []))

    for result in results:
        file_path = result.get("file", "")
        status = result.get("status", "")

        if status == "completed":
            completed.append(file_path)
        elif status == "failed":
            failed.append(file_path)
        elif status == "skipped":
            skipped.append(file_path)

    # Prepare next batch
    pending = list(state.get("pending_files", []))
    batch_size = state.get("batch_size", 3)
    next_batch = pending[:batch_size]
    remaining = pending[batch_size:]

    # Check if done
    if not next_batch:
        return {
            **state,
            "phase": "completed",
            "pending_files": [],
            "completed_files": completed,
            "failed_files": failed,
            "skipped_files": skipped,
            "current_batch": [],
            "pending_count": 0,
            "completed_count": len(completed),
            "failed_count": len(failed),
            "skipped_count": len(skipped),
            "current_batch_display": "All files processed!"
        }

    return {
        **state,
        "phase": "processing",
        "pending_files": remaining,
        "completed_files": completed,
        "failed_files": failed,
        "skipped_files": skipped,
        "current_batch": next_batch,
        "pending_count": len(remaining),
        "completed_count": len(completed),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "current_batch_display": "\n".join([f"- {f}" for f in next_batch])
    }
