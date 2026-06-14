"""Single source of truth for *current* node hashing.

This module centralises "what is the current (code_hash, desc_hash) for
this node, given the file on disk?" — the question previously answered by
three independent implementations (``module_scanner._collect_functions``,
``index.staleness.compute_staleness`` step 2, and
``index.mark_clean.compute_current_hashes``).

Disagreements between those implementations caused chronic
``CONTENT_UPDATED`` churn for class-method tests with colliding short
names and ``@workflow`` / ``@task`` envelopes (titles like
``"get_stale_tests @task"`` that no AST walker could match).

This primitive provides two entry points:

* :func:`current_node_hash` — single-node lookup; parses the file each
  call.  Used by ``mark_clean``.
* :func:`current_node_hashes_for_file` — batch lookup over all relevant
  nodes from the same file; parses the AST once.  Used by
  ``compute_staleness`` step 2 to preserve single-AST-parse-per-file
  efficiency.

Both return ``(code_hash, desc_hash)`` tuples.  When the node cannot be
located on disk (file missing, syntax error, section gone, etc.) the
stored ``(node.code_hash, node.desc_hash)`` is returned unchanged — this
preserves the historical behaviour relied on by callers.

Design notes
------------

* Python atomic functions/tests are resolved by **qualified name** (last
  ``::`` segment of the node id) — ``TestA.test_foo`` and
  ``TestB.test_foo`` get distinct hashes.  Previous implementations
  keyed by the short ``ast.walk`` name and so the last-walker-wins.
* Workflow/task envelopes carry ``@workflow`` in their id suffix even
  when the actual decorator is ``@task``; the function name is
  recovered from ``id.rsplit("::", 1)[-1].removesuffix("@workflow")``
  and dispatch is on ``subtype`` not the suffix.
* DocJSON section misses fall back to stored hashes (NOT
  ``(None, None)``) so callers' equality checks remain meaningful.
* ``_collect_functions`` in ``module_scanner.py`` is *not* routed
  through here — it walks during the original scan with parent context
  on the call stack (see decision D-2 on the cycle manifest).
"""

from __future__ import annotations

import ast
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from axiom_graph.models import hash16
from axiom_graph.scanners._step_helpers import envelope_code_hash
from axiom_graph.scanners.module_scanner import _extract_dflow_meta, _split_function

if TYPE_CHECKING:
    from axiom_graph.models import AxiomNode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Title parsing primitive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeTitle:
    """Parsed view of an :class:`~axiom_graph.models.AxiomNode`'s identity.

    Three fields surface the information that several display sites
    previously open-coded with ``.title.split(...)`` or
    ``.id.split(...)``.

    Attributes:
        qualified: The trailing ``::`` segment of ``node.id`` with any
            ``@workflow`` envelope suffix stripped.  Examples:
            ``"my_func"``, ``"TestFoo.test_bar"``.
        last: The trailing dot segment of ``qualified``.  For class
            methods this is the method name (``"test_bar"``); for bare
            functions it equals ``qualified``.
        envelope_kind: ``"workflow"`` / ``"task"`` if ``node.title``
            ends with the corresponding ``" @workflow"`` /
            ``" @task"`` annotation; otherwise ``None``.
    """

    qualified: str
    last: str
    envelope_kind: str | None


_ENVELOPE_ID_SUFFIX = "@workflow"
_ENVELOPE_TITLE_SUFFIXES: tuple[tuple[str, str], ...] = (
    (" @workflow", "workflow"),
    (" @task", "task"),
)


def parse_node_title(node: "AxiomNode") -> NodeTitle:
    """Return a :class:`NodeTitle` for ``node``.

    Reads ``node.id`` for ``qualified``/``last`` (stable, no spaces) and
    ``node.title`` for ``envelope_kind`` (preserves the original
    decorator -- ``@task`` vs ``@workflow``).  The two surfaces are
    consulted independently; the parser does *not* cross-validate them.

    Args:
        node: The :class:`~axiom_graph.models.AxiomNode` to parse.

    Returns:
        A frozen :class:`NodeTitle` instance.  Never raises -- a node
        with degenerate ``id`` (no ``::``) or empty ``title`` returns
        sane defaults.
    """
    raw_id = getattr(node, "id", "") or ""
    qualified = raw_id.rsplit("::", 1)[-1]
    if qualified.endswith(_ENVELOPE_ID_SUFFIX):
        qualified = qualified[: -len(_ENVELOPE_ID_SUFFIX)]
    last = qualified.split(".")[-1] if qualified else qualified

    title = getattr(node, "title", "") or ""
    envelope_kind: str | None = None
    for suffix, kind in _ENVELOPE_TITLE_SUFFIXES:
        if title.endswith(suffix):
            envelope_kind = kind
            break

    return NodeTitle(qualified=qualified, last=last, envelope_kind=envelope_kind)


# Subtypes that have no on-disk hash story — stored values are returned
# unchanged.  ``external_package`` and ``entity`` carry no source files;
# ``step`` and ``autostep`` are views into their enclosing function and
# inherit their hash via composite inheritance, not direct re-derivation.
_PASSTHROUGH_SUBTYPES = frozenset({"step", "autostep", "external_package"})

# File extensions handled by the tree-sitter JS/TS scanner rather than the
# Python ``ast`` walker.  Dispatch is on extension, not subtype: a JS/TS
# function carries ``subtype='function'`` (post-normalization) or ``None``,
# identical to a Python function, so subtype cannot separate the languages.
_JS_TS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx"})


def _qualified_name_from_node_id(node_id: str) -> str:
    """Return the trailing qualified name segment of a node id.

    The node id format is ``"{project_id}::{dotpath}::{name}"`` where
    ``name`` is the qualified name (e.g. ``"TestX.test_foo"``).
    """
    return node_id.rsplit("::", 1)[-1]


def _envelope_func_name(node_id: str) -> str:
    """Return the function name for an envelope node.

    Envelopes always carry a literal ``@workflow`` suffix on the id even
    when the decorator is ``@task``.  The qualified name may include a
    class prefix (``TestX.method@workflow``); the AST walker keys on the
    bare function name, so we strip the class prefix as well.
    """
    qualified = _qualified_name_from_node_id(node_id)
    if qualified.endswith("@workflow"):
        qualified = qualified[: -len("@workflow")]
    return qualified.split(".")[-1]


def _walk_python_functions(
    tree: ast.AST,
) -> dict[str, tuple[str, str | None]]:
    """Return ``{qualified_name: (code_hash, desc_hash)}`` for every function.

    Walks classes (one level deep — methods) and recurses through nested
    function definitions, mirroring the ``name_prefix`` convention used
    in :func:`axiom_graph.scanners.module_scanner._collect_functions`.

    Each function appears under its **qualified** name, so siblings like
    ``TestA.test_foo`` and ``TestB.test_foo`` produce distinct entries.
    The bare short name is *also* registered for top-level functions
    (where the qualified and short names coincide); this keeps
    backwards-compatible lookup for free functions.
    """
    out: dict[str, tuple[str, str | None]] = {}

    def _visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}{child.name}" if prefix else child.name
                code_text, docstring = _split_function(child)
                out[qualified] = (
                    hash16(code_text),
                    hash16(docstring) if docstring else None,
                )
                # Recurse into nested defs (closures still hashable by
                # qualified name, though rarely targeted directly).
                _visit(child, f"{qualified}.")
            elif isinstance(child, ast.ClassDef):
                _visit(child, f"{child.name}.")

    _visit(tree, "")
    return out


def _walk_python_envelope_hashes(tree: ast.AST) -> dict[str, str]:
    """Return ``{func_name: envelope_code_hash}`` for decorated functions.

    Walks the full tree (including class methods).  The key is the bare
    function name, matching the ``_envelope_func_name`` lookup convention.
    Decorator collisions on the same bare name across different classes
    are vanishingly rare in practice; if they ever appear, last-walker-
    wins is the same behaviour the legacy code had — and envelopes are
    never sibling-class methods today.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        dflow = _extract_dflow_meta(node)
        if dflow is None:
            continue
        decorator = dflow.get("decorator", "workflow")
        kwargs = {k: v for k, v in dflow.items() if k != "decorator"}
        out[node.name] = envelope_code_hash(decorator, kwargs)
    return out


def _scan_docjson_sections(
    abs_path: Path,
    project_root: Path,
    project_id: str,
) -> dict[str, "AxiomNode"]:
    """Scan a DocJSON file and return ``{section_id: scanned_node}``.

    Returns an empty dict if scanning fails for any reason.
    """
    try:
        from axiom_graph.docjson.parse import scan_single_json_doc  # noqa: PLC0415
        from axiom_graph.index.builder import _matched_docs_dir  # noqa: PLC0415

        matched = _matched_docs_dir(abs_path, project_root)
        scanned_nodes, _, _, _ = scan_single_json_doc(
            abs_path,
            project_root,
            project_id,
            docs_dir=matched,
        )
        return {sn.id: sn for sn in scanned_nodes}
    except Exception as exc:
        logger.debug("Failed to scan docjson at %s: %s", abs_path, exc)
        return {}


def _js_hashes_for_file(
    abs_path: Path,
    project_root: Path,
    project_id: str,
) -> dict[str, tuple[str | None, str | None]] | None:
    """Map every ``js_scanner`` node id at ``abs_path`` to its hashes.

    The tree-sitter scanner is the single source of truth for JS/TS node
    ids and hashes — re-deriving them keeps the staleness hasher in lockstep
    with the original scan.

    Returns ``None`` when the scanner *cannot evaluate the file* — tree-sitter
    is not installed, or the scan raised.  Callers preserve the stored hash in
    that case rather than treating the nodes as deleted: scanner-absent is not
    the same as function-deleted, and mapping it to ``NOT_FOUND`` is exactly
    the corruption this dispatch branch exists to prevent.
    """
    from axiom_graph.scanners import js_scanner  # noqa: PLC0415

    if not js_scanner.HAS_TREE_SITTER:
        logger.warning(
            "tree-sitter unavailable; preserving stored hashes for JS/TS nodes at %s",
            abs_path,
        )
        return None
    try:
        scanned, _ = js_scanner.scan_js_module(abs_path, project_root, project_id)
    except Exception as exc:  # tree-sitter is error-tolerant; a raise here is exotic
        logger.debug("js_scanner failed at %s: %s", abs_path, exc)
        return None
    return {sn.id: (sn.code_hash, sn.desc_hash) for sn in scanned}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def current_node_hash(
    node: "AxiomNode",
    project_root: Path,
) -> tuple[str | None, str | None]:
    """Compute current ``(code_hash, desc_hash)`` for a single node.

    Dispatches by ``(node.subtype, node.node_type)`` against the file on
    disk.  Falls back to the stored ``(node.code_hash, node.desc_hash)``
    whenever the file is missing, parsing fails, or the node cannot be
    located — see module docstring for the rationale.

    For batch operation over all nodes from one file, prefer
    :func:`current_node_hashes_for_file` to avoid re-parsing the AST
    per node.

    Args:
        node: The :class:`~axiom_graph.models.AxiomNode` to hash.
        project_root: Absolute path to the project root.

    Returns:
        Two-tuple ``(code_hash, desc_hash)`` reflecting the file on disk.
        Either element may be ``None`` (e.g. functions without
        docstrings, or envelopes whose ``desc_hash`` is always
        ``None``).
    """
    if not node.location:
        return node.code_hash, node.desc_hash

    abs_path = project_root / node.location
    if not abs_path.exists():
        return node.code_hash, node.desc_hash

    subtype = getattr(node, "subtype", None)
    node_type = node.node_type

    # Pass-through subtypes — no on-disk re-derivation.
    if subtype in _PASSTHROUGH_SUBTYPES or node_type == "entity":
        return node.code_hash, node.desc_hash

    # JS/TS — re-derive via the tree-sitter scanner; ``ast.parse`` cannot read
    # TypeScript and would fall through to the stored hash, masking real edits.
    # Dispatch on extension because a JS function's subtype matches a Python
    # function's.  Handles atomic functions and workflow/task envelopes alike.
    if abs_path.suffix in _JS_TS_EXTENSIONS:
        proj_id = node.id.split("::")[0]
        js_map = _js_hashes_for_file(abs_path, project_root, proj_id)
        if js_map is None:
            return node.code_hash, node.desc_hash
        return js_map.get(node.id, (node.code_hash, node.desc_hash))

    # DocJSON composite (file-level).
    if subtype == "docjson" and node_type == "composite_process":
        try:
            raw_text = abs_path.read_text(encoding="utf-8", errors="replace")
            file_hash = hash16(raw_text)
            return file_hash, file_hash
        except OSError as exc:
            logger.debug("Failed to read docjson file for hash: %s", exc)
            return node.code_hash, node.desc_hash

    # DocJSON atomic (section).
    if subtype == "docjson" and node_type == "atomic_process":
        proj_id = node.id.split("::")[0]
        scanned_map = _scan_docjson_sections(abs_path, project_root, proj_id)
        sn = scanned_map.get(node.id)
        if sn is None:
            return node.code_hash, node.desc_hash
        return (
            getattr(sn, "code_hash", None) or node.code_hash,
            getattr(sn, "desc_hash", None),
        )

    # Workflow / task envelope (composite_process).
    if subtype in ("workflow", "task") and node_type == "composite_process":
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(abs_path))
        except (SyntaxError, OSError) as exc:
            logger.debug("Envelope AST parse failed at %s: %s", abs_path, exc)
            return node.code_hash, node.desc_hash
        env_hashes = _walk_python_envelope_hashes(tree)
        func_name = _envelope_func_name(node.id)
        cur_env = env_hashes.get(func_name)
        if cur_env is None:
            return node.code_hash, node.desc_hash
        return cur_env, None

    # Python atomic (function / test).
    if node_type == "atomic_process" and subtype in (None, "function", "test"):
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(abs_path))
        except (SyntaxError, OSError) as exc:
            logger.debug("Python AST parse failed at %s: %s", abs_path, exc)
            return node.code_hash, node.desc_hash
        func_hashes = _walk_python_functions(tree)
        qualified = _qualified_name_from_node_id(node.id)
        if qualified in func_hashes:
            return func_hashes[qualified]
        # Fallback for legacy / synthetic ids: try the bare short name
        # (preserves behaviour for free functions whose qualified name
        # equals the short name).
        short = qualified.split(".")[-1]
        if short in func_hashes:
            return func_hashes[short]
        return node.code_hash, node.desc_hash

    # Unknown combination — be conservative.
    return node.code_hash, node.desc_hash


def current_node_hashes_for_file(
    abs_path: Path,
    nodes: list["AxiomNode"],
    project_root: Path,
) -> dict[str, tuple[str | None, str | None]]:
    """Batch ``current_node_hash`` over every ``node`` for one file.

    Parses the Python AST (or scans the DocJSON file) **once** and looks
    up every supplied node from the cached result.  This preserves the
    single-AST-parse-per-file efficiency that
    :func:`axiom_graph.index.staleness.compute_staleness` step 2 relies
    on.

    Unlike :func:`current_node_hash`, **this batch entry omits nodes
    that could not be located on disk**.  Nodes absent from the
    returned dict are either:

    * pass-through subtypes (``step``, ``autostep``,
      ``external_package``, or ``entity``) — caller already knows
      these have no on-disk hash story; or
    * "real" misses — e.g. a python function deleted from the file,
      a section removed from a DocJSON, an envelope whose decorator
      was stripped, or a parse failure.

    Callers (``compute_staleness`` step 2) use the absence to detect
    ``NOT_FOUND`` directly.  Note: pass-through subtypes and "real"
    misses are not distinguished here -- callers handle pass-throughs
    earlier in their own dispatch (status pre-set to ``VERIFIED``)
    so the only nodes that reach the dict-lookup are ones that
    *should* have been found.

    Args:
        abs_path: Absolute path to the file on disk.
        nodes: Every node whose location resolves to ``abs_path``.
        project_root: Absolute path to the project root.

    Returns:
        Dict mapping ``node.id`` to ``(code_hash, desc_hash)`` for
        every node successfully resolved on disk.  Missing entries
        signal "could not locate" -- callers map this to
        ``NOT_FOUND``.
    """
    result: dict[str, tuple[str | None, str | None]] = {}

    if not abs_path.exists():
        # Caller already handles file-missing as NOT_FOUND for every
        # node at this location.
        return result

    # Sort nodes by what we need to compute.
    docjson_section_nodes: list["AxiomNode"] = []
    docjson_composite_nodes: list["AxiomNode"] = []
    python_nodes: list["AxiomNode"] = []
    envelope_nodes: list["AxiomNode"] = []
    js_nodes: list["AxiomNode"] = []

    # JS/TS files route through the tree-sitter scanner, not the Python AST.
    # Dispatch on extension because subtype cannot separate the languages.
    is_js = abs_path.suffix in _JS_TS_EXTENSIONS

    for n in nodes:
        subtype = getattr(n, "subtype", None)
        node_type = n.node_type
        if subtype in _PASSTHROUGH_SUBTYPES or node_type == "entity":
            # Caller pre-handles these as VERIFIED; skip them entirely.
            continue
        if is_js:
            # Atomic functions and workflow/task envelopes have an on-disk
            # hash; the module composite (subtype None) is left to inherit.
            if (node_type == "atomic_process" and subtype in (None, "function", "test")) or (
                node_type == "composite_process" and subtype in ("workflow", "task")
            ):
                js_nodes.append(n)
            continue
        if subtype == "docjson" and node_type == "composite_process":
            docjson_composite_nodes.append(n)
        elif subtype == "docjson" and node_type == "atomic_process":
            docjson_section_nodes.append(n)
        elif subtype in ("workflow", "task") and node_type == "composite_process":
            envelope_nodes.append(n)
        elif node_type == "atomic_process" and subtype in (None, "function", "test"):
            python_nodes.append(n)
        # else: unknown combination -- omit (caller falls back to stored).

    # DocJSON composite — whole-file hash, computed once.
    if docjson_composite_nodes:
        try:
            raw_text = abs_path.read_text(encoding="utf-8", errors="replace")
            file_hash = hash16(raw_text)
            for n in docjson_composite_nodes:
                result[n.id] = (file_hash, file_hash)
        except OSError as exc:
            logger.debug("Failed to read docjson file for hash: %s", exc)
            # File-read failure for an existing file is exotic; omit
            # so caller maps to NOT_FOUND.

    # DocJSON sections — single scan, look up each node.
    if docjson_section_nodes:
        proj_id = docjson_section_nodes[0].id.split("::")[0]
        scanned_map = _scan_docjson_sections(abs_path, project_root, proj_id)
        for n in docjson_section_nodes:
            sn = scanned_map.get(n.id)
            if sn is not None:
                result[n.id] = (
                    getattr(sn, "code_hash", None) or n.code_hash,
                    getattr(sn, "desc_hash", None),
                )
            # else: omit -- caller maps to NOT_FOUND.

    # Python AST — parse once, walk for both functions and envelopes.
    if python_nodes or envelope_nodes:
        tree: ast.AST | None = None
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(abs_path))
        except (SyntaxError, OSError) as exc:
            logger.debug("Python AST parse failed at %s: %s", abs_path, exc)

        if tree is not None:
            func_hashes = _walk_python_functions(tree) if python_nodes else {}
            env_hashes = _walk_python_envelope_hashes(tree) if envelope_nodes else {}

            for n in python_nodes:
                qualified = _qualified_name_from_node_id(n.id)
                if qualified in func_hashes:
                    result[n.id] = func_hashes[qualified]
                    continue
                short = qualified.split(".")[-1]
                if short in func_hashes:
                    result[n.id] = func_hashes[short]
                # else: omit -- caller maps to NOT_FOUND.

            for n in envelope_nodes:
                func_name = _envelope_func_name(n.id)
                cur_env = env_hashes.get(func_name)
                if cur_env is not None:
                    result[n.id] = (cur_env, None)
                # else: omit -- caller maps to NOT_FOUND.
        # If parse failed: omit all python/envelope nodes -> NOT_FOUND.

    # JS/TS — scan once via tree-sitter, look up every node by id.
    if js_nodes:
        proj_id = js_nodes[0].id.split("::")[0]
        js_map = _js_hashes_for_file(abs_path, project_root, proj_id)
        if js_map is None:
            # Scanner could not evaluate the file (tree-sitter absent or scan
            # failure).  Preserve stored hashes so callers see cur == stored
            # -> VERIFIED, never NOT_FOUND.  "Scanner absent != deleted."
            for n in js_nodes:
                result[n.id] = (n.code_hash, n.desc_hash)
        else:
            for n in js_nodes:
                hit = js_map.get(n.id)
                if hit is not None:
                    result[n.id] = hit
                # else: omit -- a genuinely deleted JS/TS function, caller
                # maps to NOT_FOUND (parity with the Python branch).

    return result


def node_hashes_for_blob(
    blob_text: str,
    nodes: list["AxiomNode"],
    project_root: Path,
    location: str,
) -> dict[str, tuple[str | None, str | None]]:
    """Hash *nodes* against a supplied blob (e.g. ``git show`` content).

    Routes the blob through the **same dispatch** as
    :func:`current_node_hashes_for_file` (Python AST, JS/TS tree-sitter, or
    DocJSON scan) so a TypeScript file is never fed to ``ast.parse`` and a
    DocJSON section is scanned the way the indexer scans it.

    **Non-destructive.** The blob is materialised into a private temp directory
    that *mirrors* the node's real relative path (``<tmp>/<location>``) and is
    scanned with that temp directory as the logical project root. The user's
    real working-tree file at ``project_root / location`` is **never written,
    overwritten, or deleted** — a hard process kill mid-scan can only ever
    abandon the temp directory, never corrupt the user's source.

    Node-id parity is preserved because the path-dependent scanners (JS/TS
    tree-sitter, DocJSON section ids) derive ids from ``file_path.relative_to(
    project_root)`` — identical for ``<tmp>/<location>`` against ``<tmp>`` and
    for ``project_root/<location>`` against ``project_root``. The project's
    ``axiom-graph.toml`` is copied into the temp root so DocJSON docs-dir
    resolution (custom ``docs_dirs``) resolves the same way and yields matching
    section ids. Python nodes are resolved by qualified id (path-independent),
    so they match regardless.

    Args:
        blob_text: The baseline-blob source text (from ``git show``).
        nodes: The nodes located at *location* whose baseline hashes to compute.
        project_root: Absolute path to the real project root. Used only to copy
            config into the temp mirror — never written to.
        location: Repo-relative path of the file at the **current** index
            (the path the blob's nodes map to; mirrored under the temp root).

    Returns:
        ``{node.id: (code_hash, desc_hash)}`` for every node resolvable in the
        blob. Nodes absent from the blob (e.g. a function not yet present at
        baseline) are omitted -- exactly like
        :func:`current_node_hashes_for_file`.
    """
    if not nodes:
        return {}

    # Normalise the location to a relative POSIX path so the mirror reproduces
    # the exact relative path the scanners hash against.
    rel = Path(location.replace("\\", "/"))

    tmp_root: str | None = None
    try:
        tmp_root = tempfile.mkdtemp(prefix="axiom_blob_hash_")
        tmp_root_path = Path(tmp_root)

        # Mirror the file at <tmp>/<location> so relative-path-derived node ids
        # (JS/TS, DocJSON) match the stored node.id exactly.
        mirror_path = tmp_root_path / rel
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        mirror_path.write_text(blob_text, encoding="utf-8")

        # Copy the project config into the temp root so DocJSON docs-dir
        # resolution (custom `docs_dirs`) matches and produces identical
        # section ids. Absent / unreadable config falls back to defaults —
        # harmless for the default `docs` layout.
        src_toml = project_root / "axiom-graph.toml"
        if src_toml.exists():
            try:
                shutil.copyfile(src_toml, tmp_root_path / "axiom-graph.toml")
            except OSError as exc:
                logger.debug("Failed to mirror config for blob hash: %s", exc)

        return current_node_hashes_for_file(mirror_path, nodes, tmp_root_path)
    except OSError as exc:
        logger.debug("Failed to materialise blob for hashing at %s: %s", location, exc)
        return {}
    finally:
        if tmp_root is not None:
            shutil.rmtree(tmp_root, ignore_errors=True)
