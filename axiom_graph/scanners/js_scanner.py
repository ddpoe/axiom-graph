"""Cortex JS/TS scanner -- tree-sitter AST to AxiomNode + AxiomEdge objects.

Entry point:
    scan_js_module(file_path, project_root, project_id, ...)
        -> tuple[list[AxiomNode], list[AxiomEdge]]

Requires the optional ``[js]`` extra (``tree-sitter``,
``tree-sitter-javascript``, ``tree-sitter-typescript``).  When not
installed, ``HAS_TREE_SITTER`` is ``False`` and ``scan_js_module`` raises
``RuntimeError``.

Envelope + step extraction:
    Functions wrapped with the ``workflow(opts)(fn)`` / ``task(opts)(fn)``
    HOFs (see ``axiom-annotations-js``) get an envelope ``composite_process``
    node and a ``composes`` edge from the module.  Inline ``Step({...})`` /
    ``AutoStep({...})`` markers in the wrapped function body become
    ``atomic_process`` step nodes.  AutoStep delegates_to resolution covers
    plain identifier and namespace-import-member calls.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from axiom_graph.models import hash16, make_edge
from axiom_graph.scanners._step_helpers import (
    build_envelope_node as _shared_build_envelope_node,
    build_step_levels,
    build_step_meta,
    build_step_node,
    camel_to_snake as _camel_to_snake,
    parse_step_num as _parse_js_step_num,
    resolve_call_target_via_name_map,
    step_id_for,
)

# ---------------------------------------------------------------------------
# Optional dependency gate
# ---------------------------------------------------------------------------

try:
    import tree_sitter_javascript as _tsjs
    import tree_sitter_typescript as _tsts
    from tree_sitter import Language, Parser

    _JS_LANG = Language(_tsjs.language())
    _TS_LANG = Language(_tsts.language_typescript())
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False
    _JS_LANG = None  # type: ignore[assignment]
    _TS_LANG = None  # type: ignore[assignment]

from axiom_graph.models import AxiomEdge, AxiomNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JS_EXTENSIONS = frozenset((".js", ".jsx"))
_TS_EXTENSIONS = frozenset((".ts", ".tsx"))
_ALL_EXTENSIONS = _JS_EXTENSIONS | _TS_EXTENSIONS

# Test-runner identifiers recognized at module top level.  Covers Playwright,
# Vitest, Jest, and Mocha.  Member-expression callees (`test.skip`, `test.only`)
# do NOT match -- v1 limitation, documented in docs/pev-requests/test-runner-aware-envelope-detection.json.
_TEST_RUNNER_NAMES = frozenset(("test", "it"))
# Grouping callee names whose arrow-function body is recursed into one level
# deep when scanning for nested test() calls.  v1 supports
# `describe('block', () => { test(...) })`; nested describe-of-describe is
# not unfolded.
_DESCRIBE_NAMES = frozenset(("describe",))

# Test-file basename markers, mirroring the Python module_scanner's
# ``test_*`` / ``*_test`` tagging but spelled in JS/TS conventions
# (``foo.test.ts``, ``foo.spec.tsx``).  Used to split atomic function
# nodes into subtype ``"test"`` vs ``"function"`` for parity.
_TEST_FILE_INFIXES = (".test.", ".spec.")


def _is_test_file(rel_path: str) -> bool:
    """Return whether ``rel_path`` names a JS/TS test file.

    Parity with :func:`module_scanner._extract_tags`: a test file by
    basename, recognizing ``foo.test.ts`` / ``foo.spec.tsx`` plus
    Python-style ``test_*`` / ``*_test`` / ``*_spec`` stems.
    """
    basename = rel_path.rsplit("/", 1)[-1]
    if basename.startswith("test_"):
        return True
    if any(infix in basename for infix in _TEST_FILE_INFIXES):
        return True
    stem = basename.rsplit(".", 1)[0]
    return stem.endswith("_test") or stem.endswith("_spec")


def _slugify_test_name(name: str) -> str:
    """Slugify a test-name string for use in envelope IDs.

    Lowercase, replace runs of non-alphanumerics with ``-``, trim leading
    and trailing ``-``.  Idempotent and reversible-enough for human
    inspection.
    """
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()


def _rel_path_to_dotpath(rel_path: str) -> str:
    """Convert ``'src/app.ts'`` to ``'src.app'``."""
    p = rel_path
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        if p.endswith(ext):
            p = p[: -len(ext)]
            break
    return p.replace("/", ".")


def _first_sentence(text: str | None) -> str:
    """Return the first sentence of a JSDoc comment."""
    if not text:
        return ""
    text = text.strip()
    for sep in (".", "!", "\n"):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text.strip()


def _node_text(node: Any, source: bytes) -> str:
    """Get the text content of a tree-sitter node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _get_language(suffix: str) -> Any:
    """Return the tree-sitter Language for a file extension."""
    if suffix in _TS_EXTENSIONS:
        return _TS_LANG
    return _JS_LANG


def _extract_jsdoc(node: Any, source: bytes) -> str | None:
    """Extract JSDoc comment preceding a node, if any.

    Walks backward from the node's start looking for a comment sibling
    that ends with the JSDoc pattern ``*/``.
    """
    prev = node.prev_named_sibling
    if prev is None:
        return None
    if prev.type != "comment":
        return None
    text = _node_text(prev, source)
    if text.startswith("/**"):
        # Strip /** ... */ and leading * on each line
        lines = text[3:-2].splitlines()
        cleaned = []
        for line in lines:
            line = line.strip()
            if line.startswith("*"):
                line = line[1:].strip()
            cleaned.append(line)
        return "\n".join(cleaned).strip() or None
    return None


# ---------------------------------------------------------------------------
# Import analysis
# ---------------------------------------------------------------------------


def _resolve_import_path(
    import_path: str,
    file_path: Path,
    project_root: Path,
) -> Path | None:
    """Resolve a relative ESM import path to a file on disk.

    Tries the path as-is, then with common extensions appended.
    Returns None for non-relative (package) imports.
    """
    if not import_path.startswith("."):
        return None  # External / package import -- no edge

    # The import may have .js extension even for .ts source files
    base_dir = file_path.parent
    candidate = base_dir / import_path

    # Strip .js extension to try .ts/.tsx
    stem = import_path
    if stem.endswith(".js"):
        stem = stem[:-3]
    elif stem.endswith(".jsx"):
        stem = stem[:-4]

    candidate_base = base_dir / stem

    # Try in order: exact, .ts, .tsx, .js, .jsx, /index.ts, /index.js
    candidates = [
        candidate,
        candidate_base.with_suffix(".ts"),
        candidate_base.with_suffix(".tsx"),
        candidate_base.with_suffix(".js"),
        candidate_base.with_suffix(".jsx"),
        candidate_base / "index.ts",
        candidate_base / "index.js",
    ]

    for c in candidates:
        resolved = c.resolve()
        if resolved.is_file():
            try:
                resolved.relative_to(project_root.resolve())
                return resolved
            except ValueError:
                return None  # Outside project root
    return None


def _extract_imports(
    root_node: Any,
    source: bytes,
    file_path: Path,
    project_root: Path,
    project_id: str,
    module_id: str,
) -> tuple[dict[str, tuple[str, str | None]], list[AxiomEdge]]:
    """Extract ESM imports from the AST root.

    Returns:
        name_map: bound_name -> (module_node_id, original_name | None)
        edges: list of depends_on edges
    """
    name_map: dict[str, tuple[str, str | None]] = {}
    edges: list[AxiomEdge] = []

    for child in root_node.children:
        if child.type != "import_statement":
            continue

        # Skip `import type { ... }` statements (compile-time only)
        child_types = [c.type for c in child.children]
        if "type" in child_types:
            continue

        # Extract the import path from the string node
        string_node = None
        for c in child.children:
            if c.type == "string":
                string_node = c
                break
        if string_node is None:
            continue

        # Get the actual string content (inside quotes)
        import_path = _node_text(string_node, source)
        # Remove quotes
        import_path = import_path.strip("'\"")

        resolved = _resolve_import_path(import_path, file_path, project_root)
        if resolved is None:
            continue

        rel = resolved.relative_to(project_root.resolve()).as_posix()
        target_dotpath = _rel_path_to_dotpath(rel)
        target_id = f"{project_id}::{target_dotpath}"

        if target_id == module_id:
            continue

        edges.append(make_edge("depends_on", module_id, target_id))

        # Build name_map from the import_clause
        import_clause = None
        for c in child.children:
            if c.type == "import_clause":
                import_clause = c
                break
        if import_clause is None:
            continue

        for clause_child in import_clause.children:
            if clause_child.type == "named_imports":
                # import { a, b as c } from '...'
                for spec in clause_child.children:
                    if spec.type == "import_specifier":
                        # spec children: identifier [as identifier]
                        names = [c for c in spec.children if c.type == "identifier"]
                        if len(names) == 2:
                            # import { orig as alias }
                            orig = _node_text(names[0], source)
                            bound = _node_text(names[1], source)
                        elif len(names) == 1:
                            orig = _node_text(names[0], source)
                            bound = orig
                        else:
                            continue
                        name_map[bound] = (target_id, orig)
            elif clause_child.type == "namespace_import":
                # import * as Name from '...'
                ident = None
                for c in clause_child.children:
                    if c.type == "identifier":
                        ident = c
                        break
                if ident is not None:
                    bound = _node_text(ident, source)
                    name_map[bound] = (target_id, None)
            elif clause_child.type == "identifier":
                # import Name from '...' (default import)
                bound = _node_text(clause_child, source)
                name_map[bound] = (target_id, None)

    return name_map, edges


# ---------------------------------------------------------------------------
# Envelope + step helpers (workflow / task HOF + Step / AutoStep markers)
# ---------------------------------------------------------------------------

# Whitelisted envelope kwargs (from `workflow(opts)(fn)` / `task(opts)(fn)`).
_ENVELOPE_KWARGS = ("purpose", "inputs", "outputs", "critical")
# Whitelisted Step / AutoStep kwargs.
_STEP_KWARGS = ("step_num", "name", "purpose", "inputs", "outputs", "critical")


def _string_literal_value(node: Any, source: bytes) -> str | None:
    """Extract a JS string literal's content, or None if *node* isn't a string.

    Handles regular ``string`` nodes (children: ``'`` ``string_fragment`` ``'``).
    Template strings are not accepted (we want a literal contract).
    """
    if node.type != "string":
        return None
    # The fragment children carry the actual text
    parts = []
    for c in node.children:
        if c.type == "string_fragment":
            parts.append(_node_text(c, source))
    return "".join(parts)


def _extract_object_literal_pairs(
    obj_node: Any,
    source: bytes,
) -> list[tuple[str, Any]]:
    """Return list of ``(key_text, value_node)`` for an ``object`` literal.

    Skips computed keys, shorthand properties, spread elements, and method
    definitions — only plain ``pair`` children with a ``property_identifier``
    or ``string`` key are returned.

    Returns:
        Pairs in source order.  Empty for malformed / non-literal objects.
    """
    if obj_node.type != "object":
        return []
    pairs: list[tuple[str, Any]] = []
    for child in obj_node.children:
        if child.type != "pair":
            continue
        key_text: str | None = None
        value_node: Any = None
        seen_colon = False
        for c in child.children:
            if c.type == ":":
                seen_colon = True
                continue
            if not seen_colon:
                if c.type == "property_identifier":
                    key_text = _node_text(c, source)
                elif c.type == "string":
                    key_text = _string_literal_value(c, source)
            else:
                if value_node is None and c.type not in (",",):
                    value_node = c
        if key_text is not None and value_node is not None:
            pairs.append((key_text, value_node))
    return pairs


def _has_spread_or_computed(obj_node: Any) -> bool:
    """Return True if the object literal contains spread / shorthand / computed keys."""
    if obj_node.type != "object":
        return True
    for child in obj_node.children:
        if child.type == "spread_element":
            return True
        # Shorthand property: `{foo}` -> child type is `shorthand_property_identifier`
        if child.type == "shorthand_property_identifier":
            return True
        if child.type == "computed_property_name":
            return True
    return False


def _extract_envelope_kwargs(
    inner_call: Any,
    source: bytes,
) -> tuple[dict[str, str], Any | None, str | None]:
    """Extract envelope kwargs from the inner ``workflow(opts)`` / ``task(opts)`` call.

    Args:
        inner_call: The inner ``call_expression`` (whose callee identifier is
            ``workflow`` or ``task``).
        source: File source bytes.

    Returns:
        ``(kwargs, opts_node, error)``.  ``kwargs`` keys are snake_case.
        ``opts_node`` is the object-literal AST node (or ``None``).
        ``error`` is ``None`` on success, or a short violation tag string on
        failure (e.g. ``"non-literal-opts"``, ``"empty-args"``).
    """
    args_node: Any = None
    for c in inner_call.children:
        if c.type == "arguments":
            args_node = c
            break
    if args_node is None:
        return ({}, None, "missing-arguments")

    # The first non-punctuation child of arguments should be an object.
    payload_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]
    if len(payload_nodes) == 0:
        return ({}, None, "empty-args")
    if len(payload_nodes) > 1:
        return ({}, None, "extra-args")
    opts_node = payload_nodes[0]
    if opts_node.type != "object":
        return ({}, opts_node, "non-literal-opts")
    if _has_spread_or_computed(opts_node):
        return ({}, opts_node, "spread-or-computed-opts")

    kwargs: dict[str, str] = {}
    for key, value_node in _extract_object_literal_pairs(opts_node, source):
        snake = _camel_to_snake(key)
        if snake not in _ENVELOPE_KWARGS:
            continue
        sval = _string_literal_value(value_node, source)
        if sval is None:
            # Non-string value -- skip (partial extraction; not a contract violation
            # because non-whitelisted keys may have been added)
            continue
        kwargs[snake] = sval
    return (kwargs, opts_node, None)


def _build_envelope_node_js(
    *,
    func_id: str,
    func_name: str,
    decorator_name: str,
    kwargs: dict[str, str],
    rel_path: str,
    module_id: str,
    start_line: int,
    test_runner: str | None = None,
    test_name: str | None = None,
) -> tuple[AxiomNode, list[AxiomEdge], str]:
    """Thin wrapper around the shared envelope builder, fixed at ``source="tree_sitter"``."""
    return _shared_build_envelope_node(
        source="tree_sitter",
        func_id=func_id,
        func_name=func_name,
        decorator_name=decorator_name,
        kwargs=kwargs,
        rel_path=rel_path,
        module_id=module_id,
        start_line=start_line,
        test_runner=test_runner,
        test_name=test_name,
    )


# Loop-bearing tree-sitter node types.  ``for_in_statement`` covers BOTH
# ``for...in`` and ``for...of`` in tree-sitter-typescript / -javascript.
_LOOP_NODE_TYPES = frozenset(("for_statement", "while_statement", "for_in_statement", "do_statement"))
# Function-bearing nodes — DO NOT recurse into these during step walk.
_FUNCTION_BOUNDARY_TYPES = frozenset(
    (
        "function_declaration",
        "function_expression",
        "arrow_function",
        "method_definition",
        "generator_function",
        "generator_function_declaration",
    )
)


def _walk_for_step_calls(
    body_node: Any,
    source: bytes,
    in_loop: bool = False,
):
    """Yield ``(call_expr_node, marker_name, in_loop, parent_stmt)`` for Step/AutoStep calls.

    Walks ``body_node`` in source order.  ``in_loop`` is True if any ancestor
    is a for/while/do/for-in/for-of statement.  Stops descending at nested
    function boundaries (so steps inside callbacks are not extracted).

    The yielded ``parent_stmt`` is the immediate enclosing ``statement_block``
    child statement — used by the caller to look at the next statement for
    AutoStep delegates_to resolution.
    """
    # Only walk the outermost function body's statement_block(s).
    if body_node is None:
        return
    yield from _walk_step_calls_inner(body_node, source, in_loop, parent_stmt=None)


def _walk_step_calls_inner(
    node: Any,
    source: bytes,
    in_loop: bool,
    parent_stmt: Any,
):
    """Recursive helper for ``_walk_for_step_calls``."""
    # Update loop ancestry.
    next_in_loop = in_loop or (node.type in _LOOP_NODE_TYPES)

    # Walk children with the updated in_loop flag.  When the immediate child
    # is a statement-bearing block, track its position in the parent so the
    # caller can pair AutoStep with the next statement.
    if node.type in ("statement_block", "program"):
        # Iterate top-level statements; track index for next-stmt lookup.
        children = list(node.children)
        for i, child in enumerate(children):
            # parent_stmt for children of a block is the child itself
            if child.type in _FUNCTION_BOUNDARY_TYPES and node is not None:
                # Don't recurse into nested functions for step extraction.
                continue
            yield from _walk_step_calls_inner(child, source, next_in_loop, parent_stmt=child)
            # If child is a statement that contains a Step/AutoStep at its
            # top level, the inner walker will already have yielded it with
            # the correct parent_stmt.  Pass through.
        return

    # If this is a call_expression to Step/AutoStep, yield it.
    if node.type == "call_expression":
        callee = None
        for c in node.children:
            if c.type == "identifier":
                callee = _node_text(c, source)
                break
            # Anything else (member_expression, parenthesized_expression, etc.)
            # disqualifies — markers must be plain identifier calls.
            if c.type in ("member_expression", "subscript_expression"):
                callee = None
                break
        if callee in ("Step", "AutoStep"):
            yield (node, callee, next_in_loop, parent_stmt)
            # Don't descend into the marker's own arguments.
            return

    # Don't recurse into nested function bodies.
    if node.type in _FUNCTION_BOUNDARY_TYPES:
        return

    for child in node.children:
        yield from _walk_step_calls_inner(child, source, next_in_loop, parent_stmt=parent_stmt)


def _extract_step_kwargs(
    call_node: Any,
    source: bytes,
) -> tuple[dict[str, Any], Any | None, str | None]:
    """Extract ``Step({...})`` / ``AutoStep({...})`` kwargs.

    Returns:
        ``(kwargs, opts_node, error)``.  ``kwargs`` keys are snake_case.
        ``error`` is ``None`` on success, or a violation tag string on
        contract failure: ``"missing-arguments"`` (no args), ``"empty-args"``,
        ``"extra-args"`` (more than one positional arg),
        ``"non-literal-opts"`` (arg isn't an object literal),
        ``"spread-or-computed-opts"`` (object has spread / computed keys).
    """
    args_node: Any = None
    for c in call_node.children:
        if c.type == "arguments":
            args_node = c
            break
    if args_node is None:
        return ({}, None, "missing-arguments")

    payload_nodes = [c for c in args_node.children if c.type not in ("(", ")", ",")]
    if len(payload_nodes) == 0:
        return ({}, None, "empty-args")
    if len(payload_nodes) > 1:
        return ({}, None, "extra-args")
    opts_node = payload_nodes[0]
    if opts_node.type != "object":
        return ({}, opts_node, "non-literal-opts")
    if _has_spread_or_computed(opts_node):
        return ({}, opts_node, "spread-or-computed-opts")

    kwargs: dict[str, Any] = {}
    for key, value_node in _extract_object_literal_pairs(opts_node, source):
        snake = _camel_to_snake(key)
        if snake not in _STEP_KWARGS:
            continue
        # Step values may be numbers (step_num) or strings.
        if value_node.type == "number":
            raw = _node_text(value_node, source)
            if "." in raw:
                try:
                    kwargs[snake] = float(raw)
                except ValueError:
                    kwargs[snake] = raw
            else:
                try:
                    kwargs[snake] = int(raw)
                except ValueError:
                    kwargs[snake] = raw
        elif value_node.type == "string":
            sval = _string_literal_value(value_node, source)
            if sval is not None:
                kwargs[snake] = sval
        # Non-literal values for individual fields are silently skipped (the
        # whole-call contract is "object literal"; per-field non-literals are
        # not a separate violation in v1).
    return (kwargs, opts_node, None)


def _resolve_js_call_target(
    stmt_after: Any,
    source: bytes,
    name_map: dict[str, tuple[str, str | None]],
    local_func_ids: dict[str, str],
) -> tuple[str | None, str | None]:
    """Resolve the call target of the statement following an AutoStep.

    Mirrors ``module_scanner._resolve_next_call_target`` plus the namespace-
    import-member path required by US-2.

    Resolution order:
      1. Plain identifier → name_map[name]: edge to ``{module_id}::{original_name}``
         when ``original_name`` is set; no edge for namespace bindings.
      2. ``obj.fn`` where obj is a namespace-import binding (``original_name=None``):
         edge to ``{module_id}::{property_name}``.
      3. ``obj.fn`` where obj is a named-import binding: no edge in v1.
      4. Plain identifier matching a local function in this module:
         edge to that local node id.
      5. Else: no edge.

    Args:
        stmt_after: The statement node following the AutoStep.
        source: File source bytes.
        name_map: Import bindings (see ``_extract_imports``).
        local_func_ids: Map ``func_name -> node_id`` of local same-file
            functions.

    Returns:
        ``(target_id, target_short_name)``.  ``target_id`` is the resolved
        node ID or None; ``target_short_name`` is the function's short name
        for B4 reporting.
    """
    if stmt_after is None:
        return (None, None)

    # Find the call_expression in the statement (if any).
    call: Any = None
    if stmt_after.type == "expression_statement":
        for c in stmt_after.children:
            if c.type == "call_expression":
                call = c
                break
    elif stmt_after.type == "lexical_declaration" or stmt_after.type == "variable_declaration":
        # `const x = foo()` / `let x = foo()`
        for c in stmt_after.children:
            if c.type == "variable_declarator":
                for cc in c.children:
                    if cc.type == "call_expression":
                        call = cc
                        break
                if call is not None:
                    break
    elif stmt_after.type == "return_statement":
        for c in stmt_after.children:
            if c.type == "call_expression":
                call = c
                break
    elif stmt_after.type == "call_expression":
        call = stmt_after

    if call is None:
        return (None, None)

    # Inspect the callee.
    callee_node: Any = None
    for c in call.children:
        if c.type in (
            "identifier",
            "member_expression",
            "subscript_expression",
            "call_expression",  # chained -- give up
        ):
            callee_node = c
            break

    if callee_node is None:
        return (None, None)

    # Plain identifier
    if callee_node.type == "identifier":
        bound = _node_text(callee_node, source)
        return resolve_call_target_via_name_map(
            {"kind": "name", "name": bound},
            name_map,
            local_func_ids=local_func_ids,
        )

    # Member expression: obj.prop
    if callee_node.type == "member_expression":
        # Children: object, ".", property
        obj: Any = None
        prop: Any = None
        for c in callee_node.children:
            if c.type == "property_identifier":
                prop = c
            elif c.type in ("identifier", "member_expression"):
                if obj is None:
                    obj = c
        if obj is None or prop is None:
            return (None, None)
        prop_name = _node_text(prop, source)
        # Only single-level member access.  Chained (a.b.c) -> obj is a
        # member_expression -> v1 punt.
        if obj.type != "identifier":
            return (None, prop_name)
        obj_name = _node_text(obj, source)
        return resolve_call_target_via_name_map(
            {"kind": "attr", "root": obj_name, "attr": prop_name},
            name_map,
        )

    return (None, None)


# ---------------------------------------------------------------------------
# Function extraction
# ---------------------------------------------------------------------------


class _ScanContext:
    """Holds per-scan context threaded through the function-extraction pass.

    Mutable lists ``nodes`` and ``edges`` are appended-to in place.
    ``findings_out`` and ``autosteps_out`` may be ``None`` (suppress
    collection).
    """

    __slots__ = (
        "source",
        "module_id",
        "rel_path",
        "name_map",
        "local_func_ids",
        "findings_out",
        "autosteps_out",
        "is_rule_enabled",
        "nodes",
        "edges",
        "var_to_envelope",
        "seen_test_slugs",
    )

    def __init__(
        self,
        *,
        source: bytes,
        module_id: str,
        rel_path: str,
        name_map: dict[str, tuple[str, str | None]],
        findings_out: list | None,
        autosteps_out: list | None,
        is_rule_enabled,
        nodes: list,
        edges: list,
    ):
        self.source = source
        self.module_id = module_id
        self.rel_path = rel_path
        self.name_map = name_map
        self.local_func_ids: dict[str, str] = {}
        self.findings_out = findings_out
        self.autosteps_out = autosteps_out
        self.is_rule_enabled = is_rule_enabled or (lambda rid: True)
        self.nodes = nodes
        self.edges = edges
        # var_name -> envelope_id, populated as lexical_declaration HOFs emit
        # envelopes.  Read by the test-runner dispatch to back-patch
        # reference-form `test('name', myWf)` calls onto the same envelope.
        self.var_to_envelope: dict[str, str] = {}
        # Slugified test names already consumed in this module.  Drives the
        # D1 duplicate-name finding (first occurrence wins).
        self.seen_test_slugs: set[str] = set()


def _emit_strict_literal_finding(
    ctx: _ScanContext,
    *,
    rule_id: str,
    func_name: str,
    line: int,
    violation: str,
    fix_hint: str,
) -> None:
    """Append a Reviewer-blocking finding to ``findings_out`` (if enabled)."""
    if ctx.findings_out is None:
        return
    if not ctx.is_rule_enabled(rule_id):
        return
    from axiom_graph.workflows.validation import (
        SEVERITY_IMPORTANT,
        ValidationFinding,
    )

    ctx.findings_out.append(
        ValidationFinding(
            rule_id=rule_id,
            severity=SEVERITY_IMPORTANT,
            module=ctx.rel_path,
            function=func_name,
            line=line,
            message=f"{violation} — {fix_hint}",
        )
    )


def _process_hof_envelope(
    *,
    ctx: _ScanContext,
    var_name: str,
    outer_call: Any,
    func_id: str,
    test_runner: str | None = None,
    test_name: str | None = None,
) -> str | None:
    """Extract envelope kwargs, build envelope node, walk body for steps.

    ``outer_call`` is the ``workflow(opts)(fn)`` / ``task(opts)(fn)`` call
    expression.  ``func_id`` is the node ID of the wrapped function (the
    HOF's outer call_expression treated as a function).

    When ``test_runner`` is set, the envelope is tagged with ``"test"`` and
    ``test_runner`` / ``test_name`` are recorded in ``dflow_meta`` -- used
    when the workflow was discovered inline inside a ``test('name', ...)``
    call.

    Returns the emitted envelope id, or ``None`` when no envelope was
    emitted (e.g. strict-literal failure on the opts argument).
    """
    source = ctx.source
    inner_call = None
    wrapper_args = None
    for child in outer_call.children:
        if child.type == "call_expression" and inner_call is None:
            inner_call = child
        elif child.type == "arguments":
            wrapper_args = child
    if inner_call is None:
        return None

    # Decorator name (workflow/task)
    decorator_name = "workflow"
    for c in inner_call.children:
        if c.type == "identifier":
            decorator_name = _node_text(c, source)
            break

    start_line = outer_call.start_point[0] + 1

    kwargs, opts_node, env_error = _extract_envelope_kwargs(inner_call, source)
    if env_error is not None:
        # Loud failure: emit strict-literal finding.
        env_line = (opts_node.start_point[0] + 1) if opts_node is not None else start_line
        _emit_strict_literal_finding(
            ctx,
            rule_id="JS-LIT-ENV",
            func_name=var_name,
            line=env_line,
            violation=(f"{decorator_name}() opts argument is not an inline object literal ({env_error})"),
            fix_hint=(f"pass an inline object literal: {decorator_name}({{purpose: '...'}})(fn)"),
        )
        # No envelope node when opts is non-literal — we cannot extract dflow_meta.
        return None

    envelope, env_edges, envelope_id = _build_envelope_node_js(
        func_id=func_id,
        func_name=var_name,
        decorator_name=decorator_name,
        kwargs=kwargs,
        rel_path=ctx.rel_path,
        module_id=ctx.module_id,
        start_line=start_line,
        test_runner=test_runner,
        test_name=test_name,
    )
    ctx.nodes.append(envelope)
    ctx.edges.extend(env_edges)

    # Find the wrapped function body (arrow_function or function_expression)
    wrapped_fn: Any = None
    if wrapper_args is not None:
        for c in wrapper_args.children:
            if c.type in ("arrow_function", "function_expression"):
                wrapped_fn = c
                break
    if wrapped_fn is None:
        # Wrapped value isn't a function literal — no body to walk.
        _run_envelope_validation(ctx, var_name, decorator_name, kwargs, start_line, [])
        return envelope_id

    # Find the body (statement_block) of the wrapped function.
    body: Any = None
    for c in wrapped_fn.children:
        if c.type == "statement_block":
            body = c
            break
    if body is None:
        _run_envelope_validation(ctx, var_name, decorator_name, kwargs, start_line, [])
        return envelope_id

    # Walk body for Step / AutoStep markers.
    seen_step_nums: set[Any] = set()
    step_calls_for_validation: list[dict] = []

    for call_node, marker_name, in_loop, parent_stmt in _walk_for_step_calls(body, source):
        line = call_node.start_point[0] + 1
        is_auto = marker_name == "AutoStep"

        kw, _opts, err = _extract_step_kwargs(call_node, source)
        if err is not None:
            _emit_strict_literal_finding(
                ctx,
                rule_id="JS-LIT-STEP",
                func_name=var_name,
                line=line,
                violation=(f"{marker_name}() argument is not an inline object literal ({err})"),
                fix_hint=(f"pass an inline object literal: {marker_name}({{stepNum: 1, ...}})"),
            )
            continue

        step_num_value = kw.get("step_num")
        if step_num_value is None:
            # Missing step_num — let validation rules (A1) catch this.
            step_calls_for_validation.append(
                {
                    "step_num_value": None,
                    "name": kw.get("name"),
                    "purpose": kw.get("purpose"),
                    "is_auto": is_auto,
                    "line": line,
                    "in_loop": in_loop,
                }
            )
            continue

        # First-occurrence-wins on duplicate step_num.
        if step_num_value in seen_step_nums:
            # Still pass to validation so B1 fires.
            step_calls_for_validation.append(
                {
                    "step_num_value": step_num_value,
                    "name": kw.get("name"),
                    "purpose": kw.get("purpose"),
                    "is_auto": is_auto,
                    "line": line,
                    "in_loop": in_loop,
                }
            )
            continue
        seen_step_nums.add(step_num_value)

        # Build step node.
        step_num_raw, step_num_parts = _parse_js_step_num(step_num_value)
        step_id = step_id_for(func_id, step_num_raw)
        name = kw.get("name")
        purpose = kw.get("purpose")
        level_0, level_1 = build_step_levels(step_num_raw, name, purpose, is_auto)
        step_meta = build_step_meta(step_num_raw, step_num_parts, name, purpose, kw, is_auto)
        step_node = build_step_node(
            source="tree_sitter",
            step_id=step_id,
            is_auto=is_auto,
            level_0=level_0,
            level_1=level_1,
            purpose=str(purpose) if purpose else None,
            rel_path=ctx.rel_path,
            line=line,
            step_meta=step_meta,
        )
        ctx.nodes.append(step_node)
        ctx.edges.append(make_edge("composes", envelope_id, step_id))

        # Hand step record off to validation.
        step_calls_for_validation.append(
            {
                "step_num_value": step_num_value,
                "name": name,
                "purpose": purpose,
                "is_auto": is_auto,
                "line": line,
                "in_loop": in_loop,
            }
        )

        # AutoStep delegates_to resolution.
        if is_auto:
            target_id = None
            target_name = None
            has_next_call = False
            stmt_after = _next_sibling_statement(parent_stmt)
            if stmt_after is not None:
                target_id, target_name = _resolve_js_call_target(stmt_after, source, ctx.name_map, ctx.local_func_ids)
                # has_next_call is True if the next statement is or contains a call.
                has_next_call = _stmt_has_call(stmt_after)
                if target_id is not None:
                    ctx.edges.append(make_edge("delegates_to", step_id, target_id))
            if ctx.autosteps_out is not None:
                from axiom_graph.workflows.validation import AutoStepRecord

                ctx.autosteps_out.append(
                    AutoStepRecord(
                        module=ctx.rel_path,
                        function=var_name,
                        line=line,
                        step_num=step_num_value,
                        target_name=target_name,
                        target_node_id=target_id,
                        has_next_call=has_next_call,
                    )
                )

    _run_envelope_validation(ctx, var_name, decorator_name, kwargs, start_line, step_calls_for_validation)
    return envelope_id


def _next_sibling_statement(stmt: Any) -> Any:
    """Return the next named sibling statement of *stmt*, or None."""
    if stmt is None:
        return None
    sib = stmt.next_named_sibling
    return sib


def _stmt_has_call(stmt: Any) -> bool:
    """Return True if a statement node contains a top-level call_expression."""
    if stmt is None:
        return False
    if stmt.type == "call_expression":
        return True
    if stmt.type == "expression_statement":
        for c in stmt.children:
            if c.type == "call_expression":
                return True
    if stmt.type in ("lexical_declaration", "variable_declaration"):
        for c in stmt.children:
            if c.type == "variable_declarator":
                for cc in c.children:
                    if cc.type == "call_expression":
                        return True
    if stmt.type == "return_statement":
        for c in stmt.children:
            if c.type == "call_expression":
                return True
    return False


def _run_envelope_validation(
    ctx: _ScanContext,
    func_name: str,
    decorator_name: str,
    kwargs: dict,
    envelope_line: int,
    step_calls: list[dict],
) -> None:
    """Run ``validate_envelope`` against this envelope's collected step calls."""
    if ctx.findings_out is None:
        return
    from axiom_graph.workflows.validation import validate_envelope as _ve

    findings = _ve(
        rel_path=ctx.rel_path,
        func_name=func_name,
        func_node=None,
        envelope_kind=decorator_name,
        envelope_purpose=kwargs.get("purpose"),
        envelope_line=envelope_line,
        step_calls=step_calls,
        is_rule_enabled=ctx.is_rule_enabled,
    )
    ctx.findings_out.extend(findings)


def _extract_functions(
    root_node: Any,
    source: bytes,
    module_id: str,
    rel_path: str,
    *,
    ctx: _ScanContext | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge]]:
    """Extract function nodes from the AST.

    Handles five forms:
    1. Named function declarations (``function foo() {}``)
    2. Arrow functions assigned to const (``const foo = () => {}``)
    3. HOF wrappers -- workflow()/task() (``const x = workflow(...)(fn)``)
    4. Class methods
    5. Object method shorthand
    """
    nodes: list[AxiomNode] = ctx.nodes if ctx is not None else []
    edges: list[AxiomEdge] = ctx.edges if ctx is not None else []

    # Subtype parity with the Python scanner: every function in a test file is
    # tagged "test", otherwise "function".  Behavior-neutral (subtype is not
    # hashed and nothing branches on function-vs-None); keeps the column
    # meaningful for downstream tooling.
    func_subtype = "test" if _is_test_file(rel_path) else "function"

    def _make_func_node(
        name: str,
        func_node: Any,
        parent_id: str,
        jsdoc: str | None = None,
    ) -> str:
        """Create an atomic_process node for a function. Returns its node id."""
        func_text = _node_text(func_node, source)
        func_id = f"{module_id}::{name}"
        start_line = func_node.start_point[0] + 1  # 0-indexed -> 1-indexed
        end_line = func_node.end_point[0] + 1

        level_1 = f"{name} function"
        if jsdoc:
            first = _first_sentence(jsdoc)
            if first:
                level_1 = f"{name} -- {first}"

        node = AxiomNode(
            id=func_id,
            node_type="atomic_process",
            subtype=func_subtype,
            title=name,
            location=rel_path,
            source="tree_sitter",
            code_hash=hash16(func_text),
            level_0=name,
            level_1=level_1,
            level_2=jsdoc,
            level_3_location=f"{rel_path}#L{start_line}-L{end_line}",
            desc_hash=hash16(jsdoc) if jsdoc else None,
        )
        nodes.append(node)
        edges.append(make_edge("composes", parent_id, func_id))
        if ctx is not None:
            ctx.local_func_ids[name] = func_id
        return func_id

    for child in root_node.children:
        # Form 1: function declarations (possibly exported)
        if child.type == "function_declaration":
            name = _get_func_name(child, source)
            if name:
                jsdoc = _extract_jsdoc(child, source)
                _make_func_node(name, child, module_id, jsdoc)

        elif child.type == "export_statement":
            for export_child in child.children:
                if export_child.type == "function_declaration":
                    name = _get_func_name(export_child, source)
                    if name:
                        jsdoc = _extract_jsdoc(child, source)
                        _make_func_node(name, export_child, module_id, jsdoc)
                elif export_child.type == "lexical_declaration":
                    _process_lexical_declaration(
                        export_child,
                        source,
                        module_id,
                        rel_path,
                        nodes,
                        edges,
                        _make_func_node,
                        ctx=ctx,
                    )
                elif export_child.type == "class_declaration":
                    _process_class(
                        export_child,
                        source,
                        module_id,
                        rel_path,
                        nodes,
                        edges,
                        _make_func_node,
                    )

        # Form 2, 3, 5: const declarations (arrow funcs, HOFs, objects)
        elif child.type == "lexical_declaration":
            _process_lexical_declaration(child, source, module_id, rel_path, nodes, edges, _make_func_node, ctx=ctx)

        # Form 4: class declarations
        elif child.type == "class_declaration":
            _process_class(child, source, module_id, rel_path, nodes, edges, _make_func_node)

    # ---------------------------------------------------------------
    # Form 6: top-level test-runner calls
    #   test('name', workflow(opts)(fn))    -> inline-form envelope
    #   test('name', existingWorkflowVar)   -> tag the existing envelope
    # Done in a second pass so reference-form lookups see all
    # const-declared envelopes regardless of declaration order.
    # ---------------------------------------------------------------
    if ctx is not None:
        for child in root_node.children:
            for call_node, callee in _iter_test_runner_calls(child, source):
                _process_test_runner_call(
                    ctx=ctx,
                    call_node=call_node,
                    callee_name=callee,
                    make_func=_make_func_node,
                )

    return nodes, edges


def _get_func_name(func_node: Any, source: bytes) -> str | None:
    """Extract the name from a function_declaration node."""
    for child in func_node.children:
        if child.type == "identifier":
            return _node_text(child, source)
    return None


def _process_lexical_declaration(
    decl_node: Any,
    source: bytes,
    module_id: str,
    rel_path: str,
    nodes: list[AxiomNode],
    edges: list[AxiomEdge],
    make_func: Any,
    *,
    ctx: _ScanContext | None = None,
) -> None:
    """Process const/let/var declarations for arrow functions and HOFs."""
    for child in decl_node.children:
        if child.type != "variable_declarator":
            continue

        # Get the variable name
        var_name = None
        value_node = None
        for c in child.children:
            if c.type == "identifier" and var_name is None:
                var_name = _node_text(c, source)
            elif c.type == "arrow_function":
                value_node = c
            elif c.type == "function_expression":
                value_node = c
            elif c.type == "call_expression":
                value_node = c

        if var_name is None:
            continue

        # Form 2: arrow function assigned to const
        if value_node is not None and value_node.type == "arrow_function":
            jsdoc = _extract_jsdoc(decl_node, source)
            make_func(var_name, value_node, module_id, jsdoc)

        # Form 3: HOF wrapper -- workflow(opts)(fn) or task(opts)(fn)
        elif value_node is not None and value_node.type == "call_expression":
            if _is_hof_wrapper(value_node, source):
                jsdoc = _extract_jsdoc(decl_node, source)
                func_id = make_func(var_name, value_node, module_id, jsdoc)
                # Envelope + step extraction (only when ctx provided)
                if ctx is not None and func_id is not None:
                    envelope_id = _process_hof_envelope(
                        ctx=ctx,
                        var_name=var_name,
                        outer_call=value_node,
                        func_id=func_id,
                    )
                    if envelope_id is not None:
                        ctx.var_to_envelope[var_name] = envelope_id

        # Form 2b: function expression assigned to const
        elif value_node is not None and value_node.type == "function_expression":
            jsdoc = _extract_jsdoc(decl_node, source)
            make_func(var_name, value_node, module_id, jsdoc)

        # Form 5: Object with method shorthand methods
        # Check if the value is an object containing methods
        for c in child.children:
            if c.type == "object":
                _process_object_methods(
                    c,
                    source,
                    module_id,
                    rel_path,
                    nodes,
                    edges,
                    make_func,
                    obj_name=var_name,
                )


def _extract_test_name_literal(arg_node: Any, source: bytes) -> tuple[str | None, str | None]:
    """Return ``(test_name, error)`` from the first argument of a test call.

    Accepts:
        - ``string`` literals (any quote style).
        - ``template_string`` literals with no ``template_substitution``
          children (i.e. no `${expr}` interpolation).

    Returns ``(name, None)`` on success or ``(None, "<reason>")`` to drive
    the D2 finding.  String quotes / backticks are stripped.
    """
    if arg_node.type == "string":
        text = _node_text(arg_node, source)
        if len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]:
            return (text[1:-1], None)
        return (text, None)
    if arg_node.type == "template_string":
        for c in arg_node.children:
            if c.type == "template_substitution":
                return (None, "interpolated template literal")
        text = _node_text(arg_node, source)
        if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
            return (text[1:-1], None)
        return (text, None)
    return (None, f"first argument is {arg_node.type}, not a string literal")


def _process_test_runner_call(
    *,
    ctx: _ScanContext,
    call_node: Any,
    callee_name: str,
    make_func: Any,
) -> None:
    """Dispatch a top-level ``test()`` / ``it()`` call.

    Three behaviors depending on the second argument:

    1. **Inline form**: arg2 is a ``call_expression`` matching
       ``workflow(opts)(fn)`` / ``task(opts)(fn)``.  Emit a new envelope
       with ``"test"`` tag, ``dflow_meta.test_runner`` and
       ``dflow_meta.test_name``, and walk the wrapped body for steps.
    2. **Reference form**: arg2 is an ``identifier`` matching a known
       envelope variable in ``ctx.var_to_envelope``.  Mutate that
       envelope in place to add the ``"test"`` tag and the same meta
       fields.
    3. **Other**: silent no-op.  ``test('foo', plainFn)`` is a normal
       unannotated test; not the scanner's concern.

    Validation findings:
        - **D2** when arg1 is not a non-interpolated string literal.
        - **D1** when the slugified test name already appeared in this
          module (first occurrence wins; second is dropped).
    """
    source = ctx.source

    # Locate the arguments node.
    args_node = None
    for c in call_node.children:
        if c.type == "arguments":
            args_node = c
            break
    if args_node is None:
        return

    # Extract the two named arguments (skip punctuation children).
    named_args = [c for c in args_node.children if c.is_named]
    if len(named_args) < 2:
        return  # Malformed call; not our concern.
    name_arg, body_arg = named_args[0], named_args[1]
    line = call_node.start_point[0] + 1

    # --- D2: validate test-name literal -------------------------------
    test_name, name_err = _extract_test_name_literal(name_arg, source)
    if name_err is not None:
        _emit_strict_literal_finding(
            ctx,
            rule_id="D2",
            func_name=f"{callee_name}(...)",
            line=line,
            violation=f"{callee_name}() test-name argument is not a string literal ({name_err})",
            fix_hint=(f"pass a non-interpolated string literal: {callee_name}('test name', workflow(...)(fn))"),
        )
        return

    slug = _slugify_test_name(test_name)
    if not slug:
        # Empty test name (e.g. test('', ...)) -- treat like D2.
        _emit_strict_literal_finding(
            ctx,
            rule_id="D2",
            func_name=f"{callee_name}(...)",
            line=line,
            violation=f"{callee_name}() test name slugifies to empty string",
            fix_hint="provide a non-empty test name with at least one alphanumeric character",
        )
        return

    # --- D1: duplicate-name detection within file ---------------------
    if slug in ctx.seen_test_slugs:
        _emit_strict_literal_finding(
            ctx,
            rule_id="D1",
            func_name=f"{callee_name}('{test_name}')",
            line=line,
            violation=f"duplicate {callee_name}() name '{test_name}' (slug={slug})",
            fix_hint="rename one of the duplicate tests; first occurrence wins the envelope id",
        )
        return

    # --- Dispatch on body argument shape ------------------------------

    # Reference form: identifier → look up known envelope.
    if body_arg.type == "identifier":
        ref_name = _node_text(body_arg, source)
        envelope_id = ctx.var_to_envelope.get(ref_name)
        if envelope_id is None:
            return  # Not a known envelope; silent no-op.
        # Mutate the envelope node in place.
        for n in ctx.nodes:
            if n.id == envelope_id:
                if n.tags is None:
                    n.tags = []
                if "test" not in n.tags:
                    n.tags.append("test")
                if n.dflow_meta is None:
                    n.dflow_meta = {}
                n.dflow_meta["test_runner"] = callee_name
                n.dflow_meta["test_name"] = test_name
                ctx.seen_test_slugs.add(slug)
                return
        return

    # Inline form: call_expression matching the HOF wrapper shape.
    if body_arg.type == "call_expression" and _is_hof_wrapper(body_arg, source):
        synthetic_var_name = f"test::{slug}"
        # Emit the wrapped-function atomic_process node (consistent with the
        # const-form path; gives the `annotates` edge a target).
        func_id = make_func(synthetic_var_name, body_arg, ctx.module_id, None)
        if func_id is None:
            return
        envelope_id = _process_hof_envelope(
            ctx=ctx,
            var_name=synthetic_var_name,
            outer_call=body_arg,
            func_id=func_id,
            test_runner=callee_name,
            test_name=test_name,
        )
        if envelope_id is not None:
            ctx.seen_test_slugs.add(slug)
        return

    # Other shapes (plain function, member expression, etc.): silent.
    return


def _iter_test_runner_calls(stmt_node: Any, source: bytes):
    """Yield ``(call_node, callee_name)`` for test-runner calls in a statement.

    Looks at top-level ``expression_statement → call_expression`` and
    descends one level into ``describe('...', () => { ... })`` block
    callbacks (per the v1 scope -- single level of describe nesting).
    """
    # Direct: expression_statement → call_expression(test|it, ...)
    call_node = None
    if stmt_node.type == "expression_statement":
        for c in stmt_node.children:
            if c.type == "call_expression":
                call_node = c
                break
    elif stmt_node.type == "call_expression":
        call_node = stmt_node
    if call_node is None:
        return

    callee = None
    for c in call_node.children:
        if c.type == "identifier":
            callee = _node_text(c, source)
            break
    if callee is None:
        return

    if callee in _TEST_RUNNER_NAMES:
        yield (call_node, callee)
        return

    # Describe block: descend one level into the arrow body looking for
    # nested test()/it() calls.  Do not recurse further.
    if callee in _DESCRIBE_NAMES:
        args_node = None
        for c in call_node.children:
            if c.type == "arguments":
                args_node = c
                break
        if args_node is None:
            return
        # Find the arrow_function / function_expression callback.
        cb = None
        for c in args_node.children:
            if c.type in ("arrow_function", "function_expression"):
                cb = c
                break
        if cb is None:
            return
        body = None
        for c in cb.children:
            if c.type == "statement_block":
                body = c
                break
        if body is None:
            return
        for inner_stmt in body.children:
            if inner_stmt.type != "expression_statement":
                continue
            inner_call = None
            for c in inner_stmt.children:
                if c.type == "call_expression":
                    inner_call = c
                    break
            if inner_call is None:
                continue
            inner_callee = None
            for c in inner_call.children:
                if c.type == "identifier":
                    inner_callee = _node_text(c, source)
                    break
            if inner_callee in _TEST_RUNNER_NAMES:
                yield (inner_call, inner_callee)


def _is_hof_wrapper(call_node: Any, source: bytes) -> bool:
    """Check if a call_expression is a workflow()/task() HOF wrapper.

    Pattern: ``workflow({...})(fn)`` or ``task({...})(fn)``
    The outer call_expression's function is itself a call_expression
    whose function is an identifier named 'workflow' or 'task'.
    """
    # call_node is the outer call: wrapper(opts)(fn)
    # Its first child should be another call_expression: wrapper(opts)
    inner_call = None
    for child in call_node.children:
        if child.type == "call_expression":
            inner_call = child
            break
    if inner_call is None:
        return False

    # The inner call's function should be 'workflow' or 'task'
    for child in inner_call.children:
        if child.type == "identifier":
            name = _node_text(child, source)
            return name in ("workflow", "task")
    return False


def _process_class(
    class_node: Any,
    source: bytes,
    module_id: str,
    rel_path: str,
    nodes: list[AxiomNode],
    edges: list[AxiomEdge],
    make_func: Any,
) -> None:
    """Extract method nodes from a class declaration."""
    class_name = None
    class_body = None
    for child in class_node.children:
        if child.type in ("type_identifier", "identifier"):
            class_name = _node_text(child, source)
        elif child.type == "class_body":
            class_body = child

    if class_name is None or class_body is None:
        return

    for member in class_body.children:
        if member.type == "method_definition":
            method_name = None
            for c in member.children:
                if c.type == "property_identifier":
                    method_name = _node_text(c, source)
                    break
            if method_name:
                full_name = f"{class_name}.{method_name}"
                jsdoc = _extract_jsdoc(member, source)
                make_func(full_name, member, module_id, jsdoc)


def _process_object_methods(
    obj_node: Any,
    source: bytes,
    module_id: str,
    rel_path: str,
    nodes: list[AxiomNode],
    edges: list[AxiomEdge],
    make_func: Any,
    obj_name: str | None = None,
) -> None:
    """Extract method shorthand nodes from an object literal."""
    for child in obj_node.children:
        if child.type == "method_definition":
            method_name = None
            for c in child.children:
                if c.type == "property_identifier":
                    method_name = _node_text(c, source)
                    break
            if method_name:
                full_name = f"{obj_name}.{method_name}" if obj_name else method_name
                jsdoc = _extract_jsdoc(child, source)
                make_func(full_name, child, module_id, jsdoc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def scan_js_module(
    file_path: Path,
    project_root: Path,
    project_id: str,
    *,
    findings_out: list | None = None,
    autosteps_out: list | None = None,
    is_rule_enabled=None,
) -> tuple[list[AxiomNode], list[AxiomEdge]]:
    """Scan a single JS/TS file and return (nodes, edges).

    Produces one composite_process node for the module, one atomic_process
    node per function/method, plus composes and depends_on edges.  When
    functions are wrapped with the ``workflow(opts)(fn)`` / ``task(opts)(fn)``
    HOFs, an envelope ``composite_process`` node is emitted along with
    ``Step`` / ``AutoStep`` step nodes from the body.

    Args:
        file_path: Absolute path to the JS/TS file.
        project_root: Absolute path to the project root.
        project_id: Short identifier used as namespace prefix.
        findings_out: Optional list to which validation findings are
            appended (envelope/step rules + strict-literal contract
            violations).
        autosteps_out: Optional list to which ``AutoStepRecord`` entries
            are appended for the deferred B4 pass.
        is_rule_enabled: Callable ``(rule_id) -> bool`` used to filter
            findings.  When None, all rules are enabled.

    Returns:
        Tuple of (nodes, edges) discovered in the file.

    Raises:
        RuntimeError: If tree-sitter is not installed.
    """
    if not HAS_TREE_SITTER:
        raise RuntimeError("tree-sitter is required for JS/TS scanning. Install with: pip install axiom-graph[js]")

    source_bytes = file_path.read_bytes()
    source_text = source_bytes.decode("utf-8", errors="replace")
    rel_path = file_path.relative_to(project_root).as_posix()
    dotpath = _rel_path_to_dotpath(rel_path)
    module_id = f"{project_id}::{dotpath}"
    file_mtime = file_path.stat().st_mtime

    suffix = file_path.suffix
    lang = _get_language(suffix)
    parser = Parser(lang)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error:
        logger.warning("js_scanner: parse errors in %s", rel_path)

    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []

    # -----------------------------------------------------------------------
    # Module node
    # -----------------------------------------------------------------------
    module_name = dotpath.split(".")[-1]
    # Try to extract a leading comment as module doc
    module_doc = None
    if root.children and root.children[0].type == "comment":
        first_comment = _node_text(root.children[0], source_bytes)
        if first_comment.startswith("//"):
            # Single-line or multi-line // comments at the top
            lines = []
            for child in root.children:
                if child.type != "comment":
                    break
                ctext = _node_text(child, source_bytes)
                if ctext.startswith("//"):
                    line = ctext[2:].strip()
                    # Skip decoration lines
                    if line and not all(c in "=-_" for c in line):
                        lines.append(line)
            if lines:
                module_doc = " ".join(lines)

    module_node = AxiomNode(
        id=module_id,
        node_type="composite_process",
        subtype="module",
        title=module_name,
        location=rel_path,
        source="tree_sitter",
        code_hash=hash16(source_text),
        level_0=module_name,
        level_1=_first_sentence(module_doc) if module_doc else f"{module_name} module",
        level_2=module_doc,
        level_3_location=rel_path,
        desc_hash=hash16(module_doc) if module_doc else None,
        file_mtime=file_mtime,
    )
    nodes.append(module_node)

    # -----------------------------------------------------------------------
    # Import analysis
    # -----------------------------------------------------------------------
    name_map, import_edges = _extract_imports(root, source_bytes, file_path, project_root, project_id, module_id)
    edges.extend(import_edges)

    # -----------------------------------------------------------------------
    # Function extraction (envelope / step extraction wired through ctx)
    # -----------------------------------------------------------------------
    ctx = _ScanContext(
        source=source_bytes,
        module_id=module_id,
        rel_path=rel_path,
        name_map=name_map,
        findings_out=findings_out,
        autosteps_out=autosteps_out,
        is_rule_enabled=is_rule_enabled,
        nodes=nodes,
        edges=edges,
    )
    _extract_functions(root, source_bytes, module_id, rel_path, ctx=ctx)

    # Deduplicate edges by id
    seen: set[str] = set()
    unique_edges: list[AxiomEdge] = []
    for e in edges:
        if e.id not in seen:
            seen.add(e.id)
            unique_edges.append(e)

    return nodes, unique_edges
