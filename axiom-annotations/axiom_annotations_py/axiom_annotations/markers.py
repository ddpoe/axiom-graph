"""Step and AutoStep markers.

Provides the ``Step()`` and ``AutoStep()`` factory functions plus their
marker object classes (``StepMarker`` and ``AutoStepMarker``).  Arguments
are validated at call time via :mod:`axiom_annotations.validation`.
"""

from typing import Optional

from .validation import validate_autostep_args, validate_step_args


# =============================================================================
# Marker objects (returned by Step/AutoStep so they're real values, not None)
# =============================================================================


class StepMarker:
    """Lightweight marker returned by Step().

    Holds step metadata for potential runtime use (logging, timing, etc.).
    The AST scanner extracts the same data statically at registration time.
    """

    __slots__ = ("step_num", "name", "purpose", "inputs", "outputs", "critical")

    def __init__(
        self,
        step_num: float,
        name: str,
        purpose: str,
        inputs: Optional[str] = None,
        outputs: Optional[str] = None,
        critical: Optional[str] = None,
    ):
        self.step_num = step_num
        self.name = name
        self.purpose = purpose
        self.inputs = inputs
        self.outputs = outputs
        self.critical = critical

    def __repr__(self) -> str:
        return f"StepMarker({self.step_num}, {self.name!r})"


class AutoStepMarker:
    """Lightweight marker returned by AutoStep().

    Holds the step number and an optional user-supplied name.
    All other metadata (purpose, inputs, outputs, critical) is resolved
    from the called function during the assemble phase.
    """

    __slots__ = ("step_num", "name")

    def __init__(self, step_num: float, name: Optional[str] = None):
        self.step_num = step_num
        self.name = name

    def __repr__(self) -> str:
        if self.name:
            return f"AutoStepMarker({self.step_num}, {self.name!r})"
        return f"AutoStepMarker({self.step_num})"


# =============================================================================
# Step and AutoStep factory functions
# =============================================================================


def Step(
    step_num: float,
    name: str,
    purpose: str,
    inputs: Optional[str] = None,
    outputs: Optional[str] = None,
    critical: Optional[str] = None,
) -> StepMarker:
    """
    Marker for inline code blocks within a workflow/task function.

    Validates arguments at runtime — raises StepValidationError if
    name or purpose is missing/empty, or step_num is invalid.

    Minor steps (1.1, 1.2) should only be used inside loops.

    Args:
        step_num: Step number (int for major, float for minor e.g., 1.1)
        name: Short descriptive name for the step
        purpose: What this step accomplishes (required)
        inputs: Optional description of inputs
        outputs: Optional description of outputs
        critical: Optional warning about time/resources

    Returns:
        StepMarker with the step metadata.

    Raises:
        StepValidationError: If name or purpose is empty, or step_num is invalid.

    Usage:
        ax = Step(step_num=1, name="Filter cells", purpose="Remove low-quality cells")
        ax = Step(step_num=2, name="Train model", purpose="Fit classifier",
                  inputs="Filtered data", outputs="Trained model",
                  critical="Takes 30+ minutes")
    """
    validate_step_args(step_num, name, purpose)
    return StepMarker(step_num, name, purpose, inputs, outputs, critical)


def AutoStep(step_num: float, name: Optional[str] = None) -> AutoStepMarker:
    """
    Marker that extracts documentation from the next function call.

    Validates that step_num is a valid positive number. The next line
    should be a call to a @task or @workflow decorated function — this
    is verified at registration time by the cross-step validator.

    An optional ``name`` can override the called function's name in
    generated documentation.  All other metadata (purpose, inputs,
    outputs, critical) is still resolved from the called function.

    Args:
        step_num: Step number (int for major, float for minor e.g., 1.1)
        name: Optional display name for the step. If omitted, the called
              function's name is used.

    Returns:
        AutoStepMarker with the step number (and optional name).

    Raises:
        StepValidationError: If step_num is invalid or name is not a string.

    Usage:
        ax = AutoStep(step_num=1)
        result = some_decorated_function(args)

        ax = AutoStep(step_num=2, name="Load raw data")
        result = load_data(args)
    """
    validate_autostep_args(step_num, name)
    return AutoStepMarker(step_num, name)


__all__ = ["StepMarker", "AutoStepMarker", "Step", "AutoStep"]
