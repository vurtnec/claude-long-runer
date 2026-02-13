"""
Task Configuration System
=========================

Loads task configurations from directory-based templates.
Each task is defined by:
- task.json: Task metadata and settings
- init_prompt.md: Initial session prompt template
- iter_prompt.md: Iteration session prompt template
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TaskConfig:
    """
    Task configuration loaded from a task directory.

    Attributes:
        name: Unique task identifier
        description: Human-readable task description
        init_prompt: Initial session prompt template (supports variable substitution)
        iter_prompt: Iteration session prompt template (supports variable substitution)
        state_file: Filename for persisting task state
        initial_state: Default state values for new task runs
        success_conditions: List of condition definitions for task completion
        delay_seconds: Delay between iterations to avoid rate limiting
        state_processor: Optional path to a Python module that processes agent responses
    """

    name: str
    description: str
    init_prompt: str
    iter_prompt: str
    state_file: str
    initial_state: Dict[str, Any]
    success_conditions: List[Dict[str, Any]]
    delay_seconds: int = 3
    state_processor: Optional[str] = None
    browser_tool: str = "playwright"
    allowed_commands: List[str] = field(default_factory=list)
    system_prompt: Optional[str] = None

    @classmethod
    def load(cls, task_dir: str) -> "TaskConfig":
        """
        Load task configuration from a directory.

        Expected structure:
            task_dir/
                task.json           # Task metadata and settings
                init_prompt.md      # Initial session prompt
                iter_prompt.md      # Iteration prompt

        Args:
            task_dir: Path to task configuration directory

        Returns:
            TaskConfig instance

        Raises:
            FileNotFoundError: If required files are missing
            json.JSONDecodeError: If task.json is malformed
            KeyError: If required fields are missing in task.json
        """
        task_path = Path(task_dir)

        if not task_path.exists():
            raise FileNotFoundError(f"Task directory not found: {task_dir}")

        # Load task.json
        task_json_path = task_path / "task.json"
        if not task_json_path.exists():
            raise FileNotFoundError(f"task.json not found in {task_dir}")

        with open(task_json_path) as f:
            config = json.load(f)

        # Load prompt templates
        init_prompt_path = task_path / "init_prompt.md"
        if not init_prompt_path.exists():
            raise FileNotFoundError(f"init_prompt.md not found in {task_dir}")

        with open(init_prompt_path) as f:
            init_prompt = f.read()

        iter_prompt_path = task_path / "iter_prompt.md"
        if not iter_prompt_path.exists():
            raise FileNotFoundError(f"iter_prompt.md not found in {task_dir}")

        with open(iter_prompt_path) as f:
            iter_prompt = f.read()

        # Validate required fields
        required_fields = ["name", "description"]
        for field in required_fields:
            if field not in config:
                raise KeyError(f"Required field '{field}' missing in task.json")

        return cls(
            name=config["name"],
            description=config["description"],
            init_prompt=init_prompt,
            iter_prompt=iter_prompt,
            state_file=config.get("state_file", f"{config['name']}_state.json"),
            initial_state=config.get("initial_state", {}),
            success_conditions=config.get("success_conditions", []),
            delay_seconds=config.get("delay_seconds", 3),
            state_processor=config.get("state_processor"),
            browser_tool=config.get("browser_tool", "playwright"),
            allowed_commands=config.get("allowed_commands", []),
            system_prompt=config.get("system_prompt"),
        )

    def format_init_prompt(self, **variables) -> str:
        """
        Format the initial prompt with variables.

        Args:
            **variables: Variables to substitute in the prompt template

        Returns:
            Formatted prompt string
        """
        try:
            return self.init_prompt.format(**variables)
        except KeyError as e:
            raise ValueError(
                f"Missing required variable for init_prompt: {e}. "
                f"Available variables: {list(variables.keys())}"
            )

    def format_iter_prompt(self, **variables) -> str:
        """
        Format the iteration prompt with variables.

        Args:
            **variables: Variables to substitute in the prompt template

        Returns:
            Formatted prompt string
        """
        try:
            return self.iter_prompt.format(**variables)
        except KeyError as e:
            raise ValueError(
                f"Missing required variable for iter_prompt: {e}. "
                f"Available variables: {list(variables.keys())}"
            )
