"""Workflow envelope inspection tools.

Covers both annotation-based workflow/task envelopes (Python ``@workflow`` /
``@task`` decorated functions) and xstate v5 state-machine envelopes
(``createMachine`` calls in TypeScript / JavaScript modules).

Tools:
    axiom_graph_workflow_list   -- list workflow / task / state-machine
                                   envelopes with filters
    axiom_graph_workflow_detail -- show ordered steps for a workflow / task
                                   or state tree for a state machine
    axiom_graph_workflow_export -- write workflows and their code to one
                                   shareable HTML page or JSON bundle
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from axiom_graph.workflows.api import (
    StateMachineDetail,
    WorkflowExportError,
    workflow_detail as _api_workflow_detail,
    workflow_detail_to_dict as _api_workflow_detail_to_dict,
    workflow_list as _api_workflow_list,
    write_workflow_export as _api_write_workflow_export,
)

logger = logging.getLogger(__name__)


def axiom_graph_workflow_list(
    project_root: str,
    module: str | None = None,
    role: str | None = None,
    scope: str = "production",
    has_steps: bool = False,
    max_results: int = 30,
    offset: int = 0,
) -> str:
    """List workflows, tasks and state machines, with their purpose and location.

    Returns one line per envelope: name, role (``workflow``, ``task``, or
    ``state_machine``), purpose summary, file location, and the
    corresponding axiom-graph node ID (when resolved).  Use the axiom-graph
    node ID with ``axiom_graph_render``, ``axiom_graph_graph``, or
    ``axiom_graph_source`` to inspect the implementation.

    Args:
        project_root: Absolute path to the indexed project.
        module: File path substring filter.  ``module="staleness"`` returns
            only workflows defined in files whose path contains
            ``staleness``.
        role: Filter by decorator type: ``"workflow"``, ``"task"``, or
            ``"state_machine"``.  Omit to list all roles.
        scope: Which part of the codebase to include:
            ``"production"`` (default) -- exclude functions in test paths.
            ``"tests"`` -- only functions in test paths.
            ``"all"`` -- everything.
            Test paths are defined by ``test_paths`` in ``axiom-graph.toml``.
        has_steps: When True, only return functions that contain at least one
            Step marker.  Filters out leaf-level logic and shows only
            orchestration workflows with internal structure.
        max_results: Maximum rows returned (default 30).
        offset: Starting index for pagination (default 0).
    """
    logger.debug("axiom_graph_workflow_list: module=%s, role=%s, scope=%s", module, role, scope)

    rows = _api_workflow_list(
        project_root,
        module=module,
        role=role,
        scope=scope,  # type: ignore[arg-type]
        has_steps=has_steps,
    )

    total = len(rows)
    page = rows[offset : offset + max_results]

    lines = []
    for r in page:
        purpose = r.purpose
        if len(purpose) > 60:
            purpose = purpose[:57] + "..."
        ag_ref = f"  \u2192 {r.node_id}" if r.node_id else ""
        lines.append(f'{r.name}  @{r.role}  "{purpose}"  {r.file}#L{r.line}{ag_ref}')

    header = f"[{len(page)} of {total} workflows]"
    if total > offset + max_results:
        header += f"  (cap={max_results}; pass offset={offset + max_results} for next page)"

    body = "\n".join(lines) if lines else "(no workflows)"
    return f"{header}\n{body}"


def axiom_graph_workflow_detail(
    project_root: str,
    workflow_id: str,
    verbose: bool = False,
    format: str = "text",
) -> str:
    """Show envelope detail: ordered steps for a workflow / task, or
    the state tree (with transitions) for an xstate state machine.

    ``workflow_id`` is the envelope name (e.g. ``"run_pipeline"`` or the
    machine id) or an axiom-graph node ID.  For workflow / task envelopes
    steps are always included.  For state-machine envelopes the state
    tree, transitions, and invoke / spawn relationships are included.
    Pass ``verbose=True`` to also show ``purpose``, ``inputs``,
    ``outputs``, and ``critical`` on the workflow and each step (no-op for
    state machines).

    Args:
        project_root: Absolute path to the indexed project.
        workflow_id: Envelope name or axiom-graph node ID.
        verbose: When ``True``, include purpose, inputs, outputs, and
            critical fields on the workflow header and on each step.
            (No additional fields are unlocked for state machines.)
        format: ``"text"`` (the default) for the human-readable outline,
            or ``"json"`` for the envelope's full structured form —
            the same per-workflow shape the export bundle carries.
    """
    detail = _api_workflow_detail(project_root, workflow_id)
    if detail is None:
        return f"ERROR: Workflow '{workflow_id}' not found in the axiom-graph index."

    if format == "json":
        return json.dumps(_api_workflow_detail_to_dict(detail), indent=2)

    if isinstance(detail, StateMachineDetail):
        return _format_state_machine_detail(detail, verbose=verbose)

    parts = [f"=== {detail.name} (@{detail.role}) ==="]
    parts.append(f"file: {detail.file}#L{detail.line}")
    if detail.node_id:
        parts.append(f"axiom_graph_node: {detail.node_id}")

    if verbose:
        if detail.purpose:
            parts.append(f'purpose: "{detail.purpose}"')
        if detail.inputs:
            parts.append(f"inputs: {detail.inputs}")
        if detail.outputs:
            parts.append(f"outputs: {detail.outputs}")
        if detail.critical:
            parts.append(f"critical: {detail.critical}")

    if detail.steps:
        parts.append("")
        parts.append(f"Steps ({len(detail.steps)}):")
        for s in detail.steps:
            if s.delegates_to_node_id:
                delegate_ref = f" \u2192 {s.delegates_to_node_id}"
            elif s.delegates_to_name:
                delegate_ref = f" \u2192 {s.delegates_to_name} (unresolved)"
            else:
                delegate_ref = ""
            parts.append(f"  {s.step_num}. {s.name}{delegate_ref}")
            if verbose:
                if s.purpose:
                    parts.append(f'     purpose: "{s.purpose}"')
                if s.inputs:
                    parts.append(f"     inputs: {s.inputs}")
                if s.outputs:
                    parts.append(f"     outputs: {s.outputs}")
                if s.critical:
                    parts.append(f"     critical: {s.critical}")
    else:
        parts.append("")
        parts.append("(no steps recorded)")

    if detail.coverage_targets:
        parts.append("")
        parts.append(f"Validates ({len(detail.coverage_targets)}):")
        for target in detail.coverage_targets:
            parts.append(f"  - {target}")

    return "\n".join(parts)


def axiom_graph_workflow_export(
    project_root: str,
    workflow_ids: list[str] | None = None,
    files: list[str] | None = None,
    output_path: str | None = None,
    format: str = "html",
) -> str:
    """Write workflows and their source code to one shareable HTML page or JSON bundle.

    The page is self-contained: the outline of each workflow, every step,
    and the source of every file a step or its delegate target is written
    in, with no external references, so it opens offline and can be sent to
    someone without the repository.  It is the same page the dashboard's
    export button opens and ``axiom-graph workflows export`` writes.  The
    tool writes the file and returns its path and counts, not the page.

    Args:
        project_root: Absolute path to the indexed project.
        workflow_ids: Workflow or task names, or node IDs from
            ``axiom_graph_workflow_list``.  Workflows and tasks can be mixed.
        files: Source files, relative to the project root, whose every
            workflow and task is exported.
        output_path: File to write.  A relative path resolves against
            *project_root*.  Default: ``workflow-export.html`` (or
            ``.json``) in *project_root*.
        format: ``"html"`` (the default) for the page, ``"json"`` for the
            bundle.

    Returns:
        ``Wrote <path>: N workflows · M files``, or an ``ERROR:`` line
        naming every id or file that matched nothing.  Nothing is written
        on error.
    """
    root = Path(project_root)
    out = Path(output_path) if output_path else Path(f"workflow-export.{format}")
    if not out.is_absolute():
        out = root / out
    try:
        summary = _api_write_workflow_export(
            root,
            out,
            workflow_ids=workflow_ids or (),
            files=files or (),
            format=format,
        )
    except WorkflowExportError as exc:
        return f"ERROR: {exc}"
    return f"Wrote {summary.path}: {summary.label}"


def _format_state_machine_detail(detail: StateMachineDetail, *, verbose: bool) -> str:
    """Render a :class:`StateMachineDetail` for the MCP tool output.

    State-machine output uses an event-driven shape — no ``step_num``
    prefix.  Each state lists its outgoing transitions as
    ``EVENT → target`` for ``on``-handlers, ``(always) → target``
    for eventless transitions, and ``(after Nms) → target`` for delayed
    ones.  Terminal states are tagged ``[final]``.  Compound states render
    their direct children as a nested block.
    """
    parts = [f"=== {detail.name} (@{detail.role}) ==="]
    parts.append(f"file: {detail.file}#L{detail.line}")
    if detail.node_id:
        parts.append(f"axiom_graph_node: {detail.node_id}")
    if verbose and detail.purpose:
        parts.append(f'purpose: "{detail.purpose}"')

    if detail.states:
        parts.append("")
        parts.append(f"States ({len(detail.states)}):")
        for s in detail.states:
            tags: list[str] = []
            if s.is_compound:
                tags.append("compound")
            if s.is_terminal:
                tags.append("final")
            tag_blob = f"  [{', '.join(tags)}]" if tags else ""
            head = f"  {s.path}{tag_blob}"
            if verbose and s.purpose:
                head = f"{head}  —  {s.purpose}"
            parts.append(head)
            for t in s.transitions:
                if t.via == "on":
                    label = t.event or "(unnamed event)"
                elif t.via == "always":
                    label = "(always)"
                elif t.via == "after":
                    delay = t.delay or "?"
                    label = f"(after {delay}ms)"
                elif t.via.startswith("invoke."):
                    label = f"({t.via})"
                else:
                    label = f"({t.via})"
                parts.append(f"    {label} → {t.target}")
    else:
        parts.append("")
        parts.append("(no states recorded)")

    return "\n".join(parts)
