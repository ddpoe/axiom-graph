"""Shared viz helpers extracted from ``viz/server.py`` during Phase 4 split.

Pure helpers live here; module-global state (``_DB_PATH``, ``_PROJECT_ROOT``,
``_TEST_PATHS``, ``_EXCLUDE_DIRS``) remains in ``viz.server`` and is accessed
lazily by the router submodules via ``from axiom_graph.viz import server``.

No behavior changes — these are the same functions that used to live in
``viz/server.py``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from typing import Any


# ---------------------------------------------------------------------------
# Pytest built-in fixture names to exclude from fixture parsing
# ---------------------------------------------------------------------------

_BUILTIN_FIXTURES = frozenset(
    {
        "self",
        "request",
        "tmp_path",
        "tmp_path_factory",
        "monkeypatch",
        "capsys",
        "capfd",
        "caplog",
        "pytestconfig",
        "recwarn",
    }
)


# ---------------------------------------------------------------------------
# Pure parsing helpers — no module-global state
# ---------------------------------------------------------------------------


def _parse_envelope_line_start(level_3_location: str | None) -> int | None:
    """Return the line_start from a ``file.py#L10`` string, or None."""
    if not level_3_location:
        return None
    m = re.search(r"#L(\d+)", level_3_location)
    return int(m.group(1)) if m else None


def _parse_level3_lines(level_3_location: str | None) -> tuple[int | None, int | None]:
    """Parse `level_3_location` ('path/file.py#L10-L45') into (line_start, line_end)."""
    if not level_3_location:
        return None, None
    m = re.search(r"#L(\d+)(?:-L?(\d+))?", level_3_location)
    if not m:
        return None, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return start, end


def _parse_fixture_names(signature: str | None) -> list[str]:
    """Extract custom fixture names from an annotated function signature string."""
    if not signature:
        return []
    m = re.search(r"\(([^)]*)\)", signature)
    if not m:
        return []
    params = [p.strip().split(":")[0].split("=")[0].strip() for p in m.group(1).split(",")]
    return [p for p in params if p and p not in _BUILTIN_FIXTURES]


def _classname_to_stem(classname: str) -> str:
    """Extract module stem from a JUnit classname."""
    parts = classname.split(".")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].startswith("test_"):
            return parts[i]
    return parts[-1] if parts else "unknown"


# ---------------------------------------------------------------------------
# Envelope / step DB accessors — pure (take conn)
# ---------------------------------------------------------------------------


def _annotated_function_id(conn: sqlite3.Connection, envelope_id: str) -> str | None:
    """Return the function node ID that an envelope ``annotates``, if any."""
    row = conn.execute(
        "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = 'annotates' LIMIT 1",
        (envelope_id,),
    ).fetchone()
    return row["to_id"] if row else None


def _envelopes_by_subtype(conn: sqlite3.Connection, subtype: str) -> list[sqlite3.Row]:
    """Fetch all envelope nodes (composite_process + subtype), ordered by location/line."""
    return conn.execute(
        """
        SELECT n.id, n.title, n.location, n.level_1, n.level_2,
               n.level_3_location, n.dflow_meta, n.subtype
        FROM nodes n
        JOIN tags t ON t.node_id = n.id
        WHERE n.node_type = 'composite_process'
          AND n.subtype = ?
          AND t.tag = 'envelope'
        ORDER BY n.location, n.level_3_location
        """,
        (subtype,),
    ).fetchall()


def _step_count_for_envelope(conn: sqlite3.Connection, envelope_id: str) -> int:
    """Count ``composes`` edges from envelope_id → step nodes."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM edges e
        JOIN nodes n ON n.id = e.to_id
        WHERE e.from_id = ?
          AND e.edge_type = 'composes'
          AND n.node_type = 'atomic_process'
          AND n.subtype IN ('step', 'autostep')
        """,
        (envelope_id,),
    ).fetchone()
    return row["c"] if row else 0


def _steps_for_envelope(conn: sqlite3.Connection, envelope_id: str) -> list[sqlite3.Row]:
    """Ordered step/autostep nodes reachable via ``composes`` from envelope_id."""
    return conn.execute(
        """
        SELECT n.id, n.title, n.location, n.level_1, n.level_2,
               n.level_3_location, n.dflow_meta, n.subtype
        FROM edges e
        JOIN nodes n ON n.id = e.to_id
        WHERE e.from_id = ?
          AND e.edge_type = 'composes'
          AND n.node_type = 'atomic_process'
          AND n.subtype IN ('step', 'autostep')
        """,
        (envelope_id,),
    ).fetchall()


def _delegates_target(conn: sqlite3.Connection, step_id: str) -> str | None:
    """Return the function node that an AutoStep ``delegates_to``, if any."""
    row = conn.execute(
        "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = 'delegates_to' LIMIT 1",
        (step_id,),
    ).fetchone()
    return row["to_id"] if row else None


def _sort_step_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Sort step rows by numeric step_num_raw from dflow_meta."""

    def key(row):
        meta: dict = {}
        if row["dflow_meta"]:
            try:
                meta = json.loads(row["dflow_meta"])
            except (json.JSONDecodeError, TypeError):
                pass
        raw = meta.get("step_num_raw") or "0"
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# Node serialization helpers
# ---------------------------------------------------------------------------


def _node_to_dict(n: Any) -> dict:
    return dataclasses.asdict(n)


def _edge_to_dict(e: Any) -> dict:
    return dataclasses.asdict(e)
