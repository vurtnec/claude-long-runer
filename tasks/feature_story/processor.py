"""
Feature Story State Processor

Parses spec.yaml and manages step-by-step implementation progress.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml


def process(response: str, state: Any) -> None:
    """Parse agent response and update state."""
    phase = state.get("phase", "initializing")

    # First run: load spec and initialize
    if phase == "initializing":
        _initialize_from_spec(state)
        return

    # Parse agent response
    json_data = _extract_json(response)
    if not json_data:
        print("No JSON output found in agent response")
        return

    action = json_data.get("action")
    print(f"Processing action: {action}")

    if action == "step_complete":
        _handle_step_complete(json_data, state)
    elif action == "step_failed":
        _handle_step_failed(json_data, state)


def _initialize_from_spec(state: Any) -> None:
    """Load spec.yaml and initialize state."""
    # Get project_dir from state (auto-injected by long_run_executor.py)
    project_dir = state.get("project_dir", ".")
    spec_file = state.get("spec_file", "spec.yaml")

    # Try multiple locations for spec file
    possible_paths = [
        Path(project_dir) / spec_file,  # In project directory
        Path("tasks/feature_story") / spec_file,  # In task template
        Path(spec_file),  # Current directory
    ]

    spec_path = None
    for path in possible_paths:
        if path and path.exists():
            spec_path = path
            break

    if not spec_path:
        print(f"Warning: spec file not found, using defaults")
        state.update(
            phase="implementing",
            total_steps=0,
            current_step_title="No spec loaded",
            current_step_tasks_display="Please provide a spec.yaml file",
            current_step_acceptance_display="N/A",
        )
        return

    try:
        with open(spec_path) as f:
            spec = yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading spec: {e}")
        state.update(phase="completed")
        return

    # Extract data from spec
    project_name = spec.get("project_name", "Untitled Project")
    overview = spec.get("overview", "")
    technology_stack = spec.get("technology_stack", {})
    implementation_steps = spec.get("implementation_steps", [])
    success_criteria = spec.get("success_criteria", [])

    if not implementation_steps:
        print("No implementation steps found in spec")
        state.update(phase="completed")
        return

    # Get first step
    first_step = implementation_steps[0]

    state.update(
        phase="implementing",
        project_name=project_name,
        overview=overview,
        technology_stack=technology_stack,
        implementation_steps=implementation_steps,
        success_criteria=success_criteria,
        total_steps=len(implementation_steps),
        current_step=1,
        current_step_title=first_step.get("title", ""),
        current_step_tasks=first_step.get("tasks", []),
        current_step_acceptance=first_step.get("acceptance", []),
        completed_steps=[],
        failed_steps=[],
        # Display formats
        technology_stack_display=_format_tech_stack(technology_stack),
        implementation_steps_display=_format_steps_overview(implementation_steps),
        current_step_tasks_display=_format_tasks(first_step.get("tasks", [])),
        current_step_acceptance_display=_format_acceptance(first_step.get("acceptance", [])),
        completed_steps_display="(none yet)",
    )
    print(f"Initialized with {len(implementation_steps)} steps from {spec_path}")


def _handle_step_complete(data: Dict[str, Any], state: Any) -> None:
    """Handle step_complete action."""
    step_num = data.get("step", state.get("current_step", 1))

    completed_steps = list(state.get("completed_steps", []))
    if step_num not in completed_steps:
        completed_steps.append(step_num)

    implementation_steps = state.get("implementation_steps", [])
    total_steps = len(implementation_steps)
    next_step = step_num + 1

    if next_step > total_steps:
        # All steps completed
        state.update(
            phase="completed",
            completed_steps=completed_steps,
            completed_steps_display=_format_completed_steps(completed_steps, implementation_steps),
        )
        _print_summary(state)
        return

    # Prepare next step
    next_step_data = implementation_steps[next_step - 1]  # 0-indexed

    state.update(
        current_step=next_step,
        current_step_title=next_step_data.get("title", ""),
        current_step_tasks=next_step_data.get("tasks", []),
        current_step_acceptance=next_step_data.get("acceptance", []),
        completed_steps=completed_steps,
        current_step_tasks_display=_format_tasks(next_step_data.get("tasks", [])),
        current_step_acceptance_display=_format_acceptance(next_step_data.get("acceptance", [])),
        completed_steps_display=_format_completed_steps(completed_steps, implementation_steps),
    )
    print(f"Step {step_num} complete. Moving to step {next_step}.")


def _handle_step_failed(data: Dict[str, Any], state: Any) -> None:
    """Handle step_failed action."""
    step_num = data.get("step", state.get("current_step", 1))
    reason = data.get("reason", "Unknown error")

    failed_steps = list(state.get("failed_steps", []))
    failed_steps.append({"step": step_num, "reason": reason})

    state.update(failed_steps=failed_steps)
    print(f"Step {step_num} failed: {reason}")
    # Keep current step for retry


def _extract_json(response: str) -> Dict[str, Any] | None:
    """Extract JSON block from agent response."""
    json_match = re.search(r'```json\s*(\{[\s\S]*?\})\s*```', response)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON: {e}")
    return None


def _format_tech_stack(tech_stack: Dict) -> str:
    """Format technology stack for display."""
    lines = []
    for category, items in tech_stack.items():
        lines.append(f"### {category.title()}")
        if isinstance(items, dict):
            for key, value in items.items():
                lines.append(f"- **{key}**: {value}")
        else:
            lines.append(f"- {items}")
    return "\n".join(lines)


def _format_steps_overview(steps: List[Dict]) -> str:
    """Format all steps as overview."""
    lines = []
    for step in steps:
        step_num = step.get("step", 0)
        title = step.get("title", "")
        task_count = len(step.get("tasks", []))
        lines.append(f"{step_num}. **{title}** ({task_count} tasks)")
    return "\n".join(lines)


def _format_tasks(tasks: List[str]) -> str:
    """Format tasks list."""
    if not tasks:
        return "(no tasks)"
    return "\n".join(f"- [ ] {task}" for task in tasks)


def _format_acceptance(acceptance: List[Dict]) -> str:
    """Format acceptance criteria."""
    if not acceptance:
        return "(no acceptance criteria)"

    lines = []
    for i, acc in enumerate(acceptance, 1):
        acc_type = acc.get("type", "unknown")
        if acc_type == "code":
            cmd = acc.get("command", "")
            lines.append(f"{i}. **Code**: `{cmd}`")
        elif acc_type == "browser":
            url = acc.get("url", "")
            lines.append(f"{i}. **Browser**: {url}")
            steps = acc.get("steps", [])
            for step in steps:
                action = step.get("action", "")
                lines.append(f"   - {action}: {step}")
    return "\n".join(lines)


def _format_completed_steps(completed: List[int], all_steps: List[Dict]) -> str:
    """Format completed steps display."""
    if not completed:
        return "(none yet)"

    lines = []
    for step_num in sorted(completed):
        if step_num <= len(all_steps):
            title = all_steps[step_num - 1].get("title", "")
            lines.append(f"- [x] Step {step_num}: {title}")
    return "\n".join(lines)


def _print_summary(state: Any) -> None:
    """Print final summary."""
    print("\n" + "=" * 50)
    print("FEATURE STORY COMPLETE")
    print("=" * 50)
    print(f"Project: {state.get('project_name', 'Unknown')}")
    print(f"Total steps: {state.get('total_steps', 0)}")
    print(f"Completed: {len(state.get('completed_steps', []))}")
    print(f"Failed: {len(state.get('failed_steps', []))}")
    print("=" * 50)
