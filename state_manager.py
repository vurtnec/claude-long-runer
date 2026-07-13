"""
State Manager
=============

Manages task execution state with JSON file persistence.
Tracks iteration progress, task status, and custom state data.
"""

import json
from pathlib import Path
from typing import Any, Dict


class StateManager:
    """
    Task state manager with JSON file persistence.

    Handles loading existing state or initializing new state,
    and provides methods for updating and saving state.

    Attributes:
        task_name: Name of the task
        state_file: Path to the state JSON file
        data: Current state data dictionary
    """

    def __init__(
        self, task_name: str, state_file: str, initial_state: Dict[str, Any]
    ):
        """
        Initialize state manager.

        Args:
            task_name: Task identifier
            state_file: Path to state JSON file
            initial_state: Default state values for new runs
        """
        self.task_name = task_name
        self.state_file = Path(state_file)
        self.data = self._load_or_init(initial_state)

        # Save newly initialized state to disk
        if not self.state_file.exists() or self.data.get("status") == "pending":
            self.save()

    def _load_or_init(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Load existing state from file or initialize with defaults.

        Args:
            initial_state: Default state values

        Returns:
            State dictionary
        """
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    loaded_state = json.load(f)
                    print(f"Loaded existing state from {self.state_file}")
                    return loaded_state
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Failed to load state file: {e}")
                print("Initializing with default state")

        # Initialize new state with metadata
        state = {
            **initial_state,
            "task_name": self.task_name,
            "iteration": 0,
            "status": "pending",
            "last_response": "",
        }
        print("Initialized new state (will be saved after __init__)")
        return state

    def save(self) -> None:
        """
        Persist current state to JSON file.

        Raises:
            IOError: If file cannot be written
        """
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.data, f, indent=2)
        except IOError as e:
            print(f"Error: Failed to save state to {self.state_file}: {e}")
            raise

    def update(self, **kwargs: Any) -> None:
        """
        Update state fields and persist changes.

        Args:
            **kwargs: Field names and values to update
        """
        self.data.update(kwargs)
        self.save()

    def increment_iteration(self) -> int:
        """
        Increment the iteration counter and save.

        Returns:
            New iteration number
        """
        self.data["iteration"] = self.data.get("iteration", 0) + 1
        self.save()
        return self.data["iteration"]

    def is_initialized(self) -> bool:
        """
        Check if the task has been initialized.

        Returns:
            True if task status is not 'pending'
        """
        return self.data.get("status") != "pending"

    def mark_initialized(self) -> None:
        """Mark the task as initialized (no longer pending)."""
        self.update(status="initialized")

    def mark_completed(self, success: bool = True) -> None:
        """
        Mark the task as completed.

        Args:
            success: Whether the task completed successfully
        """
        self.update(status="completed" if success else "failed")

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a state value.

        Args:
            key: State key
            default: Default value if key not found

        Returns:
            State value or default
        """
        return self.data.get(key, default)

    def set_last_response(self, response: str) -> None:
        """
        Update the last response text.

        Args:
            response: Response text from the agent
        """
        self.update(last_response=response)

    def __repr__(self) -> str:
        """String representation of state."""
        return (
            f"StateManager(task={self.task_name}, "
            f"iteration={self.data.get('iteration', 0)}, "
            f"status={self.data.get('status', 'unknown')})"
        )
