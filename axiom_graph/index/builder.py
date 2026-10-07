"""Axiom-graph index builder — orchestrates scanners and upserts into SQLite.

Entry point:
    build(project_root, project_id=None) -> dict

Runs all scanners, validates ontology edges, and returns a summary dict:
    {
        "nodes_written": int,
        "nodes_skipped": int,
        "edges_written": int,
        "edges_skipped": int,
        "warnings": list[str],
    }
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from axiom_annotations import workflow, task, Step, AutoStep

from axiom_graph.config import AxiomGraphConfig, ProjectIdMismatchError, db_path_for
from axiom_graph.index import annotation_findings, db, doc_ids, doc_stamps
from axiom_graph.index.file_state import BuildDiscovery, file_unchanged_since
from axiom_graph.index.link_maintenance import link_rewrite_warnings
from axiom_graph.index.walk import TreeListing, suffix_matcher
from axiom_graph.models import make_edge
from axiom_graph.ontology import valid_edge
from axiom_graph.scanners import config_scanner, doc_scanner, module_scanner
from axiom_graph.scanners.source_roots import declared_dependencies, resolve_source_roots, roots_fingerprint
from axiom_graph.docjson import parse as json_doc_scanner
from axiom_graph.index.status import BROKEN_LINK, NOT_FOUND

if TYPE_CHECKING:
    from collections.abc import Container

    from axiom_graph.models import AxiomNode

logger = logging.getLogger(__name__)

# Built-in directories to skip when walking the project tree.
# axiom-graph.toml [axiom_graph.scan] exclude_dirs are merged in at build time.
_BASE_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".axiom_graph",
        ".cortex",
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".tox",
        "dist",
        "build",
        ".pixi",
        "worktrees",  # PEV per-cycle git worktrees live under .claude/worktrees/
    }
)

# Scanner-derived edge types the build reconciles against the sources it
# walked.  Membership is the whole scope of the reconciliation pass: an edge
# type not named here is never deleted by a build, whatever its source file
# says.  Widening this set is a deliberate decision, not a side effect —
# each entry needs its own coverage, because each edge type has its own
# cardinality rules (an AutoStep delegates at most once; a state machine's
# state delegates once per transition, and every one of those is correct).
#
# ``validates`` is safe here because the link-target resolution pass runs
# first and rewrites ``all_edges`` in place: a re-export-resolved validates
# link is in the intended set under its resolved target, and an unresolved
# one has left the list (it was never written either).  Reconciling it is
# what retires the edges a test file's helpers and fixtures owned before
# only collected tests owned validates.
_RECONCILED_SCANNER_EDGE_TYPES: frozenset[str] = frozenset({"delegates_to", "validates"})

# How many carrier files the leftover-orphan notice names before it
# summarises the rest.  The list is the remedy — it tells the reader what to
# touch — so it has to stay short enough to read in one line.
_ORPHAN_NOTICE_FILE_CAP: int = 5

#: ``index_meta`` key holding the project id the index was built with.
PROJECT_ID_META_KEY = "project_id"


def base_skip_dirs() -> frozenset[str]:
    """Return the directory names every build skips, before ``exclude_dirs`` is added.

    Returns:
        The built-in skip set.
    """
    return _BASE_SKIP_DIRS


#: Version of what the scanners produce for given bytes: the nodes, edges
#: and stored text a parse yields.  Bump it with any change to that output.
#: A build that finds another value (or none) stored under
#: :data:`SCAN_SCHEME_META_KEY` parses every file once, as if each had been
#: touched -- discovery-only builds still keep every baseline -- so no file
#: keeps nodes or edges parsed by older scanners.  The value is recorded only
#: when no scanner raised, no per-file pass failed and JS/TS scanning ran.
SCAN_SCHEME = "2"

#: ``index_meta`` key holding the :data:`SCAN_SCHEME` the last complete parse used.
SCAN_SCHEME_META_KEY = "scan_scheme"

#: ``index_meta`` key holding the fingerprint of the Python import roots the
#: last complete parse resolved imports with.  A build that resolves other
#: roots parses every file once, the same way a :data:`SCAN_SCHEME` move
#: does, and records the new fingerprint under the same conditions.
SOURCE_ROOTS_META_KEY = "source_roots"

#: ``index_meta`` key holding, per walked Python file, the top-level names of
#: the imports its last parse filed as external packages (a JSON object of
#: location -> sorted names).  The external stubs themselves do not outlive a
#: build, so this is what lets an incremental build that parses nothing still
#: report the unresolved project imports of the files it skipped.
EXTERNAL_IMPORTS_META_KEY = "external_imports"


def indexed_project_id(db_path: Path) -> str | None:
    """Return the project id an existing index was built with, or ``None``.

    That is the id stored in ``index_meta``; for an index built before the
    id was stored, it is the one prefix every node id shares.  An index with
    no nodes, or whose nodes carry more than one prefix, has none.

    Args:
        db_path: The index DB (schema already initialised).

    Returns:
        The index's project id, or ``None``.
    """
    return db.get_index_meta(db_path, PROJECT_ID_META_KEY) or db.single_node_id_prefix(db_path)


def resolve_project_id(
    project_root: Path,
    db_path: Path | None = None,
    config: AxiomGraphConfig | None = None,
) -> str:
    """Return the project id every command other than ``build`` works under.

    The order is ``axiom-graph.toml``'s ``project_id``, then the index's own
    id (:func:`indexed_project_id`), then the directory name: ``build``'s
    order without ``--id``.  An empty string counts as unset.  The index is
    read only when its file exists, so resolving never creates one.

    Args:
        project_root: The project directory.
        db_path: The index DB; defaults to the project's configured one.
        config: The project's loaded config, to save reading the toml again.

    Returns:
        The project id.
    """
    root = Path(project_root).resolve()
    if config is None:
        config = AxiomGraphConfig.load(root)
    if config.project_id:
        return config.project_id
    if db_path is None:
        raw = Path(config.db_path)
        db_path = raw if raw.is_absolute() else root / raw
    if Path(db_path).exists():
        stored = indexed_project_id(Path(db_path))
        if stored:
            return stored
    return root.name


def _resolve_project_id(db_path: Path, project_root: Path, explicit_id: str | None, toml_id: str | None) -> str:
    """Resolve the build's project id and hold it to the one the index was built with.

    The order is *explicit_id* (``--id``), then *toml_id*
    (``axiom-graph.toml``), then the index's own id
    (:func:`indexed_project_id`), then the directory name.  An empty string
    counts as unset.  The index's id is recorded when it is not stored yet;
    an index with no id of its own records the resolved one.

    Args:
        db_path: The index DB (schema already initialised).
        project_root: The resolved project directory.
        explicit_id: The id the caller passed, or ``None``.
        toml_id: ``axiom-graph.toml``'s ``project_id``, or ``None``.

    Returns:
        The project id to build under.

    Raises:
        ProjectIdMismatchError: When the resolved id differs from the
            index's id.  Nothing has been scanned or written to a node row.
    """
    explicit_id = explicit_id or None
    toml_id = toml_id or None
    stored_id = db.get_index_meta(db_path, PROJECT_ID_META_KEY)
    is_recorded = stored_id is not None
    if not is_recorded:
        stored_id = db.single_node_id_prefix(db_path)
    if explicit_id is not None:
        project_id, source = explicit_id, "--id"
    elif toml_id is not None:
        project_id, source = toml_id, "axiom-graph.toml"
    else:
        project_id, source = stored_id or project_root.name, "the directory name"

    if stored_id is not None and project_id != stored_id:
        raise ProjectIdMismatchError(
            f"This index was built with project id '{stored_id}', but this build resolved "
            f"'{project_id}' (from {source}). Building would index the whole project a second "
            f"time under '{project_id}'. Nothing was indexed. To build this index, keep its id: "
            f'set project_id = "{stored_id}" under [axiom_graph] in axiom-graph.toml, and pass '
            f"--id {stored_id} or no --id at all. Renaming an existing index's project id is not "
            "supported; `axiom-graph init` rebuilds the index from scratch under a new id and "
            "discards its verification records and change history."
        )
    if not is_recorded:
        db.set_index_meta(db_path, PROJECT_ID_META_KEY, project_id)
        logger.info("build: index records project id %r", project_id)
    return project_id


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@workflow(
    purpose="Scan project_root with all scanners, upsert nodes/edges, detect renames, purge stale entries, and compute staleness",
    inputs="project_root path, optional project_id, discovery_only flag",
    outputs="Summary dict {nodes_written, nodes_skipped, edges_written, edges_skipped, nodes_renamed, nodes_purged, warnings, annotation_findings, annotation_findings_new, annotation_findings_resolved}",
)
def build(
    project_root: Path,
    project_id: str | None = None,
    discovery_only: bool = True,
) -> dict:
    """Scan *project_root* and upsert all discovered nodes/edges into the DB.

    Parameters
    ----------
    project_root:
        Absolute (or resolvable) path to the project directory to scan.
    project_id:
        Short identifier used as the namespace prefix in all node IDs.
        Resolved in order: this explicit value, ``axiom-graph.toml``'s
        ``project_id``, the index's own id (see
        :func:`indexed_project_id`), the directory name; an empty string
        counts as unset.  An index with no id of its own records the
        resolved one; an index whose id differs raises
        :class:`ProjectIdMismatchError` before anything is scanned.
    discovery_only:
        When ``True``, skip updates to nodes that already exist in the index.
        Only new nodes (never seen before) are inserted.  Edges are always
        updated.  Use this to add newly-created files/functions without
        resetting the ``code_hash`` baseline — which would erase the staleness
        signal on everything else.

    Returns
    -------
    dict
        Summary with keys ``nodes_written``, ``nodes_skipped``,
        ``edges_written``, ``edges_skipped``, ``warnings``, plus the
        reconciliation counters ``documents_edges_reconciled`` (orphan
        documents edges deleted), ``scanner_edges_reconciled`` (superseded
        scanner-derived edges deleted), ``orphaned_steps_reaped`` (step rows
        deleted because the file this build walked no longer declares them),
        ``orphaned_step_rows`` (step rows still parentless anywhere in the
        index) and ``surplus_delegate_edges`` (delegate links still held by
        AutoSteps that hold more than one).
    """
    t0 = time.monotonic()
    logger.info("build: start (project_root=%s, discovery_only=%s)", project_root, discovery_only)

    口 = Step(
        step_num=1,
        name="Resolve project root and config",
        purpose="Resolve absolute path, load axiom-graph.toml config and skip_dirs, and resolve the Python import roots "
        "once for the whole build",
        outputs="config, skip_dirs, source_roots and their fingerprint",
    )
    project_root = Path(project_root).resolve()

    logger.debug("build: resolving config from %s", project_root)
    # Load axiom-graph.toml (returns defaults silently if absent)
    config = AxiomGraphConfig.load(project_root)
    logger.debug("build: config loaded (project_id=%s)", config.project_id)

    skip_dirs = _BASE_SKIP_DIRS | frozenset(config.scan.exclude_dirs)
    # Where an absolute Python import is looked up: explicit source_roots,
    # pytest pythonpath, packaging config, the project root, an auto src/.
    # Read fresh every build, so an edit to any of those files is seen.
    source_roots = resolve_source_roots(project_root, config.scan.source_roots, skip_dirs)
    source_roots_fp = roots_fingerprint(project_root, source_roots)
    # One listing per directory for the whole build: the code walk, the
    # doc-id gate, the doc scanners and the config scanner all read it, and
    # a skipped directory is never listed at all.
    listing = TreeListing()

    口 = Step(
        step_num=2,
        name="Init DB, run migrations, and resolve the project id",
        purpose="Ensure .axiom_graph/ dir and DB schema exist; run pending versioned schema migrations so a legacy DB upgrades in place before any scanning; resolve the project id against the one the index stores",
        outputs="db_path, project_id",
        critical="The project id resolves --id, then axiom-graph.toml, then the index's id (stored, or for an index that predates stored ids the one prefix its nodes share), then the directory name.  A resolved id that differs from the index's id raises ProjectIdMismatchError before any scanning, so a build never indexes the project a second time under another id; an index with no id of its own records the resolved one",
    )
    # Resolve configured DB path (defaults to .axiom_graph/graph.db).
    # Ensure the parent directory exists before init.
    db_path = db_path_for(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logger.debug("build: initialising DB at %s", db_path)
    db.init_db(db_path)
    logger.debug("build: DB initialised")

    # Versioned auto-migration (ADR-021): bring a legacy DB up to the
    # current schema in place, before any scanning touches it.  No-op for
    # fresh or already-current DBs.  Raises SchemaVersionError when the DB
    # was written by a newer package (downgrade guard).
    from axiom_graph.db.migrations import run_migrations  # noqa: PLC0415

    migrations_applied = run_migrations(db_path)
    if migrations_applied:
        logger.info("build: applied schema migration(s): %s", migrations_applied)

    project_id = _resolve_project_id(db_path, project_root, project_id, config.project_id)

    nodes_written = 0
    nodes_skipped = 0
    edges_written = 0
    edges_skipped = 0
    files_scanned = 0
    files_skipped_mtime = 0
    warnings: list[str] = []

    all_nodes: list = []
    all_edges: list = []
    # Annotation results per code file this build scanned, gathered with
    # every rule enabled: the store is unfiltered, and the [validation]
    # config is applied when it is read.
    scanned_annotations: dict[str, annotation_findings.FileAnnotations] = {}
    # Every code file this build walked, scanned or mtime-skipped; the store
    # drops the rows of a file that leaves this set.
    walked_code_files: set[str] = set()
    _validation_guard = lambda rid: config.validation.is_enabled(rid)  # noqa: E731

    # Resolve HEAD git SHA once — threaded into every history row
    from axiom_graph.index.git_utils import get_git_sha  # noqa: PLC0415

    logger.debug("build: resolving git SHA")
    git_sha = get_git_sha(project_root)
    logger.debug("build: git SHA=%s", git_sha)

    口 = Step(
        step_num=3,
        name="Scan Python files",
        purpose="Walk every .py file under project_root and run module_scanner on each file whose bytes or mtime "
        "moved since build last parsed it (a file with no parse record: when its mtime differs from its scan mtime); "
        "every file when the index's stored scan scheme is not SCAN_SCHEME or its stored import-roots fingerprint "
        "is not this build's; imports resolve against source_roots",
        inputs="project_root, skip_dirs, stored_mtimes, source_roots",
        outputs=(
            "all_nodes and all_edges populated with Python module/function nodes and edges; each scanned file's "
            "raw annotation findings and AutoStep records in scanned_annotations; every walked file in "
            "walked_code_files"
        ),
    )
    # ------------------------------------------------------------------
    # Module scanner — every .py file under project_root
    # ------------------------------------------------------------------
    # Batch-load all stored mtimes in one query for the fast-pass.
    stored_mtimes: dict[str, float] = {}
    if discovery_only:
        logger.debug("build: loading stored mtimes")
        stored_mtimes = db.get_all_file_mtimes(db_path)
        logger.debug("build: loaded %d stored mtimes", len(stored_mtimes))
    # The discovery walk: every walked file is read once (fingerprint and
    # stat) and parsed when its content differs from the last parse, it has
    # no scan mtime, or its mtime moved (touch or re-save to rescan).  A file
    # with no parse record yet is judged by the mtime rule alone, once.
    with db._connect(db_path) as conn:
        _parse_records = db.get_file_records_conn(conn)
        # The newest history row before this build: the DELETED rows past it
        # name the nodes this build deletes.
        history_mark = db.max_history_id_conn(conn)
        stored_scan_scheme = db.get_index_meta_conn(conn, SCAN_SCHEME_META_KEY)
        stored_roots_fp = db.get_index_meta_conn(conn, SOURCE_ROOTS_META_KEY)
    # Files parsed by older scanners, or with other import roots, are parsed
    # again, whatever their content.
    scan_scheme_moved = stored_scan_scheme != SCAN_SCHEME or stored_roots_fp != source_roots_fp
    if scan_scheme_moved and discovery_only:
        logger.info(
            "build: parsing every file once: the index was parsed under scan scheme %s with import roots %s, "
            "this build uses scan scheme %s with import roots %s",
            stored_scan_scheme or "(none)",
            stored_roots_fp or "(none)",
            SCAN_SCHEME,
            source_roots_fp,
        )
    # A file a scanner raised on keeps its old output, so the scheme stays unstamped.
    scan_raised = False
    discovery = BuildDiscovery(
        project_root,
        {loc: rec.parsed_fp for loc, rec in _parse_records.items() if rec.parsed_fp is not None},
        stored_mtimes,
        parse_everything=not discovery_only or scan_scheme_moved,
    )

    logger.debug("build: starting Python file scan")
    for py_file in _iter_python_files(project_root, skip_dirs, listing):
        rel_path = py_file.relative_to(project_root).as_posix()
        walked_code_files.add(rel_path)
        # Discovery: skip files whose content and mtime match the last parse
        if not discovery.should_parse(py_file):
            files_skipped_mtime += 1
            continue

        try:
            logger.debug("build: scanning %s", rel_path)
            file_findings: list = []
            file_autosteps: list = []
            口 = AutoStep(step_num=3.1, name="Scan single Python file")
            nodes, edges = module_scanner.scan_module(
                py_file,
                project_root,
                project_id,
                findings_out=file_findings,
                autosteps_out=file_autosteps,
                is_rule_enabled=annotation_findings.all_rules_enabled,
                source_roots=source_roots,
            )
            all_nodes.extend(nodes)
            all_edges.extend(edges)
            scanned_annotations[rel_path] = annotation_findings.FileAnnotations.from_scan(file_findings, file_autosteps)
            files_scanned += 1
        except Exception as exc:  # pragma: no cover
            scan_raised = True
            warnings.append(f"module_scanner failed on {py_file.relative_to(project_root)}: {exc}")
            logger.warning("module_scanner error on %s: %s", py_file, exc)

    logger.info(
        "build: scanned %d Python files (%d skipped: content and mtime unchanged)", files_scanned, files_skipped_mtime
    )

    # ------------------------------------------------------------------
    # JS/TS scanner — files matching js_paths globs (if tree-sitter available)
    # ------------------------------------------------------------------
    js_scanned = 0
    js_skipped_mtime = 0
    # Without tree-sitter the JS/TS files are not walked, but their stored
    # annotation rows are kept (not current, not gone) for when it returns.
    js_scanning_unavailable = False
    if config.scan.js_paths:
        from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER as _js_available

        if _js_available:
            from axiom_graph.scanners import js_scanner

            for js_file in _iter_js_files(project_root, config.scan.js_paths, skip_dirs, listing):
                rel_path = js_file.relative_to(project_root).as_posix()
                walked_code_files.add(rel_path)
                if not discovery.should_parse(js_file):
                    js_skipped_mtime += 1
                    continue

                # One file's results come from both scanners; if either
                # raises, the file keeps its stored annotation rows.
                file_findings = []
                file_autosteps = []
                js_file_scanned = True
                try:
                    nodes, edges = js_scanner.scan_js_module(
                        js_file,
                        project_root,
                        project_id,
                        findings_out=file_findings,
                        autosteps_out=file_autosteps,
                        is_rule_enabled=annotation_findings.all_rules_enabled,
                    )
                    all_nodes.extend(nodes)
                    all_edges.extend(edges)
                    js_scanned += 1
                except Exception as exc:  # pragma: no cover
                    js_file_scanned = False
                    scan_raised = True
                    warnings.append(f"js_scanner failed on {js_file.relative_to(project_root)}: {exc}")
                    logger.warning("js_scanner error on %s: %s", js_file, exc)

                # xstate scanner — runs alongside js_scanner on the same file.
                # Emits state-machine envelopes for createMachine({...}) / setup(...).createMachine(...).
                try:
                    from axiom_graph.scanners import xstate_scanner

                    xs_nodes, xs_edges = xstate_scanner.scan_xstate_module(
                        js_file,
                        project_root,
                        project_id,
                        findings_out=file_findings,
                        is_rule_enabled=annotation_findings.all_rules_enabled,
                    )
                    all_nodes.extend(xs_nodes)
                    all_edges.extend(xs_edges)
                    if js_file_scanned:
                        scanned_annotations[rel_path] = annotation_findings.FileAnnotations.from_scan(
                            file_findings, file_autosteps
                        )
                except Exception as exc:  # pragma: no cover
                    scan_raised = True
                    warnings.append(f"xstate_scanner failed on {js_file.relative_to(project_root)}: {exc}")
                    logger.warning("xstate_scanner error on %s: %s", js_file, exc)

            logger.info(
                "build: scanned %d JS/TS files (%d skipped: content and mtime unchanged)", js_scanned, js_skipped_mtime
            )
        else:
            js_scanning_unavailable = True
            logger.info("build: js_paths configured but tree-sitter not installed — skipping JS/TS scan")
            warnings.append(
                "js_paths configured but tree-sitter not installed — install axiom-graph[js] to enable JS/TS scanning"
            )

    口 = Step(
        step_num=4,
        name="Reconcile the doc-id namespace",
        purpose="Refuse to build an index whose stored document ids come from a doc-id namespace this version no longer derives, before any doc row is written",
        inputs="stored doc envelope ids, the DocJSON and Markdown files on disk",
        outputs="warnings for structurally malformed stored ids; DocIdNamespaceError when the index is un-migrated",
        critical="This phase must run BEFORE step 5's scanning loop — that loop upserts doc rows as it goes, so a gate placed after it has already changed the index and 'nothing was written' stops being true.  The malformed-id advisory never blocks: it warns and excludes the offending row, so one corrupt id cannot decide the verdict for a whole tree",
    )
    # ------------------------------------------------------------------
    # Doc-namespace reconciliation gate.
    #
    # Runs BEFORE the docs-scanning loop, not beside the advisory below it:
    # the loop calls db.upsert_doc as it goes, so a gate placed after it has
    # already let new-namespace doc rows into the index and "refuses without
    # changing anything" is no longer true.  Refusing here mutates no node
    # row, no docs row, and no file.  Schema init and auto-migration fired at
    # step 2 and are outside that promise.
    #
    # The malformed-ID advisory is separate and never blocks: it warns and
    # excludes the offending row, so a single corrupt id cannot decide the
    # verdict for a whole tree.
    # ------------------------------------------------------------------
    try:
        _stored_doc_ids = set(db.all_doc_ids(db_path))
    except Exception as exc:  # pragma: no cover - unreadable index
        _stored_doc_ids = set()
        warnings.append(f"doc id reconciliation could not read the index: {exc}")
    _malformed = doc_ids.malformed_doc_ids(_stored_doc_ids, project_id)
    for _bad in _malformed:
        warnings.append(
            f"malformed doc id in the index: {_bad!r} — a document id is "
            f"'{project_id}::<path>' with exactly one '::'; skipping this node"
        )
    _stored_doc_ids -= set(_malformed)
    _enumerated_doc_files = doc_ids.enumerate_doc_files(
        project_root, config.scan.docs_dirs, config.scan.docs_extensions, listing=listing
    )
    # Only a file whose retired id the index holds (and not its live one) can
    # count against the verdict, so only those are opened to classify: a
    # migrated index opens none.
    _verdict = doc_ids.reconcile_doc_namespace(
        _stored_doc_ids,
        project_id,
        doc_ids.classify_doc_files(
            doc_ids.namespace_suspects(_stored_doc_ids, project_id, _enumerated_doc_files)
        ).documents,
        doc_ids.enumerate_markdown_files(project_root, config.scan.docs_dirs, listing=listing),
    )
    if _verdict.blocked:
        _sample = ", ".join(f"{o} -> {n}" for o, n in list(zip(_verdict.stale_ids, _verdict.expected_ids))[:3])
        raise doc_ids.DocIdNamespaceError(
            f"This index holds {len(_verdict.stale_ids)} document id(s) under a doc-id namespace "
            f"this version no longer derives, e.g. {_sample}. Building would insert every document "
            "afresh and retire its verification, history and edges. Nothing was written. "
            f"Migrate the index first: preview with `axiom-graph doc-ids preview {project_root}`, "
            f"then run `axiom-graph doc-ids execute {project_root}`."
        )

    口 = Step(
        step_num=5,
        name="Scan doc files",
        purpose="Run doc_scanner on Markdown files and json_doc_scanner on DocJSON files (enumerated once) under every "
        "configured docs root, parsing a file only when its bytes or mtime moved since its last parse, then report "
        "the whole-tree doc-id advisories",
        inputs="docs_dir, stored_mtimes",
        outputs="all_nodes/all_edges extended with doc and section nodes; doc/section records upserted; warnings for missing roots and for dotted DocJSON filenames the store has not seen before; the current dotted set for Step 15",
        critical="The advisories enumerate every DocJSON on disk, not the files walked this build, so an incremental build sees the same set as a full one and ordinary JSON beside the docs never produces one.  A dotted filename is advised once, by the build that first sees it, and not again while it stays.  A doc id is now the configured root plus the file's path within it, so two in-project documents can no longer converge on one identity — the dotted-filename advisory is about a path that misreads, not about a collision",
    )
    # ------------------------------------------------------------------
    # Doc + JSON doc scanners — loop over configured docs_dirs
    # ------------------------------------------------------------------
    docs_scanned = 0
    docs_skipped_mtime = 0
    # Sections actually walked by the JSON doc scanner this build.  Used
    # below by the documents-edge reconciliation pass.  Sections inside
    # mtime-skipped files are intentionally NOT included — they keep
    # their existing edges untouched (see ADR-013, edge case 1 in pitch).
    scanned_section_ids: set[str] = set()
    # DocJSON doc envelope ids walked this build — drives the
    # vanished-section pruning pass below.
    scanned_doc_ids: set[str] = set()
    seen_docs_roots: set[Path] = set()
    for rel_docs in config.scan.docs_dirs:
        docs_dir = (project_root / rel_docs).resolve()
        if docs_dir in seen_docs_roots:
            continue
        seen_docs_roots.add(docs_dir)
        if not docs_dir.is_dir():
            # Only surface a user-visible warning when the user explicitly
            # configured ``docs_dirs`` in axiom-graph.toml.  A missing default
            # ``docs/`` directory is silent (preserves backward-compat:
            # projects that have never had a docs/ dir must continue to
            # build with no warnings).
            if config.scan.docs_dirs_explicit:
                warnings.append(f"docs_dirs entry not found or not a directory: {rel_docs}")
            else:
                logger.debug("docs_dirs default entry missing (silent): %s", rel_docs)
            continue
        try:
            nodes, edges, md_skipped = doc_scanner.scan_docs(
                docs_dir,
                project_root,
                project_id,
                stored_mtimes=stored_mtimes if discovery_only else None,
                docs_root_entry=doc_ids.normalise_root_entry(rel_docs),
                parse_filter=discovery.should_parse,
                listing=listing,
            )
            all_nodes.extend(nodes)
            all_edges.extend(edges)
            docs_scanned += len(nodes)
            docs_skipped_mtime += md_skipped
        except Exception as exc:  # pragma: no cover
            warnings.append(f"doc_scanner failed on {rel_docs}/: {exc}")
            logger.warning("doc_scanner error: %s", exc)

        try:
            j_nodes, j_edges, doc_recs, sec_recs, json_skipped = json_doc_scanner.scan_json_docs(
                docs_dir,
                project_root,
                project_id,
                stored_mtimes=stored_mtimes if discovery_only else None,
                docs_root_entry=doc_ids.normalise_root_entry(rel_docs),
                extensions=config.scan.docs_extensions,
                warnings=warnings,
                parse_filter=discovery.should_parse,
                listing=listing,
            )
            all_nodes.extend(j_nodes)
            all_edges.extend(j_edges)
            docs_scanned += len(j_nodes)
            docs_skipped_mtime += json_skipped
            # Capture every section walked this build — including those
            # whose ``links`` array is empty (they emit zero documents
            # edges, so deriving from ``all_edges`` would miss them).
            scanned_section_ids.update(rec["id"] for rec in sec_recs)
            scanned_doc_ids.update(rec["id"] for rec in doc_recs)
            with db._connect(db_path) as conn:
                for rec in doc_recs:
                    db.upsert_doc(conn, rec)
        except Exception as exc:  # pragma: no cover
            warnings.append(f"json_doc_scanner failed on {rel_docs}/: {exc}")
            logger.warning("json_doc_scanner error: %s", exc)

    # ------------------------------------------------------------------
    # Whole-tree doc-id signals (advisory; neither alters a derived id).
    #
    # A doc id is the configured root the file was found under, verbatim,
    # followed by the file's path within that root with ``/`` retained.
    # For an in-project root that body simply *is* the file's
    # project-relative path, and ``enumerate_doc_files`` deduplicates by
    # resolved path — so two distinct in-project documents can no longer
    # derive one identity.  The collision arm below is kept only as a
    # defensive check: no known root configuration reaches it.  See
    # ``doc_ids.live_doc_id_index`` for the reasoning in full.
    #
    # The dotted-filename arm is the one that still fires routinely, for a
    # different reason than it used to: the dots are carried into the id
    # verbatim, so ``templates.protocol.json`` and ``templates/protocol.json``
    # are two documents rather than one.  What is left is a path that
    # misreads — the id *looks* nested when it is not, which misleads prose
    # references, hand-written links, and any tool that splits an id on
    # ``.``.
    #
    # This pass enumerates every DocJSON *on disk* rather than the files
    # walked above, so an incremental build reports a finding against a
    # file the mtime fast-pass skipped just as a full build does.  Only
    # DocJSON documents are counted: a project is free to keep ordinary
    # JSON beside its docs, and those files never become doc nodes, so a
    # finding about them would always be a false positive.
    #
    # Dotted filenames are file-level findings in the annotation findings
    # store: one is advised on by the build that first sees it, and not
    # again while it stays on disk.  The store is read here, after the
    # doc-id namespace gate; this build writes it only at its last step.
    # ------------------------------------------------------------------
    stored_annotations = db.read_annotation_store(db_path)
    current_dotted: list[str] | None = None
    try:
        # A dotted file the last build found to be a document, whose bytes the
        # walk found unchanged since its last parse, is a document still: its
        # suspect classification is answered without opening it again.
        _known_documents = {
            loc
            for loc in stored_annotations.dotted
            if loc in discovery.observed and loc not in discovery.parsed and not discovery.observed[loc].missing
        }
        _signals = doc_ids.doc_id_signals(project_id, _enumerated_doc_files, known_documents=_known_documents)
        for _collision in _signals.collisions:
            _winner = doc_ids.extension_collision_winner(_collision.sources)
            if _winner is not None:
                warnings.append(
                    f"duplicate doc id {_collision.doc_id} derived from "
                    f"{len(_collision.sources)} files: {', '.join(_collision.sources)} — the extension "
                    f"never reaches a doc id, so only one can own it; indexed {_winner}.  Delete or "
                    "rename the other file"
                )
                continue
            warnings.append(
                f"duplicate doc id {_collision.doc_id} derived from "
                f"{len(_collision.sources)} files: {', '.join(_collision.sources)} — only one "
                "can own it.  The per-root path derivation should make this impossible, so "
                "please report it as a bug"
            )
        if config.scan.docs_extensions_dropped:
            warnings.append(
                "[axiom_graph.scan] docs_extensions: ignored "
                f"{', '.join(repr(x) for x in config.scan.docs_extensions_dropped)} — only "
                f"{', '.join(doc_ids.DOCJSON_EXTENSIONS)} are DocJSON extensions"
            )
        current_dotted = list(_signals.dotted)
        _seen_dotted = set(stored_annotations.dotted)
        for _dotted in current_dotted:
            if _dotted in _seen_dotted:
                continue
            warnings.append(
                f"dotted DocJSON filename {_dotted} — the dots are kept verbatim in the derived "
                "doc id, so this no longer collides with a same-named directory; it does make "
                "the id read as a nested path it is not, which misleads prose references and "
                "anything that splits an id on '.'; consider hyphens"
            )
    except Exception as exc:  # pragma: no cover
        warnings.append(f"doc id overlap scan failed: {exc}")
        logger.warning("doc id overlap scan error: %s", exc)

    口 = Step(
        step_num=6,
        name="Scan config directories",
        purpose="Scan .claude/ and other config dirs for settings, skills, and hook files, parsing a file only when "
        "its bytes or mtime moved since its last parse",
        inputs="project_root, stored_mtimes",
        outputs="all_nodes extended with config nodes",
    )
    # ------------------------------------------------------------------
    # Config scanner — .claude/ directory (if present)
    # ------------------------------------------------------------------
    config_scanned = 0
    config_skipped_mtime = 0
    config_dir_entries = [(rel, "config") for rel in config.scan.config_dirs]
    seen_config_roots: set[Path] = set()
    for config_rel, config_prefix in config_dir_entries:
        config_dir = (project_root / config_rel).resolve()
        if config_dir in seen_config_roots:
            continue
        seen_config_roots.add(config_dir)
        if not config_dir.is_dir():
            # Same rationale as docs_dirs above: only warn when the user
            # explicitly configured config_dirs in axiom-graph.toml.  Missing
            # default ``.claude/`` must be silent.
            if config.scan.config_dirs_explicit:
                warnings.append(f"config_dirs entry not found or not a directory: {config_rel}")
            else:
                logger.debug("config_dirs default entry missing (silent): %s", config_rel)
            continue
        try:
            c_nodes, c_edges, c_skipped = config_scanner.scan_config_dir(
                config_dir,
                project_root,
                project_id,
                prefix=config_prefix,
                stored_mtimes=stored_mtimes if discovery_only else None,
                skip_dirs=skip_dirs,
                parse_filter=discovery.should_parse,
                listing=listing,
            )
            all_nodes.extend(c_nodes)
            all_edges.extend(c_edges)
            config_scanned += len(c_nodes)
            config_skipped_mtime += c_skipped
        except Exception as exc:  # pragma: no cover
            warnings.append(f"config_scanner failed on {config_rel}/: {exc}")
            logger.warning("config_scanner error on %s: %s", config_rel, exc)

    # delegates_to edges are produced inline by module_scanner
    # (in _extract_step_nodes) from the AST walk — no cross-DB read.

    口 = Step(
        step_num=7,
        name="Batch upsert nodes and edges",
        purpose=(
            "Validate ontology constraints and upsert all discovered nodes (single transaction) then "
            "edges (single transaction), then resolve delegate and validates links whose target is no "
            "live node by following the re-export relation"
        ),
        inputs=(
            "all_nodes, all_edges; a pre-upsert snapshot of the index rows this build's nodes and edges name and of "
            "every row in the files it rewrites, deletes or re-keys; any other id looked up in the index on demand "
            "(live-node set)"
        ),
        outputs=(
            "nodes_written, nodes_skipped, edges_written, edges_skipped, delegate_targets_resolved, "
            "validates_targets_resolved counts updated"
        ),
        critical=(
            "A link target counts only if it is a live node: produced by this build's scan, or held by the "
            "index for a file this build did not rescan that still exists and is not NOT_FOUND. The resolver "
            "reads the re-export relation from the index, never from this build's scan output, and rewrites "
            "all_edges so every later pass sees the corrected targets. A validates link is written only when "
            "its target is live or resolves; a delegate link is always written"
        ),
    )
    # ------------------------------------------------------------------
    # Validate ontology and upsert nodes (single transaction)
    # ------------------------------------------------------------------
    walked_locations: set[str] = {loc for loc, obs in discovery.observed.items() if not obs.missing}
    # What this build changed, by file: the files it parsed, and the indexed
    # files the walk did not see that are gone from disk.  The walk already
    # read every walked file, so only the others are checked for existence.
    changed_locations: set[str] = set(discovery.parsed) | {node.location for node in all_nodes if node.location}
    with db._connect(db_path) as conn:
        # Capture what this build needs of the pre-build index, before the
        # upserts: whether it held anything, its files (one index step each),
        # and the rows the live-node set must judge as they stood -- the ids
        # this build's nodes and edges name, and every row in the files the
        # build rewrites, deletes or re-keys.  The ids of this build's nodes
        # that the index already held let the rename matcher's newly-appeared
        # target guard tell a fresh node from a re-scanned one.
        index_had_nodes = db.index_has_nodes_conn(conn)
        indexed_locations = db.distinct_locations_conn(conn) if index_had_nodes else []
        vanished_locations: set[str] = {
            loc
            for loc in indexed_locations
            if loc
            and _normalized_location(loc) not in walked_locations
            and loc not in _VIRTUAL_LOCATIONS
            and not (project_root / loc).exists()
        }
        excluded_locations = {
            loc
            for loc in indexed_locations
            if config.scan.exclude_dirs
            and any(d in loc.replace("\\", "/").split("/") for d in config.scan.exclude_dirs)
        }
        touched = {_normalized_location(loc) for loc in changed_locations | vanished_locations | excluded_locations}
        snapshot_ids = {node.id for node in all_nodes} | {
            end for edge in all_edges for end in (edge.from_id, edge.to_id)
        }
        snapshot = (
            db.get_liveness_rows_conn(
                conn,
                node_ids=snapshot_ids,
                locations=[loc for loc in indexed_locations if _normalized_location(loc) in touched],
            )
            if index_had_nodes
            else {}
        )
        existing_ids_before: set[str] = {node.id for node in all_nodes if node.id in snapshot}
        # Indexed text of the DocJSON sections about to be upserted, read
        # before the upsert absorbs any change -- the stamp check below needs
        # to know which sections are new or changed at this build.
        stored_section_text = doc_stamps.stored_section_texts(
            conn, [n.id for n in all_nodes if getattr(n, "subtype", None) == "docjson_section"]
        )
        for node in all_nodes:
            口 = AutoStep(step_num=7.1, name="Upsert node")
            written = db.upsert_node_conn(conn, node, discovery_only=discovery_only, git_sha=git_sha)
            if written:
                nodes_written += 1
            else:
                nodes_skipped += 1
    live_types = LiveNodeLookup(
        db_path,
        project_root,
        all_nodes,
        walked=walked_locations,
        snapshot=snapshot,
        snapshot_ids=snapshot_ids,
        touched=touched,
        index_empty=not index_had_nodes,
    )

    # The links a re-parse forced by a moved scan scheme or moved import roots
    # gains are found by diffing against the links stored before the write.
    links_before: set[tuple[str, str, str]] | None = None
    if scan_scheme_moved and index_had_nodes:
        with db._connect(db_path) as conn:
            links_before = _stored_scanner_links_conn(conn, {node.id for node in all_nodes})

    # ------------------------------------------------------------------
    # Validate ontology and upsert edges (single transaction)
    # ------------------------------------------------------------------
    with db._connect(db_path) as conn:
        kept_edges: list = []
        for edge in all_edges:
            from_type = live_types.get(edge.from_id)
            to_type = live_types.get(edge.to_id)

            # validates edges are auto-generated by AST call analysis and may
            # target names that are not indexed functions (e.g. classes, constants,
            # fixture-mediated calls), or names a package only re-exports.  Hold
            # them back rather than warn: the resolution pass below writes the
            # ones the re-export relation resolves, and the rest stay a silent skip.
            if edge.edge_type == "validates" and to_type is None:
                edges_skipped += 1
                continue

            if from_type and to_type:
                if not valid_edge(edge.edge_type, from_type, to_type):
                    msg = (
                        f"Ontology violation: edge '{edge.edge_type}' "
                        f"from '{edge.from_id}' ({from_type}) "
                        f"to '{edge.to_id}' ({to_type})"
                    )
                    warnings.append(msg)
                    logger.warning(msg)

            口 = Step(
                step_num=7.2,
                name="Keep edge",
                purpose="Keep the edge for the batched write, in scan order",
            )
            kept_edges.append(edge)
        # One write for the kept edges, in scan order: the same rows and rowids
        # as one upsert per edge, with the existence reads batched.  An edge is
        # a write when its id is new to the index and to the batch so far --
        # the count of distinct new ids -- and otherwise a skip.
        new_edge_ids = db.upsert_edges_conn(conn, kept_edges)
        edges_written += new_edge_ids
        edges_skipped += len(kept_edges) - new_edge_ids

    # ------------------------------------------------------------------
    # Link-target resolution — a scanner sees one file at a time and cannot
    # know where a re-exported symbol is defined, so a call routed through a
    # re-export names a node that was never minted.  After the upserts
    # above, the index holds every node and every re-export marker, and the
    # closure can be walked.  Deliberately not a phase of its own:
    # renumbering the steps below it would strand the existing step nodes,
    # which is the very surface this pass exists to repair.
    # ------------------------------------------------------------------
    targets_resolved = _resolve_delegate_targets(
        db_path,
        all_edges,
        warnings,
        live_ids=live_types,
        rescanned_ids={node.id for node in all_nodes},
    )
    delegate_targets_resolved = targets_resolved["delegates_to"]
    validates_targets_resolved = targets_resolved["validates"]
    # A resolved validates link was counted as skipped when the loop held it;
    # it becomes a write only if its upsert added a row, as in the loop.
    edges_skipped -= targets_resolved["validates_written"]
    edges_written += targets_resolved["validates_written"]
    if delegate_targets_resolved or validates_targets_resolved:
        logger.info(
            "link resolver: retargeted %d delegate and %d validates link(s) through the re-export closure",
            delegate_targets_resolved,
            validates_targets_resolved,
        )

    口 = Step(
        step_num=8,
        name="Rename detection",
        purpose="Detect renames by scoped similarity: the nodes lost from the files this build parsed or found gone, "
        "matched against the nodes those files gained, with git SHAs read in one batch",
        critical="The pools come only from parsed or vanished files, the pools a full build forms for them; a file "
        "left unparsed keeps every node it holds, so it never feeds a false rename",
    )
    # ------------------------------------------------------------------
    # Scoped-similarity rename detection (replaces the exact-code_hash lookup)
    # ------------------------------------------------------------------
    # A node disappeared and a node appeared -- are they the same symbol?  Git
    # is a scope reducer (keeps pools tiny); a single difflib body-similarity
    # ratio is the decision; exact code_hash equality is the 1.0 fast path and
    # the degraded-scope fallback.  See axiom_graph/index/rename_matcher.py.
    nodes_renamed = 0
    renamed_new_ids: list[str] = []
    renamed_old_ids: list[str] = []
    rename_skipped_reasons: dict[str, int] = {}
    # A build whose files only vanished parses nothing, yet the vanished
    # files' nodes form a lost pool as they do in a full build.
    if changed_locations or vanished_locations:
        try:
            import json as _json
            import re as _re

            from axiom_graph.index import rename_matcher as _rm  # noqa: PLC0415
            from axiom_graph.index.git_utils import get_git_sha  # noqa: PLC0415

            _PROC = ("atomic_process", "composite_process")

            def _line_range(node):
                loc = getattr(node, "level_3_location", None) or ""
                m = _re.search(r"#L(\d+)(?:-L?(\d+))?", loc)
                if m:
                    s = int(m.group(1))
                    e = int(m.group(2)) if m.group(2) else s
                    return s, e
                return None, None

            口 = AutoStep(step_num=9, name="Build scope-reduced lost/found pools")
            # The lost pool: process nodes stored at a file this build parsed or
            # found gone that this scan did not produce -- the pool a full build
            # forms for those files, so an incremental build detects the same
            # renames.  A file left unparsed keeps every node it holds.
            scanned_ids: set[str] = {n.id for n in all_nodes}
            with db._connect(db_path) as conn:
                lost_rows = [
                    old
                    for old in db.get_nodes_at_locations_conn(
                        conn, changed_locations | vanished_locations, node_types=_PROC
                    )
                    if old.id not in scanned_ids and getattr(old, "code_hash", None)
                ]
                lost_shas = db.latest_git_shas_conn(conn, [old.id for old in lost_rows], window=50)

            lost_nodes: list = []
            for old in lost_rows:
                s, e = _line_range(old)
                sha = lost_shas.get(old.id)
                lost_nodes.append(
                    _rm.LostNode(
                        node_id=old.id,
                        code_hash=old.code_hash,
                        location=(old.location or "").replace("\\", "/"),
                        start_line=s,
                        end_line=e,
                        git_sha=sha,
                    )
                )

            if lost_nodes:
                _file_cache: dict[str, list[str]] = {}
                found_nodes: list = []
                for node in all_nodes:
                    if node.node_type not in _PROC or not getattr(node, "code_hash", None):
                        continue
                    s, e = _line_range(node)
                    loc = (node.location or "").replace("\\", "/")
                    body = ""
                    try:
                        if loc not in _file_cache:
                            _file_cache[loc] = (
                                (project_root / loc)
                                .read_text(encoding="utf-8", errors="replace")
                                .splitlines(keepends=True)
                            )
                        lines = _file_cache[loc]
                        body = "".join(lines[(s - 1 if s else 0) : (e if e else len(lines))])
                    except Exception:
                        body = ""
                    found_nodes.append(
                        _rm.FoundNode(
                            node_id=node.id,
                            code_hash=node.code_hash,
                            location=loc,
                            body=body,
                            is_new=node.id not in existing_ids_before,
                        )
                    )

                cfg = AxiomGraphConfig.load(project_root)
                since_sha = get_git_sha(project_root)
                no_git = since_sha is None
                adapter = _rm.CodeRenameAdapter(
                    db_path,
                    project_root,
                    lost_nodes,
                    found_nodes,
                    since_sha,
                    cfg.rename.code_threshold,
                    no_git=no_git,
                )
                口 = AutoStep(step_num=10, name="Run matcher + apply renames")
                match = _rm.run_matcher(adapter, pool_cap=cfg.rename.pool_cap)
                nodes_renamed = len(match.applied)
                renamed_new_ids = list(adapter.applied_new_ids)
                renamed_old_ids = [applied.old_id for applied in match.applied]
                rename_skipped_reasons = dict(match.degraded_scopes)

                # Per-node durable suspect signal (D-3): a lost node that fell
                # back to exact-hash in a degraded scope and found no match.
                if match.skipped:
                    _now = db._now_utc()
                    with db._connect(db_path) as conn:
                        for sk in match.skipped:
                            conn.execute(
                                "INSERT INTO node_history "
                                "(node_id, change_type, scanned_at, git_sha, meta) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (
                                    sk.node_id,
                                    "RENAME_SCORING_SKIPPED",
                                    _now,
                                    since_sha,
                                    _json.dumps({"reason": sk.reason, "candidates": sk.candidates}),
                                ),
                            )

                if nodes_renamed:
                    logger.info(
                        "rename matcher: auto-applied %d (revertable) · %d became NOT_FOUND",
                        nodes_renamed,
                        len(match.not_found),
                    )
                for old_id, new_id, unreadable, not_patched in adapter.links_not_patched:
                    warnings.extend(
                        f"rename {old_id} -> {new_id}: {line}"
                        for line in link_rewrite_warnings(unreadable, not_patched, old_id, new_id)
                    )
                if rename_skipped_reasons:
                    parts = ", ".join(f"{r}={c}" for r, c in sorted(rename_skipped_reasons.items()))
                    msg = f"similarity skipped for {sum(rename_skipped_reasons.values())} scope(s): {parts}"
                    logger.info("rename matcher: %s", msg)
                    warnings.append(msg)
        except Exception as exc:  # pragma: no cover
            warnings.append(f"rename detection failed: {exc}")
            logger.warning("rename detection error: %s", exc)

    # ------------------------------------------------------------------
    # Scan baseline for new tests.  Runs after rename detection so a
    # renamed or moved test (which keeps its old verification) is never
    # taken for a first insert.  Not a phase of its own (nor is the stamp
    # check below); the drift sweep after them is step 11.
    # The caller re-stamps exactly these ids after staleness is recorded.
    # ------------------------------------------------------------------
    # An index that was empty before this build (init, first build) has no
    # change history, so nothing can be LINKED_STALE yet; baselining there
    # would only fill the agent-verified sign-off queue with every test.
    scan_baselined_ids = (
        _baseline_new_tests(db_path, all_nodes, existing_ids_before | set(renamed_new_ids), git_sha, project_root)
        if index_had_nodes
        else []
    )
    if links_before is not None:
        scan_baselined_ids += _baseline_gained_links(
            db_path, project_root, {node.id for node in all_nodes}, links_before, git_sha
        )

    # Same region, same reason: check every DocJSON section this build
    # scanned against its tool-write stamp.  Tool-written sections that
    # arrived by merge or pull are verified when their linked code is what
    # they were written against; raw DocJSON edits are recorded once and
    # reported, never verified.  An index that was empty before this build
    # (init, first build) has no legacy baseline to compare unstamped
    # sections against, so the unstamped-section gate is off for it.
    stamp_verified_ids, stamp_text_verified_ids, raw_docjson_edit_ids = _reconcile_doc_stamps(
        db_path,
        project_root,
        all_nodes,
        stored_section_text,
        set(renamed_new_ids),
        legacy_gate=index_had_nodes,
        mode=config.docjson.raw_docjson_edits,
        git_sha=git_sha,
    )
    if raw_docjson_edit_ids:
        warnings.append(doc_stamps.raw_docjson_edit_summary(raw_docjson_edit_ids))

    # Same region again: sections stored own-drifted in files this build did
    # not parse (their mtime stood still) are adopted when their stamp is
    # valid, so a merged tool write an earlier build indexed without adopting
    # is verified now.  The files parsed above were judged by the scan.
    if index_had_nodes:
        try:
            口 = AutoStep(step_num=11, name="Adopt drifted doc stamps")
            adopted = doc_stamps.adopt_drifted_stamps(
                db_path,
                project_root,
                skip_locations={n.location for n in all_nodes if getattr(n, "subtype", None) == "docjson_doc"},
                git_sha=git_sha,
            )
        except Exception as exc:  # pragma: no cover -- the sweep never fails a build
            logger.warning("doc stamp drift sweep failed: %s", exc)
        else:
            stamp_verified_ids.extend(adopted.verified)
            stamp_text_verified_ids.extend(adopted.text_verified)

    口 = AutoStep(step_num=12, name="Purge stale entries and prune vanished doc sections")
    # ------------------------------------------------------------------
    # Purge pass — remove DB rows for files that no longer exist on disk
    # ------------------------------------------------------------------
    nodes_purged = _purge_stale_entries(
        db_path,
        project_root,
        warnings,
        exclude_dirs=config.scan.exclude_dirs,
        git_sha=git_sha,
        walked=walked_locations,
    )

    # ------------------------------------------------------------------
    # Vanished-section pruning pass (ADR-021 / build-path invariant)
    # ------------------------------------------------------------------
    # For every DocJSON doc walked THIS build, any section node in the DB
    # that is no longer in the scan output has vanished from the file
    # (e.g. removed via raw JSON edit) and is cascade-deleted with a
    # preserved DELETED tombstone.  Docs inside mtime-skipped files are
    # not in ``scanned_doc_ids``, so their sections are untouched.
    #
    # This pass and the documents-edge reconciliation below are the two passes
    # that only run for files this build scanned, and both swallow their
    # failures into ``warnings``.  If either fails, the per-file index pass is
    # incomplete and the scan-cache stamp must not fire — see the stamp block.
    per_file_pass_failed = False
    sections_pruned = 0
    if scanned_doc_ids:
        try:
            with db._connect(db_path) as conn:
                for doc_id in scanned_doc_ids:
                    rows = conn.execute(
                        "SELECT id FROM nodes WHERE node_type = 'atomic_process' "
                        "AND subtype IN ('docjson', 'docjson_section') "
                        "AND source IN ('docjson', 'json_doc_scanner') "
                        "AND id LIKE ? ESCAPE '\\'",
                        (doc_id.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "::%",),
                    ).fetchall()
                    for r in rows:
                        if r["id"] not in scanned_section_ids:
                            db.delete_node_by_id(conn, r["id"], reason_meta={"reason": "section removed from file"})
                            sections_pruned += 1
                            logger.info("build: pruned vanished doc section %s", r["id"])
        except Exception as exc:  # pragma: no cover
            per_file_pass_failed = True
            warnings.append(f"vanished-section pruning failed: {exc}")
            logger.warning("vanished-section pruning error: %s", exc)

    # ------------------------------------------------------------------
    # documents-edge reconciliation pass
    # ------------------------------------------------------------------
    # JSON ``links`` arrays are the source of truth for ``documents`` edges.
    # External edits (raw editor, bulk find-replace, manual JSON edits) can
    # leave orphan ``documents`` edges in the DB after upsert.  For every
    # section walked THIS build, diff the DB outbound documents set against
    # the intended set derived from the JSON ``links`` array and delete any
    # orphans.  Sections inside mtime-skipped files are intentionally NOT in
    # ``scanned_section_ids`` so their edges are left untouched.
    #
    # Scope: edge_type='documents' only.  Other edge types (composes,
    # validates, …) are out of scope and untouched by this primitive.
    # See pev-2026-05-15-reconcile-orphan-documents-edges for the full
    # invariant and rationale.
    documents_edges_reconciled = 0
    # Both ends of every edge the reconcilers retire: the statuses those
    # links fed are recomputed by the staleness refresh.
    retired_edge_ends: set[str] = set()
    if scanned_section_ids:
        # Pre-compute intended targets per scanned section from this build's
        # all_edges (∅ is correct for empty-links sections).
        intended_by_section: dict[str, set[str]] = {sid: set() for sid in scanned_section_ids}
        for e in all_edges:
            if e.edge_type == "documents" and e.from_id in intended_by_section:
                intended_by_section[e.from_id].add(e.to_id)

        try:
            with db._connect(db_path) as conn:
                for section_id, intended in intended_by_section.items():
                    current = db.get_outbound_documents_targets_conn(conn, section_id)
                    orphans = current - intended
                    for target in orphans:
                        if db.delete_documents_edge_conn(conn, section_id, target):
                            documents_edges_reconciled += 1
                            retired_edge_ends.update((section_id, target))
                            logger.info(
                                "documents-edge reconciler: removed orphan %s -> %s",
                                section_id,
                                target,
                            )
            if documents_edges_reconciled > 0:
                logger.info(
                    "documents-edge reconciler: reconciled %d orphan documents edges",
                    documents_edges_reconciled,
                )
        except Exception as exc:
            per_file_pass_failed = True
            warnings.append(f"documents-edge reconciliation failed: {exc}")
            logger.warning("documents-edge reconciliation error: %s", exc)

    口 = Step(
        step_num=13,
        name="Reconcile scanner-derived edges",
        purpose=(
            "For every source this build walked, delete the scoped scanner edges its file no "
            "longer justifies, then report any leftovers the walked-source scoping cannot reach"
        ),
        inputs="all_nodes (walked sources), all_edges (intended targets), _RECONCILED_SCANNER_EDGE_TYPES",
        outputs="scanner_edges_reconciled, surplus_delegate_edges, a one-line notice when leftovers remain",
        critical="Gated on this build's NODE output, never its edge output; joins the per-file failure guard",
    )
    # ------------------------------------------------------------------
    # scanner-edge reconciliation pass
    # ------------------------------------------------------------------
    # Source files are the source of truth for the edges the scanners derive
    # from them.  An edge's identity includes its target, so retargeting a
    # step's next call mints a NEW edge row and leaves the old one behind —
    # node upsert cannot retire it, and purge cannot either (both endpoints
    # are alive in files that still exist).  For every source walked THIS
    # build, diff the DB's outbound set for each scoped edge type against the
    # set this build's scan intended, and delete the difference.
    #
    # Scope: ``_RECONCILED_SCANNER_EDGE_TYPES`` only (``delegates_to`` and
    # ``validates``) — every other edge type is untouched.
    #
    # Two properties are load-bearing, both copied from the documents
    # reconciler above:
    #
    #   * The walked-source gate comes from this build's NODE output
    #     (``all_nodes``), never its edge output.  A step that was walked but
    #     resolves no delegate emits zero edges; deriving the gate from edges
    #     would drop it out of scope and preserve its superseded edge forever.
    #   * Sources absent from this build's output — mtime-skipped files, files
    #     a scanner raised on — are out of scope entirely, NOT treated as
    #     intending ∅.  Reversing that silently strips live edges from every
    #     unchanged file.
    scanner_edges_reconciled = 0
    walked_source_ids: set[str] = {n.id for n in all_nodes}
    if walked_source_ids:
        # Intended targets per (edge_type, source) from this build's scan.
        # A walked source missing from this map intends ∅ and is reconciled
        # to ∅ — it stays in scope because the gate is the node set.
        intended_by_source: dict[tuple[str, str], set[str]] = {}
        for e in all_edges:
            if e.edge_type in _RECONCILED_SCANNER_EDGE_TYPES and e.from_id in walked_source_ids:
                intended_by_source.setdefault((e.edge_type, e.from_id), set()).add(e.to_id)

        try:
            with db._connect(db_path) as conn:
                for edge_type in sorted(_RECONCILED_SCANNER_EDGE_TYPES):
                    # Only sources that actually hold a stored edge of this
                    # type can have a superseded one; intersecting with the
                    # walked set keeps the pass O(offenders), not O(nodes).
                    candidates = db.get_edge_source_ids_conn(conn, edge_type, among=walked_source_ids)
                    for from_id in candidates:
                        intended = intended_by_source.get((edge_type, from_id), set())
                        current = db.get_outbound_edge_targets_conn(conn, from_id, edge_type)
                        for target in current - intended:
                            if db.delete_edge_conn(conn, from_id, target, edge_type):
                                scanner_edges_reconciled += 1
                                retired_edge_ends.update((from_id, target))
                                logger.info(
                                    "scanner-edge reconciler: removed superseded %s %s -> %s",
                                    edge_type,
                                    from_id,
                                    target,
                                )
            if scanner_edges_reconciled > 0:
                logger.info(
                    "scanner-edge reconciler: retired %d superseded scanner edge(s)",
                    scanner_edges_reconciled,
                )
        except Exception as exc:
            per_file_pass_failed = True
            warnings.append(f"scanner-edge reconciliation failed: {exc}")
            logger.warning("scanner-edge reconciliation error: %s", exc)

    # -- TRANSITIONAL (remove with the edge-repair cycle) --------------
    # Surplus delegate links predating the reconciler survive on files this
    # build never walked, so the walked-source scoping cannot reach them.
    # Report that they exist — one line, no detail; the offender list and the
    # remedy belong to the repair path, built once, there.  Deliberately NOT
    # gated on the walked set: surplus surviving on a source that WAS walked
    # means the reconciler failed, so this one signal covers both.  Scoped to
    # AutoStep sources, which is where cardinality ≤ 1 holds — a state
    # machine's states legitimately delegate once per transition.
    surplus_delegate_edges = 0
    try:
        with db._connect(db_path) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) - 1 AS surplus FROM edges e "
                "JOIN nodes n ON n.id = e.from_id "
                "WHERE e.edge_type = 'delegates_to' AND n.subtype = 'autostep' "
                "GROUP BY e.from_id HAVING COUNT(*) > 1"
            ).fetchall()
        surplus_delegate_edges = sum(r["surplus"] for r in rows)
        if surplus_delegate_edges:
            warnings.append(
                f"{surplus_delegate_edges} surplus workflow delegate link(s) left over on "
                f"{len(rows)} step(s) from builds before they were reconciled — each clears "
                "when its file is next scanned"
            )
    except Exception as exc:  # pragma: no cover -- report-only, never fails a build
        logger.warning("surplus delegate-link count failed: %s", exc)
    # -- end TRANSITIONAL ----------------------------------------------

    口 = AutoStep(step_num=14, name="Reap orphaned workflow step rows")
    # ------------------------------------------------------------------
    # Orphaned step-row reaping pass
    # ------------------------------------------------------------------
    # For every file this build walked, delete the step rows the index stores
    # there that the file's markers no longer justify, then report the
    # leftovers the walked-file scoping cannot reach.  Gated on this build's
    # NODE output: a file absent from it — mtime-skipped, or one whose scanner
    # raised — is out of scope, never treated as intending zero steps.  Joins
    # the per-file failure guard so a reaping failure is retried rather than
    # stamped past.
    #
    # Source files are the source of truth for the step rows the scanners
    # derive from them, and a step row that loses its marker is reachable by
    # nothing else: it can never become NOT_FOUND (steps carry no staleness
    # dimension), so purge refuses it; the location purge only fires when the
    # whole file is gone; and its envelope has usually been deleted along
    # with the function that carried it, taking the ``composes`` edge away.
    # Only the file the marker was declared in still names it, which is why
    # the diff is keyed on location.  Same shape and the same two load-bearing
    # properties as the passes above — the safety argument itself lives in the
    # helper's docstring.
    orphaned_steps_reaped, reap_failed = _reap_orphaned_step_nodes(db_path, all_nodes, warnings)
    per_file_pass_failed = per_file_pass_failed or reap_failed
    if orphaned_steps_reaped:
        warnings.append(
            f"removed {orphaned_steps_reaped} workflow step entr"
            f"{'y' if orphaned_steps_reaped == 1 else 'ies'} the source no longer declares"
        )

    # -- TRANSITIONAL (remove with the full-walk / repair cycle) --------
    # Orphaned step rows predating the reaper survive on files this build
    # never walked, so the walked-file scoping cannot reach them.  Unlike the
    # surplus-delegate notice above, this one carries its remedy: the fast
    # pass is a pure mtime comparison with no content component, so advancing
    # a file's modification time — touching or re-saving it — is enough to
    # force the re-scan that reconciles it.  Naming the carrier files is what
    # makes that actionable, so the list is named and capped rather than
    # summarised as a bare count.
    #
    # Known limitation: this finds only orphans whose envelope is gone, which
    # is what makes it a single query with no scan.  An orphan whose envelope
    # survives — a workflow renumbered so its old step numbers were left
    # behind — still has a ``composes`` parent and is invisible here.  That is
    # accepted: minting one requires editing its file, which makes the next
    # build walk and reap it, so it can only persist while the scanner raises
    # on that file, which is already reported as its own warning.  Touching
    # every source file and rebuilding clears both kinds.
    orphaned_step_rows = 0
    try:
        with db._connect(db_path) as conn:
            orphans_by_file = db.count_parentless_step_nodes_by_location_conn(conn)
        orphaned_step_rows = sum(orphans_by_file.values())
        if orphaned_step_rows:
            named = sorted(orphans_by_file)[:_ORPHAN_NOTICE_FILE_CAP]
            file_list = ", ".join(named)
            overflow = len(orphans_by_file) - len(named)
            if overflow:
                file_list += f" (+{overflow} more)"
            warnings.append(
                f"{orphaned_step_rows} orphaned workflow step entr"
                f"{'y' if orphaned_step_rows == 1 else 'ies'} left over in "
                f"{len(orphans_by_file)} file(s) from builds before they were reconciled: "
                f"{file_list} — touch or re-save each file (updating its modification time is "
                "enough, no edit needed) and rebuild to force the re-scan that clears them"
            )
    except Exception as exc:  # pragma: no cover -- report-only, never fails a build
        logger.warning("orphaned step-row count failed: %s", exc)
    # -- end TRANSITIONAL ----------------------------------------------

    # ------------------------------------------------------------------
    # Annotation findings store: replace the rows of the files this build
    # scanned, drop those of files it no longer walks, resolve B4 against
    # the whole index (every live node, re-exports followed), and report
    # which findings are new.  JS/TS files stay out of scope while the JS
    # scanner is unavailable.
    #
    # A per-file pass like the ones above: it runs before the mtime stamp,
    # and a failed write must not leave a scanned file stamped, or the next
    # build would skip it and ``check`` would trust its stale rows.  So the
    # failure skips the stamp, and the scanned files' stored mtimes are
    # cleared too: ``upsert_node_conn`` stamps a row it inserts, which would
    # otherwise let a file new to the index be skipped.
    # ------------------------------------------------------------------
    hidden_annotation_files = (
        {f for f in stored_annotations.files if f.endswith((".js", ".jsx", ".ts", ".tsx"))}
        if js_scanning_unavailable
        else set()
    )
    口 = AutoStep(step_num=15, name="Record annotation findings")
    annotation_outcome = annotation_findings.record_annotation_findings(
        db_path,
        stored_annotations,
        walked=walked_code_files,
        scanned=scanned_annotations,
        hidden=hidden_annotation_files,
        live_ids=live_types,
        dotted=current_dotted,
        is_rule_enabled=_validation_guard,
        warnings=warnings,
    )
    if not annotation_outcome.recorded:
        per_file_pass_failed = True
        try:
            with db._connect(db_path) as conn:
                for location in scanned_annotations:
                    db.clear_location_file_mtime_conn(conn, location)
        except Exception as exc:  # pragma: no cover -- the stamp is skipped either way
            logger.warning("clearing scanned-file mtimes after a failed findings write failed: %s", exc)

    # ------------------------------------------------------------------
    # Scan-cache stamp — record the on-disk mtime of every file this build
    # actually opened and parsed, so the next build's mtime fast-pass can
    # skip it.  Runs in BOTH build modes: ``upsert_node_conn`` only writes
    # ``file_mtime`` when it inserts a row, so without this pass a node's
    # stored mtime would be frozen at first insertion forever.
    #
    # Runs after — and is guarded on — the five index-integrity passes above
    # that are gated on this build's scanned set: vanished-section pruning,
    # documents-edge reconciliation, scanner-edge reconciliation, orphaned
    # step-row reaping, and the annotation findings store.  All five swallow
    # their failures into ``warnings``, so control reaches here either way.
    # Stamping is a promise that the next build may skip the file entirely,
    # and that promise must not be made while any of them left this file's
    # index incomplete: leaving the stored mtime behind is exactly what makes
    # the next build re-scan the file and retry them.
    # ------------------------------------------------------------------
    file_mtimes_stamped = 0
    parsed_locations = {node.location for node in all_nodes if node.location}
    if per_file_pass_failed:
        logger.warning("build: skipping the scanned-file mtime stamp — a per-file pass failed; the next build re-scans")
    else:
        口 = AutoStep(step_num=16, name="Stamp scanned-file mtimes")
        file_mtimes_stamped = _stamp_scanned_file_mtimes(
            db_path, all_nodes, parsed=discovery.parse_records(parsed_locations)
        )
    _record_walk(db_path, discovery, parsed_locations, stamped=not per_file_pass_failed)
    # Stamped only when every file was parsed by this version's scanners
    # with this build's import roots.
    if scan_scheme_moved and not (per_file_pass_failed or scan_raised or js_scanning_unavailable):
        db.set_index_meta(db_path, SCAN_SCHEME_META_KEY, SCAN_SCHEME)
        db.set_index_meta(db_path, SOURCE_ROOTS_META_KEY, source_roots_fp)

    # ------------------------------------------------------------------
    # Broken-link detection — flag nodes with dangling edges created by
    # file deletions or renames (ADR-013 Layer 2).
    # ------------------------------------------------------------------
    with db._connect(db_path) as conn:
        deleted_ids = db.deleted_node_ids_since_conn(conn, history_mark)
    broken_links_flagged = _flag_broken_links(
        db_path,
        warnings,
        locations=changed_locations if discovery_only else None,
        deleted_ids=deleted_ids,
    )

    口 = Step(
        step_num=17,
        name="Index doc sections into FTS",
        purpose="Re-sync node_fts with the heading and content of the sections of the doc files this build parsed "
        "(every section on a full rescan), so they are discoverable via axiom_graph_search",
    )
    # ------------------------------------------------------------------
    # Doc section FTS indexing — make doc sections searchable
    # ------------------------------------------------------------------
    doc_sections_indexed = 0
    try:
        doc_sections_indexed = db.index_doc_sections_fts(
            db_path, locations=changed_locations if discovery_only else None
        )
        if doc_sections_indexed > 0:
            logger.info("build: indexed %d doc sections into FTS", doc_sections_indexed)
    except Exception as exc:  # pragma: no cover
        warnings.append(f"doc section FTS indexing failed: {exc}")
        logger.warning("doc section FTS indexing error: %s", exc)

    口 = Step(
        step_num=18,
        name="Report unresolved project imports",
        purpose="Warn, in one line naming source_roots, when imports of what looks like the project's own code "
        "resolved to no file and were filed as external packages",
        inputs="walked_code_files, this build's nodes and edges for the files it parsed, the stored per-file "
        "external-import record (index_meta) for the files it skipped",
        outputs="one warning with the count, when it is above zero; the per-file external-import record rewritten",
        critical="Counts an external import only while a walked file's current parse still makes it, so an import "
        "the fix has resolved never counts; a name counts only when it is a top-level package or module name of the "
        "walked Python tree (skip_dirs applied), so a plain third-party import never does.  Report-only: never "
        "fails a build",
    )
    try:
        unresolved = _unresolved_project_imports(
            db_path, project_root, walked_code_files, parsed_locations, all_nodes, all_edges
        )
        if unresolved:
            warnings.append(
                f"{len(unresolved)} imported package name(s) that look like this project's own code resolved to "
                f"no file ({', '.join(sorted(unresolved)[:5])}{', ...' if len(unresolved) > 5 else ''}) and are "
                "linked as external packages, so their tests and callers get no edges — list the directories "
                "your imports start from under [axiom_graph.scan] source_roots in axiom-graph.toml"
            )
    except Exception as exc:  # pragma: no cover -- report-only, never fails a build
        logger.warning("unresolved-import count failed: %s", exc)

    elapsed = time.monotonic() - t0
    logger.info(
        "build: done (%.3fs, %d nodes written, %d skipped, %d edges, %d renamed, %d purged)",
        elapsed,
        nodes_written,
        nodes_skipped,
        edges_written,
        nodes_renamed,
        nodes_purged,
    )

    return {
        "nodes_written": nodes_written,
        "nodes_skipped": nodes_skipped,
        "edges_written": edges_written,
        "edges_skipped": edges_skipped,
        "nodes_renamed": nodes_renamed,
        "renamed_new_ids": renamed_new_ids,
        "scan_baselined_ids": scan_baselined_ids,
        "stamp_verified_ids": stamp_verified_ids,
        "stamp_text_verified_ids": stamp_text_verified_ids,
        "raw_docjson_edit_ids": raw_docjson_edit_ids,
        "nodes_purged": nodes_purged,
        "file_mtimes_stamped": file_mtimes_stamped,
        "documents_edges_reconciled": documents_edges_reconciled,
        "delegate_targets_resolved": delegate_targets_resolved,
        "validates_targets_resolved": validates_targets_resolved,
        "scanner_edges_reconciled": scanner_edges_reconciled,
        "orphaned_steps_reaped": orphaned_steps_reaped,
        "orphaned_step_rows": orphaned_step_rows,
        "surplus_delegate_edges": surplus_delegate_edges,
        "broken_links_flagged": broken_links_flagged,
        "doc_sections_indexed": doc_sections_indexed,
        "files_scanned": files_scanned,
        "files_skipped_mtime": files_skipped_mtime,
        "docs_skipped_mtime": docs_skipped_mtime,
        "config_scanned": config_scanned,
        "config_skipped_mtime": config_skipped_mtime,
        "js_scanned": js_scanned,
        "js_skipped_mtime": js_skipped_mtime,
        "warnings": warnings,
        "annotation_findings": annotation_outcome.findings,
        "annotation_findings_new": annotation_outcome.new,
        "annotation_findings_resolved": annotation_outcome.resolved,
        # Internal: what the staleness refresh after the build is seeded with.
        "changed_locations": sorted(changed_locations),
        # Internal: what the walk read, so the refresh reads no walked file again.
        "discovery_observed": discovery.observed,
        "deleted_ids": sorted(deleted_ids),
        "staleness_seed_ids": sorted(
            {end for edge in all_edges for end in (edge.from_id, edge.to_id)}
            | retired_edge_ends
            | ({node.id for node in all_nodes} - existing_ids_before)
            | set(renamed_old_ids)
            | set(renamed_new_ids)
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Depth cap for the re-export closure walk.  Deep enough for the shim
# chains that occur in practice (aggregator on top of aggregator), shallow
# enough that a pathological package tree cannot make a build crawl.
_REEXPORT_CLOSURE_MAX_DEPTH = 8


# Hop kinds in the re-export relation.  At one depth a name a module binds
# explicitly outranks a name that might arrive through a star import.
_NAMED_HOP = 0
_STAR_HOP = 1

# Node locations that name no file.  A virtual location is never rescanned
# and never gone, so the file checks of the live-node set leave it alone.
_VIRTUAL_LOCATIONS: frozenset[str] = frozenset({"", "external"})


def _top_level_python_names(walked: Iterable[str]) -> set[str]:
    """Return the names an absolute import could start with, judged from the walked Python tree.

    A directory or module counts when the directory holding it is not a
    regular package (has no ``__init__.py``): it is then the top of an import
    path under some directory, whether or not that directory is a known
    import root.  The project root's children always count.

    Args:
        walked: Project-relative POSIX paths of the code files the build walked.

    Returns:
        The candidate top-level package and module names.
    """
    py_files = [p for p in walked if p.endswith(".py")]
    package_dirs = {p.rsplit("/", 1)[0] for p in py_files if p.endswith("/__init__.py")}
    names: set[str] = set()
    for path in py_files:
        parts = path.split("/")
        for i, part in enumerate(parts):
            if i and "/".join(parts[:i]) in package_dirs:
                continue
            name = part if i < len(parts) - 1 else part[: -len(".py")]
            if name and name != "__init__":
                names.add(name)
    return names


def _unresolved_project_imports(
    db_path: Path,
    project_root: Path,
    walked: Collection[str],
    parsed_locations: Collection[str],
    built_nodes: Iterable,
    built_edges: Iterable,
) -> set[str]:
    """Return the external-package names that look like the project's own code.

    An external import is in play while a walked file's current parse
    makes it: this build's edges speak for the files it parsed, the record
    under :data:`EXTERNAL_IMPORTS_META_KEY` for the walked files it skipped
    (the external stubs and their edges do not outlive a build, so the
    stored graph cannot).  The record is rewritten here: parsed files
    replace their entry, files no longer walked drop out.  Standard-library
    names are never recorded.  An import that now resolves is therefore
    never counted.  A name counts when it is a top-level package or module
    name of the walked Python tree (:func:`_top_level_python_names`) and not
    a dependency the project declares in ``pyproject.toml``
    (:func:`declared_dependencies`): an import of a declared dependency is
    external even when a copy of its source sits in the tree.

    Args:
        db_path: The index DB.
        project_root: Project root, whose ``pyproject.toml`` declares the dependencies.
        walked: Code files this build walked.
        parsed_locations: Locations this build produced nodes for.
        built_nodes: The nodes this build's scanners produced.
        built_edges: The edges this build's scanners produced.

    Returns:
        The top-level names of the unresolved imports.
    """
    location_of = {node.id: _normalized_location(node.location) for node in built_nodes}
    fresh: dict[str, set[str]] = {}
    for edge in built_edges:
        if edge.edge_type == "depends_on" and "::external::" in edge.to_id:
            location = location_of.get(edge.from_id, "")
            name = edge.to_id.rsplit("::", 1)[-1]
            if location and name not in sys.stdlib_module_names:
                fresh.setdefault(location, set()).add(name)
    parsed = {_normalized_location(loc) for loc in parsed_locations}
    try:
        stored = json.loads(db.get_index_meta(db_path, EXTERNAL_IMPORTS_META_KEY) or "{}")
    except ValueError:
        stored = {}
    imports: dict[str, list[str]] = {}
    for loc, names in stored.items():
        kept = [name for name in names if name not in sys.stdlib_module_names]
        if kept and loc in walked and loc not in parsed:
            imports[loc] = kept
    imports.update({loc: sorted(names) for loc, names in fresh.items()})
    if imports != stored:
        db.set_index_meta(db_path, EXTERNAL_IMPORTS_META_KEY, json.dumps(imports, sort_keys=True))
    in_play = {name for names in imports.values() for name in names}
    if not in_play:
        return set()
    return (in_play & _top_level_python_names(walked)) - declared_dependencies(project_root)


def _normalized_location(location: str | None) -> str:
    """Return *location* as a forward-slash relative path, ``""`` for none."""
    return (location or "").replace("\\", "/")


class LiveNodeLookup:
    """The build's live-node set, ``{node_id: node_type}``, read from the index on demand.

    A node is live when this build's scan produced it (it takes the scan's
    node type), or when the index held it before the build's upserts, it is
    not NOT_FOUND, and its file was not rescanned by this build and still
    exists.  A node with no file (location ``""`` or ``"external"``) needs
    no file check.  So nodes a rescan of their file no longer produced,
    NOT_FOUND ghosts left by an earlier move, and nodes of files that are
    gone are not live: linking to any of those ties a caller to a
    definition that no longer exists while the moved one goes unlinked.
    "Rescanned" is read from this build's own nodes, so a file the scanner
    raised on contributes none and keeps its index entries live; a scanner
    crash cannot make live targets look missing.

    The set is never loaded whole.  An answer comes from, in order:

    1. this build's own nodes;
    2. a snapshot taken before the build's upserts of the ids its nodes and
       edges name and of every row in the files the build changes (parsed,
       gone from disk, or purged as excluded), the only files whose rows the
       build rewrites, deletes or re-keys;
    3. for any other id, the row the index holds when asked.  A row found in
       one of the snapshotted files was not there before the build, so it is
       not live.  A row elsewhere is one the build left as it was.

    Misses are read in batches by :meth:`prefetch` or one id at a time by
    :meth:`get`, on the connection bound with :meth:`reading_on` or, when
    none is, a block of its own.
    """

    def __init__(
        self,
        db_path: Path,
        project_root: Path,
        all_nodes: list,
        *,
        walked: Collection[str] = (),
        snapshot: dict[str, tuple] | None = None,
        snapshot_ids: Iterable[str] = (),
        touched: Iterable[str] = (),
        index_empty: bool = False,
    ) -> None:
        """Seed the lookup.

        Args:
            db_path: Path to the axiom-graph DB, for reads with no bound connection.
            project_root: Absolute project root, for the file-existence check.
            all_nodes: Every node this build's scanners produced.
            walked: Files the build's walk read from disk this run.
            snapshot: Pre-upsert ``id -> (node_type, location, own_status)``
                rows of *snapshot_ids* and of every node in *touched*.
            snapshot_ids: Ids read into *snapshot*; one with no row there had
                none before the build.
            touched: The files whose every pre-upsert row is in *snapshot*.
            index_empty: The index held no node before the build, so only
                this build's own nodes are live.
        """
        self._db_path = db_path
        self._project_root = project_root
        self._overlay: dict[str, str] = {}
        for node in all_nodes:
            self._overlay[node.id] = node.node_type
        self._rescanned = {_normalized_location(node.location) for node in all_nodes} - _VIRTUAL_LOCATIONS
        self._file_exists: dict[str, bool] = dict.fromkeys(walked, True)
        self._touched = {_normalized_location(loc) for loc in touched}
        self._index_empty = index_empty
        self._conn = None
        self._answers: dict[str, str | None] = {}
        rows = snapshot or {}
        for node_id in snapshot_ids:
            self._answers[node_id] = self._judge(rows.get(node_id))
        for node_id, row in rows.items():
            self._answers[node_id] = self._judge(row)

    def _judge(self, row: tuple | None) -> str | None:
        """Return the node type of a pre-build row that is live, else ``None``."""
        if row is None:
            return None
        node_type, raw_location, own_status = row
        if own_status == NOT_FOUND:
            return None
        location = _normalized_location(raw_location)
        if location not in _VIRTUAL_LOCATIONS:
            if location in self._rescanned:
                return None
            if location not in self._file_exists:
                self._file_exists[location] = (self._project_root / location).exists()
            if not self._file_exists[location]:
                return None
        return node_type

    @contextlib.contextmanager
    def reading_on(self, conn):
        """Read misses on *conn* for the duration of the block."""
        previous, self._conn = self._conn, conn
        try:
            yield self
        finally:
            self._conn = previous

    def prefetch(self, node_ids: Iterable[str]) -> None:
        """Answer every id in *node_ids* not answered yet, in one batched read."""
        missing = [i for i in dict.fromkeys(node_ids) if i not in self._overlay and i not in self._answers]
        if not missing:
            return
        if self._index_empty:
            rows: dict[str, tuple] = {}
        elif self._conn is not None:
            rows = db.get_liveness_rows_conn(self._conn, node_ids=missing)
        else:
            with db._connect(self._db_path) as conn:
                rows = db.get_liveness_rows_conn(conn, node_ids=missing)
        for node_id in missing:
            row = rows.get(node_id)
            if row is not None and _normalized_location(row[1]) in self._touched:
                row = None
            self._answers[node_id] = self._judge(row)

    def get(self, node_id: str, default: str | None = None) -> str | None:
        """Return the node type of *node_id* when it is live, else *default*."""
        if node_id in self._overlay:
            return self._overlay[node_id]
        if node_id not in self._answers:
            self.prefetch([node_id])
        answer = self._answers[node_id]
        return default if answer is None else answer

    def __contains__(self, node_id: object) -> bool:
        """Return whether *node_id* names a live node."""
        return isinstance(node_id, str) and self.get(node_id) is not None


def resolve_symbol_through_reexports(
    module_id: str,
    symbol: str,
    node_exists,
    reexport_sources,
    max_depth: int = _REEXPORT_CLOSURE_MAX_DEPTH,
) -> str | None:
    """Follow a module's re-export relation looking for one that defines *symbol*.

    A module that re-exports another's names defines nothing itself, so a
    call routed through one names a node that was never minted.  The
    definition is found by walking outward from the guessed module along
    the re-export relation.

    Each hop carries the symbol with it, because a named re-export may bind
    a name under an alias (``from .impl import real as alias``): the next
    module is searched for the name it was bound from, not the one the
    caller used.  The relation is supplied by the caller, so the kinds of
    re-export it knows can grow without touching this walk.

    Breadth-first, so the nearest definition wins.  Among candidates at the
    same distance a named binding outranks a star source, and within each
    kind the lexicographically smallest module id wins, which makes a name
    exported by two sources resolve the same way on every build rather than
    by dictionary order.  Already-visited (module, symbol) pairs are
    skipped, so a mutual re-export cycle terminates.

    Args:
        module_id: Node id of the module the target was guessed against.
        symbol: The symbol name being looked for.  A member target such as
            ``Thing.method`` keeps its dotted form.
        node_exists: Callable taking a node id and returning whether it
            names a live node.
        reexport_sources: Callable taking ``(module_id, symbol)`` and
            returning ``(kind, source_module_id, source_symbol)`` hops, where
            kind is ``_NAMED_HOP`` or ``_STAR_HOP``.
        max_depth: Maximum number of re-export hops to follow.

    Returns:
        The node id of the defining symbol, or None when the closure does
        not reach one.
    """
    visited: set[tuple[str, str]] = {(module_id, symbol)}
    frontier: list[tuple[str, str]] = [(module_id, symbol)]
    for _ in range(max_depth):
        reached: dict[tuple[str, str], int] = {}
        for current_module, current_symbol in frontier:
            for kind, source, source_symbol in reexport_sources(current_module, current_symbol):
                hop = (source, source_symbol)
                if hop in visited:
                    continue
                if hop not in reached or kind < reached[hop]:
                    reached[hop] = kind
        if not reached:
            return None
        frontier = sorted(reached, key=lambda hop: (reached[hop], hop[0], hop[1]))
        for source, source_symbol in frontier:
            candidate = f"{source}::{source_symbol}"
            if node_exists(candidate):
                return candidate
        visited.update(frontier)
    return None


def prefetch_reexport_closure(live_ids, star, named, targets: Iterable[str]) -> None:
    """Batch-read, one level at a time, what resolving *targets* through re-exports will ask.

    For a live-node set or re-export relation read on demand (each offers
    ``prefetch``): every target that is not live is walked the way
    :func:`resolve_symbol_through_reexports` walks it, all targets at once,
    so each level costs one relation read for its modules and one liveness
    read for the candidates it reaches and their envelopes.  The walks that
    follow are then answered from what was read.  This reads a superset of
    what they ask and changes no answer; with plain containers it does
    nothing.

    Args:
        live_ids: Ids of every live node.
        star: Star re-export sources per module id.
        named: Named re-export bindings per module id.
        targets: Link or AutoStep target ids about to be resolved; their own
            liveness must already be read.
    """
    from axiom_graph.scanners._step_helpers import envelope_id_for  # noqa: PLC0415

    prefetch_ids = getattr(live_ids, "prefetch", None)
    prefetch_modules = getattr(star, "prefetch", None)
    if prefetch_ids is None and prefetch_modules is None:
        return
    hops = reexport_hops(star, named)
    frontier: set[tuple[str, str]] = set()
    for target in targets:
        module_id, _, symbol = target.rpartition("::")
        if module_id and symbol and target not in live_ids:
            frontier.add((module_id, symbol))
    seen = set(frontier)
    for _ in range(_REEXPORT_CLOSURE_MAX_DEPTH):
        if not frontier:
            return
        if prefetch_modules is not None:
            prefetch_modules({module_id for module_id, _ in frontier})
        reached = {(source, sym) for module_id, symbol in frontier for _, source, sym in hops(module_id, symbol)}
        reached -= seen
        candidates = [f"{source}::{sym}" for source, sym in sorted(reached)]
        if prefetch_ids is not None:
            prefetch_ids([*candidates, *(envelope_id_for(c) for c in candidates)])
        seen |= reached
        frontier = {(source, sym) for source, sym in reached if f"{source}::{sym}" not in live_ids}


class _ReexportRelationHalf:
    """One side (``star`` or ``named``) of a :class:`ReexportRelationReader`, read per module."""

    def __init__(self, reader: ReexportRelationReader, index: int) -> None:
        self._reader = reader
        self._index = index

    def get(self, module_id: str, default=None):
        """Return the module's entries as :func:`reexport_relation_from_rows` gives them, else *default*."""
        entries = self._reader.module(module_id)[self._index]
        return entries if entries else default

    def prefetch(self, module_ids: Iterable[str]) -> None:
        """Read the relation of every module in *module_ids* in one batch."""
        self._reader.prefetch(module_ids)


class ReexportRelationReader:
    """The index's re-export relation, read one module at a time instead of every marker at once.

    ``star`` and ``named`` answer ``.get(module_id, default)`` exactly as the
    dicts :func:`reexport_relation_from_rows` builds from every ``depends_on``
    row would, from that module's own rows.  A module's rows are read on its
    first lookup, or in a batch by ``prefetch``.
    """

    def __init__(self, conn) -> None:
        """Bind the reader to an open connection.

        Args:
            conn: Open connection to the axiom-graph DB.
        """
        self._conn = conn
        self._modules: dict[str, tuple[list[str], list[tuple[str, dict[str, str]]]]] = {}
        self.star = _ReexportRelationHalf(self, 0)
        self.named = _ReexportRelationHalf(self, 1)

    def prefetch(self, module_ids: Iterable[str]) -> None:
        """Read the ``depends_on`` rows of every module not read yet, in one batched lookup."""
        missing = [m for m in dict.fromkeys(module_ids) if m not in self._modules]
        if not missing:
            return
        star, named = reexport_relation_from_rows(db.get_edges_from_conn(self._conn, "depends_on", missing))
        for module_id in missing:
            self._modules[module_id] = (star.get(module_id, []), named.get(module_id, []))

    def module(self, module_id: str) -> tuple[list[str], list[tuple[str, dict[str, str]]]]:
        """Return ``(star sources, named bindings)`` of one module."""
        if module_id not in self._modules:
            self.prefetch([module_id])
        return self._modules[module_id]

    def holds_named_marker_outside(self, excluded: Container[str]) -> bool:
        """Return whether any module of the index outside *excluded* holds a named re-export marker.

        Answers exactly as the whole relation would (``any(m not in
        excluded for m in named)``), with each row judged by
        :func:`reexport_relation_from_rows`, but stops at the first holder:
        the modules this reader already read are asked first, then the
        index's ``depends_on`` rows in ``(from_id, to_id)`` order, so the
        read ends at the first marker-holding module outside *excluded*.
        Proving there is none still reads every row.

        Args:
            excluded: Module ids whose markers do not count.

        Returns:
            True when a module outside *excluded* holds a named marker.
        """
        if any(module not in excluded and named for module, (_, named) in self._modules.items()):
            return True
        rows = db.iter_edges_of_type_conn(self._conn, "depends_on")
        with contextlib.closing(rows):
            for row in rows:
                if row[0] not in excluded and reexport_relation_from_rows((row,))[1]:
                    return True
        return False


def reexport_relation_from_rows(
    rows,
) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, dict[str, str]]]]]:
    """Build the re-export relation from ``depends_on`` edge rows.

    Each row counts on its own: a row whose meta is not a JSON object, or
    holds neither marker, adds nothing.

    Args:
        rows: Iterable of ``(from_id, to_id, meta)`` for ``depends_on``
            edges, where *meta* is the edge's meta as a dict or as its
            stored JSON text (``None`` when absent).

    Returns:
        ``(star, named)``.  ``star`` maps a module id to the sorted module
        ids it star-imports.  ``named`` maps a module id to its
        ``(source_module_id, {bound_name: original_name})`` pairs, sorted by
        source.
    """
    star: dict[str, list[str]] = {}
    named: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for from_id, to_id, raw_meta in rows:
        if not raw_meta:
            continue
        meta = raw_meta
        if not isinstance(meta, dict):
            try:
                meta = json.loads(raw_meta)
            except (TypeError, ValueError):
                continue
        if not isinstance(meta, dict):
            continue
        if meta.get(module_scanner.REEXPORT_STAR_KEY):
            star.setdefault(from_id, []).append(to_id)
        names = meta.get(module_scanner.REEXPORT_NAMES_KEY)
        if isinstance(names, dict) and names:
            named.setdefault(from_id, []).append((to_id, names))
    for sources in star.values():
        sources.sort()
    for bindings in named.values():
        bindings.sort(key=lambda pair: pair[0])
    return star, named


def reexport_hops(star: dict[str, list[str]], named: dict[str, list[tuple[str, dict[str, str]]]]):
    """Return the ``reexport_sources`` callable for :func:`resolve_symbol_through_reexports`.

    A member target (``Thing.method``) matches a binding of its first
    component and keeps the rest.  Named bindings come first, then star
    sources.

    Args:
        star: Star re-export sources per module id.
        named: Named re-export bindings per module id.

    Returns:
        Callable taking ``(module_id, symbol)`` and returning
        ``(kind, source_module_id, source_symbol)`` hops.
    """

    def reexport_sources(module_id: str, symbol: str) -> list[tuple[int, str, str]]:
        head, dot, member = symbol.partition(".")
        hops = [
            (_NAMED_HOP, source, f"{names[head]}{dot}{member}")
            for source, names in named.get(module_id, ())
            if head in names
        ]
        hops.extend((_STAR_HOP, source, symbol) for source in star.get(module_id, ()))
        return hops

    return reexport_sources


def _resolve_delegate_targets(
    db_path: Path,
    all_edges: list,
    warnings: list[str],
    live_ids: Container[str] | None = None,
    rescanned_ids: Container[str] | None = None,
) -> dict[str, int]:
    """Resolve this build's delegate and validates links whose target is no live node.

    Runs after nodes and edges are upserted, so the re-export relation is
    read from the **index** rather than from this build's scan output.  An
    incremental build only scans changed files, so a relation derived from
    the scan would resolve correctly on full rebuilds and flap on every
    other one.

    A link is a candidate when its target names no live node.  The walk
    follows named and star re-exports, named first.  The two link types
    have different write rules:

    - A delegate link is always written (the upsert loop already did), so a
      resolved one mints a new edge beside it and the scanner-edge
      reconciliation downstream retires the superseded row.
    - A validates link is written only when it resolves.  One that does not
      stays a silent skip and leaves this build's edge list.

    A target built from an attribute chain its import binding never spelled
    (``import pkg; pkg.sub.func()`` names ``pkg::func``) carries the
    scanner's unspelled-chain marker.  Its guessed module is not the one
    called, so following that module's named bindings would link a
    different function of the same name.  Such a validates link is never
    resolved, and such a delegate link follows star sources only.

    An edge's identity embeds its target, so a retarget mints a new edge
    rather than mutating one.  ``all_edges`` is rewritten in place so every
    later pass sees the corrected targets as this build's intended set;
    running the other way round would leave a permanent ghost per retarget.

    Warns when links guessed against modules this build did not rescan stay
    unresolved and no module outside this build's rescans holds a named
    marker.  Markers are written when a file is scanned, so an index
    predating them lacks them on every file the mtime fast-pass keeps
    skipping — the very files that would supply them — and resolution is
    then inactive with nothing on the surface to say so.  Only the modules
    this build left alone can show that: the markers its own rescans just
    wrote are current whatever the index's age, and counting them would let
    any rescanned module with a named import silence the warning on exactly
    the builds it exists for.  A link guessed against a module this build
    rescanned is left out of the counts for the same reason — that module's
    markers are already current, so a rescan cannot rescue it — which also
    keeps a full rescan, the remedy itself, quiet.  The warning names the
    remedy because the remedy is the part that is undiscoverable.

    The relation is read per module, for the modules the walks reach, and
    the marker question is asked only once a counted link stays
    unresolved; its read stops at the first marker-holding module outside
    the rescans.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        all_edges: This build's edge list, rewritten in place.
        warnings: Mutable list for error messages.
        live_ids: The ids a link may target this build.  None falls back to
            any id the index holds, for callers outside a build.
        rescanned_ids: Ids of the nodes this build's scan produced; a module
            among them had its file rescanned.  None, for callers outside a
            build, treats every module as not rescanned.

    Returns:
        ``{"delegates_to": n, "validates": m, "validates_written": w}``:
        links retargeted per type, and how many of the retargeted validates
        links added a row rather than replacing one.
    """
    resolved = {"delegates_to": 0, "validates": 0}
    validates_written = 0
    if not any(edge.edge_type in resolved for edge in all_edges):
        return {**resolved, "validates_written": validates_written}

    rescanned = rescanned_ids if rescanned_ids is not None else ()
    dropped: set[int] = set()
    try:
        with (
            db._connect(db_path) as conn,
            live_ids.reading_on(conn) if isinstance(live_ids, LiveNodeLookup) else contextlib.nullcontext(),
        ):
            if live_ids is None:
                existence_cache: dict[str, bool] = {}

                def node_exists(node_id: str) -> bool:
                    if node_id not in existence_cache:
                        row = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
                        existence_cache[node_id] = row is not None
                    return existence_cache[node_id]

            else:

                def node_exists(node_id: str) -> bool:
                    return node_id in live_ids

            relation = ReexportRelationReader(conn)
            star, named = relation.star, relation.named
            reexport_sources = reexport_hops(star, named)
            if live_ids is not None:
                prefetch_reexport_closure(
                    live_ids, star, named, [edge.to_id for edge in all_edges if edge.edge_type in resolved]
                )

            def star_sources(module_id: str, symbol: str) -> list[tuple[int, str, str]]:
                return [(_STAR_HOP, source, symbol) for source in star.get(module_id, ())]

            unresolved = {"delegates_to": 0, "validates": 0}
            for index, edge in enumerate(all_edges):
                if edge.edge_type not in resolved or node_exists(edge.to_id):
                    continue
                chained = bool(edge.meta and edge.meta.get(module_scanner.UNSPELLED_CHAIN_KEY))
                module_id, _, symbol = edge.to_id.rpartition("::")
                new_target = None
                if module_id and symbol and not (chained and edge.edge_type == "validates"):
                    new_target = resolve_symbol_through_reexports(
                        module_id,
                        symbol,
                        node_exists,
                        star_sources if chained else reexport_sources,
                    )
                if new_target is None or new_target == edge.to_id:
                    if edge.edge_type == "validates":
                        dropped.add(index)
                    if not chained and module_id not in rescanned:
                        unresolved[edge.edge_type] += 1
                    continue
                new_edge = make_edge(edge.edge_type, edge.from_id, new_target, meta=edge.meta)
                all_edges[index] = new_edge
                added = db.upsert_edge_conn(conn, new_edge)
                resolved[edge.edge_type] += 1
                if added and edge.edge_type == "validates":
                    validates_written += 1
                # Every rescan of the linking file re-emits the guessed target,
                # so this repeats on every such build: detail, not news.  The
                # caller logs one INFO count per build.
                logger.debug(
                    "link resolver: %s %s -> %s (was %s)",
                    edge.edge_type,
                    new_edge.from_id,
                    new_edge.to_id,
                    edge.to_id,
                )

            if (unresolved["delegates_to"] or unresolved["validates"]) and not relation.holds_named_marker_outside(
                rescanned
            ):
                message = (
                    f"{unresolved['delegates_to']} delegate link(s) and {unresolved['validates']} validates "
                    "link(s) name no live node, and no module outside this build's rescans records named "
                    "re-export markers, so link resolution could not follow the names packages re-export. "
                    "Markers are written when a "
                    "file is scanned: an index built before they existed lacks them, and the mtime fast-pass "
                    "keeps skipping the files that would supply them. Remedy (non-destructive; not init, "
                    "which deletes the index): clear the stored file mtimes (UPDATE nodes SET file_mtime = "
                    "NULL), then run axiom-graph build to rescan every file."
                )
                warnings.append(message)
                logger.warning("link resolver: %s", message)
    except Exception as exc:  # pragma: no cover
        warnings.append(f"link target resolution failed: {exc}")
        logger.warning("link target resolution error: %s", exc)
    if dropped:
        all_edges[:] = [edge for index, edge in enumerate(all_edges) if index not in dropped]
    return {**resolved, "validates_written": validates_written}


def _flag_broken_links(
    db_path: Path,
    warnings: list[str],
    *,
    locations: Iterable[str] | None = None,
    deleted_ids: Iterable[str] = (),
) -> int:
    """Flag the nodes whose links dangle after purge, and count them over the whole index.

    Queries for edges whose to_id has no matching node and persists
    BROKEN_LINK staleness on the source nodes.  A build passes the files it
    parsed and the ids it deleted: only the links leaving nodes at those
    files or entering those ids can have started to dangle, so only their
    sources are written (every other dangling source was flagged when its
    link broke).  The count is always the whole index's, one query.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        warnings: Mutable list for error messages.
        locations: Files this build parsed; ``None`` flags every dangling
            source in the index.
        deleted_ids: Nodes this build deleted.

    Returns:
        Number of nodes in the index whose links dangle.
    """
    count = 0
    try:
        from axiom_graph.index.staleness import (  # noqa: PLC0415
            _BROKEN_LINK_EDGE_TYPES,
            count_broken_link_sources,
            find_broken_links,
        )

        if locations is None:
            broken = find_broken_links(db_path)
            count = len(broken)
        else:
            with db._connect(db_path) as conn:
                scope = db.node_ids_at_locations_conn(conn, locations)
                scope |= db.edge_sources_into_conn(conn, deleted_ids, _BROKEN_LINK_EDGE_TYPES)
            broken = find_broken_links(db_path, scope) if scope else {}
            count = count_broken_link_sources(db_path)
        if broken:
            with db._connect(db_path) as conn:
                conn.executemany(
                    "UPDATE nodes SET staleness = ?, link_status = ? WHERE id = ?",
                    [(BROKEN_LINK, BROKEN_LINK, node_id) for node_id in broken],
                )
    except Exception as exc:  # pragma: no cover
        warnings.append(f"broken link detection failed: {exc}")
        logger.warning("broken link detection error: %s", exc)
    return count


_PY_NAMES = suffix_matcher([".py"])


def _iter_python_files(
    project_root: Path,
    skip_dirs: frozenset[str] = _BASE_SKIP_DIRS,
    listing: TreeListing | None = None,
):
    """Yield all .py files under *project_root*, never descending into a skipped directory.

    Args:
        project_root: Absolute path to the project root.
        skip_dirs: Names of the directories (and files) to skip: no path with
            such a component below *project_root* is yielded or listed.
        listing: The operation's shared directory listing; a fresh one when
            omitted.
    """
    listing = listing if listing is not None else TreeListing()
    yield from listing.matches(project_root, _PY_NAMES, skip_names=skip_dirs)


def _iter_js_files(
    project_root: Path,
    js_paths: list[str],
    skip_dirs: frozenset[str] = _BASE_SKIP_DIRS,
    listing: TreeListing | None = None,
):
    """Yield JS/TS files matching *js_paths* globs, never descending into a skipped directory.

    Args:
        project_root: Absolute path to the project root.
        js_paths: List of glob patterns relative to project_root.
        skip_dirs: Directory names to skip.
        listing: The operation's shared directory listing; a fresh one when
            omitted.
    """
    listing = listing if listing is not None else TreeListing()
    seen: set[Path] = set()
    for pattern in js_paths:
        for path in listing.glob(project_root, pattern, skip_names=skip_dirs):
            if not path.is_file():
                continue
            if path.suffix not in (".js", ".jsx", ".ts", ".tsx"):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            rel = path.relative_to(project_root)
            if any(part in skip_dirs for part in rel.parts):
                continue
            seen.add(resolved)
            yield path


@task(
    purpose="Delete the step rows the index stores for a file this build walked whose markers that file's scan "
    "no longer emitted; count the step rows left without a parent through the indexed edges(to_id) lookup",
    inputs="db_path, the node set collected by this build's scanners, the build's warnings list",
    outputs="(number of orphaned step rows deleted, whether the pass failed)",
)
def _reap_orphaned_step_nodes(
    db_path: Path,
    nodes: list[AxiomNode],
    warnings: list[str],
) -> tuple[int, bool]:
    """Reconcile stored step rows against the markers *nodes* declares per file.

    A step row is reached through its stored ``location``, never through its
    enclosing workflow: when an annotated function is deleted or moved, its
    envelope node goes with it and the ``composes`` edge cascades away, so the
    step rows it used to hold have no parent left to be found through.  Only
    the file they were declared in still names them.  That location is the
    declaring file in every case — cross-module delegation moves a step's
    ``delegates_to`` target, never the step itself.

    Two properties carry the safety argument:

    * **Out of scope is not the same as intending ∅.**  A file absent from
      this build's node output — skipped by the mtime fast-pass, or one whose
      scanner raised — is not reconciled at all.  Treating it as intending no
      steps would delete every step row in the project on the first
      incremental build.
    * **The gate is this build's node output, not a list of files the build
      thinks it walked.**  A derived list can drift from what the scanners
      actually produced; the node output cannot.  ``_stamp_scanned_file_mtimes``
      establishes the same invariant for the identical set, and
      ``scan_module`` returns its ``(nodes, edges)`` atomically, so a file
      whose scanner raised contributes zero nodes and falls out of scope
      instead of reading as ∅.

    A walked file that emitted no step markers at all *is* in scope and is
    reconciled to ∅ — it is in the node output because its module node is,
    and "this file declares no steps" is exactly what its scan says.

    Deletion goes through :func:`db.delete_node_by_id`, which cascades edges,
    tags, FTS and non-preserved history, writes a preserved ``DELETED``
    tombstone naming the reaper as the actor, and keeps inbound ``documents``
    edges from surviving sources (flag-don't-drop).

    Args:
        db_path: Path to the axiom-graph SQLite database.
        nodes: Every node collected by this build's scanners.
        warnings: The build's warnings list; a failure is appended here rather
            than raised, so a reaping error can never fail a build.

    Returns:
        Tuple of (rows deleted, pass failed).  The failure flag joins the
        caller's per-file guard: a build that could not finish reconciling
        must not stamp the files it read, or their orphans stay unreachable
        until those files change again.
    """
    walked_locations: set[str] = {node.location for node in nodes if node.location}
    if not walked_locations:
        return 0, False

    # Intended step rows per walked file, from this build's scan output.  A
    # walked file missing from this map intends ∅ and is reconciled to ∅.
    intended_by_location: dict[str, set[str]] = {}
    for node in nodes:
        if getattr(node, "subtype", None) in db.STEP_NODE_SUBTYPES and node.location:
            intended_by_location.setdefault(node.location, set()).add(node.id)

    reaped = 0
    try:
        with db._connect(db_path) as conn:
            stored_by_location = db.get_step_node_ids_by_location_conn(conn, walked_locations)
            for location, stored_ids in stored_by_location.items():
                intended = intended_by_location.get(location, set())
                for node_id in sorted(stored_ids - intended):
                    db.delete_node_by_id(
                        conn,
                        node_id,
                        reason_meta={
                            "actor": "build:reap-orphaned-steps",
                            "reason": "step marker no longer declared by the source file",
                        },
                    )
                    reaped += 1
                    logger.info("step reaper: removed orphaned step %s (%s)", node_id, location)
    except Exception as exc:
        warnings.append(f"orphaned step reaping failed: {exc}")
        logger.warning("orphaned step reaping error: %s", exc)
        return reaped, True

    if reaped:
        logger.info("step reaper: removed %d orphaned step row(s)", reaped)
    return reaped, False


@task(
    purpose="Record the on-disk mtime and parse record of every file this build opened and parsed, so the next "
    "build parses it again only when its bytes or mtime move; heals rows the scanners emitted with an mtime whose stored value had been cleared to NULL",
    inputs="db_path, the node set collected by this build's scanners",
    outputs="Number of file locations stamped",
)
def _stamp_scanned_file_mtimes(
    db_path: Path,
    nodes: list[AxiomNode],
    parsed: dict[str, tuple] | None = None,
) -> int:
    """Advance ``file_mtime`` for every file location present in *nodes*.

    ``file_mtime`` is the builder's scan-skip cache: it holds the file's
    on-disk modification time as observed when this build last scanned the
    file's bytes.  Only a full per-file index pass — nodes *and* edges *and*
    doc/section records — may advance it, because advancing it is a promise
    that the next :func:`build` may safely skip the file entirely.  This
    function is the single place that promise is made.  It must therefore be
    called only once the index-integrity passes gated on this build's scanned
    set — vanished-section pruning and documents-edge reconciliation — have
    run and succeeded; either of those failing after the stamp would never be
    retried, because the next build would skip the file.

    The set of files to stamp needs no skip-list: every scanner applies its
    own mtime fast-pass, and a skipped file contributes zero nodes, so the
    file-level mtimes riding on *nodes* already are exactly "the files this
    build opened and parsed".

    Two writes preserve the column's shape (module/doc nodes carry it,
    function nodes do not) while letting a cleared cache heal:

    - every row at a scanned location that already carries a non-NULL
      ``file_mtime`` is advanced, which keeps both mtime readers in
      agreement for locations that stamp more than one row;
    - every row the scanners emitted *with* a ``file_mtime`` this build is
      stamped by id even when its stored value is NULL, so a location whose
      mtime was cleared (for example by the purge of its file-level anchor,
      or by hand) rejoins the fast pass after one build.  Rows the scanners
      emit without an mtime (function rows) are never newly stamped.

    The same promise records each parsed file's content fingerprint, taken
    before the parse (*parsed*), and clears what the last staleness re-hash
    read of it (its fingerprint and structure): the parse may have inserted
    or moved nodes, so the next pass re-hashes the file.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        nodes: Every node collected by this build's scanners.
        parsed: Location -> ``(fingerprint, mtime, size)`` the discovery walk
            read of each parsed file, for its parse record.

    Returns:
        The number of distinct file locations stamped.
    """
    parsed = parsed or {}
    scanned_mtimes: dict[str, float] = {}
    stamped_ids: dict[str, str] = {}
    for node in nodes:
        mtime = node.file_mtime
        location = node.location
        if mtime is None or not location:
            continue
        stamped_ids[node.id] = location
        prior = scanned_mtimes.get(location)
        if prior is None or mtime > prior:
            scanned_mtimes[location] = mtime

    if not scanned_mtimes and not parsed:
        logger.info("build: stamped 0 scanned-file mtimes")
        return 0

    with db._connect(db_path) as conn:
        conn.executemany(
            "UPDATE nodes SET file_mtime = ? WHERE location = ? AND file_mtime IS NOT NULL",
            [(mtime, location) for location, mtime in scanned_mtimes.items()],
        )
        # Heal: rows emitted with an mtime whose stored value is NULL.
        conn.executemany(
            "UPDATE nodes SET file_mtime = ? WHERE id = ? AND file_mtime IS NULL",
            [(scanned_mtimes[location], node_id) for node_id, location in stamped_ids.items()],
        )
        # The parse may have inserted or moved nodes: the next staleness pass
        # re-hashes these files rather than derive from an earlier re-hash.
        db.record_parsed_files_conn(conn, parsed)
        db.forget_parsed_files_conn(conn, [loc for loc in scanned_mtimes if loc not in parsed])
    logger.info("build: stamped %d scanned-file mtimes", len(scanned_mtimes))
    return len(scanned_mtimes)


def _record_walk(db_path: Path, discovery: BuildDiscovery, parsed_locations: set[str], *, stamped: bool) -> None:
    """Record what the discovery walk read that the stamp did not: the upgrade seed, and failed passes.

    Every walked file left unparsed that has no parse record yet gets its
    fingerprint (it is the content the mtime rule judged unchanged).  When
    the stamp was skipped because a per-file pass failed, the parsed files
    keep no new parse record, but what the last staleness re-hash read of
    them is still forgotten, so the next pass re-hashes them.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        discovery: The build's discovery walk.
        parsed_locations: Files this build's scanners produced nodes for.
        stamped: Whether the stamp recorded the parse.
    """
    seeds = discovery.seed_records()
    if not seeds and stamped:
        return
    with db._connect(db_path) as conn:
        db.seed_file_records_conn(conn, seeds)
        if not stamped:
            db.forget_parsed_files_conn(conn, parsed_locations)


@task(
    purpose="Remove DB rows for doc files and node locations that no longer exist on disk; a file the discovery "
    "walk saw is known to exist and is not stat'ed again",
    inputs="db_path, project_root, warnings list (mutated in place)",
    outputs="Total number of nodes purged",
)
def _purge_stale_entries(
    db_path: Path,
    project_root: Path,
    warnings: list[str],
    *,
    exclude_dirs: list[str] | None = None,
    git_sha: str | None = None,
    walked: Iterable[str] | None = None,
) -> int:
    """Check all indexed file paths against disk and cascade-delete missing ones.

    Three purge phases run in sequence:

    1. **Doc purge** — removes DocJSON docs whose ``file_path`` no longer
       exists, along with their section nodes, edges, tags, FTS, and history.
    2. **Node location purge** — removes code/markdown nodes whose
       ``location`` file no longer exists, with the same cascade.
    3. **Exclude-dir purge** — removes nodes whose location contains a
       directory from ``exclude_dirs`` (e.g. worktree leftovers).

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Absolute path to the project root for resolving
            relative file paths.
        warnings: Mutable list; scanner/purge errors are appended here.
        exclude_dirs: Directory names to purge from the index (from
            ``axiom-graph.toml [axiom_graph.scan] exclude_dirs``).
        git_sha: The current build SHA, threaded down to
            ``delete_nodes_by_location`` so DELETED ghosts preserve the
            deletion-time SHA + span for later baseline-source recovery.
        walked: Files the build's walk read from disk this run; they are
            known to exist, so they are not checked again.

    Returns:
        Total number of nodes (doc + code) purged.
    """
    nodes_purged = 0
    seen_on_disk = {_normalized_location(loc) for loc in walked or ()}

    口 = Step(
        step_num=1,
        name="Purge stale docs",
        purpose="Check all indexed doc file_paths against disk and cascade-delete missing ones",
    )
    try:
        doc_paths = db.get_all_doc_file_paths(db_path)
        for fp in doc_paths:
            abs_path = project_root / fp
            if _normalized_location(fp) not in seen_on_disk and not abs_path.exists():
                logger.info("Purging stale doc file_path: %s", fp)
                doc_ids = db.get_doc_ids_by_filepath(db_path, fp)
                with db._connect(db_path) as conn:
                    for did in doc_ids:
                        db.delete_doc_by_id(conn, did)
                        nodes_purged += 1
    except Exception as exc:  # pragma: no cover
        warnings.append(f"doc purge failed: {exc}")
        logger.warning("doc purge error: %s", exc)

    口 = Step(
        step_num=2,
        name="Purge stale node locations",
        purpose="Check all indexed node locations against disk and cascade-delete missing ones",
    )
    try:
        locations = db.get_all_node_locations(db_path)
        for loc in locations:
            abs_path = project_root / loc
            if _normalized_location(loc) not in seen_on_disk and not abs_path.exists():
                logger.info("Purging stale node location: %s", loc)
                with db._connect(db_path) as conn:
                    nodes_purged += db.delete_nodes_by_location(conn, loc, git_sha)
    except Exception as exc:  # pragma: no cover
        warnings.append(f"node location purge failed: {exc}")
        logger.warning("node location purge error: %s", exc)

    口 = Step(
        step_num=3,
        name="Purge excluded directories",
        purpose="Remove nodes whose location contains a directory from exclude_dirs config",
    )
    if exclude_dirs:
        try:
            locations = db.get_all_node_locations(db_path)
            for loc in locations:
                parts = loc.replace("\\", "/").split("/")
                if any(d in parts for d in exclude_dirs):
                    logger.info("Purging excluded-dir node location: %s", loc)
                    with db._connect(db_path) as conn:
                        nodes_purged += db.delete_nodes_by_location(conn, loc, git_sha)
        except Exception as exc:  # pragma: no cover
            warnings.append(f"exclude-dir purge failed: {exc}")
            logger.warning("exclude-dir purge error: %s", exc)

    return nodes_purged


# ---------------------------------------------------------------------------
# Lightweight single-file rescan (used by MCP + viz for line-number refresh)
# ---------------------------------------------------------------------------


def _existing_ids_conn(conn, node_ids: list[str]) -> set[str]:
    """Return which of *node_ids* already have a ``nodes`` row."""
    found: set[str] = set()
    ids = list(dict.fromkeys(node_ids))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(f"SELECT id FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        found.update(r["id"] for r in rows)
    return found


def _baseline_new_tests(
    db_path: Path,
    scanned_nodes: list,
    not_new: Container[str],
    git_sha: str | None,
    project_root: Path | None = None,
) -> list[str]:
    """Baseline-verify test functions indexed for the first time.

    A test that validates code with any past content change would otherwise
    be LINKED_STALE from birth, since only a verification newer than that
    change clears it.  A test seen for the first time has, by definition,
    been written against the code as it is now, so it gets a verification
    row (``agent:scan-baseline``) and one preserved ``AGENT_VERIFIED``
    history row with op ``scan_baseline``.  A later change to its target
    flags it as usual.

    Args:
        db_path: Path to the axiom-graph DB.
        scanned_nodes: Nodes this scan produced.
        not_new: Ids that are not first inserts -- the index's ids before the
            scan, plus the new ids of renamed or moved nodes.
        git_sha: HEAD sha recorded on the history rows, when known.
        project_root: Project root; when given, each baseline also records
            the test's dependency pairs at the hashes on disk.

    Returns:
        The ids that were baselined.
    """
    from axiom_graph.index.mark_clean import (  # noqa: PLC0415
        VERIFICATION_OP_SCAN_BASELINE,
        VERIFIED_BY_SCAN_BASELINE,
        PairRecorder,
    )

    new_tests = [n.id for n in scanned_nodes if getattr(n, "subtype", None) == "test" and n.id not in not_new]
    if not new_tests:
        return []
    try:
        recorder = PairRecorder(project_root) if project_root is not None else None
        with db._connect(db_path) as conn:
            if recorder is not None:
                # Load every new test's links and targets once, before the
                # writes, instead of per test as each pair set is asked for.
                new_ids = set(new_tests)
                recorder.prepare(conn, [n for n in scanned_nodes if n.id in new_ids])
            written = db.write_baseline_verifications_conn(
                conn,
                new_tests,
                verified_by=VERIFIED_BY_SCAN_BASELINE,
                verification_op=VERIFICATION_OP_SCAN_BASELINE,
                reason="baseline: test first indexed",
                git_sha=git_sha,
                pairs_for=recorder.pairs_for if recorder is not None else None,
            )
    except Exception as exc:  # pragma: no cover -- a baseline never fails a scan
        logger.warning("test scan baseline failed: %s", exc)
        return []
    if written:
        logger.info("build: baselined %d newly indexed test(s)", len(written))
    return written


_GAINED_LINK_EDGE_TYPES = ("validates", "delegates_to")


def _stored_scanner_links_conn(conn, source_ids: set[str]) -> set[tuple[str, str, str]]:
    """Return the stored ``validates`` / ``delegates_to`` links leaving *source_ids*.

    Args:
        conn: Open connection to the index.
        source_ids: The nodes whose outgoing links are read.

    Returns:
        ``(edge_type, from_id, to_id)`` for each stored link.
    """
    links: set[tuple[str, str, str]] = set()
    ids = sorted(source_ids)
    types = ",".join("?" * len(_GAINED_LINK_EDGE_TYPES))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            f"SELECT edge_type, from_id, to_id FROM edges WHERE edge_type IN ({types}) "
            f"AND from_id IN ({','.join('?' * len(chunk))})",
            [*_GAINED_LINK_EDGE_TYPES, *chunk],
        )
        links.update((r["edge_type"], r["from_id"], r["to_id"]) for r in rows)
    return links


def _composing_envelopes_conn(conn, step_ids: set[str]) -> dict[str, list[str]]:
    """Return, per step id, the nodes that compose it (its workflow envelope).

    Args:
        conn: Open connection to the index.
        step_ids: The step nodes to look up.

    Returns:
        ``{step_id: [composing node id, ...]}`` for the steps that have one.
    """
    envelopes: dict[str, list[str]] = {}
    ids = sorted(step_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            f"SELECT from_id, to_id FROM edges WHERE edge_type = 'composes' AND to_id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        for row in rows:
            envelopes.setdefault(row["to_id"], []).append(row["from_id"])
    return envelopes


def _baseline_gained_links(
    db_path: Path,
    project_root: Path,
    source_ids: set[str],
    links_before: set[tuple[str, str, str]],
    git_sha: str | None,
) -> list[str]:
    """Settle the links a scanner upgrade found, so they flag nothing on arrival.

    A build that re-parses everything because the scan scheme or the import
    roots moved can find links older scans missed (a src-layout test's
    ``validates``, an AutoStep's cross-package ``delegates_to``).  Their
    targets may hold content changes from before the link was known; left
    alone, every such change would flag the dependent LINKED_STALE although
    nothing changed in the code.  For each dependent (the test, or the
    workflow envelope composing the AutoStep) that read link-VERIFIED before
    this build and gained a link to a target with a change that still
    counts, the gained links are recorded as verified against the index's
    current hashes: a dependent with no verification gets a scan baseline
    (``agent:scan-baseline``), one that has a verification gets pairs for
    the gained targets only.  A dependent already LINKED_STALE is left as
    it is, and a later change to any target flags it as usual.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root (for the pair recorder).
        source_ids: The nodes this build scanned.
        links_before: Their links before this build's edge writes.
        git_sha: HEAD sha recorded on the history rows, when known.

    Returns:
        The ids that received a scan baseline.
    """
    from axiom_graph.index.mark_clean import (  # noqa: PLC0415
        VERIFICATION_OP_SCAN_BASELINE,
        VERIFIED_BY_SCAN_BASELINE,
        PairRecorder,
    )

    try:
        with db._connect(db_path) as conn:
            gained = _stored_scanner_links_conn(conn, source_ids) - links_before
            if not gained:
                return []
            changed = db.effective_change_rows_conn(conn, sorted({to for _t, _f, to in gained}))
            gained = {link for link in gained if link[2] in changed}
            envelopes = _composing_envelopes_conn(
                conn, {from_id for edge_type, from_id, _to in gained if edge_type == "delegates_to"}
            )
            targets_by_dependent: dict[str, set[str]] = {}
            for edge_type, from_id, to_id in gained:
                dependents = [from_id] if edge_type == "validates" else envelopes.get(from_id, [])
                for dependent in dependents:
                    targets_by_dependent.setdefault(dependent, set()).add(to_id)
            if not targets_by_dependent:
                return []
            ids = sorted(targets_by_dependent)
            marks = ",".join("?" * len(ids))
            clean = {
                r["id"]
                for r in conn.execute(f"SELECT id, link_status FROM nodes WHERE id IN ({marks})", ids)
                if (r["link_status"] or "VERIFIED") == "VERIFIED"
            }
            ids = [i for i in ids if i in clean]
            verified = set(db.get_verifications_for_conn(conn, ids))
            recorder = PairRecorder(project_root, from_index=True)
            for dependent in [i for i in ids if i in verified]:
                pairs = recorder.pairs_for(conn, dependent) or {}
                db.refresh_verification_targets_conn(
                    conn, dependent, {t: p for t, p in pairs.items() if t in targets_by_dependent[dependent]}
                )
            written = db.write_baseline_verifications_conn(
                conn,
                [i for i in ids if i not in verified],
                verified_by=VERIFIED_BY_SCAN_BASELINE,
                verification_op=VERIFICATION_OP_SCAN_BASELINE,
                reason="baseline: links found by a scanner upgrade",
                git_sha=git_sha,
                pairs_for=recorder.pairs_for,
            )
    except Exception as exc:  # pragma: no cover -- a baseline never fails a scan
        logger.warning("gained-link baseline failed: %s", exc)
        return []
    logger.info("build: settled links a scanner upgrade found for %d node(s)", len(ids))
    return written


def _reconcile_doc_stamps(
    db_path: Path,
    project_root: Path,
    scanned_nodes: list,
    stored_text: dict,
    unchanged_ids: set[str],
    *,
    legacy_gate: bool,
    mode: str | None,
    git_sha: str | None,
) -> tuple[list[str], list[str], list[str]]:
    """Run the stamp classifier over every DocJSON section a scan produced.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root.
        scanned_nodes: Nodes the scan produced.
        stored_text: Pre-upsert ``{id: (level_1, level_2)}`` of those sections.
        unchanged_ids: Ids whose rows were moved by rename detection.
        legacy_gate: Whether unstamped new/changed sections count as raw edits.
        mode: ``raw_docjson_edits`` setting (``None`` reads the config).
        git_sha: HEAD sha for history rows.

    Returns:
        ``(verified_ids, text_verified_ids, raw_docjson_edit_ids)``: sections
        verified in full, sections whose text and vouched links only were
        verified, and raw DocJSON edits recorded.
    """
    verified: list[str] = []
    text_verified: list[str] = []
    raw: list[str] = []
    for doc in [n for n in scanned_nodes if getattr(n, "subtype", None) == "docjson_doc"]:
        try:
            sections = doc_stamps.load_section_dicts(project_root / doc.location)
            items = doc_stamps.section_inputs(scanned_nodes, sections, doc.id, stored_text, unchanged_ids=unchanged_ids)
            res = doc_stamps.reconcile_sections(
                db_path,
                project_root,
                items,
                file_path=doc.location,
                legacy_gate=legacy_gate,
                mode=mode,
                git_sha=git_sha,
            )
        except Exception as exc:  # pragma: no cover -- the check never fails a scan
            logger.warning("doc stamp check failed for %s: %s", doc.location, exc)
            continue
        verified.extend(res.verified)
        text_verified.extend(res.text_verified)
        raw.extend(res.raw_edits)
    return verified, text_verified, raw


def _is_docjson_file(path: Path) -> bool:
    """Return True iff *path* parses as a DocJSON document.

    A DocJSON document is a ``.docjson`` or ``.json`` file whose top-level
    value is an object with a ``"title"`` string and a ``"sections"`` array.
    Beyond the extension, detection is purely by content signature.

    Args:
        path: Absolute path to a file on disk.

    Returns:
        True iff the file is valid JSON matching the DocJSON shape.
    """
    import json as _json  # noqa: PLC0415

    try:
        if not doc_ids.has_docjson_extension(path):
            return False
        # Read in full; DocJSON files are small relative to the tolerance
        # here.  Malformed or non-object JSON is rejected silently.
        text = path.read_text(encoding="utf-8", errors="replace")
        data = _json.loads(text)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if not isinstance(data.get("title"), str):
        return False
    if not isinstance(data.get("sections"), list):
        return False
    return True


def _matched_docs_dir(abs_path: Path, project_root: Path) -> Path | None:
    """Return the configured docs root that contains *abs_path*, or None.

    Iterates ``config.scan.docs_dirs`` (honoring absolute / project-relative
    entries), resolves each to an absolute path, and returns the first root
    that is an ancestor of ``abs_path``.  Returns ``None`` when no configured
    root matches -- callers should then fall back to legacy behavior.
    """
    from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

    try:
        cfg = AxiomGraphConfig.load(project_root)
    except Exception:
        return None
    for entry in cfg.scan.docs_dirs or ["docs"]:
        entry_path = Path(entry)
        root_abs = entry_path if entry_path.is_absolute() else (project_root / entry_path)
        try:
            abs_path.relative_to(root_abs)
            return root_abs
        except ValueError:
            continue
    return None


def rescan_file_if_needed(db_path: Path, root: Path, node) -> bool:
    """Re-scan a single file if its mtime has changed since the last build.

    Updates structural metadata (level_3_location, line numbers) in the DB
    via discovery_only upsert — staleness baselines are preserved.

    Returns True if a rescan was performed, False if the file was unchanged.
    """
    location = node.location
    if not location:
        return False

    abs_path = root / location
    if not abs_path.exists():
        return False

    stored_mtime = db.get_file_mtime(db_path, location)
    if file_unchanged_since(stored_mtime, abs_path.stat().st_mtime):
        return False

    project_id = node.id.split("::")[0]

    try:
        if abs_path.suffix == ".py":
            scanned_nodes, _edges = module_scanner.scan_module(abs_path, root, project_id)
        elif _is_docjson_file(abs_path):
            matched_docs_dir = _matched_docs_dir(abs_path, root)
            scanned_nodes, _edges, _doc_recs, _sec_recs = json_doc_scanner.scan_single_json_doc(
                abs_path, root, project_id, docs_dir=matched_docs_dir
            )
        elif abs_path.suffix in (".ts", ".tsx", ".js", ".jsx"):
            from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER as _js_ok

            if not _js_ok:
                return False
            from axiom_graph.scanners import js_scanner

            scanned_nodes, _edges = js_scanner.scan_js_module(abs_path, root, project_id)
        else:
            return False

        with db._connect(db_path) as conn:
            existed = _existing_ids_conn(conn, [n.id for n in scanned_nodes])
            stored_text = doc_stamps.stored_section_texts(
                conn, [n.id for n in scanned_nodes if getattr(n, "subtype", None) == "docjson_section"]
            )
            for n in scanned_nodes:
                db.upsert_node_conn(conn, n, discovery_only=True)
        _baseline_new_tests(db_path, scanned_nodes, existed, None, root)
        _reconcile_doc_stamps(
            db_path, root, scanned_nodes, stored_text, set(), legacy_gate=True, mode=None, git_sha=None
        )
        return True
    except Exception as exc:
        logger.warning("rescan_file_if_needed failed for %s: %s", location, exc)
        return False
