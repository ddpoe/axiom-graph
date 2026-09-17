"""Public structured Python API for axiom-graph data.

These functions return typed dataclasses suitable for programmatic use
(e.g. build-time generators emitting Markdown catalogs of ``@workflow``
functions).  They are the primitive layer that the MCP tools and any
future CLI formatters build on — MCP text blobs are produced by
formatting the output of these functions, not by a parallel query path.

Public surface:
    ``WorkflowRow``             -- list-row dataclass
    ``workflow_list``           -- list workflow/task functions with filters
    ``StepRow``                 -- single-step dataclass inside ``WorkflowDetail``
    ``WorkflowDetail``          -- full workflow view: header + ordered steps
    ``workflow_detail``         -- fetch one workflow by name or node ID
    ``ExpandedStep``            -- single entry in an expanded-step sequence
    ``workflow_expanded_steps`` -- expand a workflow's composed + delegated
                                   step tree with transitive renumbering
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from axiom_graph.config import AxiomGraphConfig
from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.scanners.node_hashing import parse_node_title

__all__ = [
    "WorkflowGraph",
    "build_workflow_graph",
    "load_workflow_graph",
    "DelegateTarget",
    "step_delegate_target",
    "WorkflowRow",
    "workflow_list",
    "StepRow",
    "WorkflowDetail",
    "workflow_detail",
    "workflow_detail_to_dict",
    "ExpandedStep",
    "workflow_expanded_steps",
    "WorkflowBundle",
    "workflow_export_bundle",
    "workflow_bundle_to_dict",
    "Transition",
    "StateRow",
    "StateMachineDetail",
]


# ---------------------------------------------------------------------------
# Internal loaders (index-resident)
# ---------------------------------------------------------------------------


STEP_SUBTYPES = frozenset({"step", "autostep"})


@dataclass(frozen=True)
class WorkflowGraph:
    """An in-memory snapshot of the index structures the workflow API walks.

    Produced once by :func:`load_workflow_graph` and handed to every
    accessor that needs it, so a caller expanding many envelopes pays for
    one read of the index rather than one read per envelope.

    Attributes:
        nodes_by_id: Every indexed node, keyed by node ID.
        composes_out: Envelope / state node ID to the list of node IDs it
            composes, in edge-scan order.
        delegates_out: Step or AutoStep node ID to the target node IDs it
            delegates to, in edge-scan order.  Restricted to step sources
            by construction, so a state machine's transition fan-out is
            deliberately absent — no consumer reads delegation for a
            state, and the machine path builds its transitions from a
            separate scan.  Read it through
            :func:`step_delegate_target`, which is where the choice
            between several recorded targets is made.
        annotates_rev: Annotated node ID to the list of envelope node IDs
            that annotate it.
    """

    nodes_by_id: dict = field(default_factory=dict)
    composes_out: dict[str, list[str]] = field(default_factory=dict)
    delegates_out: dict[str, list[str]] = field(default_factory=dict)
    annotates_rev: dict[str, list[str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """True when the graph holds at least one node."""
        return bool(self.nodes_by_id)


def build_workflow_graph(nodes_by_id: dict, edges) -> WorkflowGraph:
    """Fold nodes and edges into the structures the workflow API walks.

    Args:
        nodes_by_id: Every node, keyed by node ID.
        edges: Every edge, in any order.

    Returns:
        The populated :class:`WorkflowGraph`.  ``delegates_to`` edges
        whose source is not a ``step`` / ``autostep`` node are dropped, so
        the delegate map describes step delegation only.
    """
    composes_out: dict[str, list[str]] = {}
    delegates_out: dict[str, list[str]] = {}
    annotates_rev: dict[str, list[str]] = {}
    for e in edges:
        if e.edge_type == "composes":
            composes_out.setdefault(e.from_id, []).append(e.to_id)
        elif e.edge_type == "delegates_to":
            source = nodes_by_id.get(e.from_id)
            if source is None or source.subtype not in STEP_SUBTYPES:
                continue
            delegates_out.setdefault(e.from_id, []).append(e.to_id)
        elif e.edge_type == "annotates":
            annotates_rev.setdefault(e.to_id, []).append(e.from_id)
    return WorkflowGraph(
        nodes_by_id=nodes_by_id,
        composes_out=composes_out,
        delegates_out=delegates_out,
        annotates_rev=annotates_rev,
    )


def load_workflow_graph(project_root: str | Path) -> WorkflowGraph:
    """Read the index once into a :class:`WorkflowGraph`.

    Args:
        project_root: Path to the indexed project.

    Returns:
        A populated :class:`WorkflowGraph`, or an empty one when the
        axiom-graph DB is absent.
    """
    ag_db = _db_path(str(project_root))
    if not ag_db.exists():
        return WorkflowGraph()
    nodes = {n.id: n for n in db.all_nodes(ag_db)}
    return build_workflow_graph(nodes, db.all_edges(ag_db))


def _load_graph(project_root: str | Path) -> WorkflowGraph:
    """Internal alias for :func:`load_workflow_graph`."""
    return load_workflow_graph(project_root)


@dataclass(frozen=True)
class DelegateTarget:
    """What a step delegates to, and the intent that target declares.

    Attributes:
        id: Node ID of the function the step calls.
        name: Short function name, or the node ID's trailing segment when
            the target node itself is absent from the index.
        location: Path to the target's source file, relative to the
            project root and forward-slash separated.  Empty string when
            the target node is not in the index.
        line: 1-based line number of the target's definition, or ``0``
            when unknown.
        purpose: ``purpose`` declared on the target's ``@workflow`` /
            ``@task`` envelope.  Empty when the target is undecorated.
        inputs: ``inputs`` declared on the target's envelope, else empty.
        outputs: ``outputs`` declared on the target's envelope, else
            empty.
        critical: ``critical`` declared on the target's envelope, else
            empty.
    """

    id: str
    name: str
    location: str
    line: int
    purpose: str = ""
    inputs: str = ""
    outputs: str = ""
    critical: str = ""


def _envelope_id_for(graph: WorkflowGraph, annotated_id: str) -> str | None:
    """Return the envelope annotating *annotated_id*, or ``None``.

    Deterministic when several envelopes somehow annotate one node: the
    lexicographically smallest envelope ID wins, matching the delegate
    tiebreak so every surface names the same envelope.
    """
    envelopes = graph.annotates_rev.get(annotated_id)
    if not envelopes:
        return None
    return min(envelopes)


def step_delegate_target(step_node_id: str, graph: WorkflowGraph) -> DelegateTarget | None:
    """Resolve what a step delegates to, in one place for every surface.

    This is the only resolution path in the project: the viz dashboard,
    the MCP tools and the export bundle all read a step's callee through
    this function, so they cannot name different targets for the same
    step.

    Args:
        step_node_id: Node ID of the ``step`` / ``autostep`` marker.
        graph: An already-loaded :class:`WorkflowGraph`.  The accessor
            never opens the index itself, so a caller resolving many
            steps pays a single read.

    Returns:
        A :class:`DelegateTarget`, or ``None`` when the step calls
        nothing.  A step whose target is present but undecorated resolves
        to a target with empty intent fields rather than ``None``, and a
        step whose target is missing from the index resolves to a target
        with an empty ``location`` — neither raises.
    """
    recorded = graph.delegates_out.get(step_node_id)
    if not recorded:
        return None
    # One tiebreak, expressed once: the lexicographically smallest target
    # wins.  A step is expected to hold exactly one target; when the index
    # holds several, every surface still names the same one, and the answer
    # does not reshuffle when the index is rebuilt in a different order.
    target_id = min(recorded)

    target_node = graph.nodes_by_id.get(target_id)
    if target_node is None:
        return DelegateTarget(
            id=target_id,
            name=target_id.rsplit("::", 1)[-1],
            location="",
            line=0,
        )

    envelope_id = _envelope_id_for(graph, target_id)
    envelope = graph.nodes_by_id.get(envelope_id) if envelope_id else None
    meta = envelope.dflow_meta if envelope is not None and isinstance(envelope.dflow_meta, dict) else {}

    return DelegateTarget(
        id=target_id,
        name=parse_node_title(target_node).last,
        location=(target_node.location or "").replace("\\", "/"),
        line=_func_line_from_level_3(target_node.level_3_location),
        purpose=str(meta.get("purpose") or ""),
        inputs=str(meta.get("inputs") or ""),
        outputs=str(meta.get("outputs") or ""),
        critical=str(meta.get("critical") or ""),
    )


# ---------------------------------------------------------------------------
# workflow_list — now index-resident (reads envelope nodes from the DB)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowRow:
    """One row returned by :func:`workflow_list`.

    Attributes:
        name: Function name as declared in source.
        role: Decorator role, either ``"workflow"`` or ``"task"``.
        purpose: Full, untruncated purpose string.  Empty when absent.
        file: Path to the source file, relative to the project root,
            always forward-slash separated.
        line: 1-based line number of the function definition.
        node_id: Axiom-graph node ID of the annotated function.
        step_count: Number of step nodes composed by the envelope.
        has_steps: Convenience flag equivalent to ``step_count > 0``.
    """

    name: str
    role: str
    purpose: str
    file: str
    line: int
    node_id: str | None
    step_count: int
    has_steps: bool


def _func_line_from_level_3(level_3: str | None) -> int:
    """Return the 1-based start line parsed from a level_3_location string.

    Format: ``"path/file.py#L10-L50"`` or ``"path/file.py#L10"``.
    """
    if not level_3 or "#L" not in level_3:
        return 1
    after_hash = level_3.rsplit("#L", 1)[-1]
    head = after_hash.split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return 1


def workflow_list(
    project_root: str | Path,
    *,
    module: str | None = None,
    role: str | None = None,
    scope: Literal["production", "tests", "all"] = "production",
    has_steps: bool = False,
) -> list[WorkflowRow]:
    """List axiom-graph workflow and task envelopes as structured rows.

    Args:
        project_root: Path to the indexed project (accepts ``str`` or ``Path``).
        module: Substring filter on the function's file path.
        role: Restrict to ``"workflow"`` or ``"task"``.  ``None`` returns both.
        scope: ``"production"`` excludes paths matching ``scan.test_paths`` in
            ``axiom-graph.toml``; ``"tests"`` keeps only those paths; ``"all"``
            disables filtering.
        has_steps: When True, only return envelopes composing at least one
            step node.

    Returns:
        List of :class:`WorkflowRow`.  Empty when the axiom-graph DB is
        absent.
    """
    project_root_str = str(project_root)
    graph = load_workflow_graph(project_root_str)
    nodes_by_id = graph.nodes_by_id
    composes_out = graph.composes_out
    annotates_rev = graph.annotates_rev
    if not nodes_by_id:
        return []

    roles = {role} if role else {"workflow", "task", "state_machine"}

    rows: list[WorkflowRow] = []
    for env in nodes_by_id.values():
        if env.node_type != "composite_process":
            continue
        if env.subtype not in roles:
            continue
        # Find annotated function via annotates out-edge.
        annotated_func_id: str | None = None
        # annotates_rev is target -> sources; we need source (env) -> target.
        # Rebuild once per call is fine for the small loop: walk composes_out
        # of envelope? No — annotates is env → func.  Use edges by env id.
        # Simpler: search annotates_rev for an entry whose source list
        # contains this env.
        for tgt, sources in annotates_rev.items():
            if env.id in sources:
                annotated_func_id = tgt
                break
        func_node = nodes_by_id.get(annotated_func_id) if annotated_func_id else None

        purpose = ""
        if env.dflow_meta and isinstance(env.dflow_meta, dict):
            purpose = str(env.dflow_meta.get("purpose") or "")

        file_path = (func_node.location if func_node else env.location) or ""
        file_path = file_path.replace("\\", "/")
        line = _func_line_from_level_3(func_node.level_3_location if func_node else env.level_3_location)

        # Count composed children:
        #   - workflow/task envelopes count step + autostep children
        #   - state_machine envelopes count direct state children (top-level
        #     states only; nested compound-state children are counted under
        #     their immediate parent state, not the machine envelope).
        step_count = 0
        if env.subtype == "state_machine":
            allowed_subtypes = {"state"}
        else:
            allowed_subtypes = {"step", "autostep"}
        for child_id in composes_out.get(env.id, []):
            child = nodes_by_id.get(child_id)
            if child is not None and child.subtype in allowed_subtypes:
                step_count += 1

        func_name = parse_node_title(func_node).last if func_node else parse_node_title(env).last

        rows.append(
            WorkflowRow(
                name=func_name,
                role=env.subtype or "",
                purpose=purpose,
                file=file_path,
                line=line,
                node_id=annotated_func_id,
                step_count=step_count,
                has_steps=step_count > 0,
            )
        )

    if has_steps:
        rows = [r for r in rows if r.has_steps]

    if module:
        rows = [r for r in rows if module in r.file]

    if scope in ("production", "tests"):
        test_prefixes = AxiomGraphConfig.load(Path(project_root_str)).scan.test_paths
        if test_prefixes:
            want_tests = scope == "tests"

            def _is_test(path: str) -> bool:
                return any(path.startswith(tp) for tp in test_prefixes)

            rows = [r for r in rows if _is_test(r.file) == want_tests]

    # Stable ordering: by file, then line.
    rows.sort(key=lambda r: (r.file, r.line))
    return rows


# ---------------------------------------------------------------------------
# workflow_expanded_steps — index-resident step expansion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpandedStep:
    """One entry in an expanded step sequence.

    Attributes:
        rendered_step_num: The dotted step number after transitive prefixing
            (e.g. ``"3.2.1"`` for a step reached via outer AutoSteps ``3`` and
            ``2``).  Always a string so leading-zero significance is
            preserved (``"1.10"`` stays distinct from ``"1.1"``).
        step_node_id: Axiom-graph node ID of the step being rendered.
        context_chain: Ordered list of envelope node IDs walked through to
            reach this step.  The innermost envelope is last.
        note: Optional annotation describing why expansion stopped at this
            step.  Values observed in this release: ``"cycle detected at
            {envelope_id}"`` and ``"target not annotated"``.
    """

    rendered_step_num: str
    step_node_id: str
    context_chain: list[str] = field(default_factory=list)
    note: str | None = None


def _step_num_parts(node) -> list[int]:
    """Return the step_num_parts array for a step node, falling back to [0]."""
    meta = getattr(node, "dflow_meta", None)
    if isinstance(meta, dict):
        parts = meta.get("step_num_parts")
        if isinstance(parts, list) and all(isinstance(p, int) for p in parts):
            return parts
    return [0]


def _step_num_raw(node) -> str:
    """Return the authored step_num_raw string, falling back to ``title``."""
    meta = getattr(node, "dflow_meta", None)
    if isinstance(meta, dict):
        raw = meta.get("step_num_raw")
        if isinstance(raw, str):
            return raw
    return node.title if node is not None else ""


def workflow_expanded_steps(
    project_root: str | Path,
    workflow_node_id: str,
    *,
    graph: WorkflowGraph | None = None,
) -> list[ExpandedStep]:
    """Expand a workflow's step tree transitively, renumbering child steps.

    Walks outbound ``composes`` from the envelope to collect direct step
    children (ordered by ``step_num_parts``).  For each AutoStep child with
    an outbound ``delegates_to`` edge to a ``@task``-decorated function,
    looks up the target's envelope via inbound ``annotates``, recurses with
    a per-call visited-envelope set, and prefixes child ``step_num_parts``
    with the outer AutoStep's parts.

    Args:
        project_root: Absolute path to the indexed project.
        workflow_node_id: Node ID of the envelope (``{…}@workflow``) OR of
            the annotated function.  Both are accepted — the function-ID
            form is resolved to its envelope via inbound ``annotates``.
        graph: An already-loaded :class:`WorkflowGraph` to walk.  Pass one
            when expanding several envelopes so the index is read once
            rather than once per envelope; omit it and the index is read
            for this call alone.

    Returns:
        Ordered list of :class:`ExpandedStep`.  Empty when the envelope is
        unknown or has no step children.  Never raises.
    """
    try:
        if graph is None:
            graph = load_workflow_graph(project_root)
    except Exception:
        return []

    nodes_by_id = graph.nodes_by_id
    composes_out = graph.composes_out

    if not nodes_by_id:
        return []

    # Accept either the envelope ID or the annotated function ID.
    envelope = nodes_by_id.get(workflow_node_id)
    if envelope is None:
        return []
    if envelope.node_type != "composite_process" or envelope.subtype not in ("workflow", "task"):
        # Try to resolve as the annotated function.
        env_id = _envelope_id_for(graph, workflow_node_id)
        if env_id is None:
            return []
        envelope = nodes_by_id.get(env_id)
        if envelope is None:
            return []

    def _recurse(env_id: str, prefix: list[int], chain: list[str], visited: set[str]) -> list[ExpandedStep]:
        out: list[ExpandedStep] = []
        children = composes_out.get(env_id, [])
        # Order by step_num_parts (integer-array comparison).
        child_nodes = [nodes_by_id[c] for c in children if c in nodes_by_id]
        child_nodes = [n for n in child_nodes if n.subtype in ("step", "autostep")]
        child_nodes.sort(key=lambda n: _step_num_parts(n))

        for step in child_nodes:
            own_parts = _step_num_parts(step)
            rendered_parts = list(prefix) + list(own_parts)
            rendered = ".".join(str(p) for p in rendered_parts)
            # Emit the outer step entry.
            if step.subtype == "autostep":
                delegated = step_delegate_target(step.id, graph)
                target_id = delegated.id if delegated else None
                note: str | None = None
                if target_id is None:
                    # AutoStep without a delegates_to target (no call followed).
                    out.append(
                        ExpandedStep(
                            rendered_step_num=rendered,
                            step_node_id=step.id,
                            context_chain=list(chain),
                        )
                    )
                    continue
                inner_env_id = _envelope_id_for(graph, target_id)
                if inner_env_id is None:
                    out.append(
                        ExpandedStep(
                            rendered_step_num=rendered,
                            step_node_id=step.id,
                            context_chain=list(chain),
                            note="target not annotated",
                        )
                    )
                    continue
                if inner_env_id in visited:
                    out.append(
                        ExpandedStep(
                            rendered_step_num=rendered,
                            step_node_id=step.id,
                            context_chain=list(chain),
                            note=f"cycle detected at {inner_env_id}",
                        )
                    )
                    continue
                # Emit the outer AutoStep entry, then recurse.
                out.append(
                    ExpandedStep(
                        rendered_step_num=rendered,
                        step_node_id=step.id,
                        context_chain=list(chain),
                    )
                )
                visited.add(inner_env_id)
                try:
                    out.extend(
                        _recurse(
                            inner_env_id,
                            rendered_parts,
                            chain + [inner_env_id],
                            visited,
                        )
                    )
                finally:
                    visited.discard(inner_env_id)
            else:
                out.append(
                    ExpandedStep(
                        rendered_step_num=rendered,
                        step_node_id=step.id,
                        context_chain=list(chain),
                    )
                )
        return out

    return _recurse(envelope.id, [], [envelope.id], {envelope.id})


# ---------------------------------------------------------------------------
# workflow_detail — index-resident, built on top of workflow_expanded_steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepRow:
    """One step inside a :class:`WorkflowDetail`.

    Attributes:
        step_num: Dotted step number, preserving leading/trailing zero
            significance (``"1.10"`` does not collapse to ``"1.1"``).  For
            transitively-expanded steps this is the renumbered value
            produced by :func:`workflow_expanded_steps`.
        name: Step name.
        purpose: Step purpose.  Empty string when not provided.
        inputs: Step inputs summary.  Empty string when not provided.
        outputs: Step outputs summary.  Empty string when not provided.
        critical: Step criticality note.  Empty string when not provided.
        is_auto: ``True`` for ``AutoStep(...)``, ``False`` for plain
            ``Step(...)``.
        delegates_to_name: Short function name delegated to (from the
            ``delegates_to`` target's node ID) or ``None`` when the step
            calls nothing.
        delegates_to_node_id: Axiom-graph node ID for the callee, or
            ``None`` when the step calls nothing.
        location: Path to the file the step *marker itself* is written
            in, relative to the project root and forward-slash separated.
            Under transitive expansion this is frequently a different
            module from the envelope that was requested, because an
            AutoStep's delegate target declares its own steps.
        line: 1-based line number of the step marker inside
            ``location``.  ``0`` when unknown.  Not interchangeable with
            ``target.line``.
        depth: Outline nesting level, read off ``step_num`` — ``0`` for
            ``"3"``, ``1`` for ``"3.1"``, ``2`` for ``"3.1.1"``.  This is a
            property of the number the author wrote, not of how the step
            was reached: a minor step an envelope declares in its own file
            nests exactly as deep as one pulled in through an AutoStep.
        note: Why expansion stopped at this step, when it did:
            ``"target not annotated"`` or ``"cycle detected at {id}"``.
            ``None`` for an ordinary row.
        target: The resolved delegate target and the intent it declares,
            or ``None`` when the step calls nothing.
    """

    step_num: str
    name: str
    purpose: str
    inputs: str
    outputs: str
    critical: str
    is_auto: bool
    delegates_to_name: str | None
    delegates_to_node_id: str | None
    location: str = ""
    line: int = 0
    depth: int = 0
    note: str | None = None
    target: DelegateTarget | None = None


@dataclass(frozen=True)
class WorkflowDetail:
    """Full structured view of a single axiom-graph workflow/task envelope."""

    name: str
    role: str
    purpose: str
    file: str
    line: int
    node_id: str | None
    inputs: str
    outputs: str
    critical: str
    steps: list[StepRow] = field(default_factory=list)
    coverage_targets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Transition:
    """One outgoing transition from a state-machine state.

    Attributes:
        event: Event name that triggers the transition (e.g.
            ``"RUN_CLICKED"``).  Empty string for ``always`` and ``after``
            transitions, where ``via`` carries the trigger semantics.
        target: Resolved target state's full node ID, or short string when
            unresolved.
        via: Transition source — ``"on"`` for event handlers, ``"always"``
            for eventless transitions, ``"after"`` for delayed transitions,
            ``"invoke.onDone"`` / ``"invoke.onError"`` for invoke callbacks.
        delay: Delay value (as authored, typically a numeric string) when
            ``via == "after"``; empty otherwise.
        internal: ``True`` when the long-form transition object had
            ``internal: true``; ``False`` otherwise.
    """

    event: str
    target: str
    via: str
    delay: str = ""
    internal: bool = False


@dataclass(frozen=True)
class StateRow:
    """One state inside a :class:`StateMachineDetail`.

    Attributes:
        path: Dot-separated path under the machine envelope (e.g.
            ``"idle"`` or ``"running.streaming"``).
        node_id: Axiom-graph node ID of the state.
        purpose: ``meta.purpose`` from the state config.  Empty when absent.
        is_compound: ``True`` when the state has nested ``states``.
        is_terminal: ``True`` when ``type: 'final'``.
        transitions: Outgoing transitions in source order.
    """

    path: str
    node_id: str
    purpose: str
    is_compound: bool
    is_terminal: bool
    transitions: list[Transition] = field(default_factory=list)


@dataclass(frozen=True)
class StateMachineDetail:
    """Full structured view of a single xstate machine envelope.

    Returned by :func:`workflow_detail` when the resolved envelope's
    subtype is ``"state_machine"`` (analogous to :class:`WorkflowDetail`
    for ``"workflow"`` / ``"task"`` envelopes).
    """

    name: str
    role: str  # always "state_machine"
    purpose: str
    file: str
    line: int
    node_id: str | None  # envelope ID; state machines have no annotated function
    states: list[StateRow] = field(default_factory=list)


def _step_sort_key(step_number: str) -> tuple[int, ...]:
    """Parse ``"1.10"`` → ``(1, 10)`` so sub-steps order naturally."""
    try:
        return tuple(int(p) for p in step_number.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _resolve_envelope_by_name(nodes_by_id, name: str):
    """Best-effort resolution of an envelope by short name.

    Prefers exact match on the enclosing function's short name for
    ``workflow``/``task`` envelopes.  For ``state_machine`` envelopes, the
    envelope id has the shape ``{module}::{machine_name}@machine`` — the
    short name is the trailing segment with ``@machine`` stripped.
    """
    for n in nodes_by_id.values():
        if n.node_type != "composite_process":
            continue
        if n.subtype not in ("workflow", "task", "state_machine"):
            continue
        last = n.id.rsplit("::", 1)[-1]
        if last.endswith("@workflow"):
            candidate = last[: -len("@workflow")].split(".")[-1]
            if candidate == name:
                return n
        elif last.endswith("@machine"):
            candidate = last[: -len("@machine")].split(".")[-1]
            if candidate == name:
                return n
    return None


def _build_state_machine_detail(
    project_root_str: str,
    envelope,
    nodes_by_id: dict,
    composes_out: dict[str, list[str]],
) -> "StateMachineDetail":
    """Render a state-machine envelope as :class:`StateMachineDetail`.

    Recursively walks ``composes`` from the envelope to collect every
    descendant state node, gathers its ``delegates_to`` outgoing edges from
    the DB (with full meta), and emits one :class:`StateRow` per state
    sorted by path.
    """
    # Pull all delegates_to edges for our state nodes (with meta) from the DB.
    ag_db = _db_path(project_root_str)
    all_edges_list = db.all_edges(ag_db) if ag_db.exists() else []
    transitions_by_from: dict[str, list[Transition]] = {}
    for e in all_edges_list:
        if e.edge_type != "delegates_to":
            continue
        m = e.meta if isinstance(e.meta, dict) else {}
        # Only include edges that look like transitions: must carry "via".
        via = str(m.get("via") or "")
        if not via:
            continue
        transitions_by_from.setdefault(e.from_id, []).append(
            Transition(
                event=str(m.get("event") or ""),
                target=e.to_id,
                via=via,
                delay=str(m.get("delay") or ""),
                internal=bool(m.get("internal", False)),
            )
        )

    # Recursively collect descendant state nodes.
    state_rows: list[StateRow] = []

    def _walk(parent_id: str, parent_path: str) -> None:
        children = composes_out.get(parent_id, [])
        for child_id in children:
            child = nodes_by_id.get(child_id)
            if child is None or child.subtype != "state":
                continue
            # The state's path is its trailing segment after envelope_id +
            # ".states.".  Use the level_0 / authored short name when present
            # in dflow_meta; fall back to the node id tail.
            state_meta = child.dflow_meta if isinstance(child.dflow_meta, dict) else {}
            # Path under the envelope: stored as full {envelope_id}.states.{path}.
            short_path = ""
            if state_meta.get("xstate_path"):
                short_path = str(state_meta.get("xstate_path"))
            else:
                tail = child_id.rsplit("::", 1)[-1]
                # Strip the envelope's local suffix; envelope_id ends with @machine
                short_path = tail
            purpose = ""
            if state_meta.get("purpose"):
                purpose = str(state_meta.get("purpose"))
            elif child.level_2:
                purpose = str(child.level_2)
            is_compound = child.node_type == "composite_process"
            is_terminal = bool(state_meta.get("terminal", False)) or "final" in (child.tags or [])

            state_rows.append(
                StateRow(
                    path=short_path,
                    node_id=child.id,
                    purpose=purpose,
                    is_compound=is_compound,
                    is_terminal=is_terminal,
                    transitions=list(transitions_by_from.get(child.id, [])),
                )
            )
            if is_compound:
                _walk(child.id, short_path)

    _walk(envelope.id, "")

    state_rows.sort(key=lambda r: r.path)

    # Header bookkeeping.
    meta = envelope.dflow_meta if isinstance(envelope.dflow_meta, dict) else {}
    purpose = str(meta.get("purpose") or "")
    file_path = (envelope.location or "").replace("\\", "/")
    line = _func_line_from_level_3(envelope.level_3_location)

    env_last = envelope.id.rsplit("::", 1)[-1]
    if env_last.endswith("@machine"):
        env_last = env_last[: -len("@machine")]
    machine_name = env_last.split(".")[-1]

    return StateMachineDetail(
        name=machine_name,
        role="state_machine",
        purpose=purpose,
        file=file_path,
        line=line,
        node_id=envelope.id,
        states=state_rows,
    )


def _step_row(expanded: ExpandedStep, step_node, graph: WorkflowGraph) -> StepRow:
    """Build one :class:`StepRow` from an expander entry and its node.

    The step's own file and line come from the marker node; the delegate
    target and the intent it declares come from
    :func:`step_delegate_target`, which is the project's single
    resolution path.  An ``AutoStep`` carries no intent of its own — the
    marker has nowhere to put it — so the target's ``purpose`` /
    ``inputs`` / ``outputs`` / ``critical`` fill any field the step left
    blank.  An authored value always wins over an inherited one.

    Args:
        expanded: The expander entry naming the step and its position.
        step_node: The indexed ``step`` / ``autostep`` node.
        graph: An already-loaded :class:`WorkflowGraph`.

    Returns:
        The populated :class:`StepRow`.
    """
    s_meta = step_node.dflow_meta if isinstance(step_node.dflow_meta, dict) else {}
    is_auto = step_node.subtype == "autostep"
    target = step_delegate_target(step_node.id, graph)

    purpose = str(s_meta.get("purpose") or "")
    inputs = str(s_meta.get("inputs") or "")
    outputs = str(s_meta.get("outputs") or "")
    critical = str(s_meta.get("critical") or "")
    name = str(s_meta.get("name") or step_node.level_0 or "")
    if is_auto and target is not None:
        purpose = purpose or target.purpose
        inputs = inputs or target.inputs
        outputs = outputs or target.outputs
        critical = critical or target.critical
        name = name or target.name

    return StepRow(
        step_num=expanded.rendered_step_num,
        name=name,
        purpose=purpose,
        inputs=inputs,
        outputs=outputs,
        critical=critical,
        is_auto=is_auto,
        delegates_to_name=target.id.rsplit("::", 1)[-1] if target else None,
        delegates_to_node_id=target.id if target else None,
        location=(step_node.location or "").replace("\\", "/"),
        line=_func_line_from_level_3(step_node.level_3_location),
        depth=expanded.rendered_step_num.count("."),
        note=expanded.note,
        target=target,
    )


def workflow_detail(
    project_root: str | Path,
    workflow_id: str | int,
    *,
    graph: WorkflowGraph | None = None,
) -> "WorkflowDetail | StateMachineDetail | None":
    """Fetch one axiom-graph envelope as a structured detail dataclass.

    ``workflow_id`` may be either an axiom-graph node ID (the envelope's
    ``{…}@workflow`` ID or the annotated function's ID) or a plain function
    name like ``"run_pipeline"``.  Integer-shaped strings are treated as
    names (the legacy integer-ID lookup is gone; the axiom-graph
    index has no counterpart).

    Args:
        project_root: Path to the indexed project.
        workflow_id: Envelope node ID, annotated-function node ID, or
            plain function name.
        graph: An already-loaded :class:`WorkflowGraph`.  Pass one when
            describing several envelopes so the index is read once rather
            than once per envelope.

    Returns:
        - :class:`WorkflowDetail` for ``workflow``/``task`` envelopes.
        - :class:`StateMachineDetail` for ``state_machine`` envelopes
          (xstate v5 machines).
        - ``None`` when no matching envelope exists.
    """
    project_root_str = str(project_root)
    if graph is None:
        graph = load_workflow_graph(project_root_str)
    nodes_by_id = graph.nodes_by_id
    composes_out = graph.composes_out
    annotates_rev = graph.annotates_rev
    if not nodes_by_id:
        return None

    lookup_key = str(workflow_id)
    envelope = nodes_by_id.get(lookup_key)
    if (
        envelope is not None
        and envelope.node_type == "composite_process"
        and envelope.subtype in ("workflow", "task", "state_machine")
    ):
        pass
    else:
        # Try: key is the annotated function ID.
        env_id = _envelope_id_for(graph, lookup_key)
        envelope = nodes_by_id.get(env_id) if env_id else None
        if envelope is None:
            # Fall back to name resolution.
            envelope = _resolve_envelope_by_name(nodes_by_id, lookup_key)
            if envelope is None:
                return None

    # State machine path: build a StateMachineDetail.
    if envelope.subtype == "state_machine":
        return _build_state_machine_detail(project_root_str, envelope, nodes_by_id, composes_out)

    # Find annotated function via an annotates edge out of the envelope.
    annotated_func_id: str | None = None
    for tgt, sources in annotates_rev.items():
        if envelope.id in sources:
            annotated_func_id = tgt
            break
    func_node = nodes_by_id.get(annotated_func_id) if annotated_func_id else None

    meta = envelope.dflow_meta if isinstance(envelope.dflow_meta, dict) else {}
    purpose = str(meta.get("purpose") or "")
    inputs = str(meta.get("inputs") or "")
    outputs = str(meta.get("outputs") or "")
    critical = str(meta.get("critical") or "")

    file_path = (func_node.location if func_node else envelope.location) or ""
    file_path = file_path.replace("\\", "/")
    line = _func_line_from_level_3(func_node.level_3_location if func_node else envelope.level_3_location)

    # Build expanded step rows, reusing the graph this call already loaded.
    exp = workflow_expanded_steps(project_root_str, envelope.id, graph=graph)
    step_rows: list[StepRow] = []
    for e in exp:
        step_node = nodes_by_id.get(e.step_node_id)
        if step_node is None:
            continue
        step_rows.append(_step_row(e, step_node, graph))

    # Derive the displayed function name.
    func_display_name = parse_node_title(func_node).last if func_node else parse_node_title(envelope).last

    return WorkflowDetail(
        name=func_display_name,
        role=envelope.subtype or "",
        purpose=purpose,
        file=file_path,
        line=line,
        node_id=annotated_func_id,
        inputs=inputs,
        outputs=outputs,
        critical=critical,
        steps=step_rows,
        coverage_targets=[],
    )


# ---------------------------------------------------------------------------
# Serialization + export bundle
# ---------------------------------------------------------------------------


def workflow_detail_to_dict(detail: "WorkflowDetail | StateMachineDetail") -> dict:
    """Serialize one envelope's structured detail to JSON-ready data.

    This is the only serializer for a workflow's structured form.  The
    export bundle's per-workflow entries and the MCP tool's JSON output
    both come through here, so the two surfaces cannot describe the same
    workflow differently.  Nested rows (:class:`StepRow`,
    :class:`DelegateTarget`, :class:`StateRow`, :class:`Transition`) are
    emitted whole — a projection down to a narrower key set would be a
    second serializer.

    Args:
        detail: The dataclass returned by :func:`workflow_detail`.

    Returns:
        A nested ``dict`` / ``list`` structure safe to hand to
        ``json.dumps``.
    """
    return asdict(detail)


@dataclass(frozen=True)
class WorkflowBundle:
    """A self-contained description of a set of workflows and their code.

    Attributes:
        workflows: One detail entry per selected envelope, in the order
            they were requested.
        sources: Project-relative file path to that file's full text.
            Deduplicated — a file shared by several workflows appears
            once.
    """

    workflows: list = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


def _bundle_source_paths(details: list) -> list[str]:
    """Every project-relative file a set of workflow details refers to.

    The union covers three kinds of file, because a reader clicking
    through the bundle can reach all three: the file each workflow is
    defined in, the file each step marker is written in (often another
    module, once expansion walks into a delegate target), and the file
    each delegate target is defined in.  Target files are included even
    when the target's own workflow was never selected, or the
    click-through dead-ends.

    Args:
        details: The workflow detail entries going into a bundle.

    Returns:
        Sorted, deduplicated project-relative paths.
    """
    paths: set[str] = set()
    for detail in details:
        if getattr(detail, "file", ""):
            paths.add(detail.file)
        for step in getattr(detail, "steps", []):
            if step.location:
                paths.add(step.location)
            if step.target is not None and step.target.location:
                paths.add(step.target.location)
    return sorted(paths)


def workflow_export_bundle(
    project_root: str | Path,
    workflow_ids: list[str],
) -> WorkflowBundle:
    """Assemble the selected workflows and every source file they reach.

    The index is read once and reused for every selected envelope, so
    exporting forty workflows costs one graph read plus the forty
    envelopes rather than forty graph reads.

    Args:
        project_root: Path to the indexed project.
        workflow_ids: Envelope node IDs, annotated-function node IDs, or
            plain function names.  Unknown IDs are skipped rather than
            raising; repeated IDs are collapsed.

    Returns:
        A :class:`WorkflowBundle`.  Files that cannot be read are omitted
        from ``sources`` rather than failing the whole export.
    """
    root = Path(str(project_root))
    graph = load_workflow_graph(root)

    details: list = []
    seen: set = set()
    for workflow_id in workflow_ids:
        detail = workflow_detail(root, workflow_id, graph=graph)
        if detail is None:
            continue
        identity = (detail.name, detail.file, detail.line)
        if identity in seen:
            continue
        seen.add(identity)
        details.append(detail)

    sources: dict[str, str] = {}
    for rel_path in _bundle_source_paths(details):
        candidate = root / rel_path
        try:
            sources[rel_path] = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    return WorkflowBundle(workflows=details, sources=sources)


def workflow_bundle_to_dict(bundle: WorkflowBundle) -> dict:
    """Serialize a :class:`WorkflowBundle` to JSON-ready data.

    Per-workflow entries go through :func:`workflow_detail_to_dict`, the
    same serializer the MCP tool's JSON mode uses, so a workflow reads
    identically in both places.

    Args:
        bundle: The bundle to serialize.

    Returns:
        ``{"workflows": [...], "sources": {path: text}}``.
    """
    return {
        "workflows": [workflow_detail_to_dict(detail) for detail in bundle.workflows],
        "sources": dict(bundle.sources),
    }
