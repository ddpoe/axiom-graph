"""Project registry for axiom-graph viz.

The registry tracks known project paths so viz can switch between them
without re-launching the server.  Persisted as JSON at
``~/.axiom_graph/projects.json``.

Used by ``axiom_graph.viz.server`` for the project-picker UI and by
``axiom_graph.cli.indexing.cmd_checkout`` / ``axiom_graph.mcp.lifecycle``
to auto-register worktrees the moment their DB snapshot is copied in.

Worktree entries are disambiguated from the primary clone by the
``[wt: <dirname>]`` suffix on the display name; both share the same
``project_id`` from ``axiom-graph.toml``.

Module-level constants ``REGISTRY_DIR`` and ``REGISTRY_PATH`` are
intentionally read inside each function (rather than imported by name
from callers) so tests can monkeypatch them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REGISTRY_DIR = Path.home() / ".axiom_graph"
REGISTRY_PATH = REGISTRY_DIR / "projects.json"


def load_registry() -> list[dict]:
    """Load the project registry from disk.

    Returns:
        List of registry entries (dicts with ``path``, ``name``, ``added``).
        Empty list if the registry file is missing, malformed, or unreadable.
    """
    if not REGISTRY_PATH.exists():
        return []
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        return data.get("projects", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_registry(projects: list[dict]) -> None:
    """Write the project registry to disk, creating the parent dir if needed.

    Args:
        projects: Full registry list to persist.
    """
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps({"projects": projects}, indent=2),
        encoding="utf-8",
    )


def project_display_name(project_root: Path) -> str:
    """Derive a human-readable name for a project root.

    Worktrees are disambiguated from the primary clone by appending
    ``[wt: <dirname>]``.  Detection: when a path is a git worktree,
    its ``.git`` is a *file* (containing ``gitdir: ...``), not a
    directory.  Cheap and avoids shelling out to git.

    Args:
        project_root: Absolute path to the project root.

    Returns:
        Display name string.  Falls back to the directory basename if
        ``axiom-graph.toml`` is missing or unparseable.
    """
    from .config import AxiomGraphConfig

    name: str | None = None
    try:
        cfg = AxiomGraphConfig.load(project_root)
        if cfg.project_id:
            name = cfg.project_id
    except Exception as exc:
        logger.debug("failed to load project config for display name: %s", exc)

    if name is None:
        # Basename is already disambiguating when project_id is absent.
        return project_root.name

    git_path = project_root / ".git"
    if git_path.is_file():
        return f"{name} [wt: {project_root.name}]"
    return name


def upsert_registry(project_root: Path) -> list[dict]:
    """Add or update a project in the registry.

    Idempotent: a second call on the same path refreshes the ``name``
    field but does not duplicate the entry.

    Args:
        project_root: Absolute path to the project root.

    Returns:
        The updated registry list.
    """
    resolved = str(project_root.resolve())
    projects = load_registry()
    for p in projects:
        if p["path"] == resolved:
            p["name"] = project_display_name(project_root)
            save_registry(projects)
            return projects
    projects.append(
        {
            "path": resolved,
            "name": project_display_name(project_root),
            "added": datetime.now(timezone.utc).isoformat(),
        }
    )
    save_registry(projects)
    return projects


def prune_registry() -> list[dict]:
    """Drop entries whose ``path`` no longer exists on disk.

    Reads the registry, filters out dead paths, and writes back if any
    entries were removed.  Robust to all worktree-deletion paths
    (``git worktree remove``, ``rm -rf``, force-prune).

    Returns:
        The (possibly filtered) registry list.
    """
    projects = load_registry()
    live = [p for p in projects if Path(p["path"]).exists()]
    if len(live) != len(projects):
        save_registry(live)
    return live
