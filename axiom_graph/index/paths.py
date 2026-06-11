"""Public DB path helpers.

These functions resolve the on-disk SQLite path for an axiom-graph project.
Lives in the ``index`` package because the DB path is the canonical shared
artifact every domain talks to — it predates and is independent of any
particular domain (docjson, workflows, lifecycle).

Public surface:
    ``db_path``    -- return the DB path for a project (file may not exist)
    ``require_db`` -- return the DB path, raising ``FileNotFoundError`` when missing

Per ADR-019, this module's allowed imports are: ``axiom_graph.config`` and
stdlib only.  Nothing else.
"""

from __future__ import annotations

from pathlib import Path

from axiom_graph.config import db_path_for


def db_path(project_root: str | Path) -> Path:
    """Return the path to the axiom-graph DB for a project.

    The DB file may or may not exist on disk; this function does not
    perform any I/O beyond a ``resolve()`` on the project root.

    Args:
        project_root: Absolute (or resolvable) path to the project root.

    Returns:
        ``Path`` pointing at ``.axiom_graph/graph.db`` under the project root.
    """
    return db_path_for(Path(project_root).resolve())


def require_db(project_root: str | Path) -> Path:
    """Return the DB path, raising if it does not exist.

    Args:
        project_root: Absolute (or resolvable) path to the project root.

    Returns:
        ``Path`` to ``.axiom_graph/graph.db``.

    Raises:
        FileNotFoundError: If the DB file does not exist on disk.
    """
    path = db_path(project_root)
    if not path.exists():
        raise FileNotFoundError(f"No index at {path}. Call axiom_graph_build('{project_root}') first.")
    return path
