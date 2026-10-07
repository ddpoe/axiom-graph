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
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


from axiom_graph.index import db
from axiom_graph.index.status import VERIFIED
from axiom_graph.lifecycle.api import read_graph_view, refresh_before_read
from axiom_graph.query.api import node_tags
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
    from ..index.builder import resolve_project_id

    _PROJECT_ID = resolve_project_id(project_root, _DB_PATH, config=_cfg)
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


def _docs_extensions() -> list[str]:
    """Return the configured DocJSON extensions; the first is the write extension.

    Falls back to the default extensions when no project is active or the
    config cannot be loaded.
    """
    from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

    if _PROJECT_ROOT is None:
        return AxiomGraphConfig().scan.docs_extensions
    try:
        return AxiomGraphConfig.load(_PROJECT_ROOT).scan.docs_extensions
    except Exception as exc:
        logger.warning("could not load docs_extensions from %s, using the default: %s", _PROJECT_ROOT, exc)
        return AxiomGraphConfig().scan.docs_extensions


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
    return db.open_connection(_DB_PATH)


def _node_to_dict(n: Any) -> dict:
    return dataclasses.asdict(n)


def _edge_to_dict(e: Any) -> dict:
    return dataclasses.asdict(e)


def _hydrate_tags(nodes: list) -> list:
    tags_map = node_tags(_db(), [n.id for n in nodes])
    for n in nodes:
        n.tags = tags_map.get(n.id, [])
    return nodes


def _compute_staleness_for_viz(nodes: list) -> dict[str, tuple[str, str]]:
    """Return ``{node_id: (own_status, link_status)}`` for *nodes* after the read helper refreshed them.

    A node-naming view (the neighbourhood) gets the cone of its nodes
    (:func:`~axiom_graph.lifecycle.api.refresh_before_read`): the files their
    statuses read and the journal rows naming them, stored as ``check --full``
    would store them.  Evaluating only the shown nodes and storing that
    (the old ``record_staleness_settled`` over a subset) could store a status
    the rest of the graph contradicts.
    """
    if _PROJECT_ROOT is None or _DB_PATH is None:
        return {}
    rr = refresh_before_read(_DB_PATH, _PROJECT_ROOT, [n.id for n in nodes])
    return {n.id: rr.statuses.get(n.id, (VERIFIED, VERIFIED)) for n in nodes}


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

    node_count = len(nodes)

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
def search(q: str = "", type: str | None = None, max_results: int = 50) -> dict:
    """FTS5 search.  Returns nodes + the stage label that produced them."""
    if not q.strip():
        return {"nodes": [], "mode": "empty", "total": 0}

    nodes, kw_mode, total = db.fts_search(_db(), q, node_type=type, max_results=max_results)
    _hydrate_tags(nodes)
    return {"nodes": [_node_to_dict(n) for n in nodes], "mode": kw_mode, "total": total}


@app.get("/api/all")
def get_all() -> dict:
    """Full dump of all nodes + edges + staleness.  For small projects only.
    Sets `large: true` when node count > 400 to signal the frontend to switch
    to filtered/neighborhood mode.
    """
    view = read_graph_view(_db(), _PROJECT_ROOT)
    nodes, edges, staleness, verifications = view.nodes, view.edges, view.staleness, view.verifications

    out = {
        "nodes": [_node_to_dict(n) for n in nodes],
        "edges": [_edge_to_dict(e) for e in edges],
        "staleness": _staleness_to_dicts(staleness),
        "verifications": verifications,
        "large": len(nodes) > 400,
    }
    if view.behind:
        out["behind"] = view.behind
    return out


@app.get("/api/check")
def get_check() -> dict:
    """Hash-based staleness for every node after the incremental check.  Returns full map + summary counts.

    Runs the ``check`` command's refresh (``refresh_before_read`` in
    ``"check"`` mode, equal to ``check --full`` by parity), then reads the
    stored statuses, on one connection.
    """
    view = read_graph_view(_db(), _PROJECT_ROOT, mode="check", edges=False, tags=False)
    nodes, stored, verifications = view.nodes, view.staleness, view.verifications
    statuses = {n.id: stored.get(n.id, (VERIFIED, VERIFIED)) for n in nodes}
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
