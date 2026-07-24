"""Shared fixtures for axiom_graph tests."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from axiom_graph.index import db


def seed_section_node(
    conn: sqlite3.Connection,
    section_id: str,
    *,
    heading: str = "",
    content: str = "",
    level: int = 2,
    position: int = 0,
    tags: list[str] | None = None,
    desc_hash: str | None = "hash",
    code_hash: str = "hash",
    location: str = "docs/test.json",
    updated_at: str = "2026-01-01T00:00:00Z",
    subtype: str = "docjson_section",
    source: str = "json_doc_scanner",
) -> None:
    """Insert a DocJSON section node row directly (test fixture helper).

    Replaces the retired ``doc_sections`` table fixtures: sections are plain
    ``nodes`` rows (``subtype='docjson_section'``) with tags in the
    polymorphic ``tags`` table.

    Args:
        conn: Open DB connection (caller commits via context manager).
        section_id: Full section ID (``{doc_id}::{dot.path}``).
        heading: Section heading (stored in ``level_1``).
        content: Section markdown content (stored in ``level_2``).
        level: Heading level 1-6 (``doc_level``).
        position: Sibling position (``doc_position``).
        tags: Optional tag list written to the ``tags`` table.
        desc_hash: Content-mirror hash.
        code_hash: Staleness baseline hash.
        location: Relative DocJSON file path.
        updated_at: ISO timestamp.
        subtype: Node subtype (default ``docjson_section``).
        source: Scanner source (default ``json_doc_scanner``).
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO nodes
            (id, node_type, subtype, title, location, source, code_hash,
             desc_hash, level_0, level_1, level_2, doc_position, doc_level,
             updated_at)
        VALUES (?, 'atomic_process', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            section_id,
            subtype,
            heading or section_id,
            location,
            source,
            code_hash,
            desc_hash,
            section_id,
            heading,
            content,
            position,
            level,
            updated_at,
        ),
    )
    conn.execute("DELETE FROM tags WHERE node_id = ?", (section_id,))
    for tag in tags or []:
        conn.execute(
            "INSERT OR IGNORE INTO tags (node_id, tag) VALUES (?, ?)",
            (section_id, tag),
        )


@pytest.fixture
def mini_project(tmp_path: Path) -> Path:
    """Return a tmp directory initialised with an axiom-graph DB.

    Callers write their own .py files into it and call build() or
    scan_module() directly.
    """
    ag_dir = tmp_path / ".axiom_graph"
    ag_dir.mkdir()
    db.init_db(ag_dir / "graph.db")
    return tmp_path


@pytest.fixture
def db_path(mini_project: Path) -> Path:
    return mini_project / ".axiom_graph" / "graph.db"


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    """Temp directory with a real git repo + axiom-graph DB.

    Has an initial commit so HEAD exists.  E2E tests write .py files,
    commit, and call builder/staleness functions against real git.
    """
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    (tmp_path / ".gitkeep").touch()
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    ag_dir = tmp_path / ".axiom_graph"
    ag_dir.mkdir()
    db.init_db(ag_dir / "graph.db")
    return tmp_path


@pytest.fixture
def git_db_path(git_project: Path) -> Path:
    return git_project / ".axiom_graph" / "graph.db"


def seed_section_tuple(conn: sqlite3.Connection, params: tuple) -> None:
    """Seed a section node from a positional legacy-shaped fixture tuple.

    Accepts either the 11-tuple (id, doc_id, heading, level, tags_json,
    content, desc_hash, parent_id, depth, position, updated_at) or the
    6-tuple (id, doc_id, heading, level, position, updated_at).
    """
    import json as _json

    if len(params) == 11:
        (sec_id, _doc, heading, level, tags_json, content, desc_hash, _parent, _depth, position, updated_at) = params
        tags = _json.loads(tags_json) if tags_json else None
    elif len(params) == 6:
        (sec_id, _doc, heading, level, position, updated_at) = params
        tags, content, desc_hash = None, "", "hash"
    else:  # pragma: no cover - fixture authoring error
        raise ValueError(f"unexpected section fixture tuple arity: {len(params)}")
    seed_section_node(
        conn,
        sec_id,
        heading=heading or "",
        content=content or "",
        level=level if level is not None else 2,
        position=position if position is not None else 0,
        tags=tags,
        desc_hash=desc_hash,
        updated_at=updated_at or "2026-01-01T00:00:00Z",
    )


def seed_section_row(conn: sqlite3.Connection, rec: dict) -> None:
    """Seed a section node from a legacy-shaped fixture record dict."""
    import json as _json

    tags_val = rec.get("tags")
    if isinstance(tags_val, str) and tags_val:
        tags = _json.loads(tags_val)
    elif isinstance(tags_val, list):
        tags = tags_val
    else:
        tags = None
    seed_section_node(
        conn,
        rec["id"],
        heading=rec.get("heading") or "",
        content=rec.get("content") or "",
        level=rec.get("level") if rec.get("level") is not None else 2,
        position=rec.get("position") if rec.get("position") is not None else 0,
        tags=tags,
        desc_hash=rec.get("desc_hash"),
        updated_at=rec.get("updated_at") or "2026-01-01T00:00:00Z",
    )
