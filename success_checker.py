"""
Success Condition Checker
==========================

Extensible system for checking task completion conditions.
Supports multiple condition types and custom validation functions.
"""

from typing import Any, Callable, Dict, List


class SuccessChecker:
    """
    Evaluates success conditions for task completion.

    Supports multiple built-in condition types and custom functions.
    All conditions must be satisfied for success (AND logic).

    Built-in condition types:
    - text_contains: Check if response contains a string
    - text_not_contains: Check if response does not contain a string
    - state_equals: Check if a state field equals a value
    - state_not_equals: Check if a state field does not equal a value
    - iteration_limit: Check if iteration count reached limit
    - custom_function: Execute a custom validation function
    """

    # Type signature for condition checker functions
    ConditionChecker = Callable[[Dict[str, Any], Dict[str, Any]], bool]

    def __init__(self, conditions: List[Dict[str, Any]]):
        """
        Initialize success checker with conditions.

        Args:
            conditions: List of condition definitions. Each condition is a dict with:
                - type: Condition type name
                - Additional fields depend on type

        Example conditions:
            [
                {"type": "text_contains", "text": "no critical issues"},
                {"type": "state_equals", "key": "review_passed", "value": true},
                {"type": "iteration_limit", "max": 5}
            ]
        """
        self.conditions = conditions
        self._condition_types: Dict[str, SuccessChecker.ConditionChecker] = {
            "text_contains": self._check_text_contains,
            "text_not_contains": self._check_text_not_contains,
            "state_equals": self._check_state_equals,
            "state_not_equals": self._check_state_not_equals,
            "iteration_limit": self._check_iteration_limit,
            "custom_function": self._check_custom_function,
        }

    def check(self, state: Dict[str, Any]) -> bool:
        """
        Check if all success conditions are satisfied.

        Args:
            state: Current task state dictionary

        Returns:
            True if all conditions pass, False otherwise

        Raises:
            ValueError: If condition type is unknown or invalid
        """
        if not self.conditions:
            # No conditions defined - never complete automatically
            return False

        for condition in self.conditions:
            cond_type = condition.get("type")

            if not cond_type:
                raise ValueError(f"Condition missing 'type' field: {condition}")

            checker_fn = self._condition_types.get(cond_type)
            if not checker_fn:
                raise ValueError(
                    f"Unknown condition type: {cond_type}. "
                    f"Available types: {list(self._condition_types.keys())}"
                )

            # If any condition fails, return False
            if not checker_fn(state, condition):
                return False

        # All conditions passed
        return True

    def _check_text_contains(
        self, state: Dict[str, Any], condition: Dict[str, Any]
    ) -> bool:
        """Check if last response contains a text string."""
        text = condition.get("text", "")
        last_response = state.get("last_response", "")

        if not text:
            raise ValueError("text_contains condition requires 'text' field")

        case_sensitive = condition.get("case_sensitive", False)

        if case_sensitive:
            return text in last_response
        else:
            return text.lower() in last_response.lower()

    def _check_text_not_contains(
        self, state: Dict[str, Any], condition: Dict[str, Any]
    ) -> bool:
        """Check if last response does not contain a text string."""
        text = condition.get("text", "")
        last_response = state.get("last_response", "")

        if not text:
            raise ValueError("text_not_contains condition requires 'text' field")

        case_sensitive = condition.get("case_sensitive", False)

        if case_sensitive:
            return text not in last_response
        else:
            return text.lower() not in last_response.lower()

    def _check_state_equals(
        self, state: Dict[str, Any], condition: Dict[str, Any]
    ) -> bool:
        """Check if a state field equals a specific value."""
        key = condition.get("key")
        expected_value = condition.get("value")

        if key is None:
            raise ValueError("state_equals condition requires 'key' field")
        if expected_value is None:
            raise ValueError("state_equals condition requires 'value' field")

        return state.get(key) == expected_value

    def _check_state_not_equals(
        self, state: Dict[str, Any], condition: Dict[str, Any]
    ) -> bool:
        """Check if a state field does not equal a specific value."""
        key = condition.get("key")
        expected_value = condition.get("value")

        if key is None:
            raise ValueError("state_not_equals condition requires 'key' field")
        if expected_value is None:
            raise ValueError("state_not_equals condition requires 'value' field")

        return state.get(key) != expected_value

    def _check_iteration_limit(
        self, state: Dict[str, Any], condition: Dict[str, Any]
    ) -> bool:
        """Check if iteration count reached a limit."""
        max_iterations = condition.get("max")

        if max_iterations is None:
            raise ValueError("iteration_limit condition requires 'max' field")

        current_iteration = state.get("iteration", 0)
        return current_iteration >= max_iterations

    def _check_custom_function(
        self, state: Dict[str, Any], condition: Dict[str, Any]
    ) -> bool:
        """
        Execute a custom validation function.

        WARNING: This uses eval() and should only be used with trusted conditions.
        The function string should define a function that takes state as argument.

        Example:
            {"type": "custom_function", "function": "lambda s: s.get('score', 0) > 80"}
        """
        function_str = condition.get("function")

        if not function_str:
            raise ValueError("custom_function condition requires 'function' field")

        try:
            # Evaluate the function string in a restricted namespace
            # This is still potentially dangerous - use with caution
            custom_fn = eval(function_str, {"__builtins__": {}}, {})
            return bool(custom_fn(state))
        except Exception as e:
            raise ValueError(
                f"Failed to execute custom_function: {e}. "
                f"Function string: {function_str}"
            )

    def add_condition_type(
        self, name: str, checker_fn: ConditionChecker
    ) -> None:
        """
        Register a custom condition type.

        Args:
            name: Condition type name
            checker_fn: Function that takes (state, condition) and returns bool
        """
        self._condition_types[name] = checker_fn

    def get_condition_summary(self) -> str:
        """
        Get a human-readable summary of conditions.

        Returns:
            String describing all conditions
        """
        if not self.conditions:
            return "No success conditions defined"

        lines = ["Success conditions (all must be satisfied):"]
        for i, condition in enumerate(self.conditions, 1):
            cond_type = condition.get("type", "unknown")
            lines.append(f"  {i}. {cond_type}: {condition}")

        return "\n".join(lines)
