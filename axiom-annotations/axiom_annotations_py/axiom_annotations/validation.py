"""Per-marker argument validation for Step() and AutoStep().

These validators check a single marker's arguments in isolation — they are
stateless and have no knowledge of surrounding steps.  Sequence-level
checks (ordering, duplicates, minor-in-loops) live in the consumer
(axiom-graph) and are not part of this package.
"""

from typing import Any

from .exceptions import StepValidationError


def validate_step_num(step_num: Any) -> None:
    """Validate step_num is a positive int or float.

    Raises:
        StepValidationError: If step_num is invalid.
    """
    if not isinstance(step_num, (int, float)):
        raise StepValidationError(step_num, f"step_num must be int or float, got {type(step_num).__name__}")
    if step_num <= 0:
        raise StepValidationError(step_num, "step_num must be positive")


def validate_step_args(
    step_num: Any,
    name: Any,
    purpose: Any,
) -> None:
    """Validate arguments for a Step() marker.

    Raises:
        StepValidationError: If any required argument is missing or invalid.
    """
    validate_step_num(step_num)

    if not name or (isinstance(name, str) and not name.strip()):
        raise StepValidationError(step_num, "'name' is required and cannot be empty")

    if not isinstance(name, str):
        raise StepValidationError(step_num, f"'name' must be a string, got {type(name).__name__}")

    if not purpose or (isinstance(purpose, str) and not purpose.strip()):
        raise StepValidationError(step_num, "'purpose' is required and cannot be empty")

    if not isinstance(purpose, str):
        raise StepValidationError(step_num, f"'purpose' must be a string, got {type(purpose).__name__}")


def validate_autostep_args(step_num: Any, name: Any = None) -> None:
    """Validate arguments for an AutoStep() marker.

    Args:
        step_num: Must be a positive int or float.
        name: Optional display name. If provided, must be a non-empty string.

    Raises:
        StepValidationError: If step_num is invalid or name is invalid.
    """
    validate_step_num(step_num)

    if name is not None:
        if not isinstance(name, str):
            raise StepValidationError(step_num, f"'name' must be a string, got {type(name).__name__}")
        if not name.strip():
            raise StepValidationError(step_num, "'name' cannot be an empty string")


__all__ = [
    "validate_step_num",
    "validate_step_args",
    "validate_autostep_args",
]
