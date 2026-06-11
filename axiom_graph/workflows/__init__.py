"""Workflow-aware code for axiom-graph.

This subpackage consolidates all envelope/step/annotation-rule code.
Split out in Phase 4 (ADR-005) — previously scattered across
``axiom_graph/api.py``, ``axiom_graph/scanners/annotation_validation.py``,
and the MCP server module.

Public surface re-exports the primary workflow API dataclasses and
functions so callers may import from ``axiom_graph.workflows`` directly.
"""

from axiom_graph.workflows.api import (
    ExpandedStep,
    StepRow,
    WorkflowDetail,
    WorkflowRow,
    workflow_detail,
    workflow_expanded_steps,
    workflow_list,
)

__all__ = [
    "ExpandedStep",
    "StepRow",
    "WorkflowDetail",
    "WorkflowRow",
    "workflow_detail",
    "workflow_expanded_steps",
    "workflow_list",
]
