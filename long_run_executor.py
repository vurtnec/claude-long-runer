#!/usr/bin/env python3
"""
Long-Running Task Executor
===========================

Generic framework for executing long-running Claude Agent SDK tasks.
Based on the official autonomous-coding project architecture.

Usage:
    python long_run_executor.py --task pr_review --params '{"pr_number": 453}' --max-iterations 5

Features:
- Task-based configuration system
- State persistence and resumption
- Success condition checking
- Error handling and retries
"""

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from claude_agent_sdk import ClaudeSDKClient

from client import create_client
from success_checker import SuccessChecker
from state_manager import StateManager
from task_config import TaskConfig


def load_processor(processor_path: Path):
    """
    Dynamically load a state processor module.

    Args:
        processor_path: Path to the processor Python file

    Returns:
        The loaded module with a process() function
    """
    spec = importlib.util.spec_from_file_location("processor", processor_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load processor from {processor_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_agent_session(
    client: ClaudeSDKClient,
    message: str,
    state: StateManager,
) -> tuple[str, str]:
    """
    Run a single agent session.

    Args:
        client: Claude SDK client
        message: The prompt to send
        state: State manager for tracking progress

    Returns:
        Tuple of (status, response_text) where status is:
        - "continue" if agent should continue working
        - "error" if an error occurred
    """
    print("Sending prompt to Claude Agent SDK...\n")

    try:
        # Send the query
        await client.query(message)

        # Collect response text and show tool use
        response_text = ""
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

                        # Check if command was blocked by security hook
                        if "blocked" in str(result_content).lower():
                            print(f"   [BLOCKED] {result_content}", flush=True)
                        elif is_error:
                            # Show errors (truncated)
                            error_str = str(result_content)[:500]
                            print(f"   [Error] {error_str}", flush=True)
                        else:
                            # Tool succeeded - just show brief confirmation
                            print("   [Done]", flush=True)

        print("\n" + "-" * 70 + "\n")

        # Store response in state
        state.set_last_response(response_text)

        return "continue", response_text

    except Exception as e:
        print(f"Error during agent session: {e}")
        return "error", str(e)


async def run_long_task(
    task_name: str,
    task_params: Dict[str, Any],
    project_dir: Path,
    model: str,
    max_iterations: int = 5,
    resume: bool = False,
) -> bool:
    """
    Execute a long-running task with iteration loop.

    Args:
        task_name: Name of the task (directory name in tasks/)
        task_params: Parameters to pass to prompt templates
        project_dir: Working directory for the task
        model: Claude model to use
        max_iterations: Maximum number of iterations
        resume: Whether to resume from existing state

    Returns:
        True if task completed successfully, False otherwise
    """
    print("\n" + "=" * 70)
    print(f"  LONG-RUNNING TASK EXECUTOR: {task_name}")
    print("=" * 70)
    print(f"\nProject directory: {project_dir}")
    print(f"Model: {model}")
    print(f"Max iterations: {max_iterations}")
    print(f"Resume mode: {resume}")
    print()

    # 1. Load task configuration
    try:
        tasks_dir = Path(__file__).parent / "tasks"
        task_config = TaskConfig.load(str(tasks_dir / task_name))
        print(f"Loaded task: {task_config.description}")
        print()
    except Exception as e:
        print(f"Error loading task configuration: {e}")
        return False

    # 2. Initialize state manager
    state_file_path = project_dir / task_config.state_file
    state = StateManager(
        task_name=task_name,
        state_file=str(state_file_path),
        initial_state=task_config.initial_state,
    )

    # Merge task params into state (so processor can access them)
    params_to_merge = {k: v for k, v in task_params.items() if k not in state.data}
    if params_to_merge:
        state.update(**params_to_merge)

    print(f"State: {state}")
    print()

    # 3. Create success condition checker
    checker = SuccessChecker(task_config.success_conditions)
    print(checker.get_condition_summary())
    print()

    # 4. Determine if this is initial run or continuation
    is_first_run = not state.is_initialized()

    if resume and not is_first_run:
        print("Resuming from existing state")
        current_iteration = state.get("iteration", 0)
        print(f"Starting from iteration {current_iteration + 1}")
    elif is_first_run:
        print("Starting fresh task")
    else:
        print("Continuing existing task")

    print()

    # 4.5. Pre-run processor for first iteration to initialize state
    if is_first_run and task_config.state_processor:
        processor_path = tasks_dir / task_name / task_config.state_processor
        if processor_path.exists():
            print("Pre-running state processor to initialize file list...")
            try:
                processor = load_processor(processor_path)
                processor.process("", state)  # Empty response for initialization
                print("State processor initialized successfully")
                print()
            except Exception as e:
                print(f"Warning: State processor initialization failed: {e}")
                print()

    # 5. Main execution loop
    iteration = state.get("iteration", 0)
    success = False

    while iteration < max_iterations:
        iteration += 1
        print("\n" + "=" * 70)
        print(f"  ITERATION {iteration}/{max_iterations}")
        print("=" * 70)
        print()

        # Create client (fresh context for each iteration)
        client = create_client(project_dir, model)

        # Choose prompt based on whether this is the first run
        if is_first_run:
            try:
                # Merge task params with current state for init prompt
                prompt_vars = {**task_params, **state.data}
                prompt = task_config.format_init_prompt(**prompt_vars)
                is_first_run = False  # Only use initializer once
                print("[Using initialization prompt]")
            except ValueError as e:
                print(f"Error formatting init prompt: {e}")
                return False
        else:
            try:
                # Merge task params with current state for iteration prompt
                prompt_vars = {**task_params, **state.data}

                # Add computed fields for prompt
                prompt_vars["review_status"] = (
                    "✅ Passed" if prompt_vars.get("review_passed", False)
                    else "⚠️ Issues Remaining"
                )

                prompt = task_config.format_iter_prompt(**prompt_vars)
                print("[Using iteration prompt]")
            except ValueError as e:
                print(f"Error formatting iteration prompt: {e}")
                return False

        print()

        # Run session with async context manager
        async with client:
            status, response = await run_agent_session(client, prompt, state)

        # Run state processor if configured
        if task_config.state_processor:
            processor_path = tasks_dir / task_name / task_config.state_processor
            if processor_path.exists():
                print(f"Running state processor: {task_config.state_processor}")
                try:
                    processor = load_processor(processor_path)
                    processor.process(response, state)
                    print("State processor completed successfully")
                except Exception as e:
                    print(f"Warning: State processor failed: {e}")
                    # Continue execution even if processor fails

        # Update iteration count
        state.increment_iteration()

        # Handle session status
        if status == "error":
            print("\nSession encountered an error")
            if iteration < max_iterations:
                print("Will retry with next iteration...")
                await asyncio.sleep(task_config.delay_seconds)
                continue
            else:
                print("Max iterations reached. Stopping.")
                break

        # Check success conditions
        print("\nChecking success conditions...")
        if checker.check(state.data):
            print("✅ All success conditions satisfied!")
            success = True
            state.mark_completed(success=True)
            break

        # Continue to next iteration
        if iteration < max_iterations:
            print(f"\nContinuing to next iteration in {task_config.delay_seconds}s...")
            await asyncio.sleep(task_config.delay_seconds)

    # 6. Final summary
    print("\n" + "=" * 70)
    if success:
        print("  TASK COMPLETED SUCCESSFULLY")
    else:
        print(f"  TASK INCOMPLETE (reached max iterations: {max_iterations})")
    print("=" * 70)
    print(f"\nFinal state: {state}")
    print(f"State saved to: {state_file_path}")
    print()

    if not success:
        state.mark_completed(success=False)
        print("To continue this task, run with --resume flag")
        print()

    return success


def main() -> None:
    """Main entry point for the executor."""
    parser = argparse.ArgumentParser(
        description="Execute long-running Claude Agent SDK tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run PR review task
  python long_run_executor.py \\
    --task pr_review \\
    --params '{"pr_number": 453}' \\
    --max-iterations 3

  # Resume an interrupted task
  python long_run_executor.py \\
    --task pr_review \\
    --resume

  # Custom project directory
  python long_run_executor.py \\
    --task pr_review \\
    --params '{"pr_number": 453}' \\
    --project-dir /path/to/project
        """,
    )

    parser.add_argument(
        "--task",
        type=str,
        required=True,
        help="Task name (directory in tasks/)",
    )
    parser.add_argument(
        "--params",
        type=str,
        default="{}",
        help='Task parameters as JSON string (e.g., \'{"pr_number": 453}\')',
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=5,
        help="Maximum number of iterations (default: 5)",
    )
    parser.add_argument(
        "--project-dir",
        type=str,
        default=".",
        help="Project working directory (default: current directory)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="claude-sonnet-4-5-20250929",
        help="Claude model to use (default: claude-sonnet-4-5-20250929)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing state file",
    )

    args = parser.parse_args()

    # Parse task parameters
    try:
        task_params = json.loads(args.params)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in --params: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve project directory
    project_dir = Path(args.project_dir).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)

    # Run the task
    try:
        success = asyncio.run(
            run_long_task(
                task_name=args.task,
                task_params=task_params,
                project_dir=project_dir,
                model=args.model,
                max_iterations=args.max_iterations,
                resume=args.resume,
            )
        )

        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        print("\n\n⏸️  Interrupted by user. Progress saved to state file.")
        sys.exit(130)

    except Exception as e:
        print(f"\n\n❌ Fatal error: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
