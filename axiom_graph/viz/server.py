"""Axiom-graph Viz — FastAPI server for the visualization dashboard.

All routes are thin wrappers over axiom_graph.index.db.  No new query logic lives
here; the db module is the single source of truth.

Module-level state (_PROJECT_ROOT, _DB_PATH) is set by run_server() before
uvicorn starts — this avoids any need for dependency injection or config files.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
import threading

logger = logging.getLogger(__name__)
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


from axiom_graph.index import db
from axiom_graph.index.staleness import record_staleness
from axiom_graph.registry import (
    prune_registry as _prune_registry,
    project_display_name as _project_display_name,
    upsert_registry as _upsert_registry,
)

# ---------------------------------------------------------------------------
# Module-level state — set by run_server() before uvicorn starts
# ---------------------------------------------------------------------------

_PROJECT_ROOT: Path | None = None
_PRIMARY_PROJECT_ROOT: Path | None = None  # Set once at launch; fallback target.
_PROJECT_ID: str | None = None  # From axiom-graph.toml or directory name
_DB_PATH: Path | None = None
_TEST_PATHS: list[str] = []  # From axiom-graph.toml [axiom_graph.scan] test_paths
_EXCLUDE_DIRS: list[str] = []  # From axiom-graph.toml [axiom_graph.scan] exclude_dirs
_TRANSITIVE_TAGS: list[str] = []  # From [axiom_graph.staleness] transitive_tags
_FROZEN_TAGS: list[str] = []  # From [axiom_graph.staleness] frozen_tags

# Guard concurrent access during project switch
_switch_lock = threading.Lock()


def _apply_project(project_root: Path) -> None:
    """Set all module globals to point at *project_root*.

    Must be called while holding ``_switch_lock``.
    """
    global _PROJECT_ROOT, _PROJECT_ID, _DB_PATH, _TEST_PATHS, _EXCLUDE_DIRS  # noqa: PLW0603
    global _TRANSITIVE_TAGS, _FROZEN_TAGS  # noqa: PLW0603
    from axiom_graph.config import db_path_for

    _PROJECT_ROOT = project_root
    _DB_PATH = db_path_for(project_root)
    from ..config import AxiomGraphConfig

    _cfg = AxiomGraphConfig.load(project_root)
    _PROJECT_ID = _cfg.project_id or project_root.name
    _TEST_PATHS = _cfg.scan.test_paths
    _EXCLUDE_DIRS = _cfg.scan.exclude_dirs
    _TRANSITIVE_TAGS = _cfg.staleness.transitive_tags
    _FROZEN_TAGS = _cfg.staleness.frozen_tags


# ---------------------------------------------------------------------------
# App — no docs endpoints to keep the surface clean
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Axiom-graph Viz", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _db() -> Path:
    if _DB_PATH is None:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return _DB_PATH


def _docs_roots() -> list[Path]:
    """Return all configured docs roots as absolute paths.

    Reads ``config.scan.docs_dirs`` from the active project's config.  Each
    entry is resolved to an absolute path (absolute entries honored as-is;
    relative entries resolved against the project root).  Duplicate absolute
    paths are collapsed.  Order is preserved so ``[0]`` is the primary root.
    """
    if _PROJECT_ROOT is None:
        return []
    from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

    try:
        cfg = AxiomGraphConfig.load(_PROJECT_ROOT)
        entries = cfg.scan.docs_dirs or ["docs"]
    except Exception:
        entries = ["docs"]
    seen: set[str] = set()
    out: list[Path] = []
    for entry in entries:
        ep = Path(entry)
        abs_path = ep if ep.is_absolute() else (_PROJECT_ROOT / ep)
        key = str(abs_path)
        if key in seen:
            continue
        seen.add(key)
        out.append(abs_path)
    return out


def _primary_docs_root() -> Path:
    """Return the first configured docs root (fallback: project_root/docs)."""
    roots = _docs_roots()
    if roots:
        return roots[0]
    assert _PROJECT_ROOT is not None  # noqa: S101 — _db() check is the gate in callers
    return _PROJECT_ROOT / "docs"


def _docs_root_rels() -> list[str]:
    """Return configured docs roots as POSIX-relative-to-project strings.

    Entries outside the project root are returned as their absolute POSIX
    path (rare edge case — absolute docs_dirs entries).
    """
    if _PROJECT_ROOT is None:
        return ["docs"]
    out: list[str] = []
    for p in _docs_roots():
        try:
            rel = p.relative_to(_PROJECT_ROOT).as_posix()
            out.append(rel or ".")
        except ValueError:
            out.append(p.as_posix())
    return out or ["docs"]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _node_to_dict(n: Any) -> dict:
    return dataclasses.asdict(n)


def _edge_to_dict(e: Any) -> dict:
    return dataclasses.asdict(e)


def _get_tags_bulk(node_ids: list[str]) -> dict[str, list[str]]:
    """Batch-load tags for a list of node IDs.  Returns {node_id: [tag, ...]}."""
    if not node_ids:
        return {}
    conn = _connect()
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"SELECT node_id, tag FROM tags WHERE node_id IN ({placeholders})",
        node_ids,
    ).fetchall()
    conn.close()
    result: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        result[r["node_id"]].append(r["tag"])
    return dict(result)


def _hydrate_tags(nodes: list) -> list:
    tags_map = _get_tags_bulk([n.id for n in nodes])
    for n in nodes:
        n.tags = tags_map.get(n.id, [])
    return nodes


def _compute_staleness_for_viz(nodes: list) -> dict[str, tuple[str, str]]:
    """Thin wrapper — delegates to record_staleness (computes + records transitions + persists).

    Strips the via list from the 3-tuple returned by record_staleness,
    returning the 2-tuple (own_status, link_status) that the viz frontend
    expects.
    """
    if _PROJECT_ROOT is None or _DB_PATH is None:
        return {}
    full = record_staleness(
        _DB_PATH,
        _PROJECT_ROOT,
        nodes,
        transitive_tags=_TRANSITIVE_TAGS,
        frozen_tags=_FROZEN_TAGS,
    )
    return {nid: (own, link) for nid, (own, link, _via) in full.items()}


def _staleness_to_dicts(
    statuses: dict[str, tuple[str, str]],
) -> dict[str, dict[str, str]]:
    """Convert ``(own_status, link_status)`` tuples to JSON-friendly dicts.

    The frontend expects ``{own_status: ..., link_status: ...}`` objects, not
    Python tuples (which serialize as JSON arrays).
    """
    return {node_id: {"own_status": own, "link_status": link} for node_id, (own, link) in statuses.items()}


# ---------------------------------------------------------------------------
# Routes — all defined before the static mount so they take precedence
# ---------------------------------------------------------------------------


@app.get("/api/meta")
def get_meta() -> dict:
    """Project summary: counts, edge types, tags, statuses."""
    nodes = db.all_nodes(_db())
    edges = db.all_edges(_db())

    type_counts = Counter(n.node_type for n in nodes)
    edge_types = sorted({e.edge_type for e in edges})

    conn = _connect()
    tag_rows = conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()
    conn.close()

    # Check embeddings availability
    node_count = len(nodes)
    emb_info = {"available": False, "count": 0, "node_count": node_count, "coverage": 0.0}
    try:
        emb_conn = _connect()
        # Check if vec_embeddings table exists
        has_table = (
            emb_conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='vec_embeddings'"
            ).fetchone()[0]
            > 0
        )
        if has_table:
            emb_count = emb_conn.execute("SELECT COUNT(*) FROM embedding_hashes").fetchone()[0]
            emb_info = {
                "available": emb_count > 0,
                "count": emb_count,
                "node_count": node_count,
                "coverage": round(emb_count / node_count, 4) if node_count > 0 else 0.0,
            }
        emb_conn.close()
    except Exception:
        pass  # embeddings unavailable — defaults are fine

    return {
        "project_id": _PROJECT_ID or (_PROJECT_ROOT.name if _PROJECT_ROOT else "unknown"),
        "project_root": str(_PROJECT_ROOT.resolve()) if _PROJECT_ROOT else "",
        "node_count": node_count,
        "edge_count": len(edges),
        "type_counts": dict(type_counts),
        "edge_types": edge_types,
        "tags": [r["tag"] for r in tag_rows],
        "statuses": sorted({n.status for n in nodes}),
        "test_paths": _TEST_PATHS,
        "embeddings": emb_info,
    }


# ---------------------------------------------------------------------------
# Project switching endpoints
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def get_projects() -> dict:
    """Return the registry of known projects and which one is active.

    Lazy cleanup: entries whose ``path`` no longer exists are dropped from
    the response and pruned from the on-disk registry in the same pass.
    Active-project fallback: if the currently active project's path has
    been deleted, fall back to the launch-time primary project.
    """
    global _PROJECT_ROOT  # noqa: PLW0603 — fallback rebind on missing active path
    projects = _prune_registry()
    if _PROJECT_ROOT is not None and not _PROJECT_ROOT.exists() and _PRIMARY_PROJECT_ROOT is not None:
        with _switch_lock:
            _apply_project(_PRIMARY_PROJECT_ROOT)
    active = str(_PROJECT_ROOT.resolve()) if _PROJECT_ROOT else None
    for p in projects:
        p["active"] = p["path"] == active
    return {"projects": projects, "active_project": active}


class _ProjectBody(BaseModel):
    project_root: str


@app.post("/api/projects/register")
def register_project(body: _ProjectBody) -> dict:
    """Validate and register a new project path."""
    root = Path(body.project_root).resolve()
    db_file = root / ".axiom_graph" / "graph.db"
    if not db_file.exists():
        raise HTTPException(
            status_code=400,
            detail=f"No .axiom_graph/graph.db found at {root}",
        )
    projects = _upsert_registry(root)
    active = str(_PROJECT_ROOT.resolve()) if _PROJECT_ROOT else None
    for p in projects:
        p["active"] = p["path"] == active
    return {"projects": projects, "registered": {"path": str(root), "name": _project_display_name(root)}}


@app.post("/api/projects/switch")
def switch_project(body: _ProjectBody) -> dict:
    """Hot-swap the active project root and return fresh meta."""
    root = Path(body.project_root).resolve()
    db_file = root / ".axiom_graph" / "graph.db"
    if not db_file.exists():
        raise HTTPException(
            status_code=400,
            detail=f"No .axiom_graph/graph.db found at {root}",
        )
    with _switch_lock:
        _apply_project(root)
        _upsert_registry(root)
    # Return the same shape as /api/meta so the frontend can reinit.
    return get_meta()


@app.get("/api/search")
def search(q: str = "", type: str | None = None, max_results: int = 50, mode: str = "keyword") -> dict:
    """FTS5 or semantic search.  Returns nodes + mode label."""
    if not q.strip():
        return {"nodes": [], "mode": "empty", "total": 0}

    if mode == "semantic":
        try:
            from axiom_graph.index.embeddings import get_embedder

            embedder = get_embedder()
            query_vec = embedder([q])[0]
            nodes, total = db.semantic_search(
                _db(),
                query_vec,
                max_results=max_results,
                node_type=type,
            )
            if not nodes:
                # Fall back to keyword if semantic returns nothing
                nodes, kw_mode, total = db.fts_search(_db(), q, node_type=type, max_results=max_results)
                _hydrate_tags(nodes)
                return {"nodes": [_node_to_dict(n) for n in nodes], "mode": f"keyword ({kw_mode})", "total": total}
            _hydrate_tags(nodes)
            return {"nodes": [_node_to_dict(n) for n in nodes], "mode": "semantic", "total": total}
        except Exception as exc:
            logger.warning("Semantic search failed, falling back to keyword: %s", exc)
            nodes, kw_mode, total = db.fts_search(_db(), q, node_type=type, max_results=max_results)
            _hydrate_tags(nodes)
            return {"nodes": [_node_to_dict(n) for n in nodes], "mode": f"keyword ({kw_mode})", "total": total}

    nodes, kw_mode, total = db.fts_search(_db(), q, node_type=type, max_results=max_results)
    _hydrate_tags(nodes)
    return {"nodes": [_node_to_dict(n) for n in nodes], "mode": kw_mode, "total": total}


@app.get("/api/all")
def get_all() -> dict:
    """Full dump of all nodes + edges + staleness.  For small projects only.
    Sets `large: true` when node count > 400 to signal the frontend to switch
    to filtered/neighborhood mode.
    """
    nodes = db.all_nodes(_db())
    edges = db.all_edges(_db())
    _hydrate_tags(nodes)
    staleness = db.get_all_staleness(_db())
    verifications = db.get_all_verifications(_db())

    return {
        "nodes": [_node_to_dict(n) for n in nodes],
        "edges": [_edge_to_dict(e) for e in edges],
        "staleness": _staleness_to_dicts(staleness),
        "verifications": verifications,
        "large": len(nodes) > 400,
    }


@app.get("/api/check")
def get_check() -> dict:
    """Full hash-based staleness for every node.  Returns full map + summary counts.

    Delegates to ``record_staleness()`` which computes staleness, records
    transition events, and persists results in one transaction.
    """
    nodes = db.all_nodes(_db())
    statuses = _compute_staleness_for_viz(nodes)
    verifications = db.get_all_verifications(_db())
    # Count by own_status for the summary (frontend expects string keys)
    own_statuses = [own for own, _link in statuses.values()]
    return {
        "statuses": _staleness_to_dicts(statuses),
        "summary": dict(Counter(own_statuses)),
        "verifications": verifications,
    }


@app.get("/api/config")
def get_viz_config() -> dict:
    """Return the subset of the active project's config the frontend needs.

    Returns:
        dict with keys:
            - ``docs_dirs``: POSIX-relative (or absolute, when configured
              outside the project) docs root paths, in order — ``[0]`` is
              the primary write target.
            - ``project_id``: namespace prefix used for all node IDs.

    The DB path is intentionally omitted — the frontend has no need for it,
    and exposing filesystem internals serves no UI purpose.
    """
    if _PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")
    return {
        "docs_dirs": _docs_root_rels(),
        "project_id": _PROJECT_ID,
    }


# Register split router modules (workflows/docs/nodes).  Must happen before
# the static mount so /api/* routes take precedence.
from axiom_graph.viz.workflows import workflows_router  # noqa: E402
from axiom_graph.viz.docs import docs_router  # noqa: E402
from axiom_graph.viz.nodes import nodes_router  # noqa: E402

app.include_router(workflows_router)
app.include_router(docs_router)
app.include_router(nodes_router)

# Back-compat re-exports — the route handlers below moved out of this
# module in Phase 4 Task 4 (ADR-005, commit 578357c).  Tests and any
# external callers still import them as ``viz.server.<name>``; honor
# the 2.0.0 CHANGELOG promise that "Backwards-compat shims preserved
# for the old single-file imports".
from axiom_graph.viz.nodes import (  # noqa: E402, F401
    get_history_since_endpoint,
    get_node_diff_endpoint,
    get_recent_shas,
    get_staleness_cause,
)
from axiom_graph.viz.docs import render_doc_api  # noqa: E402, F401

# Mount static files LAST so all /api/* routes take precedence.
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------
# Entry point — called by `axiom-graph viz` CLI command
# ---------------------------------------------------------------------------


def run_server(project_root: Path, port: int = 8080, open_browser: bool = True) -> None:
    """Set module state and launch uvicorn.  Blocks until the server stops."""
    global _PRIMARY_PROJECT_ROOT  # noqa: PLW0603
    _apply_project(project_root)
    _PRIMARY_PROJECT_ROOT = project_root
    _upsert_registry(project_root)

    if open_browser:
        import threading
        import webbrowser

        def _open() -> None:
            import time

            time.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")

        threading.Thread(target=_open, daemon=True).start()

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
