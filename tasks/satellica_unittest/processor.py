"""
Satellica Unit Test Generator State Processor

Note: Test execution is handled by the agent via Bash tool, not by this processor.
The processor only manages state transitions between batches.
"""

import json
import re
import subprocess
from typing import Any, Dict, List


def process(response: str, state: Any) -> None:
    """Parse agent response and update state."""

    # First run: initialize file list
    pending_files = state.get("pending_files", [])
    if not pending_files and state.get("phase") == "generating":
        _initialize_file_list(state)
        _initialize_first_batch(state)

    # Refresh pending_files after potential initialization
    pending_files = state.get("pending_files", [])

    # Initialize batch if needed
    current_batch = state.get("current_batch", [])
    if not current_batch and pending_files and state.get("phase") == "generating":
        _initialize_first_batch(state)

    # Extract JSON from response
    json_data = _extract_json(response)
    if not json_data:
        print("No JSON output found in agent response")
        return

    action = json_data.get("action")
    print(f"Processing action: {action}")

    if action == "batch_complete":
        _handle_batch_complete(json_data, state)
    elif action == "generation_complete":
        _handle_generation_complete(state)


def _extract_json(response: str) -> Dict[str, Any] | None:
    """Extract JSON block from agent response."""
    json_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', response)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON: {e}")
    return None


def _initialize_file_list(state: Any) -> None:
    """Dynamically generate file list for testing."""
    project_dir = state.get("project_dir", "")

    try:
        # Find all .ts and .tsx files, excluding specific directories and patterns
        result = subprocess.run(
            f"find {project_dir} -type f \\( -name '*.tsx' -o -name '*.ts' \\) "
            f"! -path '*/node_modules/*' "
            f"! -path '*/.next/*' "
            f"! -path '*/dist/*' "
            f"! -path '*/.git/*' "
            f"! -path '*/components/ui/*' "
            f"! -path '*/app/*' "
            f"! -path '*/test-utils/*' "
            f"! -name '*.test.ts' "
            f"! -name '*.test.tsx' "
            f"! -name '*.spec.ts' "
            f"! -name '*.spec.tsx' "
            f"! -name '*.d.ts' "
            f"! -name 'index.ts' "
            f"! -name 'index.tsx' "
            f"! -name '*.config.*' "
            f"! -name 'middleware.ts' "
            f"! -name 'vitest.setup.ts'",
            shell=True,
            capture_output=True,
            text=True,
            check=True
        )

        files = [f.strip() for f in result.stdout.strip().split('\n') if f.strip()]
        files = [f.replace(project_dir + "/", "") for f in files]
        files.sort()

        state.update(
            pending_files=files,
            total_files=len(files),
            pending_count=len(files)
        )
        print(f"Initialized file list with {len(files)} files")

    except subprocess.CalledProcessError as e:
        print(f"Error finding files: {e}")
        state.update(pending_files=[], total_files=0, pending_count=0)


def _initialize_first_batch(state: Any) -> None:
    """Initialize the first batch."""
    pending_files = state.get("pending_files", [])
    batch_size = state.get("batch_size", 3)
    project_dir = state.get("project_dir", "")

    if not pending_files:
        state.update(phase="completed")
        return

    first_batch = pending_files[:batch_size]
    state.update(
        current_batch=first_batch,
        current_batch_display=_format_batch_display(first_batch, project_dir),
    )
    print(f"Initialized first batch with {len(first_batch)} files")


def _handle_batch_complete(data: Dict[str, Any], state: Any) -> None:
    """
    Handle batch_complete action.

    The agent has already run tests and verified they pass before outputting JSON.
    This function just updates state and prepares the next batch.
    """
    results = data.get("results", [])

    project_dir = state.get("project_dir", "")
    pending_files = state.get("pending_files", [])
    completed_files = state.get("completed_files", [])
    failed_files = state.get("failed_files", [])
    skipped_files = state.get("skipped_files", [])

    # Track files processed in this batch
    processed_in_this_batch = set()

    for result in results:
        source_file = result.get("source_file", "")
        status = result.get("status", "")

        # Normalize source path
        if project_dir and source_file.startswith(project_dir + "/"):
            source_file = source_file[len(project_dir) + 1:]
        elif project_dir and source_file.startswith(project_dir):
            source_file = source_file[len(project_dir):]

        processed_in_this_batch.add(source_file)

        if status == "created":
            if source_file not in completed_files:
                completed_files.append(source_file)
        elif status == "skipped":
            if source_file not in skipped_files:
                skipped_files.append(source_file)
        elif status == "failed":
            if source_file not in failed_files:
                failed_files.append(source_file)

    # Remove processed files from pending
    current_batch = state.get("current_batch", [])
    files_to_remove = set(current_batch) | processed_in_this_batch
    pending_files = [f for f in pending_files if f not in files_to_remove]

    # Prepare next batch
    batch_size = state.get("batch_size", 3)
    next_batch = pending_files[:batch_size]

    state.update(
        pending_files=pending_files,
        completed_files=completed_files,
        failed_files=failed_files,
        skipped_files=skipped_files,
        current_batch=next_batch,
        current_batch_display=_format_batch_display(next_batch, project_dir),
        pending_count=len(pending_files),
        completed_count=len(completed_files),
        failed_count=len(failed_files),
        skipped_count=len(skipped_files),
    )

    if len(pending_files) == 0:
        state.update(phase="completed")
        _print_summary(state)
    else:
        print(f"✅ Batch complete. Moving to next batch. Remaining: {len(pending_files)} files")


def _handle_generation_complete(state: Any) -> None:
    """Handle generation_complete action."""
    state.update(phase="completed")
    _print_summary(state)


def _print_summary(state: Any) -> None:
    """Print final summary."""
    print("\n" + "=" * 50)
    print("UNIT TEST GENERATION COMPLETE")
    print("=" * 50)
    print(f"Total files: {state.get('total_files', 0)}")
    print(f"Completed: {state.get('completed_count', 0)}")
    print(f"Skipped: {state.get('skipped_count', 0)}")
    print(f"Failed: {state.get('failed_count', 0)}")
    print("=" * 50)

    # Print failed files if any
    failed_files = state.get('failed_files', [])
    if failed_files:
        print("\nFailed files:")
        for f in failed_files:
            print(f"  - {f}")


def _format_batch_display(files: List[str], project_dir: str = "") -> str:
    """Format file list for display with full paths."""
    if not files:
        return "(No files remaining)"
    if project_dir:
        return "\n".join(f"{i}. `{project_dir}/{f}`" for i, f in enumerate(files, 1))
    return "\n".join(f"{i}. `{f}`" for i, f in enumerate(files, 1))
