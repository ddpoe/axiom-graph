"""Decorator factories for ``@workflow`` and ``@task``.

By default these decorators simply validate (purpose non-empty is handled
by downstream hooks), preserve the wrapped function via ``functools.wraps``,
and return it unchanged — so a zero-dependency consumer can annotate code
without any registry infrastructure.

Applications that want to collect decorated functions into a registry
install a callback via :func:`set_register_hook`.  When a hook is
registered, it is invoked at decoration time with the same keyword
arguments the decorator received, giving the host application full control
over registration and validation.
"""

import functools
from typing import Any, Callable, Optional

# Module-level register hook.  ``None`` means "no-op, just wrap the
# function".  Host applications call set_register_hook() at import time
# to install their own registry integration.
_register_hook: Optional[Callable[..., None]] = None


def set_register_hook(hook: Optional[Callable[..., None]]) -> None:
    """Install (or clear) the registry callback for @workflow / @task.

    The hook, if set, is invoked as::

        hook(func, purpose=..., kind=..., inputs=..., outputs=..., critical=...)

    every time a ``@workflow`` or ``@task`` decorator is applied.  Pass
    ``None`` to restore the default no-op behaviour.

    Args:
        hook: Callable receiving the decorated function and decorator kwargs,
              or ``None`` to clear any previously installed hook.
    """
    global _register_hook
    _register_hook = hook


def _base_decorator(
    purpose: str,
    kind: str,
    inputs: Optional[str] = None,
    outputs: Optional[str] = None,
    critical: Optional[str] = None,
) -> Callable:
    """
    Base decorator factory for workflow and task decorators.

    Args:
        purpose: What this function accomplishes
        kind: "WORKFLOW" or "TASK"
        inputs: Optional description of inputs
        outputs: Optional description of outputs
        critical: Optional warning about time/resources
    """

    def decorator(func: Callable) -> Callable:
        if _register_hook is not None:
            _register_hook(
                func,
                purpose=purpose,
                kind=kind,
                inputs=inputs,
                outputs=outputs,
                critical=critical,
            )

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return wrapper

    return decorator


def workflow(
    purpose: str,
    inputs: Optional[str] = None,
    outputs: Optional[str] = None,
    critical: Optional[str] = None,
) -> Callable:
    """
    Decorator for workflow entry-point functions.

    Workflows are top-level orchestration functions that coordinate
    multiple tasks. They appear in WorkflowEntry table for discovery.

    Args:
        purpose: What this workflow accomplishes (required)
        inputs: Optional description of inputs
        outputs: Optional description of outputs
        critical: Optional warning about time/resources

    Usage:
        @workflow(purpose="Complete single-cell analysis pipeline")
        def run_scrna_pipeline(data_path: str):
            ...
    """
    return _base_decorator(purpose, "WORKFLOW", inputs, outputs, critical)


def task(
    purpose: str,
    inputs: Optional[str] = None,
    outputs: Optional[str] = None,
    critical: Optional[str] = None,
) -> Callable:
    """
    Decorator for task functions.

    Tasks are reusable units of work that can be called from workflows
    or other tasks. AutoStep markers pull documentation from tasks.

    Args:
        purpose: What this task accomplishes (required)
        inputs: Optional description of inputs
        outputs: Optional description of outputs
        critical: Optional warning about time/resources

    Usage:
        @task(purpose="Load and validate data from h5ad file")
        def load_adata(path: str):
            ...
    """
    return _base_decorator(purpose, "TASK", inputs, outputs, critical)


__all__ = [
    "workflow",
    "task",
    "set_register_hook",
    "_base_decorator",
]
