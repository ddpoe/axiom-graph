"""axiom-annotations — zero-dependency workflow/task/Step/AutoStep markers.

Public surface::

    from axiom_annotations import (
        workflow, task,          # decorators
        Step, AutoStep,          # inline markers (factory functions)
        StepMarker, AutoStepMarker,
        StepValidationError,
        set_register_hook,       # host-app integration point
    )

Per-marker validation helpers (``validate_step_num``, ``validate_step_args``,
``validate_autostep_args``) live in ``axiom_annotations.validation``.
"""

from .decorators import set_register_hook, task, workflow
from .exceptions import StepValidationError
from .markers import AutoStep, AutoStepMarker, Step, StepMarker

__all__ = [
    "workflow",
    "task",
    "Step",
    "AutoStep",
    "StepMarker",
    "AutoStepMarker",
    "StepValidationError",
    "set_register_hook",
]

__version__ = "0.1.0"
