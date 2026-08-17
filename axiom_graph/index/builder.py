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

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from axiom_annotations import workflow, task, Step, AutoStep

from axiom_graph.config import AxiomGraphConfig
from axiom_graph.index import db, doc_ids
from axiom_graph.index.file_state import file_unchanged_since
from axiom_graph.models import make_edge
from axiom_graph.ontology import valid_edge
from axiom_graph.scanners import config_scanner, doc_scanner, module_scanner
from axiom_graph.docjson import parse as json_doc_scanner
from axiom_graph.index.status import BROKEN_LINK

if TYPE_CHECKING:
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
_RECONCILED_SCANNER_EDGE_TYPES: frozenset[str] = frozenset({"delegates_to"})

# How many carrier files the leftover-orphan notice names before it
# summarises the rest.  The list is the remedy — it tells the reader what to
# touch — so it has to stay short enough to read in one line.
_ORPHAN_NOTICE_FILE_CAP: int = 5


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@workflow(
    purpose="Scan project_root with all scanners, upsert nodes/edges, detect renames, purge stale entries, and compute staleness",
    inputs="project_root path, optional project_id, discovery_only flag",
    outputs="Summary dict {nodes_written, nodes_skipped, edges_written, edges_skipped, nodes_renamed, nodes_purged, warnings}",
)
def build(
    project_root: Path,
    project_id: str | None = None,
    discovery_only: bool = True,
    embedder_thread=None,
) -> dict:
    """Scan *project_root* and upsert all discovered nodes/edges into the DB.

    Parameters
    ----------
    project_root:
        Absolute (or resolvable) path to the project directory to scan.
    project_id:
        Short identifier used as the namespace prefix in all node IDs.
        Explicit value wins over ``axiom-graph.toml`` and directory-name fallback.
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
        purpose="Resolve absolute path, load axiom-graph.toml config, determine project_id and skip_dirs",
    )
    project_root = Path(project_root).resolve()

    logger.debug("build: resolving config from %s", project_root)
    # Load axiom-graph.toml (returns defaults silently if absent)
    config = AxiomGraphConfig.load(project_root)
    logger.debug("build: config loaded (project_id=%s)", config.project_id)

    # CLI --id beats axiom-graph.toml [axiom_graph] project_id beats directory name
    if project_id is None:
        project_id = config.project_id or project_root.name

    skip_dirs = _BASE_SKIP_DIRS | frozenset(config.scan.exclude_dirs)

    口 = Step(
        step_num=2,
        name="Init DB, run migrations, and preload mtimes",
        purpose="Ensure .axiom_graph/ dir and DB schema exist; run pending versioned schema migrations so a legacy DB upgrades in place before any scanning; batch-load stored file mtimes for mtime fast-pass",
        outputs="db_path, stored_mtimes dict",
    )
    # Resolve configured DB path (defaults to .axiom_graph/graph.db).
    # Ensure the parent directory exists before init.
    from axiom_graph.config import db_path_for  # noqa: PLC0415

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

    nodes_written = 0
    nodes_skipped = 0
    edges_written = 0
    edges_skipped = 0
    files_scanned = 0
    files_skipped_mtime = 0
    warnings: list[str] = []

    all_nodes: list = []
    all_edges: list = []
    annotation_findings: list = []
    autostep_records: list = []
    _validation_guard = lambda rid: config.validation.is_enabled(rid)  # noqa: E731

    # Resolve HEAD git SHA once — threaded into every history row
    from axiom_graph.index.git_utils import get_git_sha  # noqa: PLC0415

    logger.debug("build: resolving git SHA")
    git_sha = get_git_sha(project_root)
    logger.debug("build: git SHA=%s", git_sha)

    口 = Step(
        step_num=3,
        name="Scan Python files",
        purpose="Walk all .py files under project_root, apply mtime fast-pass, run module_scanner on changed files",
        inputs="project_root, skip_dirs, stored_mtimes",
        outputs="all_nodes and all_edges populated with Python module/function nodes and edges",
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

    logger.debug("build: starting Python file scan")
    for py_file in _iter_python_files(project_root, skip_dirs):
        # mtime fast-pass: skip files unchanged since last build
        if discovery_only:
            rel_path = py_file.relative_to(project_root).as_posix()
            stored_mtime = stored_mtimes.get(rel_path)
            if file_unchanged_since(stored_mtime, py_file.stat().st_mtime):
                files_skipped_mtime += 1
                continue

        try:
            口 = AutoStep(step_num=3.1, name="Scan single Python file")
            logger.debug("build: scanning %s", py_file.relative_to(project_root))
            nodes, edges = module_scanner.scan_module(
                py_file,
                project_root,
                project_id,
                findings_out=annotation_findings,
                autosteps_out=autostep_records,
                is_rule_enabled=_validation_guard,
            )
            all_nodes.extend(nodes)
            all_edges.extend(edges)
            files_scanned += 1
        except Exception as exc:  # pragma: no cover
            warnings.append(f"module_scanner failed on {py_file.relative_to(project_root)}: {exc}")
            logger.warning("module_scanner error on %s: %s", py_file, exc)

    logger.info("build: scanned %d Python files (%d skipped mtime)", files_scanned, files_skipped_mtime)

    # ------------------------------------------------------------------
    # JS/TS scanner — files matching js_paths globs (if tree-sitter available)
    # ------------------------------------------------------------------
    js_scanned = 0
    js_skipped_mtime = 0
    if config.scan.js_paths:
        from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER as _js_available

        if _js_available:
            from axiom_graph.scanners import js_scanner

            for js_file in _iter_js_files(project_root, config.scan.js_paths, skip_dirs):
                if discovery_only:
                    rel_path = js_file.relative_to(project_root).as_posix()
                    stored_mtime = stored_mtimes.get(rel_path)
                    if file_unchanged_since(stored_mtime, js_file.stat().st_mtime):
                        js_skipped_mtime += 1
                        continue

                try:
                    nodes, edges = js_scanner.scan_js_module(
                        js_file,
                        project_root,
                        project_id,
                        findings_out=annotation_findings,
                        autosteps_out=autostep_records,
                        is_rule_enabled=_validation_guard,
                    )
                    all_nodes.extend(nodes)
                    all_edges.extend(edges)
                    js_scanned += 1
                except Exception as exc:  # pragma: no cover
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
                        findings_out=annotation_findings,
                        is_rule_enabled=_validation_guard,
                    )
                    all_nodes.extend(xs_nodes)
                    all_edges.extend(xs_edges)
                except Exception as exc:  # pragma: no cover
                    warnings.append(f"xstate_scanner failed on {js_file.relative_to(project_root)}: {exc}")
                    logger.warning("xstate_scanner error on %s: %s", js_file, exc)

            logger.info("build: scanned %d JS/TS files (%d skipped mtime)", js_scanned, js_skipped_mtime)
        else:
            logger.info("build: js_paths configured but tree-sitter not installed — skipping JS/TS scan")
            warnings.append(
                "js_paths configured but tree-sitter not installed — install axiom-graph[js] to enable JS/TS scanning"
            )

    口 = Step(
        step_num=4,
        name="Scan doc files",
        purpose="Run doc_scanner on Markdown files and json_doc_scanner on DocJSON files in docs/, then report doc-id overlaps across the whole tree",
        inputs="docs_dir, stored_mtimes",
        outputs="all_nodes/all_edges extended with doc and section nodes; doc/section records upserted; warnings for missing roots, for doc ids more than one file derives, and for dotted filenames",
        critical="The overlap signal enumerates every DocJSON on disk, not the files walked this build — an incremental build must notice a collision against an mtime-skipped file, and ordinary JSON beside the docs must never produce one",
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
    # Doc ids flatten every configured root into one ``docs.`` namespace
    # and rewrite ``/`` to ``.``; neither transform is injective, so two
    # files can derive one identity and the last-scanned one silently
    # wins.  This pass enumerates every DocJSON *on disk* rather than the
    # files walked above, so an incremental build reports an overlap
    # against a file the mtime fast-pass skipped just as a full build does.
    # Only DocJSON documents are counted: a project is free to keep
    # ordinary JSON beside its docs, and those files never become doc
    # nodes, so a finding about them would always be a false positive.
    # ------------------------------------------------------------------
    try:
        _doc_files = doc_ids.enumerate_doc_files(project_root, config.scan.docs_dirs)
        _signals = doc_ids.doc_id_signals(project_id, _doc_files)
        for _collision in _signals.collisions:
            warnings.append(
                f"duplicate doc id {_collision.doc_id} derived from "
                f"{len(_collision.sources)} files: {', '.join(_collision.sources)} — last scanned wins"
            )
        for _dotted in _signals.dotted:
            warnings.append(
                f"dotted DocJSON filename {_dotted} — the extra dots are indistinguishable "
                "from directory separators in the derived doc id; consider hyphens"
            )
    except Exception as exc:  # pragma: no cover
        warnings.append(f"doc id overlap scan failed: {exc}")
        logger.warning("doc id overlap scan error: %s", exc)

    口 = Step(
        step_num=5,
        name="Scan config directories",
        purpose="Scan .claude/ and other config dirs for settings, skills, and hook files",
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
        step_num=6,
        name="Batch upsert nodes and edges",
        purpose=(
            "Validate ontology constraints and upsert all discovered nodes (single transaction) then "
            "edges (single transaction), then retarget delegate links that name no node by following "
            "the re-export relation"
        ),
        inputs="all_nodes, all_edges, node_type_map",
        outputs="nodes_written, nodes_skipped, edges_written, edges_skipped, delegate_targets_resolved counts updated",
        critical=(
            "The delegate resolver reads its symbol table from the index, never from this build's scan "
            "output, and rewrites all_edges so the reconciliation pass downstream sees corrected targets"
        ),
    )
    # ------------------------------------------------------------------
    # Validate ontology and upsert nodes (single transaction)
    # ------------------------------------------------------------------
    node_type_map: dict[str, str] = {}
    with db._connect(db_path) as conn:
        # Capture pre-build node IDs so the rename matcher's newly-appeared
        # target guard can tell a fresh node from a re-scanned one.
        existing_ids_before: set[str] = {r["id"] for r in conn.execute("SELECT id FROM nodes")}
        for node in all_nodes:
            node_type_map[node.id] = node.node_type
            口 = AutoStep(step_num=6.1, name="Upsert node")
            written = db.upsert_node_conn(conn, node, discovery_only=discovery_only, git_sha=git_sha)
            if written:
                nodes_written += 1
            else:
                nodes_skipped += 1

    # ------------------------------------------------------------------
    # Validate ontology and upsert edges (single transaction)
    # ------------------------------------------------------------------
    with db._connect(db_path) as conn:
        for edge in all_edges:
            from_type = node_type_map.get(edge.from_id)
            to_type = node_type_map.get(edge.to_id)

            # validates edges are auto-generated by AST call analysis and may
            # target names that are not indexed functions (e.g. classes, constants,
            # fixture-mediated calls). Silently skip rather than warn — these are
            # expected misses, not ontology violations.
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

            口 = AutoStep(step_num=6.2, name="Upsert edge")
            written = db.upsert_edge_conn(conn, edge)
            if written:
                edges_written += 1
            else:
                edges_skipped += 1

    # ------------------------------------------------------------------
    # Delegate-target resolution — a scanner sees one file at a time and
    # cannot know where a re-exported symbol is defined, so a call routed
    # through a re-export shim resolves to a module that defines nothing.
    # After the upserts above, the index holds every node and every
    # re-export marker, and the closure can be walked.  Deliberately not a
    # phase of its own: renumbering the steps below it would strand the
    # existing step nodes, which is the very surface this pass exists to
    # repair (see the cycle's decision log).
    # ------------------------------------------------------------------
    delegate_targets_resolved = _resolve_delegate_targets(db_path, all_edges, warnings)
    if delegate_targets_resolved:
        logger.info(
            "delegate resolver: retargeted %d delegate link(s) through the re-export closure",
            delegate_targets_resolved,
        )

    口 = Step(
        step_num=7,
        name="Rename detection",
        purpose="Detect hash-similarity renames: find existing code nodes missing from this scan whose code_hash matches a newly discovered node",
        critical="Mtime-skipped files must be included in scanned_ids to prevent false renames",
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
    rename_skipped_reasons: dict[str, int] = {}
    if files_scanned > 0 or docs_scanned > 0:
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

            口 = AutoStep(step_num=7.1, name="Build scope-reduced lost/found pools")
            # Include mtime-skipped nodes (still on disk, unchanged) so they are
            # not mistaken for lost nodes.
            scanned_ids: set[str] = {n.id for n in all_nodes}
            if files_skipped_mtime > 0:
                for n in db.all_nodes(db_path):
                    if n.node_type in _PROC and (project_root / n.location).exists():
                        scanned_ids.add(n.id)

            lost_nodes: list = []
            for old in db.all_nodes(db_path):
                if old.node_type not in _PROC or old.id in scanned_ids:
                    continue
                if not getattr(old, "code_hash", None):
                    continue
                s, e = _line_range(old)
                sha = None
                for row in db.get_history(db_path, old.id, limit=50):
                    if row.get("git_sha"):
                        sha = row["git_sha"]
                        break
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
                口 = AutoStep(step_num=7.2, name="Run matcher + apply renames")
                match = _rm.run_matcher(adapter, pool_cap=cfg.rename.pool_cap)
                nodes_renamed = len(match.applied)
                renamed_new_ids = list(adapter.applied_new_ids)
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
                if rename_skipped_reasons:
                    parts = ", ".join(f"{r}={c}" for r, c in sorted(rename_skipped_reasons.items()))
                    msg = f"similarity skipped for {sum(rename_skipped_reasons.values())} scope(s): {parts}"
                    logger.info("rename matcher: %s", msg)
                    warnings.append(msg)
        except Exception as exc:  # pragma: no cover
            warnings.append(f"rename detection failed: {exc}")
            logger.warning("rename detection error: %s", exc)

    口 = AutoStep(step_num=8, name="Purge stale entries and prune vanished doc sections")
    # ------------------------------------------------------------------
    # Purge pass — remove DB rows for files that no longer exist on disk
    # ------------------------------------------------------------------
    nodes_purged = _purge_stale_entries(
        db_path, project_root, warnings, exclude_dirs=config.scan.exclude_dirs, git_sha=git_sha
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
        step_num=9,
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
    # Scope: ``_RECONCILED_SCANNER_EDGE_TYPES`` only — every other edge type
    # is untouched.
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
                    candidates = db.get_edge_source_ids_conn(conn, edge_type) & walked_source_ids
                    for from_id in candidates:
                        intended = intended_by_source.get((edge_type, from_id), set())
                        current = db.get_outbound_edge_targets_conn(conn, from_id, edge_type)
                        for target in current - intended:
                            if db.delete_edge_conn(conn, from_id, target, edge_type):
                                scanner_edges_reconciled += 1
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

    口 = AutoStep(step_num=10, name="Reap orphaned workflow step rows")
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
    # Scan-cache stamp — record the on-disk mtime of every file this build
    # actually opened and parsed, so the next build's mtime fast-pass can
    # skip it.  Runs in BOTH build modes: ``upsert_node_conn`` only writes
    # ``file_mtime`` when it inserts a row, so without this pass a node's
    # stored mtime would be frozen at first insertion forever.
    #
    # Runs after — and is guarded on — the four index-integrity passes above
    # that are gated on this build's scanned set: vanished-section pruning,
    # documents-edge reconciliation, scanner-edge reconciliation, and
    # orphaned step-row reaping.  All four swallow their failures into
    # ``warnings``, so control reaches here either way.  Stamping is a promise
    # that the next build may skip the file entirely, and that promise must
    # not be made while any of them left this file's index incomplete:
    # leaving the stored mtime behind is exactly what makes the next build
    # re-scan the file and retry them.
    #
    # Known and accepted exception — embedding generation (Step 13) is also
    # gated on this build's node set, also swallows its failures into
    # ``warnings``, and runs AFTER this stamp, so a file whose embeddings
    # failed is not retried until its bytes change.  That is deliberate.
    # Embeddings are an optional, separately hash-gated derived subsystem:
    # a missing vector degrades semantic search but does not corrupt the
    # index, and gating the scan cache on an optional subsystem would keep
    # the fast pass dead on every install that has no embedder configured.
    # ------------------------------------------------------------------
    file_mtimes_stamped = 0
    if per_file_pass_failed:
        logger.warning("build: skipping the scanned-file mtime stamp — a per-file pass failed; the next build re-scans")
    else:
        口 = AutoStep(step_num=11, name="Stamp scanned-file mtimes")
        file_mtimes_stamped = _stamp_scanned_file_mtimes(db_path, all_nodes)

    # ------------------------------------------------------------------
    # Broken-link detection — flag nodes with dangling edges created by
    # file deletions or renames (ADR-013 Layer 2).
    # ------------------------------------------------------------------
    broken_links_flagged = _flag_broken_links(db_path, warnings)

    口 = Step(
        step_num=12,
        name="Index doc sections into FTS",
        purpose="Add doc section heading+content to node_fts so they are discoverable via axiom_graph_search",
    )
    # ------------------------------------------------------------------
    # Doc section FTS indexing — make doc sections searchable
    # ------------------------------------------------------------------
    doc_sections_indexed = 0
    try:
        doc_sections_indexed = db.index_doc_sections_fts(db_path)
        if doc_sections_indexed > 0:
            logger.info("build: indexed %d doc sections into FTS", doc_sections_indexed)
    except Exception as exc:  # pragma: no cover
        warnings.append(f"doc section FTS indexing failed: {exc}")
        logger.warning("doc section FTS indexing error: %s", exc)

    口 = Step(
        step_num=13,
        name="Generate embeddings",
        purpose="Compute embedding vectors for code nodes and doc sections for semantic search",
    )
    # ------------------------------------------------------------------
    # Embedding generation — semantic search vectors
    # ------------------------------------------------------------------
    embeddings_generated = 0
    try:
        embeddings_generated = _generate_embeddings(db_path, all_nodes, warnings, embedder_thread)
    except Exception as exc:  # pragma: no cover
        warnings.append(f"embedding generation failed: {exc}")
        logger.warning("embedding generation error: %s", exc)

    # ------------------------------------------------------------------
    # Annotation B4 deferred pass: resolve AutoStep targets against the full
    # envelope set discovered in this build.
    # ------------------------------------------------------------------
    try:
        from axiom_graph.workflows.validation import validate_autostep_targets

        envelope_ids = {
            n.id
            for n in all_nodes
            if n.node_type == "composite_process" and getattr(n, "subtype", None) in ("workflow", "task")
        }
        # Fallback: if subtype not populated, use tags
        if not envelope_ids:
            envelope_ids = {
                n.id
                for n in all_nodes
                if n.node_type == "composite_process" and any(t in ("workflow", "task") for t in (n.tags or []))
            }
        b4_findings = validate_autostep_targets(
            autostep_records,
            envelope_node_ids=envelope_ids,
            is_rule_enabled=_validation_guard,
        )
        annotation_findings.extend(b4_findings)
    except Exception as exc:  # pragma: no cover
        logger.warning("annotation B4 pass failed: %s", exc)

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
        "nodes_purged": nodes_purged,
        "file_mtimes_stamped": file_mtimes_stamped,
        "documents_edges_reconciled": documents_edges_reconciled,
        "delegate_targets_resolved": delegate_targets_resolved,
        "scanner_edges_reconciled": scanner_edges_reconciled,
        "orphaned_steps_reaped": orphaned_steps_reaped,
        "orphaned_step_rows": orphaned_step_rows,
        "surplus_delegate_edges": surplus_delegate_edges,
        "broken_links_flagged": broken_links_flagged,
        "doc_sections_indexed": doc_sections_indexed,
        "embeddings_generated": embeddings_generated,
        "files_scanned": files_scanned,
        "files_skipped_mtime": files_skipped_mtime,
        "docs_skipped_mtime": docs_skipped_mtime,
        "config_scanned": config_scanned,
        "config_skipped_mtime": config_skipped_mtime,
        "js_scanned": js_scanned,
        "js_skipped_mtime": js_skipped_mtime,
        "warnings": warnings,
        "annotation_findings": [f.to_dict() for f in annotation_findings],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_embeddings(
    db_path: Path,
    all_nodes: list,
    warnings: list[str],
    embedder_thread=None,
) -> int:
    """Generate embedding vectors for code nodes and doc sections.

    Uses the embeddings module to convert node text (level_1 + level_2) into
    dense vectors stored in sqlite-vec. Skips nodes whose content hash has
    not changed since the last embedding.

    Processes nodes in batches for memory efficiency.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        all_nodes: List of AxiomNode objects from the current scan.
        warnings: Mutable list for error messages.
        embedder_thread: Optional warm-up thread to join before calling
            get_embedder(), avoiding a redundant model load.

    Returns:
        Number of embeddings generated (new or updated).
    """
    import os

    if os.environ.get("AXIOM_GRAPH_SKIP_EMBEDDINGS", "").strip() == "1":
        logger.info("build: skipping embeddings (AXIOM_GRAPH_SKIP_EMBEDDINGS=1)")
        return 0

    from axiom_graph.index.embeddings import (  # noqa: PLC0415
        EMBEDDING_DIM,
        content_hash_for_embedding,
        get_embedder,
    )

    # Initialize the vec table (no-op if already exists, returns False if
    # sqlite-vec is unavailable)
    if not db.init_embeddings(db_path, EMBEDDING_DIM):
        logger.info("build: skipping embeddings (sqlite-vec unavailable)")
        return 0

    # Wait for the background warm-up thread so get_embedder() returns the
    # cached model instead of loading it a second time.
    if embedder_thread is not None:
        t_wait = time.monotonic()
        embedder_thread.join()
        wait_elapsed = time.monotonic() - t_wait
        if wait_elapsed > 0.1:
            logger.info("build: waited %.1fs for embedder warm-up thread", wait_elapsed)

    embedder = get_embedder()
    generated = 0
    batch_size = 64

    # Bulk-load all existing embedding hashes (single query instead of N+1)
    t_hash = time.monotonic()
    existing_hashes = db.get_all_embedding_hashes(db_path)

    # Collect all items to embed: code nodes + doc sections
    items: list[tuple[str, str]] = []  # (node_id, text_to_embed)
    hashes: dict[str, str] = {}  # node_id -> content_hash
    total_candidates = 0

    for node in all_nodes:
        total_candidates += 1
        text = (node.level_1 or "") + "\n" + (node.level_2 or "")
        c_hash = content_hash_for_embedding(node.level_1, node.level_2)
        if existing_hashes.get(node.id) != c_hash:
            items.append((node.id, text))
            hashes[node.id] = c_hash

    # Also embed doc sections
    n_doc_sections = 0
    try:
        all_sections = db.list_all_doc_sections(db_path)
        n_doc_sections = len(all_sections)
        for sec in all_sections:
            total_candidates += 1
            sec_id = sec["id"]
            heading = sec.get("heading", "")
            content = sec.get("content", "")
            text = heading + "\n" + content
            c_hash = content_hash_for_embedding(heading, content)
            if sec_id in hashes:
                continue  # already queued via the scanned-nodes loop above
            if existing_hashes.get(sec_id) != c_hash:
                items.append((sec_id, text))
                hashes[sec_id] = c_hash
    except Exception as exc:
        warnings.append(f"doc section embedding collection failed: {exc}")
        logger.warning("doc section embedding error: %s", exc)

    hash_elapsed = time.monotonic() - t_hash
    logger.info(
        "build: collecting embeddings (%d code nodes, %d doc sections)",
        len(all_nodes),
        n_doc_sections,
    )

    if not items:
        logger.info(
            "build: no embeddings to generate (all %d up-to-date, hash check %.2fs)",
            total_candidates,
            hash_elapsed,
        )
        return 0

    total_batches = (len(items) + batch_size - 1) // batch_size
    logger.info(
        "build: %d of %d need embedding (%d up-to-date, hash check %.2fs)",
        len(items),
        total_candidates,
        total_candidates - len(items),
        hash_elapsed,
    )
    logger.info("build: generating %d embeddings in %d batches", len(items), total_batches)

    # Process in batches
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        batch_num = i // batch_size + 1
        t_batch = time.monotonic()
        texts = [text for _, text in batch]
        try:
            vectors = embedder(texts)
            # Batch DB write: single connection for the whole batch
            db_items = [(node_id, vec, hashes[node_id]) for (node_id, _), vec in zip(batch, vectors)]
            generated += db.upsert_embeddings_batch(db_path, db_items)
        except Exception as exc:
            warnings.append(f"embedding batch {batch_num} failed: {exc}")
            logger.warning("embedding batch error: %s", exc)
        logger.info(
            "build: embedding batch %d/%d — %d items (%.1fs)",
            batch_num,
            total_batches,
            len(batch),
            time.monotonic() - t_batch,
        )

    logger.info("build: generated %d embeddings", generated)
    return generated


# Depth cap for the re-export closure walk.  Deep enough for the shim
# chains that occur in practice (aggregator on top of aggregator), shallow
# enough that a pathological package tree cannot make a build crawl.
_REEXPORT_CLOSURE_MAX_DEPTH = 8


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

    The relation is supplied by the caller as "the modules this one
    re-exports from", not as any particular import form, so a second kind
    of re-export can be added later by extending the populator rather than
    this walk.

    Breadth-first, so the nearest definition wins.  Among candidates at the
    same distance the lexicographically smallest module id wins, which
    makes a name exported by two sources resolve the same way on every
    build rather than by dictionary order.  Already-visited modules are
    skipped, so a mutual re-export cycle terminates.

    Args:
        module_id: Node id of the module the target was guessed against.
        symbol: The symbol name being looked for.
        node_exists: Callable taking a node id and returning whether the
            index holds it.
        reexport_sources: Callable taking a module node id and returning
            the module ids it re-exports from.
        max_depth: Maximum number of re-export hops to follow.

    Returns:
        The node id of the defining symbol, or None when the closure does
        not reach one.
    """
    visited: set[str] = {module_id}
    frontier: list[str] = [module_id]
    for _ in range(max_depth):
        next_frontier: list[str] = []
        for current in frontier:
            for source in reexport_sources(current):
                if source in visited:
                    continue
                visited.add(source)
                next_frontier.append(source)
        if not next_frontier:
            return None
        for source in sorted(next_frontier):
            candidate = f"{source}::{symbol}"
            if node_exists(candidate):
                return candidate
        frontier = next_frontier
    return None


def _resolve_delegate_targets(db_path: Path, all_edges: list, warnings: list[str]) -> int:
    """Retarget this build's delegate links that name no node in the index.

    Runs after nodes and edges are upserted, so both the symbol table and
    the re-export relation are read from the **index** rather than from
    this build's scan output.  An incremental build only scans changed
    files, so a table derived from the scan would resolve correctly on
    full rebuilds and flap on every other one.

    An edge's identity embeds its target, so a retarget mints a NEW edge
    rather than mutating one.  ``all_edges`` is rewritten in place so the
    scanner-edge reconciliation pass downstream sees the corrected targets
    as this build's intended set and retires the superseded rows; running
    the other way round would leave a permanent ghost per retarget.

    Warns when there are unresolved delegate links and the index records no
    re-export relation at all.  The markers that populate that relation are
    written when a file is scanned, so an index predating them holds none
    and the mtime fast-pass keeps skipping the very files that would supply
    them — resolution is then inactive with nothing on the surface to say
    so.  The warning names the remedy because the remedy is the part that
    is undiscoverable.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        all_edges: This build's edge list, rewritten in place.
        warnings: Mutable list for error messages.

    Returns:
        Number of delegate links retargeted.
    """
    candidates = [(i, edge) for i, edge in enumerate(all_edges) if edge.edge_type == "delegates_to"]
    if not candidates:
        return 0

    resolved = 0
    try:
        with db._connect(db_path) as conn:
            existence_cache: dict[str, bool] = {}

            def node_exists(node_id: str) -> bool:
                if node_id not in existence_cache:
                    row = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
                    existence_cache[node_id] = row is not None
                return existence_cache[node_id]

            # The re-export relation, read once.  Only the star form
            # populates it today; the walk above never learns that.
            relation: dict[str, list[str]] = {}
            for row in conn.execute("SELECT from_id, to_id, meta FROM edges WHERE edge_type = 'depends_on'"):
                raw_meta = row["meta"]
                if not raw_meta:
                    continue
                try:
                    meta = json.loads(raw_meta)
                except (TypeError, ValueError):
                    continue
                if isinstance(meta, dict) and meta.get("reexport"):
                    relation.setdefault(row["from_id"], []).append(row["to_id"])
            for sources in relation.values():
                sources.sort()

            def reexport_sources(module_id: str) -> list[str]:
                return relation.get(module_id, [])

            unresolved = 0
            for index, edge in candidates:
                if node_exists(edge.to_id):
                    continue
                unresolved += 1
                module_id, _, symbol = edge.to_id.rpartition("::")
                if not module_id or not symbol:
                    continue
                new_target = resolve_symbol_through_reexports(
                    module_id,
                    symbol,
                    node_exists,
                    reexport_sources,
                )
                if new_target is None or new_target == edge.to_id:
                    continue
                new_edge = make_edge("delegates_to", edge.from_id, new_target)
                all_edges[index] = new_edge
                db.upsert_edge_conn(conn, new_edge)
                resolved += 1
                logger.info(
                    "delegate resolver: %s -> %s (was %s)",
                    new_edge.from_id,
                    new_edge.to_id,
                    edge.to_id,
                )

            if unresolved and not relation:
                message = (
                    f"{unresolved} delegate link(s) name no node and the index records no "
                    "re-export markers, so delegate-target resolution had nothing to follow. "
                    "Markers are written when a file is scanned, so an index built before "
                    "they existed holds none and the mtime fast-pass keeps skipping the files "
                    "that would supply them. Remedy: force a full rescan — clear the stored "
                    "file mtimes (UPDATE nodes SET file_mtime = NULL) and build again. If a "
                    "full rescan still records no markers, the project has no star re-exports "
                    "and these targets are out of this pass's reach."
                )
                warnings.append(message)
                logger.warning("delegate resolver: %s", message)
    except Exception as exc:  # pragma: no cover
        warnings.append(f"delegate target resolution failed: {exc}")
        logger.warning("delegate target resolution error: %s", exc)
    return resolved


def _flag_broken_links(db_path: Path, warnings: list[str]) -> int:
    """Detect and flag nodes with broken links after purge.

    Queries for edges whose to_id has no matching node and persists
    BROKEN_LINK staleness on the source nodes.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        warnings: Mutable list for error messages.

    Returns:
        Number of nodes flagged as BROKEN_LINK.
    """
    flagged = 0
    try:
        from axiom_graph.index.staleness import find_broken_links  # noqa: PLC0415

        broken = find_broken_links(db_path)
        if broken:
            with db._connect(db_path) as conn:
                for node_id in broken:
                    conn.execute(
                        "UPDATE nodes SET staleness = ?, link_status = ? WHERE id = ?",
                        (BROKEN_LINK, BROKEN_LINK, node_id),
                    )
                    flagged += 1
    except Exception as exc:  # pragma: no cover
        warnings.append(f"broken link detection failed: {exc}")
        logger.warning("broken link detection error: %s", exc)
    return flagged


def _iter_python_files(project_root: Path, skip_dirs: frozenset[str] = _BASE_SKIP_DIRS):
    """Yield all .py files under *project_root*, skipping ignored directories."""
    for path in project_root.rglob("*.py"):
        # Check every component of the path relative to project_root
        rel = path.relative_to(project_root)
        if any(part in skip_dirs for part in rel.parts):
            continue
        yield path


def _iter_js_files(
    project_root: Path,
    js_paths: list[str],
    skip_dirs: frozenset[str] = _BASE_SKIP_DIRS,
):
    """Yield JS/TS files matching *js_paths* globs, skipping ignored dirs.

    Args:
        project_root: Absolute path to the project root.
        js_paths: List of glob patterns relative to project_root.
        skip_dirs: Directory names to skip.
    """
    seen: set[Path] = set()
    for pattern in js_paths:
        for path in project_root.glob(pattern):
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
    purpose="Delete the step rows the index stores for a file this build walked whose markers that file's scan no longer emitted",
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
    purpose="Record the on-disk mtime of every file this build opened and parsed, so the next build's mtime fast-pass can skip it",
    inputs="db_path, the node set collected by this build's scanners",
    outputs="Number of file locations stamped",
)
def _stamp_scanned_file_mtimes(db_path: Path, nodes: list[AxiomNode]) -> int:
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

    Embedding generation is knowingly outside that guard: it too is gated on
    this build's node set but runs *after* the stamp, so a file whose
    embeddings failed is not retried until its bytes change.  See the call
    site in :func:`build` for why that is accepted.

    The set of files to stamp needs no skip-list: every scanner applies its
    own mtime fast-pass, and a skipped file contributes zero nodes, so the
    file-level mtimes riding on *nodes* already are exactly "the files this
    build opened and parsed".

    Only rows that already carry a non-NULL ``file_mtime`` are updated, which
    preserves the column's shape (module/doc nodes carry it, function nodes
    do not) and keeps both mtime readers in agreement for locations that
    stamp more than one row.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        nodes: Every node collected by this build's scanners.

    Returns:
        The number of distinct file locations stamped.
    """
    scanned_mtimes: dict[str, float] = {}
    for node in nodes:
        mtime = node.file_mtime
        location = node.location
        if mtime is None or not location:
            continue
        prior = scanned_mtimes.get(location)
        if prior is None or mtime > prior:
            scanned_mtimes[location] = mtime

    if not scanned_mtimes:
        logger.info("build: stamped 0 scanned-file mtimes")
        return 0

    with db._connect(db_path) as conn:
        conn.executemany(
            "UPDATE nodes SET file_mtime = ? WHERE location = ? AND file_mtime IS NOT NULL",
            [(mtime, location) for location, mtime in scanned_mtimes.items()],
        )
    logger.info("build: stamped %d scanned-file mtimes", len(scanned_mtimes))
    return len(scanned_mtimes)


@task(
    purpose="Remove DB rows for doc files and node locations that no longer exist on disk",
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

    Returns:
        Total number of nodes (doc + code) purged.
    """
    nodes_purged = 0

    口 = Step(
        step_num=1,
        name="Purge stale docs",
        purpose="Check all indexed doc file_paths against disk and cascade-delete missing ones",
    )
    try:
        doc_paths = db.get_all_doc_file_paths(db_path)
        for fp in doc_paths:
            abs_path = project_root / fp
            if not abs_path.exists():
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
            if not abs_path.exists():
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


def _is_docjson_file(path: Path) -> bool:
    """Return True iff *path* parses as a DocJSON document.

    A DocJSON document is a JSON file whose top-level value is an object with
    a ``"title"`` string and a ``"sections"`` array.  Detection is purely by
    content signature -- the file's path is ignored.

    Args:
        path: Absolute path to a file on disk.

    Returns:
        True iff the file is valid JSON matching the DocJSON shape.
    """
    import json as _json  # noqa: PLC0415

    try:
        if path.suffix.lower() != ".json":
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
        elif abs_path.suffix == ".json" and _is_docjson_file(abs_path):
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
            for n in scanned_nodes:
                db.upsert_node_conn(conn, n, discovery_only=True)
        return True
    except Exception as exc:
        logger.warning("rescan_file_if_needed failed for %s: %s", location, exc)
        return False
