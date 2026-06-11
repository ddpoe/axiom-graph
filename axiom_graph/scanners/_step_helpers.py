"""Language-agnostic step / envelope extraction helpers.

Shared between ``module_scanner`` (Python AST) and ``js_scanner``
(tree-sitter JS/TS), with a third consumer (``xstate_scanner``) on the
way.  Every helper here operates on **already-extracted, normalized
data** -- strings, dicts, ints -- so AST-specific traversal stays in
each language scanner.

Helpers split into three groups:

1. String / kwargs utilities: ``camel_to_snake``, ``envelope_code_hash``,
   ``parse_step_num``.
2. Identifier / level builders: ``envelope_id_for``, ``step_id_for``,
   ``envelope_subtype``, ``build_envelope_levels``,
   ``build_step_levels``, ``build_step_meta``.
3. Node builders + name-map resolver: ``build_envelope_node``,
   ``build_step_node``, ``resolve_call_target_via_name_map``.

Group 3 takes a ``source`` string ("ast" or "tree_sitter") so the same
helper produces nodes attributable to either scanner.

Generic primitives like ``hash16`` (content-addressing) and ``make_edge``
(``AxiomEdge`` factory) live in :mod:`axiom_graph.models`, not here --
they're not step-specific.  Anything in this module that needs them
imports them from there.
"""

from __future__ import annotations

import re

from axiom_graph.models import AxiomNode, AxiomEdge, hash16, make_edge

# ---------------------------------------------------------------------------
# String / kwargs utilities
# ---------------------------------------------------------------------------


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def camel_to_snake(name: str) -> str:
    """Convert a camelCase identifier to snake_case (``stepNum`` -> ``step_num``)."""
    return _CAMEL_RE.sub("_", name).lower()


# ---------------------------------------------------------------------------
# Hash / parse helpers
# ---------------------------------------------------------------------------

# Canonical kwarg list for envelope ``code_hash`` — must stay in sync across
# every scanner that emits envelope nodes, otherwise JS- and Python-derived
# envelopes for the same kwargs would hash differently.
_ENVELOPE_HASH_KWARGS = ("critical", "inputs", "outputs", "purpose")


def envelope_code_hash(decorator_name: str, kwargs: dict) -> str:
    """Return a deterministic short hash for an envelope's kwargs.

    Args:
        decorator_name: ``"workflow"`` or ``"task"`` (reserved for future
            per-decorator kwarg sets; both currently share the same list).
        kwargs: Decorator kwarg dict (e.g. ``{"purpose": "...", "inputs": "..."}``).

    Returns:
        Short SHA-256 hex digest (16 chars).
    """
    parts: list[str] = []
    for key in _ENVELOPE_HASH_KWARGS:
        val = kwargs.get(key, "")
        parts.append(f"{key}={val!r}")
    serialized = "|".join(parts)
    return hash16(serialized)


def parse_step_num(value: object) -> tuple[str, list[int]]:
    """Return ``(step_num_raw, step_num_parts)`` for a step_num value.

    ``step_num_raw`` preserves the authored literal as a string;
    ``step_num_parts`` is the integer array used for sort stability and
    expansion-renderer prefix arithmetic.

    Args:
        value: int, float, or str (already extracted from the source AST).

    Returns:
        Tuple of (raw_string, parts_list).  On non-integer parts in the
        string form, returns ``(raw, [0])`` so callers can still produce a
        deterministic id while signalling malformed input.
    """
    if isinstance(value, int):
        return (str(value), [value])
    if isinstance(value, float):
        # Float input loses trailing zeros (e.g. ``1.10`` becomes 1.1).
        # Callers that need to preserve the authored literal should pass a
        # string read directly from source text instead.
        raw = f"{value:g}"
        parts: list[int] = []
        for p in raw.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                return (raw, [0])
        return (raw, parts)
    raw = str(value)
    parts = []
    for p in raw.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            return (raw, [0])
    return (raw, parts)


# ---------------------------------------------------------------------------
# Identifier / level builders
# ---------------------------------------------------------------------------


def envelope_id_for(func_id: str) -> str:
    """Return the envelope node id for a wrapped function."""
    return f"{func_id}@workflow"


def step_id_for(func_id: str, step_num_raw: str) -> str:
    """Return the step node id for a step inside a wrapped function."""
    return f"{func_id}::step-{step_num_raw}"


def envelope_subtype(decorator_name: str) -> str:
    """Return ``"task"`` or ``"workflow"`` for an envelope's subtype field."""
    return "task" if decorator_name == "task" else "workflow"


def build_envelope_levels(
    func_name: str,
    decorator_name: str,
    purpose: str | None,
) -> tuple[str, str]:
    """Return (level_0, level_1) for an envelope node.

    level_0 is the title shown in listings; level_1 is the one-line
    description with the purpose appended when present.
    """
    level_0 = f"{func_name} @{decorator_name}"
    level_1 = f"{decorator_name} envelope for {func_name}"
    if purpose:
        level_1 = f"{level_1} — {purpose}"
    return level_0, level_1


def build_step_levels(
    step_num_raw: str,
    name: str | None,
    purpose: str | None,
    is_auto: bool,
) -> tuple[str, str]:
    """Return (level_0, level_1) for a step / autostep node.

    level_0 is the step name when present, else a synthetic
    ``Step <num>`` / ``AutoStep <num>`` title.  level_1 chains step number,
    name, and purpose with em-dash separators.
    """
    level_0 = str(name) if name else (f"AutoStep {step_num_raw}" if is_auto else f"Step {step_num_raw}")
    level_1_parts = [f"step {step_num_raw}"]
    if name:
        level_1_parts.append(str(name))
    if purpose:
        level_1_parts.append(str(purpose))
    level_1 = " — ".join(level_1_parts)
    return level_0, level_1


def build_step_meta(
    step_num_raw: str,
    step_num_parts: list[int],
    name: object,
    purpose: object,
    kwargs: dict,
    is_auto: bool,
) -> dict:
    """Return the ``dflow_meta`` dict for a step / autostep node.

    Mirrors the per-field shape both scanners emit today: ``inputs``,
    ``outputs``, and ``critical`` are coerced via ``str(...)`` only when
    present in *kwargs* (else ``None``).
    """
    return {
        "step_num_raw": step_num_raw,
        "step_num_parts": step_num_parts,
        "name": str(name) if name else None,
        "purpose": str(purpose) if purpose else None,
        "inputs": str(kwargs["inputs"]) if "inputs" in kwargs else None,
        "outputs": str(kwargs["outputs"]) if "outputs" in kwargs else None,
        "critical": str(kwargs["critical"]) if "critical" in kwargs else None,
        "subtype": "autostep" if is_auto else "step",
    }


# ---------------------------------------------------------------------------
# Node builders
# ---------------------------------------------------------------------------


def build_envelope_node(
    *,
    source: str,
    func_id: str,
    func_name: str,
    decorator_name: str,
    kwargs: dict,
    rel_path: str,
    module_id: str,
    start_line: int,
    test_runner: str | None = None,
    test_name: str | None = None,
) -> tuple[AxiomNode, list[AxiomEdge], str]:
    """Construct an envelope ``composite_process`` node + composes/annotates edges.

    Args:
        source: Scanner source attribution -- ``"ast"`` (Python) or
            ``"tree_sitter"`` (JS/TS).
        func_id: Node ID of the wrapped function.
        func_name: Short name of the wrapped function (for level_0/level_1).
        decorator_name: ``"workflow"`` or ``"task"``.
        kwargs: Decorator kwargs already coerced to native Python types.
        rel_path: Repo-relative file path.
        module_id: Module node ID (source of the ``composes`` edge).
        start_line: 1-based source line of the decorator / wrapper.
        test_runner: ``"test"`` or ``"it"`` when the envelope was discovered
            via a test-runner call wrapping the workflow.  When set, the
            ``"test"`` tag is added and ``test_runner`` / ``test_name`` are
            recorded in ``dflow_meta``.  ``None`` for ordinary const-form
            envelopes.
        test_name: The test-name string literal that wrapped the envelope.
            Only used when ``test_runner`` is set.

    Returns:
        Tuple ``(envelope_node, edges, envelope_id)``.
    """
    envelope_id = envelope_id_for(func_id)
    subtype = envelope_subtype(decorator_name)
    code_hash = envelope_code_hash(decorator_name, kwargs)

    purpose = kwargs.get("purpose", "")
    # For test-wrapped envelopes, prefer the human-readable test name over the
    # synthetic ``test::slug`` identifier when building display levels.
    display_name = test_name if test_runner is not None and test_name else func_name
    level_0, level_1 = build_envelope_levels(display_name, decorator_name, purpose)
    level_3 = f"{rel_path}#L{start_line}"

    dflow_meta = dict(kwargs)
    tags = [subtype, "envelope"]
    if test_runner is not None:
        dflow_meta["test_runner"] = test_runner
        if test_name is not None:
            dflow_meta["test_name"] = test_name
        tags.append("test")

    envelope = AxiomNode(
        id=envelope_id,
        node_type="composite_process",
        subtype=subtype,
        title=level_0,
        location=rel_path,
        source=source,
        code_hash=code_hash,
        desc_hash=None,
        level_0=level_0,
        level_1=level_1,
        level_2=purpose if purpose else None,
        level_3_location=level_3,
        dflow_meta=dflow_meta,
        tags=tags,
    )
    edges = [
        make_edge("composes", module_id, envelope_id),
        make_edge("annotates", envelope_id, func_id),
    ]
    return envelope, edges, envelope_id


def build_step_node(
    *,
    source: str,
    step_id: str,
    is_auto: bool,
    level_0: str,
    level_1: str,
    purpose: str | None,
    rel_path: str,
    line: int,
    step_meta: dict,
) -> AxiomNode:
    """Construct a step / autostep ``atomic_process`` node.

    Step nodes carry no staleness dimensions — the empty-string
    ``code_hash`` is a sentinel; ``compute_staleness`` short-circuits on
    subtype ``step`` / ``autostep``.
    """
    subtype = "autostep" if is_auto else "step"
    return AxiomNode(
        id=step_id,
        node_type="atomic_process",
        subtype=subtype,
        title=level_0,
        location=rel_path,
        source=source,
        code_hash="",
        desc_hash=None,
        level_0=level_0,
        level_1=level_1,
        level_2=str(purpose) if purpose else None,
        level_3_location=f"{rel_path}#L{line}",
        dflow_meta=step_meta,
        tags=["step"] if not is_auto else ["autostep"],
    )


# ---------------------------------------------------------------------------
# Name-map resolver
# ---------------------------------------------------------------------------


def resolve_call_target_via_name_map(
    callee: dict,
    name_map: dict[str, tuple[str, str | None]],
    local_func_ids: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a normalized callee descriptor to an axiom-graph node id.

    Each scanner extracts an AST-specific call expression and normalizes it
    into a *callee descriptor* dict, which this helper looks up against the
    scanner's name_map (and optionally a local-function id map).  Keeps the
    AST traversal in each scanner while sharing the resolution logic.

    Callee descriptor shapes:

    - ``{"kind": "name", "name": "<identifier>"}`` — bare identifier call,
      e.g. ``foo(...)``.
    - ``{"kind": "attr", "root": "<identifier>", "attr": "<property>"}`` —
      single-level member access, e.g. ``mod.foo(...)``.

    Resolution order:

    1. Plain ``name`` in name_map: returns ``f"{module_id}::{original}"``
       when ``original`` is set (named import); ``(None, name)`` for
       namespace / default bindings (caller decides whether to emit edge).
    2. Plain ``name`` in local_func_ids: returns the local function id.
    3. ``attr`` form with ``root`` matching a *namespace* binding in
       name_map (``original is None``): returns ``f"{module_id}::{attr}"``.
    4. ``attr`` form with ``root`` matching a *named* import: in v1, no
       resolution -- caller's responsibility, returns ``(None, attr)``.
    5. Anything else: ``(None, None)`` or ``(None, attr_or_name)`` so
       caller can populate B4 records with the unresolved short name.

    Args:
        callee: Descriptor dict produced by the calling scanner.
        name_map: Imports binding ``{bound_name: (module_id, original_name | None)}``.
        local_func_ids: Optional ``{func_name: node_id}`` of local same-file
            functions (used by JS scanner for in-file fallback).

    Returns:
        ``(target_id, target_short_name)``.  ``target_id`` is the resolved
        node id or None; ``target_short_name`` is the function's short name
        (or None when the descriptor is empty), useful for B4 reporting.
    """
    kind = callee.get("kind")
    if kind == "name":
        bound = callee.get("name")
        if bound is None:
            return (None, None)
        if bound in name_map:
            mod_id, original = name_map[bound]
            if original is not None:
                return (f"{mod_id}::{original}", original)
            # Namespace / default binding -- bare call gives no edge.
            return (None, bound)
        if local_func_ids is not None and bound in local_func_ids:
            return (local_func_ids[bound], bound)
        return (None, bound)

    if kind == "attr":
        root = callee.get("root")
        attr = callee.get("attr")
        if attr is None:
            return (None, None)
        if root is None:
            return (None, attr)
        if root in name_map:
            mod_id, original = name_map[root]
            if original is None:
                # Namespace import: X.foo() -> {module_id}::foo.
                return (f"{mod_id}::{attr}", attr)
            # Named-import-member: deferred in v1.
            return (None, attr)
        return (None, attr)

    return (None, None)
