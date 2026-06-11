"""xstate v5 state-machine scanner — tree-sitter AST to AxiomNode + AxiomEdge.

Entry point:
    scan_xstate_module(file_path, project_root, project_id, ...)
        -> tuple[list[AxiomNode], list[AxiomEdge]]

Reads xstate v5 declarative state machines: ``createMachine({...})`` and
``setup({...}).createMachine({...})``.  Produces:

* One ``composite_process`` envelope per machine (``subtype="state_machine"``).
* One node per state — ``atomic_process`` for simple states,
  ``composite_process`` for compound states (both ``subtype="state"``).
* ``composes`` edges for the state hierarchy.
* ``delegates_to`` edges for ``on`` / ``always`` / ``after`` transitions
  and ``invoke`` actor calls, with ``meta`` carrying ``event`` / ``via`` /
  ``delay`` / ``internal``.

Coexists with :mod:`axiom_graph.scanners.js_scanner` — both scanners run
on the same file independently.  No interference: this scanner emits
only state-machine and state nodes, never module / function / step nodes.

Strict-literal contract: every read expects an inline literal (object,
string, identifier).  Mismatch (variable, function call, spread)
emits a ``ValidationFinding`` with severity ``IMPORTANT`` to
``findings_out`` and skips the offending subtree.  Other state machines
in the same file remain scannable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from axiom_graph.models import AxiomEdge, AxiomNode, hash16, make_edge

try:
    import tree_sitter_javascript as _tsjs  # noqa: F401  (presence check)
    import tree_sitter_typescript as _tsts  # noqa: F401
    from tree_sitter import Parser

    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False

# Reuse helpers from js_scanner so we share import resolution and literal parsing.
from axiom_graph.scanners.js_scanner import (
    _extract_imports,
    _extract_object_literal_pairs,
    _get_language,
    _node_text,
    _rel_path_to_dotpath,
    _string_literal_value,
)


# ---------------------------------------------------------------------------
# Strict-literal finding helper
# ---------------------------------------------------------------------------


def _emit_strict_finding(
    findings_out: list | None,
    rel_path: str,
    machine_name: str,
    line: int,
    message: str,
) -> None:
    """Append an IMPORTANT-severity ValidationFinding to *findings_out*.

    Reuses :class:`axiom_graph.workflows.validation.ValidationFinding` so
    Reviewer-side aggregation is identical to the js_scanner path.
    """
    if findings_out is None:
        return
    from axiom_graph.workflows.validation import (
        SEVERITY_IMPORTANT,
        ValidationFinding,
    )

    findings_out.append(
        ValidationFinding(
            rule_id="X1",
            severity=SEVERITY_IMPORTANT,
            module=rel_path,
            function=machine_name,
            line=line,
            message=message,
        )
    )


# ---------------------------------------------------------------------------
# Identifier / literal walkers
# ---------------------------------------------------------------------------


def _identifier_text(node: Any, source: bytes) -> str | None:
    """Return the text of an ``identifier`` node, else None."""
    if node.type == "identifier":
        return _node_text(node, source)
    return None


def _walk_call_chain(node: Any) -> list[Any]:
    """Return ``[func, arg]`` for ``func(arg)``, else []."""
    if node.type != "call_expression":
        return []
    fn_node = None
    args_node = None
    for c in node.children:
        if c.type in ("identifier", "member_expression", "call_expression"):
            fn_node = c
        elif c.type == "arguments":
            args_node = c
    return [fn_node, args_node] if fn_node is not None else []


def _first_arg_object(call_node: Any) -> Any | None:
    """Return the first ``arguments`` child if it's an object literal, else None."""
    for c in call_node.children:
        if c.type == "arguments":
            for ac in c.children:
                if ac.type == "object":
                    return ac
                if ac.type in (",", "(", ")"):
                    continue
                # First argument is non-object → strict-literal violation
                return None
            return None
    return None


def _is_create_machine_call(node: Any, source: bytes) -> bool:
    """Return True if *node* is ``createMachine(...)`` (bare identifier)."""
    if node.type != "call_expression":
        return False
    for c in node.children:
        if c.type == "identifier":
            return _node_text(c, source) == "createMachine"
        # member_expression case (e.g. setup(...).createMachine(...))
        if c.type == "member_expression":
            # Right-hand property name
            for mc in c.children:
                if mc.type == "property_identifier" and _node_text(mc, source) == "createMachine":
                    return True
            return False
    return False


def _is_setup_create_machine(node: Any, source: bytes) -> tuple[bool, Any | None]:
    """Detect ``setup({...}).createMachine({...})``.

    Returns ``(matched, setup_call_node | None)``.  The setup call node is
    needed to extract the actors / actions / guards map for invoke
    resolution.
    """
    if node.type != "call_expression":
        return (False, None)
    # Must be a call on a member_expression whose property is createMachine.
    fn = None
    for c in node.children:
        if c.type == "member_expression":
            fn = c
            break
    if fn is None:
        return (False, None)
    # Member expression: <call_expression>.createMachine
    setup_call = None
    is_create = False
    for c in fn.children:
        if c.type == "call_expression":
            # Check if that's setup(...)
            for cc in c.children:
                if cc.type == "identifier" and _node_text(cc, source) == "setup":
                    setup_call = c
                    break
        elif c.type == "property_identifier" and _node_text(c, source) == "createMachine":
            is_create = True
    if is_create and setup_call is not None:
        return (True, setup_call)
    return (False, None)


# ---------------------------------------------------------------------------
# Setup actors map extraction
# ---------------------------------------------------------------------------


def _extract_setup_actors(setup_call: Any, source: bytes) -> dict[str, str]:
    """Extract ``actors`` map from a ``setup({actors: {...}})`` call.

    Returns ``{actor_name: identifier_name}`` where identifier_name is
    the bare identifier passed to ``fromPromise(identifier)`` /
    ``fromCallback(identifier)`` / etc., or directly when the actor value
    is a bare identifier.

    Strict-literal contract: only top-level identifier or bare
    ``fromPromise(identifier)`` calls are captured; arrow-functions / other
    call shapes are skipped silently (caller will surface unresolvable
    invokes via the standard finding path).
    """
    out: dict[str, str] = {}
    obj = _first_arg_object(setup_call)
    if obj is None:
        return out
    for key, value in _extract_object_literal_pairs(obj, source):
        if key != "actors":
            continue
        if value.type != "object":
            continue
        for actor_name, actor_value in _extract_object_literal_pairs(value, source):
            ident: str | None = None
            if actor_value.type == "identifier":
                ident = _node_text(actor_value, source)
            elif actor_value.type == "call_expression":
                # fromPromise(impl) / fromCallback(impl) / etc.
                args = None
                for c in actor_value.children:
                    if c.type == "arguments":
                        args = c
                        break
                if args is not None:
                    for ac in args.children:
                        if ac.type == "identifier":
                            ident = _node_text(ac, source)
                            break
            if ident:
                out[actor_name] = ident
    return out


# ---------------------------------------------------------------------------
# Machine name detection (id field or const-binding fallback)
# ---------------------------------------------------------------------------


def _extract_machine_id(machine_obj: Any, source: bytes) -> str | None:
    """Return the value of the ``id`` field if present and a string literal."""
    for key, value in _extract_object_literal_pairs(machine_obj, source):
        if key == "id":
            return _string_literal_value(value, source)
    return None


def _binding_name_for_call(call_node: Any, source: bytes) -> str | None:
    """If *call_node* is the RHS of a ``const X = createMachine(...)``, return X.

    Walks up to the nearest variable_declarator and returns its identifier
    text.  Returns None when *call_node* is not bound.
    """
    cur = call_node.parent
    while cur is not None:
        if cur.type == "variable_declarator":
            for c in cur.children:
                if c.type == "identifier":
                    return _node_text(c, source)
            return None
        cur = cur.parent
    return None


# ---------------------------------------------------------------------------
# Transition target resolution
# ---------------------------------------------------------------------------


def _resolve_state_target(
    target: str,
    parent_path: str,
    sibling_paths: set[str],
    all_paths: set[str],
    machine_short_id: str,
) -> str | None:
    """Resolve a transition target string against a state path index.

    Args:
        target: The authored target string (``"foo"``, ``"#machine.a.b"``,
            ``"a.b"``, ``".sibling"``).
        parent_path: Dot-path of the enclosing state ("" at machine root).
        sibling_paths: Set of bare state names under the same parent
            (e.g. ``{"a", "b"}``, not dot-paths).
        all_paths: Set of every state's dot-path under the machine.
        machine_short_id: The machine's authored id (or const-binding name).

    Returns:
        Resolved dot-path under the machine, or None when unresolvable.
    """
    if not target:
        return None
    # Absolute dot-path: "#machine.a.b"
    if target.startswith("#"):
        body = target[1:]
        # Allow either "machineId.foo" or just "foo" with leading "#".
        head, _, rest = body.partition(".")
        if head == machine_short_id:
            candidate = rest
        else:
            # Non-matching machine prefix — try the body as-is.
            candidate = body
        if candidate in all_paths:
            return candidate
        return None
    # Sibling: bare name in the same parent.
    if target in sibling_paths:
        return f"{parent_path}.{target}" if parent_path else target
    # Dot-path: try as absolute under the machine.
    if target in all_paths:
        return target
    # Try parent-relative dot-path.
    if parent_path:
        candidate = f"{parent_path}.{target}"
        if candidate in all_paths:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Transition extractors
# ---------------------------------------------------------------------------


def _extract_transition_target(value: Any, source: bytes) -> tuple[str | None, bool]:
    """Pull the authored target string + ``internal`` flag from a transition value.

    Long-form: ``{target: 'foo', internal: true}``.
    Short-form: ``'foo'`` (string) or array of strings (we take the first).

    Returns ``(target_string, internal_flag)`` or ``(None, False)`` on
    strict-literal violation.
    """
    if value.type == "string":
        return (_string_literal_value(value, source), False)
    if value.type == "object":
        target_str: str | None = None
        internal = False
        for k, v in _extract_object_literal_pairs(value, source):
            if k == "target":
                if v.type == "string":
                    target_str = _string_literal_value(v, source)
                elif v.type == "array":
                    # Take the first string element.
                    for ac in v.children:
                        if ac.type == "string":
                            target_str = _string_literal_value(ac, source)
                            break
            elif k == "internal":
                if v.type == "true":
                    internal = True
                elif v.type == "false":
                    internal = False
        return (target_str, internal)
    if value.type == "array":
        for ac in value.children:
            if ac.type == "string":
                return (_string_literal_value(ac, source), False)
            if ac.type == "object":
                return _extract_transition_target(ac, source)
    return (None, False)


# ---------------------------------------------------------------------------
# Recursive state walk
# ---------------------------------------------------------------------------


def _collect_state_paths(machine_obj: Any, source: bytes) -> dict[str, dict]:
    """Walk the state tree and return {dot_path: {node info}}.

    Each entry carries:
      - ``raw_node``: tree-sitter object node for the state config
      - ``parent_path``: dot-path of parent (empty for top-level)
      - ``children_paths``: list of immediate child dot-paths
      - ``is_compound``: bool
      - ``is_terminal``: bool
      - ``meta_purpose``: optional purpose string
      - ``line``: 1-based line of the state config
    """
    out: dict[str, dict] = {}

    def _walk(states_obj: Any, parent_path: str) -> list[str]:
        if states_obj.type != "object":
            return []
        sibling_paths: list[str] = []
        for state_name, state_value in _extract_object_literal_pairs(states_obj, source):
            if state_value.type != "object":
                # Strict-literal violation handled by caller via _findings.
                continue
            full_path = f"{parent_path}.{state_name}" if parent_path else state_name
            sibling_paths.append(full_path)

            inner_states_node: Any | None = None
            is_terminal = False
            meta_purpose: str | None = None
            for k, v in _extract_object_literal_pairs(state_value, source):
                if k == "states":
                    inner_states_node = v
                elif k == "type":
                    if v.type == "string" and _string_literal_value(v, source) == "final":
                        is_terminal = True
                elif k == "meta":
                    if v.type == "object":
                        for mk, mv in _extract_object_literal_pairs(v, source):
                            if mk == "purpose" and mv.type == "string":
                                meta_purpose = _string_literal_value(mv, source)

            child_paths: list[str] = []
            if inner_states_node is not None:
                child_paths = _walk(inner_states_node, full_path)

            out[full_path] = {
                "raw_node": state_value,
                "parent_path": parent_path,
                "children_paths": child_paths,
                "is_compound": bool(child_paths),
                "is_terminal": is_terminal,
                "meta_purpose": meta_purpose,
                "line": state_value.start_point[0] + 1,
                "name": state_name,
            }
        return sibling_paths

    # Top-level states map.
    top_states: Any | None = None
    for k, v in _extract_object_literal_pairs(machine_obj, source):
        if k == "states":
            top_states = v
            break
    if top_states is not None:
        _walk(top_states, "")
    return out


# ---------------------------------------------------------------------------
# Transition / invoke extraction (per state)
# ---------------------------------------------------------------------------


def _extract_state_transitions(
    state_obj: Any,
    source: bytes,
    *,
    parent_path: str,
    sibling_paths: set[str],
    all_paths: set[str],
    machine_short_id: str,
    envelope_id: str,
    state_node_id: str,
    setup_actors: dict[str, str],
    name_map: dict[str, tuple[str, str | None]],
    rel_path: str,
    machine_name: str,
    findings_out: list | None,
) -> list[AxiomEdge]:
    """Extract ``on`` / ``always`` / ``after`` / ``invoke`` edges for a state."""
    edges: list[AxiomEdge] = []

    def _resolve_target_or_unresolved(target_str: str | None) -> str:
        """Map an authored target to a state node ID, or '<unresolved>'."""
        if target_str is None:
            return f"{envelope_id}.<unresolved>"
        resolved_path = _resolve_state_target(
            target_str,
            parent_path,
            sibling_paths,
            all_paths,
            machine_short_id,
        )
        if resolved_path is None:
            return f"{envelope_id}.<unresolved>"
        return f"{envelope_id}.states.{resolved_path}"

    for key, value in _extract_object_literal_pairs(state_obj, source):
        if key == "on":
            if value.type != "object":
                _emit_strict_finding(
                    findings_out,
                    rel_path,
                    machine_name,
                    value.start_point[0] + 1,
                    f"state.on must be an inline object literal (got {value.type}); transitions skipped",
                )
                continue
            for evt_name, evt_value in _extract_object_literal_pairs(value, source):
                target_str, internal = _extract_transition_target(evt_value, source)
                if target_str is None:
                    _emit_strict_finding(
                        findings_out,
                        rel_path,
                        machine_name,
                        evt_value.start_point[0] + 1,
                        f"on.{evt_name} target must be a string or {{target: '...'}} literal",
                    )
                    continue
                target_id = _resolve_target_or_unresolved(target_str)
                meta = {"event": evt_name, "via": "on"}
                if internal:
                    meta["internal"] = True
                edges.append(make_edge("delegates_to", state_node_id, target_id, meta=meta))

        elif key == "always":
            # Single transition or array of transitions.  In array form,
            # tree-sitter array children include ``[``, ``]``, ``,`` as
            # anonymous structural tokens — skip those — but any other
            # non-literal element (bare identifier, function call, etc.)
            # must still emit a finding under the strict-literal contract.
            transitions = []
            if value.type == "array":
                for ac in value.children:
                    if ac.type in ("[", "]", ","):
                        continue
                    if ac.type in ("string", "object"):
                        transitions.append(ac)
                    else:
                        _emit_strict_finding(
                            findings_out,
                            rel_path,
                            machine_name,
                            ac.start_point[0] + 1,
                            "always array element must be a string or {target: '...'} literal",
                        )
            else:
                transitions.append(value)
            for tr in transitions:
                target_str, internal = _extract_transition_target(tr, source)
                if target_str is None:
                    _emit_strict_finding(
                        findings_out,
                        rel_path,
                        machine_name,
                        tr.start_point[0] + 1,
                        "always target must be a string or {target: '...'} literal",
                    )
                    continue
                target_id = _resolve_target_or_unresolved(target_str)
                meta = {"via": "always"}
                if internal:
                    meta["internal"] = True
                edges.append(make_edge("delegates_to", state_node_id, target_id, meta=meta))

        elif key == "after":
            # {1000: 'next'} or {1000: {target: 'next'}} — numeric keys are
            # ``number`` nodes inside ``pair``, not ``property_identifier``,
            # so we walk the pairs directly here rather than via the helper.
            if value.type != "object":
                _emit_strict_finding(
                    findings_out,
                    rel_path,
                    machine_name,
                    value.start_point[0] + 1,
                    f"state.after must be an inline object literal (got {value.type}); delays skipped",
                )
                continue
            for child in value.children:
                if child.type != "pair":
                    continue
                delay_key: str | None = None
                delay_value: Any | None = None
                key_non_literal: Any | None = None
                seen_colon = False
                for c in child.children:
                    if c.type == ":":
                        seen_colon = True
                        continue
                    if not seen_colon:
                        if c.type == "number":
                            delay_key = _node_text(c, source)
                        elif c.type == "property_identifier":
                            delay_key = _node_text(c, source)
                        elif c.type == "string":
                            delay_key = _string_literal_value(c, source)
                        elif c.type == "computed_property_name":
                            # ``[expr]: value`` — non-literal key, must emit
                            # a strict-literal finding (not silently dropped).
                            key_non_literal = c
                    else:
                        if delay_value is None and c.type not in (",",):
                            delay_value = c
                if delay_key is None and key_non_literal is not None:
                    # Key is a computed/non-literal expression — surface it.
                    _emit_strict_finding(
                        findings_out,
                        rel_path,
                        machine_name,
                        key_non_literal.start_point[0] + 1,
                        "after delay key must be a number, string, or "
                        "property identifier literal "
                        f"(got {key_non_literal.type}); delay skipped",
                    )
                    continue
                if delay_key is None or delay_value is None:
                    continue
                target_str, internal = _extract_transition_target(delay_value, source)
                if target_str is None:
                    _emit_strict_finding(
                        findings_out,
                        rel_path,
                        machine_name,
                        delay_value.start_point[0] + 1,
                        f"after.{delay_key} target must be a string or {{target: '...'}} literal",
                    )
                    continue
                target_id = _resolve_target_or_unresolved(target_str)
                meta = {"via": "after", "delay": str(delay_key)}
                if internal:
                    meta["internal"] = True
                edges.append(make_edge("delegates_to", state_node_id, target_id, meta=meta))

        elif key == "invoke":
            # Single invoke object or array.  In array form, tree-sitter
            # array children include ``[``, ``]``, ``,`` as anonymous
            # structural tokens — skip those.  Accept ``object`` and
            # ``string`` elements (matching the ``always`` array contract);
            # any other element type (bare identifier, function call, etc.)
            # must emit a finding under the strict-literal contract.
            invokes: list[Any] = []
            if value.type == "array":
                for ac in value.children:
                    if ac.type in ("[", "]", ","):
                        continue
                    if ac.type in ("object", "string"):
                        invokes.append(ac)
                    else:
                        _emit_strict_finding(
                            findings_out,
                            rel_path,
                            machine_name,
                            ac.start_point[0] + 1,
                            f"invoke array element must be an inline object literal (got {ac.type}); element skipped",
                        )
            elif value.type == "object":
                invokes.append(value)
            for inv in invokes:
                src_str: str | None = None
                on_done_target: str | None = None
                on_error_target: str | None = None
                for k, v in _extract_object_literal_pairs(inv, source):
                    if k == "src":
                        if v.type == "string":
                            src_str = _string_literal_value(v, source)
                        else:
                            _emit_strict_finding(
                                findings_out,
                                rel_path,
                                machine_name,
                                v.start_point[0] + 1,
                                f"invoke.src must be a string literal (got {v.type}); no invoke edge produced",
                            )
                    elif k == "onDone":
                        on_done_target, _ = _extract_transition_target(v, source)
                        if on_done_target is None:
                            _emit_strict_finding(
                                findings_out,
                                rel_path,
                                machine_name,
                                v.start_point[0] + 1,
                                "invoke.onDone target must be a string or {target: '...'} literal",
                            )
                    elif k == "onError":
                        on_error_target, _ = _extract_transition_target(v, source)
                        if on_error_target is None:
                            _emit_strict_finding(
                                findings_out,
                                rel_path,
                                machine_name,
                                v.start_point[0] + 1,
                                "invoke.onError target must be a string or {target: '...'} literal",
                            )
                if src_str is not None:
                    # Resolve through setup actors map → imports name_map.
                    target_id = _resolve_invoke_src(src_str, setup_actors, name_map, envelope_id)
                    if target_id is None:
                        _emit_strict_finding(
                            findings_out,
                            rel_path,
                            machine_name,
                            inv.start_point[0] + 1,
                            f"invoke.src '{src_str}' not in setup({{actors}}) map or unresolvable through imports",
                        )
                        target_id = f"{envelope_id}.<unresolved>"
                    edges.append(
                        make_edge(
                            "delegates_to",
                            state_node_id,
                            target_id,
                            meta={"via": "invoke", "src": src_str},
                        )
                    )
                if on_done_target is not None:
                    target_id = _resolve_target_or_unresolved(on_done_target)
                    edges.append(
                        make_edge(
                            "delegates_to",
                            state_node_id,
                            target_id,
                            meta={"via": "invoke.onDone"},
                        )
                    )
                if on_error_target is not None:
                    target_id = _resolve_target_or_unresolved(on_error_target)
                    edges.append(
                        make_edge(
                            "delegates_to",
                            state_node_id,
                            target_id,
                            meta={"via": "invoke.onError"},
                        )
                    )

    return edges


def _resolve_invoke_src(
    src_name: str,
    setup_actors: dict[str, str],
    name_map: dict[str, tuple[str, str | None]],
    envelope_id: str,
) -> str | None:
    """Resolve an ``invoke.src`` string to a target node ID.

    1. Look up *src_name* in *setup_actors* → identifier name.
    2. Look up that identifier in *name_map* → ``{module_id}::{original}``.
    3. Return the resolved node ID, or None when any step fails.
    """
    ident = setup_actors.get(src_name)
    if ident is None:
        return None
    if ident in name_map:
        mod_id, original = name_map[ident]
        if original is not None:
            return f"{mod_id}::{original}"
        # Default / namespace import — best-effort use bound name.
        return f"{mod_id}::{ident}"
    # Local symbol — emit edge to {module_of_envelope}::{ident} as best-effort.
    # envelope_id has shape "{project}::{module_dotpath}::{name}@machine"
    # We strip the trailing "::{name}@machine" to recover the module id.
    if "::" in envelope_id and envelope_id.endswith("@machine"):
        base = envelope_id[: -len("@machine")]
        # base ends with "::{name}", strip it.
        if "::" in base:
            module_id = base.rsplit("::", 1)[0]
            return f"{module_id}::{ident}"
    return None


# ---------------------------------------------------------------------------
# Spawn extraction
# ---------------------------------------------------------------------------


def _walk_for_spawn_calls(node: Any, source: bytes, out: list[str]) -> None:
    """Recursively collect identifiers passed as the first arg of ``spawn(...)``."""
    if node.type == "call_expression":
        # Check if the callee is "spawn"
        is_spawn = False
        for c in node.children:
            if c.type == "identifier" and _node_text(c, source) == "spawn":
                is_spawn = True
                break
            if c.type == "member_expression":
                # ignore — spawn() is bare
                break
        if is_spawn:
            for c in node.children:
                if c.type == "arguments":
                    for ac in c.children:
                        if ac.type == "identifier":
                            out.append(_node_text(ac, source))
                            break
    for child in node.children:
        _walk_for_spawn_calls(child, source, out)


# ---------------------------------------------------------------------------
# Machine envelope detection (top-level walker)
# ---------------------------------------------------------------------------


def _find_machine_calls(
    root_node: Any,
    source: bytes,
) -> list[tuple[Any, Any | None]]:
    """Walk the root and return ``[(machine_call, setup_call_or_None), ...]``.

    Detects both ``createMachine(...)`` and ``setup(...).createMachine(...)``.
    Recursive over the entire AST since machines may be assigned at module
    scope or nested.
    """
    found: list[tuple[Any, Any | None]] = []

    def _walk(n: Any) -> None:
        if n.type == "call_expression":
            is_setup, setup_call = _is_setup_create_machine(n, source)
            if is_setup:
                found.append((n, setup_call))
                # Don't recurse further into this call — already captured.
                return
            if _is_create_machine_call(n, source):
                found.append((n, None))
                return
        for c in n.children:
            _walk(c)

    _walk(root_node)
    return found


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def scan_xstate_module(
    file_path: Path,
    project_root: Path,
    project_id: str,
    *,
    findings_out: list | None = None,
    is_rule_enabled=None,  # noqa: ARG001  (kept for future per-rule gating)
) -> tuple[list[AxiomNode], list[AxiomEdge]]:
    """Scan a single JS/TS file for xstate v5 state machines.

    Args:
        file_path: Absolute path to the JS/TS file.
        project_root: Absolute path to the project root.
        project_id: Short identifier used as namespace prefix.
        findings_out: Optional list to which strict-literal findings are
            appended (severity ``IMPORTANT``).
        is_rule_enabled: Reserved for future rule filtering — ignored in v1.

    Returns:
        Tuple ``(nodes, edges)``.  Empty when the file has no
        ``createMachine`` calls or when tree-sitter is unavailable.

    Notes:
        Coexists with :func:`axiom_graph.scanners.js_scanner.scan_js_module`
        — both run on the same file independently.  The xstate scanner
        emits no module / function / step nodes.
    """
    if not HAS_TREE_SITTER:
        return [], []

    source_bytes = file_path.read_bytes()
    rel_path = file_path.relative_to(project_root).as_posix()
    dotpath = _rel_path_to_dotpath(rel_path)
    module_id = f"{project_id}::{dotpath}"

    suffix = file_path.suffix
    lang = _get_language(suffix)
    parser = Parser(lang)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    # Imports — needed for invoke.src resolution.
    name_map, _import_edges = _extract_imports(root, source_bytes, file_path, project_root, project_id, module_id)

    machine_calls = _find_machine_calls(root, source_bytes)
    if not machine_calls:
        return [], []

    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []
    machine_envelope_by_binding: dict[str, str] = {}

    for machine_call, setup_call in machine_calls:
        machine_obj = _first_arg_object(machine_call)
        if machine_obj is None:
            # Strict-literal violation: createMachine(non-object).
            binding = _binding_name_for_call(machine_call, source_bytes) or "<unbound>"
            _emit_strict_finding(
                findings_out,
                rel_path,
                binding,
                machine_call.start_point[0] + 1,
                "createMachine(...) argument must be an inline object literal",
            )
            continue

        # Determine machine name: id field, else const-binding fallback.
        explicit_id = _extract_machine_id(machine_obj, source_bytes)
        binding_name = _binding_name_for_call(machine_call, source_bytes)
        machine_short_id = explicit_id or binding_name
        if machine_short_id is None:
            _emit_strict_finding(
                findings_out,
                rel_path,
                "<unbound>",
                machine_call.start_point[0] + 1,
                "createMachine has no `id` field and no const-binding to derive a name",
            )
            continue

        envelope_id = f"{module_id}::{machine_short_id}@machine"
        if binding_name:
            machine_envelope_by_binding[binding_name] = envelope_id

        # Walk meta.purpose at the machine root.
        machine_purpose: str | None = None
        for k, v in _extract_object_literal_pairs(machine_obj, source_bytes):
            if k == "meta" and v.type == "object":
                for mk, mv in _extract_object_literal_pairs(v, source_bytes):
                    if mk == "purpose" and mv.type == "string":
                        machine_purpose = _string_literal_value(mv, source_bytes)

        machine_line = machine_call.start_point[0] + 1
        level_0 = machine_short_id
        level_1 = (
            f"xstate machine {machine_short_id} — {machine_purpose}"
            if machine_purpose
            else f"xstate machine {machine_short_id}"
        )
        envelope_dflow_meta = {
            "purpose": machine_purpose or "",
            "machine_id": machine_short_id,
        }
        envelope_node = AxiomNode(
            id=envelope_id,
            node_type="composite_process",
            subtype="state_machine",
            title=level_0,
            location=rel_path,
            source="tree_sitter",
            code_hash=hash16(_node_text(machine_obj, source_bytes)),
            desc_hash=None,
            level_0=level_0,
            level_1=level_1,
            level_2=machine_purpose,
            level_3_location=f"{rel_path}#L{machine_line}",
            dflow_meta=envelope_dflow_meta,
            tags=["state_machine", "xstate"],
        )
        nodes.append(envelope_node)
        edges.append(make_edge("composes", module_id, envelope_id))

        # Setup actors map (empty when not a setup form).
        setup_actors: dict[str, str] = {}
        if setup_call is not None:
            setup_actors = _extract_setup_actors(setup_call, source_bytes)

        # Walk states.
        states_index = _collect_state_paths(machine_obj, source_bytes)

        # Validate strict-literal: catch entries in `states` whose value isn't
        # an object literal (variable / call) and emit findings.
        top_states_node: Any | None = None
        for k, v in _extract_object_literal_pairs(machine_obj, source_bytes):
            if k == "states":
                top_states_node = v
                break
        if top_states_node is not None and top_states_node.type == "object":
            _validate_states_literal_recursively(
                top_states_node,
                source_bytes,
                findings_out,
                rel_path,
                machine_short_id,
            )

        all_paths = set(states_index.keys())

        # Build state nodes + composes edges.
        state_node_id_by_path: dict[str, str] = {}
        for path, info in states_index.items():
            state_node_id = f"{envelope_id}.states.{path}"
            state_node_id_by_path[path] = state_node_id

        for path, info in states_index.items():
            state_node_id = state_node_id_by_path[path]
            is_compound = info["is_compound"]
            is_terminal = info["is_terminal"]
            meta_purpose = info["meta_purpose"]
            state_name = info["name"]
            state_line = info["line"]
            level_0 = state_name
            level_1 = f"state {path} — {meta_purpose}" if meta_purpose else f"state {path}"
            tags = ["state"]
            if is_terminal:
                tags.append("final")
            dflow_meta = {
                "xstate_path": path,
                "purpose": meta_purpose or "",
                "terminal": is_terminal,
            }
            state_node = AxiomNode(
                id=state_node_id,
                node_type="composite_process" if is_compound else "atomic_process",
                subtype="state",
                title=level_0,
                location=rel_path,
                source="tree_sitter",
                code_hash="",  # state nodes don't carry staleness
                desc_hash=None,
                level_0=level_0,
                level_1=level_1,
                level_2=meta_purpose,
                level_3_location=f"{rel_path}#L{state_line}",
                dflow_meta=dflow_meta,
                tags=tags,
            )
            nodes.append(state_node)
            # composes from envelope (top-level) or from parent state.
            parent_path = info["parent_path"]
            if parent_path:
                parent_node_id = state_node_id_by_path.get(parent_path)
                if parent_node_id:
                    edges.append(make_edge("composes", parent_node_id, state_node_id))
            else:
                edges.append(make_edge("composes", envelope_id, state_node_id))

        # Now emit transition / invoke edges for each state.
        # Build sibling paths cache per parent.
        siblings_by_parent: dict[str, set[str]] = {}
        for path, info in states_index.items():
            parent = info["parent_path"]
            siblings_by_parent.setdefault(parent, set()).add(info["name"])

        for path, info in states_index.items():
            state_node_id = state_node_id_by_path[path]
            parent_path = info["parent_path"]
            sibling_paths = siblings_by_parent.get(parent_path, set())
            transition_edges = _extract_state_transitions(
                info["raw_node"],
                source_bytes,
                parent_path=parent_path,
                sibling_paths=sibling_paths,
                all_paths=all_paths,
                machine_short_id=machine_short_id,
                envelope_id=envelope_id,
                state_node_id=state_node_id,
                setup_actors=setup_actors,
                name_map=name_map,
                rel_path=rel_path,
                machine_name=machine_short_id,
                findings_out=findings_out,
            )
            edges.extend(transition_edges)

        # Spawn relationships (machine envelope → composes another machine).
        spawn_targets: list[str] = []
        _walk_for_spawn_calls(machine_obj, source_bytes, spawn_targets)
        for target_ident in spawn_targets:
            if target_ident in name_map:
                mod_id, original = name_map[target_ident]
                if original is not None:
                    target_id = f"{mod_id}::{original}@machine"
                else:
                    target_id = f"{mod_id}::{target_ident}@machine"
                edges.append(make_edge("composes", envelope_id, target_id))

    # Dedupe edges by id.
    seen: set[str] = set()
    unique_edges: list[AxiomEdge] = []
    for e in edges:
        if e.id not in seen:
            seen.add(e.id)
            unique_edges.append(e)

    return nodes, unique_edges


def _validate_states_literal_recursively(
    states_obj: Any,
    source: bytes,
    findings_out: list | None,
    rel_path: str,
    machine_short_id: str,
) -> None:
    """Emit findings for any state entry whose value is not an inline object.

    Walks the entire state subtree (not just top-level) so a non-literal
    nested state still surfaces a finding.  Sibling states with literal
    objects continue to be extracted.
    """
    if states_obj.type != "object":
        return
    for key, value in _extract_object_literal_pairs(states_obj, source):
        if value.type != "object":
            _emit_strict_finding(
                findings_out,
                rel_path,
                machine_short_id,
                value.start_point[0] + 1,
                f"states.{key} must be an inline object literal (got {value.type}); subtree skipped",
            )
            continue
        # Recurse into nested states.
        for k, v in _extract_object_literal_pairs(value, source):
            if k == "states":
                _validate_states_literal_recursively(v, source, findings_out, rel_path, machine_short_id)


__all__ = ["scan_xstate_module", "HAS_TREE_SITTER"]
