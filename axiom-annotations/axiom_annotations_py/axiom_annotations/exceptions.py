"""Exceptions raised by axiom-annotations markers and decorators."""

from typing import Optional


class StepValidationError(Exception):
    """Raised when a Step() or AutoStep() marker has invalid arguments.

    This fires at runtime (when the function is called), giving immediate
    feedback in notebooks and during execution.
    """

    def __init__(self, step_num: Optional[float] = None, message: str = ""):
        self.step_num = step_num
        prefix = f"Step {step_num}" if step_num is not None else "Step"
        super().__init__(f"{prefix}: {message}")
