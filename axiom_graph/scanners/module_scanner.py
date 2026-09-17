"""Cortex module scanner — Python AST → AxiomNode + AxiomEdge objects.

Entry point:
    scan_module(file_path, project_root, project_id)
        -> tuple[list[AxiomNode], list[AxiomEdge]]

No imports of the scanned project. Pure stdlib ast + hashlib.
"""

from __future__ import annotations

import ast
import logging
import re
import sys
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

import docstring_parser

from axiom_annotations import task, Step

from axiom_graph.models import AxiomEdge, AxiomNode, StepMarker, hash16, make_edge
from axiom_graph.scanners._step_helpers import (
    build_envelope_node as _shared_build_envelope_node,
    build_step_levels,
    build_step_meta,
    build_step_node,
    parse_step_num,
    resolve_call_target_via_name_map,
    step_id_for,
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _split_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, str | None]:
    """Return (code_text, docstring).

    code_text = ast.unparse of signature + body minus docstring, with
    ``@workflow(...)`` and ``@task(...)`` annotation decorators stripped so
    that editing decorator kwargs does NOT flip the function's code_hash.
    All other decorators (``@lru_cache``, ``@classmethod``,
    ``@staticmethod``, ``@property``, ``@dataclass``, user decorators) are
    preserved.  Normalised by ``ast.unparse`` — reformatting does not trigger
    drift.  Includes: name, parameters, annotations, return type, non-
    annotation decorators.
    """
    body = list(node.body)
    docstring: str | None = None
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        docstring = body[0].value.value
        body = body[1:]

    # Strip @workflow(...) / @task(...) decorators from hash input.  The
    # envelope node carries those kwargs as its own code_hash; keeping them
    # in the function hash would contaminate F's signal.
    filtered_decorators = [
        dec
        for dec in node.decorator_list
        if _decorator_name(dec.func if isinstance(dec, ast.Call) else dec) not in _DFLOW_DECORATOR_NAMES
    ]

    stripped = ast.FunctionDef(
        name=node.name,
        args=node.args,
        body=body or [ast.Pass()],
        decorator_list=filtered_decorators,
        returns=node.returns,
        lineno=node.lineno,
        col_offset=node.col_offset,
    )
    ast.fix_missing_locations(stripped)
    return ast.unparse(stripped), docstring


def _split_module(tree: ast.Module, module_doc: str | None) -> str:
    """Return ast.unparse of all non-docstring top-level statements."""
    body = list(tree.body)
    if module_doc and body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    return ast.unparse(mod)


@task(
    purpose="Parse a single .py file via AST, extract functions/classes/modules with code and desc hashes, detect dFlow decorators, and generate composes/depends_on/validates edges",
    inputs="file_path, project_root, project_id",
    outputs="Tuple of (list[AxiomNode], list[AxiomEdge])",
)
def scan_module(
    file_path: Path,
    project_root: Path,
    project_id: str,
    findings_out: list | None = None,
    autosteps_out: list | None = None,
    is_rule_enabled=None,
) -> tuple[list[AxiomNode], list[AxiomEdge]]:
    """Scan a single .py file and return (nodes, edges).

    Produces one composite_process node for the module, one atomic_process
    node per function/method at every nesting level, plus composes and
    depends_on edges.
    """
    口 = Step(
        step_num=1,
        name="Read and parse AST",
        purpose="Read file text, compute relative path and dotpath, parse Python AST",
    )
    # Sample the mtime BEFORE reading the bytes.  A write landing between the
    # two then means the index holds the new bytes under the old mtime, so the
    # next build re-scans redundantly; sampling after the read would mean the
    # index holds the old bytes under the new mtime and the file is skipped
    # permanently.
    file_mtime = file_path.stat().st_mtime
    source = file_path.read_text(encoding="utf-8", errors="replace")
    source_lines = source.splitlines()
    rel_path = file_path.relative_to(project_root).as_posix()
    dotpath = _rel_path_to_dotpath(rel_path)
    module_id = f"{project_id}::{dotpath}"

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        # Unparseable file — emit a minimal stub node so it appears in the index
        node = AxiomNode(
            id=module_id,
            node_type="composite_process",
            subtype="module",
            title=dotpath.split(".")[-1],
            location=rel_path,
            source="ast",
            code_hash=hash16(source),
            level_0=dotpath.split(".")[-1],
            level_1=f"{dotpath.split('.')[-1]} module (syntax error — not parsed)",
            file_mtime=file_mtime,
        )
        return [node], []

    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []

    口 = Step(
        step_num=2,
        name="Extract module node",
        purpose="Create composite_process node for the module with code_hash, desc_hash, and tags",
        outputs="module_node appended to nodes list",
    )
    # -----------------------------------------------------------------------
    # Module node
    # -----------------------------------------------------------------------
    module_doc = ast.get_docstring(tree)
    module_name = dotpath.split(".")[-1]
    module_tags: list[str] = []
    # Tag test modules
    basename = rel_path.split("/")[-1]
    if basename.startswith("test_") or basename.endswith("_test.py"):
        module_tags.append("test")
    # Tag entrypoint modules (has if __name__ == "__main__" block)
    for stmt in ast.iter_child_nodes(tree):
        if isinstance(stmt, ast.If):
            test = stmt.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            ):
                module_tags.append("entrypoint")
                break

    module_node = AxiomNode(
        id=module_id,
        node_type="composite_process",
        subtype="module",
        title=module_name,
        location=rel_path,
        source="ast",
        # Whole-file content hash (parity with DocJSON / JS-TS module nodes and
        # the syntax-error stub above).  Lets the staleness content gate compare
        # a fresh hash16(read_text(...)) against this without re-parsing the AST.
        code_hash=hash16(source),
        level_0=module_name,
        level_1=_first_sentence(module_doc) if module_doc else f"{module_name} module",
        level_2=module_doc,
        level_3_location=rel_path,
        desc_hash=hash16(module_doc) if module_doc else None,
        file_mtime=file_mtime,
        tags=module_tags,
    )
    nodes.append(module_node)

    口 = Step(
        step_num=3,
        name="Module-level import analysis and name_map",
        purpose="Walk the imports that are direct children of the module to build name_map (bound name → module node ID) and emit module-level depends_on edges",
        outputs="name_map dict, external_pkg_ids set, depends_on edges appended — an edge to a module this one re-exports from carries a marker (star, and/or bound name → original name) for the build-time resolver",
    )
    # -----------------------------------------------------------------------
    # Import analysis — statements that are direct children of the module
    # -----------------------------------------------------------------------
    # current_package_parts: used for resolving relative imports.
    # name_map: bound name → intra-project module node_id.
    #   Only contains names whose import resolves to a file under project_root.
    #   stdlib, third-party, and unresolvable imports are excluded because
    #   _resolve_import returns None for anything outside the project.
    # Both module-level depends_on edges and name_map are built in one pass
    # so name_map is available when _collect_functions emits function-level edges.
    # Inside a package's own __init__.py the package IS this module, so a
    # relative import anchors on it rather than on its parent — which is how
    # Python resolves it.  A project-root __init__.py (dotpath "__init__")
    # has no package above it and anchors on the root.
    if basename == "__init__.py":
        current_package_parts = [] if dotpath == "__init__" else dotpath.split(".")
    else:
        current_package_parts = dotpath.split(".")[:-1]  # e.g. ["pm"] for pm/cli.py
    # name_map: bound_name -> (module_node_id, original_name_or_None)
    # original_name is set for "from X import Y" bindings so we can resolve
    # Y to a function-level node ID. It is None for whole-module bindings —
    # "import X" and "from pkg import submodule" alike — where the function
    # name comes from attribute access (pm.method.execute → "execute").
    module_imports = [n for n in ast.iter_child_nodes(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    module_scan = _scan_imports(
        module_imports,
        project_root=project_root,
        project_id=project_id,
        module_id=module_id,
        package_parts=current_package_parts,
    )
    name_map: dict[str, tuple[str, str | None]] = dict(module_scan.bindings)
    binding_spellings: dict[str, str] = dict(module_scan.spellings)
    external_pkg_ids: set[str] = set(module_scan.external_ids)

    # A module-level import makes the imported name an attribute of this
    # module, so every one is a re-export as far as a caller routed through
    # this module is concerned.  The edge to a module this one re-exports
    # from carries a durable marker so the build-time resolver can follow
    # the relation later; the edge itself is an ordinary dependency.
    named_reexports = _named_reexports(module_scan.bindings)
    for target_id in sorted(module_scan.module_targets):
        meta = _reexport_meta(target_id in module_scan.star_sources, named_reexports.get(target_id))
        edges.append(make_edge("depends_on", module_id, target_id, meta=meta))
    for ext_id in sorted(module_scan.external_ids):
        edges.append(make_edge("depends_on", module_id, ext_id))

    口 = Step(
        step_num=4,
        name="Deferred import union",
        purpose="Walk every import that is NOT a direct child of the module — function body, class body, nested def, guarded block — and record the dependencies the module-level pass did not already carry",
        inputs="the module AST and the module-level pass's target sets",
        outputs="depends_on edges appended for deferred-only targets, external_pkg_ids extended",
        critical="A flattened whole-file union, deliberately not a scoped one: module dependency is a property of the file. Per-function binding scope is a separate concern handled in _collect_functions, where it decides delegate targets.",
    )
    # ------------------------------------------------------------------
    # Deferred imports — every import that is NOT a direct child of the
    # module: inside a function body, a class body, a nested def, or a
    # guarded block.  A deferred import is the standard remedy for a
    # circular import or a slow module load, and the module genuinely
    # depends on what it imports there.  Flattened deliberately: module
    # dependency is a whole-file property, so every scope contributes to
    # one union.  Per-function *binding* scope is handled separately in
    # ``_collect_functions``, where it is load-bearing for delegation.
    # ------------------------------------------------------------------
    module_import_ids = {id(n) for n in module_imports}
    deferred_imports = [
        n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) and id(n) not in module_import_ids
    ]
    if deferred_imports:
        deferred_scan = _scan_imports(
            deferred_imports,
            project_root=project_root,
            project_id=project_id,
            module_id=module_id,
            package_parts=current_package_parts,
        )
        for target_id in sorted(deferred_scan.module_targets - module_scan.module_targets):
            edges.append(make_edge("depends_on", module_id, target_id))
        for ext_id in sorted(deferred_scan.external_ids - module_scan.external_ids):
            external_pkg_ids.add(ext_id)
            edges.append(make_edge("depends_on", module_id, ext_id))

    口 = Step(
        step_num=5,
        name="Collect functions and edges",
        purpose="Recursively extract function/method nodes at all nesting levels; emit composes, depends_on, and validates edges via AST call graph",
        outputs="Function nodes and edges appended, external package stubs created",
    )
    # local_func_ids: bare name → node id for module-level functions, so an
    # AutoStep that delegates to a function defined in the *same file* (not just
    # an imported one) resolves to a delegates_to edge.  Built as a whole-module
    # pre-pass over top-level defs so forward references resolve too — a workflow
    # may delegate to a function defined later in the file.
    local_func_ids: dict[str, str] = {
        child.name: f"{module_id}::{child.name}"
        for child in ast.iter_child_nodes(tree)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    # -----------------------------------------------------------------------
    # Function nodes (all nesting levels)
    # -----------------------------------------------------------------------
    _collect_functions(
        tree=tree,
        source_lines=source_lines,
        rel_path=rel_path,
        module_id=module_id,
        dotpath=dotpath,
        project_id=project_id,
        nodes=nodes,
        edges=edges,
        parent_id=module_id,
        name_prefix="",
        top_level=True,
        name_map=name_map,
        local_func_ids=local_func_ids,
        findings_out=findings_out,
        autosteps_out=autosteps_out,
        is_rule_enabled=is_rule_enabled,
        project_root=project_root,
        package_parts=current_package_parts,
        binding_spellings=binding_spellings,
    )

    # Emit stub nodes for external packages and tag the module
    if external_pkg_ids:
        module_node.tags.append("has_external_deps")
        for ext_id in sorted(external_pkg_ids):
            pkg_name = ext_id.split("::")[-1]
            nodes.append(_make_external_node(ext_id, pkg_name))

    # Deduplicate edges by id.  When one target is reached both through a
    # spelled path and through an unspelled chain, the spelled form wins: it
    # is not a guess, so the resolver may treat it as any other target.
    seen: dict[str, int] = {}
    unique_edges: list[AxiomEdge] = []
    for e in edges:
        if e.id not in seen:
            seen[e.id] = len(unique_edges)
            unique_edges.append(e)
        elif (unique_edges[seen[e.id]].meta or {}).get(UNSPELLED_CHAIN_KEY) and not (e.meta or {}).get(
            UNSPELLED_CHAIN_KEY
        ):
            unique_edges[seen[e.id]] = e

    return nodes, unique_edges


# ---------------------------------------------------------------------------
# Import scanning
# ---------------------------------------------------------------------------


class _ImportScan(NamedTuple):
    """Everything one set of import statements contributes to the index.

    Attributes:
        bindings: ``{bound_name: (module_node_id, original_name_or_None)}``.
            ``original_name`` is None for whole-module bindings.
        module_targets: Intra-project module node ids this scope depends on.
        external_ids: External package stub ids this scope depends on.
        star_sources: Subset of ``module_targets`` this scope re-exports
            from via ``from X import *``.
        spellings: ``{bound_name: dotted_path}`` for bindings that spell a
            path below the bound name — ``import a.b`` binds ``a`` to the
            ``a.b`` module and spells ``b``.  Every other binding spells
            nothing and is absent.
    """

    bindings: dict[str, tuple[str, str | None]]
    module_targets: set[str]
    external_ids: set[str]
    star_sources: set[str]
    spellings: dict[str, str]


# Statements that open a new binding scope.  An import inside one binds
# there, not in the scope being walked.
_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _iter_scope_imports(node: ast.AST):
    """Yield the import statements that bind names in *node*'s own scope.

    Descends through block statements that do not open a scope — ``if``,
    ``try``, ``for``, ``with``, ``match`` — so a guarded or lazily-taken
    import still counts, and stops at nested ``def`` / ``class`` bodies,
    whose imports bind in a scope of their own.

    Args:
        node: The AST node whose scope to walk (usually a FunctionDef).

    Yields:
        ``ast.Import`` / ``ast.ImportFrom`` statements in that scope.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            yield child
        elif isinstance(child, _SCOPE_BOUNDARIES):
            continue
        elif isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
            yield from _iter_scope_imports(child)


def _scan_imports(
    import_nodes,
    *,
    project_root: Path,
    project_id: str,
    module_id: str,
    package_parts: list[str],
) -> _ImportScan:
    """Resolve a set of import statements into bindings and dependency targets.

    Shared by the module-level walk, the deferred-import union, and the
    per-function binding overlay, so all three agree on what an import
    means.  Names that resolve outside ``project_root`` are excluded from
    the bindings and reported as external package stubs instead.

    ``from pkg import name`` binds ``name`` to the **submodule** when
    ``pkg/name.py`` (or ``pkg/name/__init__.py``) exists on disk: the
    receiver of a later ``name.f()`` is that module, not the package.  The
    package edge is kept alongside the submodule edge — importing a
    submodule does execute its package.

    Args:
        import_nodes: Iterable of ``ast.Import`` / ``ast.ImportFrom``.
        project_root: Absolute path to the project being scanned.
        project_id: Project namespace prefix for node ids.
        module_id: Node id of the module being scanned (self-imports are
            skipped).
        package_parts: Dotted-path components of the enclosing package,
            used to resolve relative imports.

    Returns:
        An ``_ImportScan``.
    """
    bindings: dict[str, tuple[str, str | None]] = {}
    module_targets: set[str] = set()
    external_ids: set[str] = set()
    star_sources: set[str] = set()
    spellings: dict[str, str] = {}

    for imp_node in import_nodes:
        if isinstance(imp_node, ast.Import):
            for alias in imp_node.names:
                target_id = _resolve_import(alias.name, project_root, project_id)
                if target_id and target_id != module_id:
                    bound = alias.asname or alias.name.split(".")[0]
                    bindings[bound] = (target_id, None)  # whole-module binding
                    module_targets.add(target_id)
                    # `import a.b` binds `a` to the a.b module and spells `b`;
                    # an aliased import spells nothing below its alias.
                    if alias.asname is None and "." in alias.name:
                        spellings[bound] = alias.name.split(".", 1)[1]
                    else:
                        spellings.pop(bound, None)
                elif target_id is None:
                    ext_id = _external_node_id(alias.name, project_id)
                    if ext_id:
                        external_ids.add(ext_id)
            continue

        if not isinstance(imp_node, ast.ImportFrom):
            continue

        level = imp_node.level or 0
        mod = imp_node.module or ""
        if level > 0:
            base_parts = package_parts[: len(package_parts) - (level - 1)]
            resolved_name = ".".join(base_parts + [mod]) if mod else ".".join(base_parts)
        else:
            resolved_name = mod
        if not resolved_name:
            continue

        target_id = _resolve_import(resolved_name, project_root, project_id)
        if target_id is None:
            ext_id = _external_node_id(resolved_name, project_id)
            if ext_id:
                external_ids.add(ext_id)
            continue
        if target_id != module_id:
            module_targets.add(target_id)
        for alias in imp_node.names:
            if alias.name == "*":
                if target_id != module_id:
                    star_sources.add(target_id)
                continue
            bound = alias.asname or alias.name
            submodule_id = _resolve_import(f"{resolved_name}.{alias.name}", project_root, project_id)
            if submodule_id and submodule_id != module_id:
                # The imported name IS a module — bind it as a namespace so
                # `name.f()` resolves inside the file that defines `f`.
                bindings[bound] = (submodule_id, None)
                spellings.pop(bound, None)
                module_targets.add(submodule_id)
            elif submodule_id is None and target_id != module_id:
                # alias.name is the original name in the source module, so a
                # function-level node id can be constructed from it later.
                bindings[bound] = (target_id, alias.name)
                spellings.pop(bound, None)

    return _ImportScan(bindings, module_targets, external_ids, star_sources, spellings)


def _attribute_target_id(binding: tuple[str, str | None], attr_name: str) -> str:
    """Return the node id an attribute call on a bound name resolves to.

    A namespace binding (``import mod``, ``from pkg import submodule``)
    makes ``mod.f()`` a module-level function of ``mod``.  A named import
    (``from mod import Thing``) makes ``Thing.f()`` a member of ``Thing``,
    which the index stores under the qualified name ``Thing.f``.

    Args:
        binding: ``(module_node_id, original_name_or_None)`` from a name_map.
        attr_name: The attribute being called.

    Returns:
        The resolved node id.
    """
    mod_id, original = binding
    if original is None:
        return f"{mod_id}::{attr_name}"
    return f"{mod_id}::{original}.{attr_name}"


# Edge-meta keys read by the build-time link resolver.  ``REEXPORT_STAR_KEY``
# keeps the shape earlier releases wrote and read (any truthy value marks a
# star source).  Named bindings live under a key of their own, so a reader
# that predates them sees nothing on a named-only edge instead of a star
# source it would over-resolve through.
REEXPORT_STAR_KEY = "reexport"
REEXPORT_NAMES_KEY = "reexport_names"
# Marks a link target built by dropping attribute-chain components the import
# binding never spelled, e.g. ``import pkg; pkg.sub.func()`` → ``pkg::func``.
UNSPELLED_CHAIN_KEY = "unspelled_chain"


def _named_reexports(bindings: dict[str, tuple[str, str | None]]) -> dict[str, dict[str, str]]:
    """Group a scope's named bindings by the module each one came from.

    Args:
        bindings: ``{bound_name: (module_node_id, original_name_or_None)}``.
            Whole-module bindings (original None) are namespace bindings,
            not re-exported names, and are left out.

    Returns:
        ``{source_module_id: {bound_name: original_name}}``.
    """
    grouped: dict[str, dict[str, str]] = {}
    for bound, (source_id, original) in bindings.items():
        if original is not None:
            grouped.setdefault(source_id, {})[bound] = original
    return grouped


def _reexport_meta(is_star: bool, names: dict[str, str] | None) -> dict | None:
    """Return the re-export marker for one module-level dependency edge.

    Args:
        is_star: Whether the module star-imports the edge's target.
        names: ``{bound_name: original_name}`` the module binds from the
            target, or None.

    Returns:
        The marker dict, or None when the edge re-exports nothing.
    """
    meta: dict = {}
    if is_star:
        meta[REEXPORT_STAR_KEY] = "star"
    if names:
        meta[REEXPORT_NAMES_KEY] = dict(sorted(names.items()))
    return meta or None


def _next_call(stmt: ast.stmt) -> ast.Call | None:
    """Return the call a statement makes directly, if it makes one.

    Args:
        stmt: A statement following an AutoStep marker.

    Returns:
        The ``ast.Call`` for ``f()``, ``x = f()``, ``x: T = f()`` or
        ``return f()``, else None.
    """
    if isinstance(stmt, (ast.Assign, ast.Expr, ast.AnnAssign, ast.Return)) and isinstance(stmt.value, ast.Call):
        return stmt.value
    return None


def _unspelled_chain(func_ref: ast.Attribute, spellings: dict[str, str]) -> bool:
    """Return whether an attribute call's middle chain differs from its binding's spelling.

    The target of ``root.a.b.attr()`` is built from ``root``'s binding and
    ``attr`` alone, so the components between them are dropped.  That is
    only sound when they are exactly what the binding spelled.

    Args:
        func_ref: The called attribute node.
        spellings: ``{bound_name: dotted_path}`` for the scope.

    Returns:
        True when the dropped components are not the binding's spelling.
    """
    middle: list[str] = []
    node = func_ref.value
    while isinstance(node, ast.Attribute):
        middle.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return False
    return ".".join(reversed(middle)) != spellings.get(node.id, "")


def _chain_meta(func_ref: ast.Attribute, spellings: dict[str, str]) -> dict | None:
    """Return the edge meta for an attribute-built link target.

    Args:
        func_ref: The called attribute node.
        spellings: ``{bound_name: dotted_path}`` for the scope.

    Returns:
        ``{UNSPELLED_CHAIN_KEY: True}`` for an unspelled chain, else None.
    """
    return {UNSPELLED_CHAIN_KEY: True} if _unspelled_chain(func_ref, spellings) else None


def _next_call_chain_meta(
    stmt: ast.stmt,
    name_map: dict[str, tuple[str, str | None]],
    spellings: dict[str, str],
) -> dict | None:
    """Return the edge meta for the delegate target of the statement after an AutoStep.

    Only a target built from an import binding can be a guess; a bare call
    or a ``self`` / ``cls`` call carries no marker.

    Args:
        stmt: The statement following the AutoStep.
        name_map: The scope's import bindings.
        spellings: ``{bound_name: dotted_path}`` for the scope.

    Returns:
        ``{UNSPELLED_CHAIN_KEY: True}`` for an unspelled chain, else None.
    """
    call = _next_call(stmt)
    if call is None or not isinstance(call.func, ast.Attribute):
        return None
    root = call.func.value
    while isinstance(root, ast.Attribute):
        root = root.value
    if not (isinstance(root, ast.Name) and root.id in name_map):
        return None
    return _chain_meta(call.func, spellings)


# ---------------------------------------------------------------------------
# Recursive function collector
# ---------------------------------------------------------------------------


def _collect_functions(
    tree: ast.AST,
    source_lines: list[str],
    rel_path: str,
    module_id: str,
    dotpath: str,
    project_id: str,
    nodes: list[AxiomNode],
    edges: list[AxiomEdge],
    parent_id: str,
    name_prefix: str,
    top_level: bool,
    name_map: dict[str, tuple[str, str | None]] | None = None,
    local_func_ids: dict[str, str] | None = None,
    findings_out: list | None = None,
    autosteps_out: list | None = None,
    is_rule_enabled=None,
    project_root: Path | None = None,
    package_parts: list[str] | None = None,
    class_name: str | None = None,
    binding_spellings: dict[str, str] | None = None,
) -> None:
    """Walk direct children of tree for FunctionDef / AsyncFunctionDef.

    ``binding_spellings`` travels with ``name_map`` and is overlaid the same
    way, so a local import that rebinds a name also replaces what that name
    spells.  It decides whether an attribute-built link target is marked as
    an unspelled chain.

    Imports declared inside a function body bind in that function's scope
    only, so each function is walked with its own binding overlay layered
    over the enclosing one.  A local binding shadows an enclosing one of
    the same name, which is what Python itself does — the reader of a
    function body sees the local import and expects it to win.

    ``class_name`` names the class whose body is being walked, or None at
    module level.  It qualifies ``self`` / ``cls`` delegate targets and is
    deliberately not derived from ``name_prefix``: inside a method that
    prefix names the enclosing *function*, while a closure defined there is
    still a member of the same class.
    """
    if name_map is None:
        name_map = {}
    for child in ast.iter_child_nodes(tree):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Per-function binding overlay.  Delegation is not scope-free: an
        # import inside one function must not resolve a call in another.
        scope_name_map = name_map
        scope_spellings = binding_spellings or {}
        if project_root is not None:
            local_scan = _scan_imports(
                _iter_scope_imports(child),
                project_root=project_root,
                project_id=project_id,
                module_id=module_id,
                package_parts=package_parts or [],
            )
            if local_scan.bindings:
                scope_name_map = {**name_map, **local_scan.bindings}
                scope_spellings = {k: v for k, v in scope_spellings.items() if k not in local_scan.bindings}
                scope_spellings.update(local_scan.spellings)

        qualified_name = f"{name_prefix}{child.name}" if name_prefix else child.name
        func_id = f"{project_id}::{dotpath}::{qualified_name}"

        code_text, docstring = _split_function(child)
        code_hash = hash16(code_text)
        sig = _build_signature(child)

        level_1 = sig
        if docstring:
            first = _first_sentence(docstring)
            if first:
                level_1 = f"{sig} — {first}"

        start_line = child.decorator_list[0].lineno if child.decorator_list else child.lineno
        level_3 = f"{rel_path}#L{start_line}-L{child.end_lineno}"

        raises = _extract_raises(child)
        func_tags = _extract_tags(child, rel_path)
        autodoc = _parse_docstring_meta(docstring, raises)
        dflow_dec = _extract_dflow_meta(child)
        if dflow_dec:
            autodoc = {**autodoc, **dflow_dec} if autodoc else dflow_dec

        # Append raises to level_2 so FTS can find "what raises X?"
        level_2_text = docstring or ""
        if raises:
            raises_line = "Raises: " + ", ".join(raises)
            level_2_text = (level_2_text + "\n\n" + raises_line).strip() if level_2_text else raises_line

        func_subtype = "test" if "test" in func_tags else "function"

        func_node = AxiomNode(
            id=func_id,
            node_type="atomic_process",
            subtype=func_subtype,
            title=qualified_name,
            location=rel_path,
            source="ast",
            code_hash=code_hash,
            level_0=qualified_name,
            level_1=level_1,
            level_2=level_2_text or None,
            level_3_location=level_3,
            level_steps=_extract_steps(child),
            desc_hash=hash16(docstring) if docstring else None,
            file_mtime=None,
            tags=func_tags,
            dflow_meta=autodoc,
        )
        nodes.append(func_node)

        # composes edge: parent → this function
        edge_type = "composes"
        edges.append(make_edge(edge_type, parent_id, func_id))

        # Envelope + step nodes when the function carries @workflow / @task.
        # These are emitted directly from the AST walk — no cross-DB read.
        if dflow_dec is not None:
            envelope_node, envelope_edges, envelope_id = _build_envelope_node(
                child,
                dflow_dec=dflow_dec,
                func_id=func_id,
                rel_path=rel_path,
                module_id=module_id,
            )
            nodes.append(envelope_node)
            edges.extend(envelope_edges)
            step_nodes, step_edges = _extract_step_nodes(
                child,
                func_id=func_id,
                envelope_id=envelope_id,
                rel_path=rel_path,
                project_id=project_id,
                name_map=scope_name_map,
                local_func_ids=local_func_ids,
                findings_out=findings_out,
                autosteps_out=autosteps_out,
                envelope_kind=dflow_dec.get("decorator", "workflow") if dflow_dec else "workflow",
                envelope_purpose=dflow_dec.get("purpose") if dflow_dec else None,
                envelope_node_id=envelope_id,
                is_rule_enabled=is_rule_enabled,
                module_id=module_id,
                class_name=class_name,
                binding_spellings=scope_spellings,
            )
            nodes.extend(step_nodes)
            edges.extend(step_edges)

        # depends_on edges: this function → intra-project modules it references.
        # Walks the full function subtree (including nested defs) so that names
        # used inside closures still count as dependencies of the enclosing function.
        # Only fires for names in name_map — stdlib/third-party are never in it.
        if scope_name_map:
            used: set[str] = set()
            for name_node in ast.walk(child):
                if isinstance(name_node, ast.Name) and name_node.id in scope_name_map:
                    used.add(scope_name_map[name_node.id][0])
                elif isinstance(name_node, ast.Attribute):
                    # db_adapter.get(...) → root is "db_adapter"
                    root = name_node.value
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name) and root.id in scope_name_map:
                        used.add(scope_name_map[root.id][0])
            for target_id in sorted(used):
                edges.append(make_edge("depends_on", func_id, target_id))

        # validates edges: test functions → directly called production functions.
        # Only emitted for test-tagged functions. Walks Call nodes (direct calls
        # only — no transitive traversal) and resolves each call against name_map.
        # Calls to stdlib/third-party are silently ignored (never in name_map).
        # Unresolvable targets (e.g. fixture-mediated calls) are silently skipped.
        if scope_name_map and "test" in func_tags:
            for call_node in ast.walk(child):
                if not isinstance(call_node, ast.Call):
                    continue
                func_ref = call_node.func
                if isinstance(func_ref, ast.Name) and func_ref.id in scope_name_map:
                    mod_id, orig_name = scope_name_map[func_ref.id]
                    if orig_name is not None:
                        # from mod import func → func(...)
                        edges.append(make_edge("validates", func_id, f"{mod_id}::{orig_name}"))
                elif isinstance(func_ref, ast.Attribute):
                    # mod.func(...) — resolve root to its binding, attr is the member
                    attr_name = func_ref.attr
                    root = func_ref.value
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name) and root.id in scope_name_map:
                        target = _attribute_target_id(scope_name_map[root.id], attr_name)
                        edges.append(
                            make_edge("validates", func_id, target, meta=_chain_meta(func_ref, scope_spellings))
                        )

        # Recurse into nested functions (classes too, for methods).  The
        # scope map goes down, not the module one: a closure sees the
        # bindings of the function that encloses it.
        _collect_functions(
            tree=child,
            source_lines=source_lines,
            rel_path=rel_path,
            module_id=module_id,
            dotpath=dotpath,
            project_id=project_id,
            nodes=nodes,
            edges=edges,
            parent_id=func_id,
            name_prefix=f"{qualified_name}.",
            top_level=False,
            name_map=scope_name_map,
            local_func_ids=local_func_ids,
            project_root=project_root,
            package_parts=package_parts,
            binding_spellings=scope_spellings,
            # A closure defined in a method is still inside the class, so the
            # class qualifier passes through the function boundary unchanged.
            class_name=class_name,
        )

    # Also descend into class bodies so methods are discovered
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, ast.ClassDef):
            _collect_functions(
                tree=child,
                source_lines=source_lines,
                rel_path=rel_path,
                module_id=module_id,
                dotpath=dotpath,
                project_id=project_id,
                nodes=nodes,
                edges=edges,
                parent_id=module_id,  # class methods compose the module
                name_prefix=f"{child.name}.",
                top_level=False,
                name_map=name_map,
                local_func_ids=local_func_ids,
                project_root=project_root,
                package_parts=package_parts,
                binding_spellings=binding_spellings,
                # Matches name_prefix above, so a self/cls target is byte-
                # identical to the id this same walk mints for the method.
                class_name=child.name,
            )


# ---------------------------------------------------------------------------
# Step marker extraction
# ---------------------------------------------------------------------------

_STEP_CALLABLES = {"Step", "AutoStep"}


def _call_inside_loop(func_node: ast.AST, target_call: ast.Call) -> bool:
    """Return True iff ``target_call`` is syntactically nested inside a for/while.

    Walks the function AST, tracking the current ``for``/``while`` depth, and
    returns True iff the given call node is reached while at least one loop
    is open on the stack.
    """
    loop_depth = 0
    found = False

    class _Visitor(ast.NodeVisitor):
        def visit_For(self, node: ast.For) -> None:
            nonlocal loop_depth
            loop_depth += 1
            self.generic_visit(node)
            loop_depth -= 1

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            nonlocal loop_depth
            loop_depth += 1
            self.generic_visit(node)
            loop_depth -= 1

        def visit_While(self, node: ast.While) -> None:
            nonlocal loop_depth
            loop_depth += 1
            self.generic_visit(node)
            loop_depth -= 1

        def visit_Call(self, node: ast.Call) -> None:
            nonlocal found
            if node is target_call and loop_depth > 0:
                found = True
            self.generic_visit(node)

    _Visitor().visit(func_node)
    return found


def _extract_steps(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[StepMarker] | None:
    """Back-compat step-marker extractor used by renderers.

    Returns a list of :class:`StepMarker` suitable for storage in the
    function node's ``level_steps`` field (old shape).  The new code path
    that promotes steps to first-class nodes is
    :func:`_extract_step_nodes`.
    """
    markers: list[StepMarker] = []

    for node in ast.walk(func_node):
        # Match:  口 = Step(...) or 口 = AutoStep(...)
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Name) and func.id in _STEP_CALLABLES):
            continue

        kwargs: dict[str, object] = {}
        for kw in call.keywords:
            if kw.arg is None:
                continue
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, TypeError):
                # Non-literal value — store string representation
                kwargs[kw.arg] = ast.unparse(kw.value)

        # step_num and name/purpose are required; be lenient and skip if missing
        step_num = kwargs.get("step_num")
        if step_num is None:
            continue
        name = kwargs.get("name", "")
        purpose = kwargs.get("purpose", "")
        if not name and not purpose:
            continue

        markers.append(
            StepMarker(
                step_num=step_num,
                name=str(name),
                purpose=str(purpose),
                inputs=str(kwargs["inputs"]) if "inputs" in kwargs else None,
                outputs=str(kwargs["outputs"]) if "outputs" in kwargs else None,
                critical=str(kwargs["critical"]) if "critical" in kwargs else None,
            )
        )

    if not markers:
        return None

    # Sort by step_num so they appear in order
    markers.sort(key=lambda m: float(m.step_num))
    return markers


def _extract_step_nodes(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    func_id: str,
    envelope_id: str,
    rel_path: str,
    project_id: str,
    name_map: dict[str, tuple[str, str | None]] | None = None,
    local_func_ids: dict[str, str] | None = None,
    findings_out: list | None = None,
    autosteps_out: list | None = None,
    envelope_kind: str = "workflow",
    envelope_purpose: str | None = None,
    envelope_node_id: str | None = None,
    is_rule_enabled=None,
    module_id: str | None = None,
    class_name: str | None = None,
    binding_spellings: dict[str, str] | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge]]:
    """Extract step/autostep nodes and their structural edges.

    Produces ``atomic_process`` nodes with subtype ``step`` or ``autostep``
    for every ``Step(...)`` / ``AutoStep(...)`` call inside ``func_node``,
    plus a ``composes`` edge from the envelope to each step.  For AutoSteps
    whose next statement is a direct call to an intra-project function —
    an imported name (``name_map``), a function defined in the same module
    (``local_func_ids``), or a sibling method reached through ``self`` /
    ``cls`` (``module_id`` + ``class_name``) — a ``delegates_to`` edge is
    emitted from the autostep to the target function node.

    Step nodes carry NO staleness dimensions (empty code_hash sentinel,
    NULL desc_hash); ``compute_staleness`` short-circuits for their
    subtype.

    Args:
        func_node: The enclosing function AST node.
        func_id: Node ID of the enclosing function.
        envelope_id: Node ID of the envelope composing this function.
        rel_path: Repo-relative source file path.
        project_id: Project namespace prefix.
        name_map: Scanner name_map for intra-project call resolution.  Each
            value is ``(module_node_id, original_name_or_None)``.
        local_func_ids: ``{func_name: node_id}`` for functions defined in the
            same module, so an AutoStep delegating to a same-file function
            resolves (not only imported ones).
        module_id: Node ID of the enclosing module, used to build the target
            ID for a ``self`` / ``cls`` delegate.
        class_name: Name of the class enclosing ``func_node``, or None when
            it is not a method.  Required for ``self`` / ``cls`` resolution.

    Returns:
        Tuple of (step_nodes, edges).  Never raises; on malformed
        ``step_num`` the first-occurrence wins the node ID and a WARNING
        is logged.
    """
    step_nodes: list[AxiomNode] = []
    step_edges: list[AxiomEdge] = []
    seen_step_ids: set[str] = set()

    # Walk statement list to reason about AutoStep → next-call pairing.
    # We need the full body-level sequencing (also inside for/while, etc.).
    # Pair an AutoStep assignment with the NEXT statement if that next
    # statement is (or contains at top level) a direct call to a name.
    for stmt_list in _iter_statement_lists(func_node):
        for i, stmt in enumerate(stmt_list):
            call = _extract_step_call(stmt)
            if call is None:
                continue
            func_ref = call.func
            if not (isinstance(func_ref, ast.Name) and func_ref.id in _STEP_CALLABLES):
                continue

            kwargs: dict[str, object] = {}
            for kw in call.keywords:
                if kw.arg is None:
                    continue
                try:
                    kwargs[kw.arg] = ast.literal_eval(kw.value)
                except (ValueError, TypeError):
                    kwargs[kw.arg] = ast.unparse(kw.value)

            step_num_val = kwargs.get("step_num")
            if step_num_val is None:
                continue
            name = kwargs.get("name", "")
            purpose = kwargs.get("purpose", "")
            # Step() requires name+purpose; AutoStep() only requires step_num.
            is_auto = func_ref.id == "AutoStep"
            if not is_auto and not name and not purpose:
                continue

            # For string literals containing dots we want to preserve the
            # authored representation; prefer re-parsing the raw source when
            # the value is a Constant node.
            step_num_raw, step_num_parts = _step_num_from_call(call)
            if not step_num_raw:
                step_num_raw, step_num_parts = parse_step_num(step_num_val)

            # Minor-in-loop validation: 2+ parts must be inside a for/while.
            if len(step_num_parts) >= 2 and not _call_inside_loop(func_node, call):
                line = getattr(call, "lineno", "?")
                logger.warning(
                    "minor step %r outside loop in %s:%s",
                    step_num_raw,
                    rel_path,
                    line,
                )

            step_id = step_id_for(func_id, step_num_raw)
            if step_id in seen_step_ids:
                line = getattr(call, "lineno", "?")
                logger.warning(
                    "duplicate step_num %r inside %s (at %s:%s) — first wins",
                    step_num_raw,
                    func_id,
                    rel_path,
                    line,
                )
                continue
            seen_step_ids.add(step_id)

            level_0, level_1 = build_step_levels(step_num_raw, name, purpose, is_auto)
            step_meta = build_step_meta(step_num_raw, step_num_parts, name, purpose, kwargs, is_auto)
            node = build_step_node(
                source="ast",
                step_id=step_id,
                is_auto=is_auto,
                level_0=level_0,
                level_1=level_1,
                purpose=purpose if purpose else None,
                rel_path=rel_path,
                line=getattr(call, "lineno", 1),
                step_meta=step_meta,
            )
            step_nodes.append(node)

            # composes: envelope → step
            step_edges.append(make_edge("composes", envelope_id, step_id))

            # delegates_to on AutoStep + next direct call to a @task function.
            # We cannot at scan time verify the target IS decorated (we'd
            # need cross-module inspection).  Emit the edge pointing at the
            # resolved function node id; Pass B in staleness walks inbound
            # `annotates` to confirm envelope-ness.
            target_id = None
            target_name = None
            has_next_call = False
            if is_auto and i + 1 < len(stmt_list):
                nxt = stmt_list[i + 1]
                _call_node = (
                    nxt.value if isinstance(nxt, ast.Expr) else (nxt.value if isinstance(nxt, ast.Assign) else None)
                )
                if isinstance(_call_node, ast.Call):
                    has_next_call = True
                    _fn = _call_node.func
                    if isinstance(_fn, ast.Name):
                        target_name = _fn.id
                    elif isinstance(_fn, ast.Attribute):
                        target_name = _fn.attr
                # A self/cls target needs no import bindings, so it resolves
                # even for a caller that passed no name_map at all.
                if name_map is not None or class_name is not None:
                    target_id = _resolve_next_call_target(
                        nxt,
                        name_map or {},
                        local_func_ids,
                        module_id=module_id,
                        class_name=class_name,
                    )
                    if target_id:
                        chain_meta = _next_call_chain_meta(nxt, name_map or {}, binding_spellings or {})
                        step_edges.append(make_edge("delegates_to", step_id, target_id, meta=chain_meta))

            # Record AutoStep for B4 deferred resolution.
            if is_auto and autosteps_out is not None:
                from axiom_graph.workflows.validation import AutoStepRecord

                autosteps_out.append(
                    AutoStepRecord(
                        module=rel_path,
                        function=func_node.name,
                        line=getattr(call, "lineno", 1),
                        step_num=step_num_val,
                        target_name=target_name,
                        target_node_id=target_id,
                        has_next_call=has_next_call,
                    )
                )

    # ----------------------------------------------------------------------
    # Run intra-envelope validation (A1-A3, B1-B3, C1) over all Step/AutoStep
    # calls in this envelope.  Findings are appended to ``findings_out``
    # if provided; otherwise this pass is skipped.
    # ----------------------------------------------------------------------
    if findings_out is not None:
        from axiom_graph.workflows.validation import validate_envelope as _validate_envelope

        step_calls: list[dict] = []
        for stmt_list in _iter_statement_lists(func_node):
            for stmt in stmt_list:
                call = _extract_step_call(stmt)
                if call is None:
                    continue
                func_ref = call.func
                if not (isinstance(func_ref, ast.Name) and func_ref.id in _STEP_CALLABLES):
                    continue
                kwargs: dict[str, object] = {}
                for kw in call.keywords:
                    if kw.arg is None:
                        continue
                    try:
                        kwargs[kw.arg] = ast.literal_eval(kw.value)
                    except (ValueError, TypeError):
                        kwargs[kw.arg] = None
                step_calls.append(
                    {
                        "call_ast": call,
                        "step_num_value": kwargs.get("step_num"),
                        "name": kwargs.get("name"),
                        "purpose": kwargs.get("purpose"),
                        "is_auto": func_ref.id == "AutoStep",
                        "line": getattr(call, "lineno", 1),
                        "in_loop": _call_inside_loop(func_node, call),
                    }
                )

        guard = is_rule_enabled if callable(is_rule_enabled) else (lambda rid: True)
        envelope_line = func_node.decorator_list[0].lineno if func_node.decorator_list else func_node.lineno
        findings = _validate_envelope(
            rel_path=rel_path,
            func_name=func_node.name,
            func_node=func_node,
            envelope_kind=envelope_kind,
            envelope_purpose=envelope_purpose,
            envelope_line=envelope_line,
            step_calls=step_calls,
            is_rule_enabled=guard,
        )
        findings_out.extend(findings)

    return step_nodes, step_edges


def _iter_statement_lists(func_node: ast.AST):
    """Yield every statement-list (body, orelse, finalbody) inside func_node.

    Walking statement lists — rather than ``ast.walk`` — lets us reason about
    "the AutoStep assignment is immediately followed by a call" without
    confusing unrelated calls elsewhere in the function.

    No visited-set is kept, deliberately.  Every list pushed below is a fresh
    copy, so two pushes can never denote the same list, and the AST is a finite
    tree the stack only ever descends — nothing can be reached twice.  An
    ``id()``-keyed guard here is not merely redundant: a yielded list is freed
    once ``body`` rebinds, CPython recycles its address for the next
    ``list(nested)``, and the guard then skips a body it has never seen.  That
    silently dropped markers declared inside loop and ``try`` bodies.
    """
    stack: list[list[ast.stmt]] = [list(getattr(func_node, "body", []) or [])]
    while stack:
        body = stack.pop()
        yield body
        for stmt in body:
            for attr in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, attr, None)
                if nested:
                    stack.append(list(nested))
            # ast.Try has handlers, each with its own body.
            handlers = getattr(stmt, "handlers", None) or []
            for h in handlers:
                nested = getattr(h, "body", None)
                if nested:
                    stack.append(list(nested))


def _extract_step_call(stmt: ast.stmt) -> ast.Call | None:
    """Return the Step()/AutoStep() call inside this statement, or None.

    Accepts ``x = Step(...)``, ``Step(...)`` (bare expression), and
    ``x: T = Step(...)``.
    """
    if isinstance(stmt, ast.Assign):
        v = stmt.value
        return v if isinstance(v, ast.Call) else None
    if isinstance(stmt, ast.Expr):
        v = stmt.value
        return v if isinstance(v, ast.Call) else None
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        v = stmt.value
        return v if isinstance(v, ast.Call) else None
    return None


def _step_num_from_call(call: ast.Call) -> tuple[str | None, list[int]]:
    """Read step_num from a Call keyword whose value is a Constant.

    Preserves authored string literals — e.g. ``step_num="1.10"`` returns
    ``("1.10", [1, 10])``.  Returns ``(None, [])`` when step_num is missing
    or non-constant.
    """
    for kw in call.keywords:
        if kw.arg != "step_num":
            continue
        if isinstance(kw.value, ast.Constant):
            v = kw.value.value
            if isinstance(v, str):
                parts: list[int] = []
                for p in v.split("."):
                    try:
                        parts.append(int(p))
                    except ValueError:
                        return (v, [0])
                return (v, parts)
            if isinstance(v, int):
                return (str(v), [v])
            if isinstance(v, float):
                raw = f"{v:g}"
                parts = []
                for p in raw.split("."):
                    try:
                        parts.append(int(p))
                    except ValueError:
                        return (raw, [0])
                return (raw, parts)
        return (None, [])
    return (None, [])


def _resolve_next_call_target(
    stmt: ast.stmt,
    name_map: dict[str, tuple[str, str | None]],
    local_func_ids: dict[str, str] | None = None,
    *,
    module_id: str | None = None,
    class_name: str | None = None,
) -> str | None:
    """Given the statement AFTER an AutoStep, find the node ID of the called function.

    Handles ``x = foo(...)``, ``foo(...)``, and ``x = obj.method(...)``
    (qualified). Returns the axiom-graph node ID of the intra-project target,
    or None if no call or the target is unresolved.

    A bare name is resolved against ``name_map`` (imports) first, then
    ``local_func_ids`` (functions defined in the same module), so an AutoStep
    delegating to a same-file function resolves as well as an imported one.

    An attribute call resolves through ``name_map`` when the receiver is an
    import binding, and through ``module_id`` + ``class_name`` when it is
    ``self`` or ``cls``.  The second form reads the class from the caller's
    walk position rather than from a lookup table, so two classes in one
    module sharing a method name each resolve to their own.
    """
    call = _next_call(stmt)
    if call is None:
        return None

    func_ref = call.func
    if isinstance(func_ref, ast.Name):
        target_id, _ = resolve_call_target_via_name_map(
            {"kind": "name", "name": func_ref.id},
            name_map,
            local_func_ids=local_func_ids,
        )
        return target_id
    if isinstance(func_ref, ast.Attribute):
        attr_name = func_ref.attr
        root = func_ref.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            # Python scanner accepts BOTH namespace and named-import attr
            # access (mod.foo() or Thing.foo()).  The shared helper
            # conservatively resolves only the namespace case, so both are
            # resolved here from the binding: a namespace receiver makes the
            # attribute a module-level function, a named one makes it a
            # member of the imported symbol.
            if root.id in name_map:
                return _attribute_target_id(name_map[root.id], attr_name)
            # Intra-class delegation.  `self` and `cls` are never imports, so
            # the target comes from the enclosing class instead — the same
            # walk that reaches this call is guaranteed to mint that method
            # node under the identical id, so this form cannot dangle.
            #
            # Tested against the DIRECT receiver, not the walked-down root: in
            # `self.collaborator.method()` the root is still `self`, but the
            # outermost attribute names a method of the collaborator, not a
            # sibling.  Pairing the two would invent an edge to a method the
            # class does not have — worse than the missing edge it replaces.
            direct = func_ref.value
            if isinstance(direct, ast.Name) and direct.id in ("self", "cls") and class_name and module_id:
                return f"{module_id}::{class_name}.{attr_name}"
    return None


def _build_envelope_node(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    dflow_dec: dict,
    func_id: str,
    rel_path: str,
    module_id: str,
) -> tuple[AxiomNode, list[AxiomEdge], str]:
    """Construct the envelope composite_process node + composes/annotates edges.

    Thin AST-aware wrapper around the language-agnostic helper: pulls
    ``func_name`` and ``start_line`` from the AST node and forwards.
    """
    decorator = dflow_dec.get("decorator", "workflow")
    kwargs = {k: v for k, v in dflow_dec.items() if k != "decorator"}
    start_line = func_node.decorator_list[0].lineno if func_node.decorator_list else func_node.lineno
    return _shared_build_envelope_node(
        source="ast",
        func_id=func_id,
        func_name=func_node.name,
        decorator_name=decorator,
        kwargs=kwargs,
        rel_path=rel_path,
        module_id=module_id,
        start_line=start_line,
    )


# ---------------------------------------------------------------------------
# Signature builder
# ---------------------------------------------------------------------------


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return 'func_name(arg1, arg2, ...)' — strips self/cls."""
    try:
        args_node = node.args
        # Build arg list manually to strip self/cls cleanly
        parts: list[str] = []

        all_args = list(args_node.posonlyargs) + list(args_node.args)
        defaults_offset = len(all_args) - len(args_node.defaults)

        for i, arg in enumerate(all_args):
            if i == 0 and arg.arg in ("self", "cls"):
                continue
            default_idx = i - defaults_offset
            if default_idx >= 0:
                default_val = ast.unparse(args_node.defaults[default_idx])
                parts.append(f"{arg.arg}={default_val}")
            else:
                parts.append(arg.arg)

        if args_node.vararg:
            parts.append(f"*{args_node.vararg.arg}")
        if args_node.kwarg:
            parts.append(f"**{args_node.kwarg.arg}")
        for kwonly, default in zip(args_node.kwonlyargs, args_node.kw_defaults):
            if default is not None:
                parts.append(f"{kwonly.arg}={ast.unparse(default)}")
            else:
                parts.append(kwonly.arg)

        return f"{node.name}({', '.join(parts)})"
    except Exception as exc:
        logger.debug("signature extraction failed for %s: %s", node.name, exc)
        return node.name + "()"


# ---------------------------------------------------------------------------
# External package helpers
# ---------------------------------------------------------------------------

# sys.stdlib_module_names is available on Python >= 3.10 (matches our minimum)
_STDLIB_NAMES: frozenset[str] = frozenset(sys.stdlib_module_names)


def _is_stdlib(module_name: str) -> bool:
    """Return True if the top-level package of *module_name* is from the stdlib."""
    return module_name.split(".")[0] in _STDLIB_NAMES


def _external_node_id(module_name: str, project_id: str) -> str | None:
    """Return the external entity node ID for a non-stdlib, non-project import.

    Returns None for stdlib modules, private names (leading underscore), or
    empty strings.
    """
    top = module_name.split(".")[0]
    if not top or top.startswith("_") or _is_stdlib(module_name):
        return None
    return f"{project_id}::external::{top}"


def _make_external_node(node_id: str, pkg_name: str) -> AxiomNode:
    """Create a stub entity node representing an external (third-party) package."""
    return AxiomNode(
        id=node_id,
        node_type="entity",
        subtype="external_package",
        title=pkg_name,
        location="external",
        source="ast",
        code_hash=hash16(f"external::{pkg_name}"),
        level_0=pkg_name,
        level_1=f"External package: {pkg_name}",
        tags=["external"],
    )


# ---------------------------------------------------------------------------
# Import resolution
# ---------------------------------------------------------------------------


def _resolve_import(module_name: str, project_root: Path, project_id: str) -> str | None:
    """Try to resolve a dotted module name to a node id within project_root."""
    parts = module_name.split(".")
    # Try longest match first: methods.binary_label.run → methods/binary_label/run.py
    candidates = [
        project_root / Path(*parts).with_suffix(".py"),
        project_root / Path(*parts) / "__init__.py",
    ]
    for candidate in candidates:
        if candidate.exists():
            rel = candidate.relative_to(project_root).as_posix()
            dotpath = _rel_path_to_dotpath(rel)
            return f"{project_id}::{dotpath}"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rel_path_to_dotpath(rel_path: str) -> str:
    """Convert a repo-relative path like 'pkg/sub/mod.py' to 'pkg.sub.mod'."""
    p = rel_path
    if p.endswith("/__init__.py"):
        p = p[: -len("/__init__.py")]
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def _extract_source(lines: list[str], start: int, end: int) -> str:
    """Return source text for lines [start, end] (1-based, inclusive)."""
    return "\n".join(lines[start - 1 : end])


def _first_sentence(text: str | None) -> str:
    """Return the first sentence of a docstring (up to first '.', '!', or newline)."""
    if not text:
        return ""
    # Strip leading indentation / blank lines
    text = text.strip()
    # Take first line that has content
    for line in text.splitlines():
        line = line.strip()
        if line:
            # Cut at first sentence-ending punctuation
            m = re.search(r"[.!?]", line)
            if m:
                return line[: m.start() + 1]
            return line
    return text.strip()


def _extract_raises(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return sorted list of exception names raised directly in this function."""
    raised: set[str] = set()
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if isinstance(exc, ast.Call):  # raise SomeError(...)
            exc = exc.func
        if isinstance(exc, ast.Name):  # raise SomeError
            raised.add(exc.id)
        elif isinstance(exc, ast.Attribute):  # raise pkg.SomeError
            raised.add(exc.attr)
    return sorted(raised)


def _parse_docstring_meta(docstring: str | None, raises: list[str]) -> dict | None:
    """Parse a docstring with ``docstring_parser`` and return structured autodoc metadata.

    Returns a dict suitable for storage in ``dflow_meta``, or *None* when
    there is nothing interesting to store.

    Keys:
        params   — [{name, type, desc}, ...]
        returns  — [{type, desc}, ...]
        raises   — ["ExceptionName", ...]   (from AST, enriched with docstring descriptions)
        raises_doc — [{type, desc}, ...]     (from docstring only — includes descriptions)

    ``params`` and ``returns`` are extracted from the docstring using
    ``docstring_parser``; ``raises`` is from AST extraction (passed in).
    """
    meta: dict = {}

    if docstring:
        try:
            parsed = docstring_parser.parse(docstring)
        except Exception as exc:
            logger.debug("docstring parsing failed: %s", exc)
            parsed = None

        if parsed is not None:
            params = []
            for p in parsed.params:
                entry: dict = {"name": p.arg_name}
                if p.type_name:
                    entry["type"] = p.type_name
                if p.description:
                    entry["desc"] = p.description.strip()
                if p.is_optional:
                    entry["optional"] = True
                if p.default:
                    entry["default"] = p.default
                params.append(entry)

            returns = []
            for r in parsed.many_returns:
                entry = {}
                if r.type_name:
                    entry["type"] = r.type_name
                if r.description:
                    entry["desc"] = r.description.strip()
                if entry:
                    returns.append(entry)

            raises_doc = []
            for r in parsed.raises:
                entry = {}
                if r.type_name:
                    entry["type"] = r.type_name
                if r.description:
                    entry["desc"] = r.description.strip()
                if entry:
                    raises_doc.append(entry)

            if params:
                meta["params"] = params
            if returns:
                meta["returns"] = returns
            if raises_doc:
                meta["raises_doc"] = raises_doc

    if raises:
        meta["raises"] = raises

    return meta if meta else None


_DFLOW_DECORATOR_NAMES = {"workflow", "task"}


def _extract_dflow_meta(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict | None:
    """Extract dFlow decorator keyword arguments into a metadata dict.

    Recognises @workflow and @task decorators and returns their keyword
    arguments (e.g. ``purpose``, ``task_name``) so they can be stored in
    ``dflow_meta`` for display and search.

    Returns None if no recognised dFlow decorator is found.
    """
    for dec in func_node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        name = _decorator_name(dec)
        if name not in _DFLOW_DECORATOR_NAMES:
            continue
        meta: dict = {"decorator": name}
        for kw in dec.keywords:
            if kw.arg is None:
                continue
            try:
                meta[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, TypeError):
                meta[kw.arg] = ast.unparse(kw.value)
        return meta
    return None


def _decorator_name(dec: ast.expr) -> str:
    """Return a string like 'click.command' or 'pytest.fixture' for a decorator node."""
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute) and isinstance(dec.value, ast.Name):
        return f"{dec.value.id}.{dec.attr}"
    return ""


_ENTRYPOINT_CLI_DECORATORS = {"click.command", "click.group"}
_ENTRYPOINT_HTTP_DECORATORS = {
    "app.route",
    "app.get",
    "app.post",
    "app.put",
    "app.delete",
    "app.patch",
    "router.get",
    "router.post",
    "router.put",
    "router.delete",
    "router.patch",
}
_FIXTURE_DECORATORS = {"pytest.fixture", "fixture"}


def _extract_tags(func_node: ast.FunctionDef | ast.AsyncFunctionDef, rel_path: str) -> list[str]:
    """Return tags for a function node based on name, file, and decorators."""
    tags: list[str] = []
    basename = rel_path.split("/")[-1]
    in_test_file = basename.startswith("test_") or basename.endswith("_test.py")

    if in_test_file:
        tags.append("test")
    if func_node.name.startswith("test_") and not in_test_file:
        tags.append("test")  # test func outside a test file (rare but valid)

    for dec in func_node.decorator_list:
        name = _decorator_name(dec)
        if name in _ENTRYPOINT_CLI_DECORATORS:
            tags.append("entrypoint:cli")
        elif name in _ENTRYPOINT_HTTP_DECORATORS:
            tags.append("entrypoint:http")
        elif name in _FIXTURE_DECORATORS:
            tags.append("test:fixture")

    return tags


# _read_dflow_edges removed in Phase 3: delegates_to edges are now emitted
# inline from the AST walk (via _extract_step_nodes) as part of the single
# in-process scan.  See axiom_graph::docs.pev.cycles.pev-2026-04-21-phase3-axiom-annotations.
