"""Workflow / task / test routes — backed entirely by graph.db (axiom-graph).

Extracted from ``viz/server.py`` during Phase 4 split.  Module-globals
(``_PROJECT_ROOT``, ``_DB_PATH``, ``_PROJECT_ID``, ``_TEST_PATHS``,
``_EXCLUDE_DIRS``) live in ``viz.server`` and are accessed lazily via the
``server`` module attribute (Option A — minimal diff).

Route bodies copied verbatim from the original ``viz/server.py``.
"""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, HTTPException

from axiom_annotations import workflow
from axiom_graph.index import db

from axiom_graph.viz._core import (
    _annotated_function_id,
    _delegates_target,
    _envelopes_by_subtype,
    _parse_envelope_line_start,
    _parse_level3_lines,
    _sort_step_rows,
    _step_count_for_envelope,
    _steps_for_envelope,
)

logger = logging.getLogger(__name__)

workflows_router = APIRouter()


def _connect() -> sqlite3.Connection:
    from axiom_graph.viz import server

    conn = sqlite3.connect(str(server._DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _excluded_by_scan_config(location: str) -> bool:
    """True if *location* is under an excluded dir or a configured test path."""
    from axiom_graph.viz import server

    mod_path = location.replace("\\", "/")
    if server._EXCLUDE_DIRS:
        parts = mod_path.split("/")
        if any(d in parts for d in server._EXCLUDE_DIRS):
            return True
    if server._TEST_PATHS:
        if any(mod_path.startswith(tp.replace("\\", "/")) for tp in server._TEST_PATHS):
            return True
    return False


def _envelope_to_row_item(conn: sqlite3.Connection, env_row: sqlite3.Row, *, subtype: str) -> dict | None:
    """Transform an envelope DB row into a workflow/task list item dict.

    Returns None when the envelope lives under an excluded / test path.
    """
    loc = env_row["location"] or ""
    if _excluded_by_scan_config(loc):
        return None

    meta: dict = {}
    if env_row["dflow_meta"]:
        try:
            meta = json.loads(env_row["dflow_meta"])
        except (json.JSONDecodeError, TypeError):
            meta = {}

    # Envelope title is "func_name @workflow" — strip the tag to get the name.
    title = env_row["title"] or ""
    func_name = title.split(" @", 1)[0] if " @" in title else title

    annotated_id = _annotated_function_id(conn, env_row["id"])
    step_count = _step_count_for_envelope(conn, env_row["id"])
    line_start = _parse_envelope_line_start(env_row["level_3_location"])

    # module_name: derive from annotated function node id (project::module.path::func)
    module_name = ""
    if annotated_id and "::" in annotated_id:
        parts = annotated_id.split("::")
        if len(parts) >= 2:
            module_name = parts[1]

    return {
        "id": env_row["id"],
        "name": func_name,
        "purpose": meta.get("purpose"),
        "inputs": meta.get("inputs"),
        "outputs": meta.get("outputs"),
        "critical": meta.get("critical"),
        "module": loc,
        "module_name": module_name,
        "line_start": line_start,
        "step_count": step_count,
        "role": subtype,
        "cortex_node_id": annotated_id,
    }


def _envelopes_as_items(subtype: str) -> list[dict]:
    """Return all envelopes of *subtype* as frontend-facing item dicts."""
    conn = _connect()
    try:
        rows = _envelopes_by_subtype(conn, subtype)
        items = []
        for r in rows:
            item = _envelope_to_row_item(conn, r, subtype=subtype)
            if item is not None:
                items.append(item)
    finally:
        conn.close()
    return items


def _step_row_to_dict(conn: sqlite3.Connection, step_row: sqlite3.Row) -> dict:
    """Transform a step node row into a frontend-facing step dict."""
    from axiom_graph.viz import server

    meta: dict = {}
    if step_row["dflow_meta"]:
        try:
            meta = json.loads(step_row["dflow_meta"])
        except (json.JSONDecodeError, TypeError):
            meta = {}

    subtype = step_row["subtype"] or meta.get("subtype") or "step"
    is_auto = subtype == "autostep"

    target_id = _delegates_target(conn, step_row["id"])
    calls_function: str | None = None
    cortex_location: str | None = None
    cortex_line_start: int | None = None
    if target_id:
        target = db.get_node(server._DB_PATH, target_id) if server._DB_PATH else None
        if target is not None:
            from axiom_graph.scanners.node_hashing import parse_node_title

            calls_function = parse_node_title(target).last
            cortex_location = target.location
            cortex_line_start = _parse_envelope_line_start(target.level_3_location)

    line = _parse_envelope_line_start(step_row["level_3_location"])

    step_num_raw = meta.get("step_num_raw") or ""
    return {
        "step_number": step_num_raw,
        "name": meta.get("name"),
        "purpose": meta.get("purpose"),
        "inputs": meta.get("inputs"),
        "outputs": meta.get("outputs"),
        "critical": meta.get("critical"),
        "calls_function": calls_function,
        "is_auto": is_auto,
        "cortex_node_id": target_id,
        "cortex_location": cortex_location,
        "cortex_line_start": cortex_line_start,
        "line": line,
    }


@workflows_router.get("/api/workflows")
def get_workflows() -> dict:
    """List all ``@workflow`` envelopes from graph.db."""
    from axiom_graph.viz import server

    if server._DB_PATH is None:
        return {"available": False, "workflows": []}
    items = _envelopes_as_items("workflow")
    return {"available": bool(items), "workflows": items}


@workflows_router.get("/api/tasks")
def get_tasks() -> dict:
    """List all ``@task`` envelopes from graph.db."""
    from axiom_graph.viz import server

    if server._DB_PATH is None:
        return {"available": False, "tasks": []}
    items = _envelopes_as_items("task")
    return {"available": bool(items), "tasks": items}


@workflows_router.get("/api/tests")
@workflow(
    purpose="Aggregate test functions with tier classification and coverage counts",
    inputs="None (reads from axiom-graph DB envelopes + validates edges)",
    outputs="dict with items list (test metadata, tier, validates_count) and counts",
)
def get_tests() -> dict:
    """Unified test endpoint, sourced entirely from graph.db."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT n.id, n.title, n.location, n.level_3_location,
                   n.level_1, n.dflow_meta
            FROM nodes n
            JOIN tags t ON t.node_id = n.id
            WHERE t.tag = 'test'
              AND n.node_type = 'atomic_process'
              AND n.id NOT IN (
                  SELECT node_id FROM tags WHERE tag = 'test:fixture'
              )
            ORDER BY n.location, n.level_3_location
        """).fetchall()

        node_ids = [r["id"] for r in rows]
        validates_map: dict[str, int] = {}
        validates_targets: dict[str, list[str]] = defaultdict(list)
        if node_ids:
            placeholders = ",".join("?" * len(node_ids))
            edge_rows = conn.execute(
                f"""SELECT from_id, to_id FROM edges
                    WHERE edge_type = 'validates'
                      AND from_id IN ({placeholders})""",
                node_ids,
            ).fetchall()
            for er in edge_rows:
                validates_map[er["from_id"]] = validates_map.get(er["from_id"], 0) + 1
                validates_targets[er["from_id"]].append(er["to_id"])

        env_to_target: dict[str, str] = {}
        env_meta: dict[str, dict] = {}
        env_step_counts: dict[str, int] = {}
        env_rows = conn.execute(
            """
            SELECT n.id, n.dflow_meta
            FROM nodes n
            JOIN tags t ON t.node_id = n.id
            WHERE n.node_type = 'composite_process'
              AND n.subtype = 'workflow'
              AND t.tag = 'envelope'
            """
        ).fetchall()
        for er in env_rows:
            env_id = er["id"]
            target = _annotated_function_id(conn, env_id)
            if target is None:
                continue
            env_to_target[target] = env_id
            meta: dict = {}
            if er["dflow_meta"]:
                try:
                    meta = json.loads(er["dflow_meta"])
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            env_meta[env_id] = meta
            env_step_counts[env_id] = _step_count_for_envelope(conn, env_id)
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    conn.close()

    latest_results: dict[str, dict] = {}

    items = []
    for r in rows:
        short_name = r["title"].split("::")[-1].split(".")[-1]
        if not short_name.startswith("test_"):
            continue

        line_start, line_end = _parse_level3_lines(r["level_3_location"])
        loc = r["location"].replace("\\", "/")

        env_id = env_to_target.get(r["id"])
        has_workflow = env_id is not None
        meta = env_meta.get(env_id or "", {})
        step_count = env_step_counts.get(env_id or "", 0) if has_workflow else 0
        validates_count = validates_map.get(r["id"], 0)

        if has_workflow and step_count > 0:
            tier = "T3"
        elif has_workflow:
            tier = "T2"
        else:
            tier = "T1"

        result_info = latest_results.get(r["id"], {})
        result_status = result_info.get("status", None)
        if result_status:
            result_status = result_status.lower()

        items.append(
            {
                "cortex_id": r["id"],
                "name": short_name,
                "module": loc,
                "location": loc,
                "line_start": line_start,
                "line_end": line_end,
                "docstring": r["level_1"],
                "has_workflow": has_workflow,
                "step_count": step_count,
                "validates_count": validates_count,
                "validates": validates_targets.get(r["id"], []),
                "tier": tier,
                "result": result_status,
                "result_timestamp": result_info.get("ran_at"),
                "id": r["id"],
                "cortex_node_id": r["id"],
                "purpose": meta.get("purpose") or r["level_1"],
                "critical": meta.get("critical"),
                "covers_count": validates_count,
            }
        )

    return {"available": True, "tests": items}


@workflows_router.get("/api/t1_tests")
def get_t1_tests() -> dict:
    """Deprecated — kept for backward compat."""
    return {"t1_tests": []}


@workflows_router.get("/api/fixtures")
def get_fixtures() -> dict:
    """Return all axiom-graph nodes tagged ``test:fixture``."""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT n.id, n.title, n.location, n.level_3_location, n.level_1
            FROM nodes n
            JOIN tags t ON t.node_id = n.id
            WHERE t.tag = 'test:fixture'
            ORDER BY n.location, n.level_3_location
        """).fetchall()
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    conn.close()

    fixtures = []
    for r in rows:
        line_start, line_end = _parse_level3_lines(r["level_3_location"])
        fixtures.append(
            {
                "id": r["id"],
                "title": r["title"],
                "location": r["location"],
                "line_start": line_start,
                "line_end": line_end,
                "docstring": r["level_1"],
            }
        )
    return {"fixtures": fixtures}


@workflows_router.get("/api/test-detail/{cortex_id:path}")
def get_test_detail_by_cortex_id(cortex_id: str) -> dict:
    """Return detail for a test by its axiom-graph node ID."""
    from axiom_graph.viz import server

    conn = _connect()
    try:
        row = conn.execute(
            """SELECT n.id, n.title, n.location, n.level_3_location,
                      n.level_1, n.dflow_meta
               FROM nodes n WHERE n.id = ?""",
            (cortex_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Node not found: {cortex_id}")

        edge_rows = conn.execute(
            "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = 'validates'",
            (cortex_id,),
        ).fetchall()
        validates = [er["to_id"] for er in edge_rows]

        env_row = conn.execute(
            """
            SELECT n.id, n.dflow_meta
            FROM nodes n
            JOIN edges e ON e.from_id = n.id
            JOIN tags t ON t.node_id = n.id
            WHERE e.to_id = ?
              AND e.edge_type = 'annotates'
              AND n.node_type = 'composite_process'
              AND n.subtype = 'workflow'
              AND t.tag = 'envelope'
            LIMIT 1
            """,
            (cortex_id,),
        ).fetchone()

        envelope_info: dict = {}
        envelope_id: str | None = None
        steps_rows: list[sqlite3.Row] = []
        if env_row:
            envelope_id = env_row["id"]
            if env_row["dflow_meta"]:
                try:
                    envelope_info = json.loads(env_row["dflow_meta"])
                except (json.JSONDecodeError, TypeError):
                    envelope_info = {}
            steps_rows = _steps_for_envelope(conn, envelope_id)

        fixture_rows = conn.execute("""
            SELECT n.id, n.title, n.location, n.level_3_location, n.level_1
            FROM nodes n
            JOIN tags t ON t.node_id = n.id
            WHERE t.tag = 'test:fixture'
        """).fetchall()

        steps_sorted = _sort_step_rows(list(steps_rows)) if steps_rows else []
        steps = [_step_row_to_dict(conn, sr) for sr in steps_sorted]
    except HTTPException:
        conn.close()
        raise
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    conn.close()

    short_name = row["title"].split("::")[-1].split(".")[-1]
    line_start, line_end = _parse_level3_lines(row["level_3_location"])
    loc = (row["location"] or "").replace("\\", "/")

    has_workflow = envelope_id is not None
    step_count = len(steps)
    if has_workflow and step_count > 0:
        tier = "T3"
    elif has_workflow:
        tier = "T2"
    else:
        tier = "T1"

    result_info: dict = {}

    fixture_by_name: dict[str, dict] = {}
    for fr in fixture_rows:
        fshort = fr["title"].split("::")[-1].split(".")[-1]
        fline_start, fline_end = _parse_level3_lines(fr["level_3_location"])
        fixture_by_name[fshort] = {
            "name": fshort,
            "cortex_node_id": fr["id"],
            "location": fr["location"],
            "line_start": fline_start,
            "line_end": fline_end,
            "docstring": fr["level_1"],
        }

    fixtures = []
    if server._PROJECT_ROOT and loc:
        source_path = server._PROJECT_ROOT / loc
        if source_path.exists():
            try:
                source = source_path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name == short_name:
                            sig_names = [a.arg for a in node.args.args]
                            fixtures = [fixture_by_name[n] for n in sig_names if n in fixture_by_name]
                            break
            except Exception as exc:
                logger.debug("fixture extraction from source failed: %s", exc)

    return {
        "cortex_id": cortex_id,
        "name": short_name,
        "module": loc,
        "location": loc,
        "line_start": line_start,
        "line_end": line_end,
        "docstring": row["level_1"],
        "purpose": envelope_info.get("purpose") or row["level_1"],
        "has_workflow": has_workflow,
        "step_count": step_count,
        "tier": tier,
        "validates_count": len(validates),
        "validates": validates,
        "result": result_info.get("status", "").lower() if result_info.get("status") else None,
        "result_timestamp": result_info.get("ran_at"),
        "fixtures": fixtures,
        "steps": steps,
        "critical": envelope_info.get("critical"),
    }


def _envelope_steps_payload(envelope_id: str) -> dict:
    """Build the ``{func, steps}`` payload for an envelope node id."""
    from axiom_graph.viz import server

    if server._DB_PATH is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    conn = _connect()
    try:
        env_row = conn.execute(
            """SELECT n.id, n.title, n.location, n.level_3_location, n.dflow_meta,
                      n.subtype, n.level_1, n.level_2
               FROM nodes n
               WHERE n.id = ? AND n.node_type = 'composite_process'""",
            (envelope_id,),
        ).fetchone()
        if env_row is None:
            conn.close()
            raise HTTPException(status_code=404, detail=f"Envelope not found: {envelope_id!r}")

        meta: dict = {}
        if env_row["dflow_meta"]:
            try:
                meta = json.loads(env_row["dflow_meta"])
            except (json.JSONDecodeError, TypeError):
                meta = {}

        target_id = _annotated_function_id(conn, env_row["id"])

        step_rows = _steps_for_envelope(conn, env_row["id"])
        step_rows = _sort_step_rows(list(step_rows))
        steps = [_step_row_to_dict(conn, sr) for sr in step_rows]
    except HTTPException:
        conn.close()
        raise
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    conn.close()

    title = env_row["title"] or ""
    func_name = title.split(" @", 1)[0] if " @" in title else title
    line_start = _parse_envelope_line_start(env_row["level_3_location"])

    if target_id and server._DB_PATH:
        tgt = db.get_node(server._DB_PATH, target_id)
        if tgt and tgt.level_3_location:
            tgt_line = _parse_envelope_line_start(tgt.level_3_location)
            if tgt_line:
                line_start = tgt_line

    return {
        "func": {
            "id": env_row["id"],
            "name": func_name,
            "purpose": meta.get("purpose"),
            "inputs": meta.get("inputs"),
            "outputs": meta.get("outputs"),
            "critical": meta.get("critical"),
            "line_start": line_start,
            "module": env_row["location"],
            "cortex_node_id": target_id,
        },
        "steps": steps,
    }


@workflows_router.get("/api/test/{func_id:path}/detail")
def get_test_detail(func_id: str) -> dict:
    """Full detail for a test envelope (graph.db-backed)."""
    from axiom_graph.viz import server

    base = _envelope_steps_payload(func_id)

    target_id = base["func"].get("cortex_node_id")
    module_path = base["func"].get("module")
    func_name = base["func"].get("name")

    fixture_by_name: dict[str, dict] = {}
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT n.id, n.title, n.location, n.level_3_location, n.level_1
               FROM nodes n
               JOIN tags t ON t.node_id = n.id
               WHERE t.tag = 'test:fixture'"""
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        short = r["title"].split("::")[-1].split(".")[-1]
        line_start, line_end = _parse_level3_lines(r["level_3_location"])
        fixture_by_name[short] = {
            "name": short,
            "cortex_node_id": r["id"],
            "location": r["location"],
            "line_start": line_start,
            "line_end": line_end,
            "docstring": r["level_1"],
        }

    fixtures: list[dict] = []
    if server._PROJECT_ROOT and module_path and func_name:
        source_path = server._PROJECT_ROOT / module_path
        if source_path.exists():
            try:
                source = source_path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.name == func_name:
                            sig_names = [a.arg for a in node.args.args]
                            fixtures = [fixture_by_name[n] for n in sig_names if n in fixture_by_name]
                            break
            except Exception as exc:
                logger.debug("fixture extraction failed: %s", exc)

    return {**base, "fixtures": fixtures, "target_id": target_id}


@workflows_router.get("/api/test/{func_id:path}/steps")
def get_test_steps(func_id: str) -> dict:
    """Ordered steps for a test envelope (graph.db-backed)."""
    return _envelope_steps_payload(func_id)


@workflows_router.get("/api/workflow/{func_id:path}/steps")
def get_workflow_steps(func_id: str) -> dict:
    """Ordered steps for a @workflow envelope (graph.db-backed)."""
    return _envelope_steps_payload(func_id)


@workflows_router.get("/api/source")
def get_source(path: str) -> dict:
    """Return raw source content for a file under the project root.

    The path safety check prevents directory traversal outside the project root.
    Relative paths are resolved against the project root; the workflow- and
    test-view source panels pass step ``level_3_location`` paths here.
    """
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")
    try:
        raw = Path(path)
        file_path = (raw if raw.is_absolute() else server._PROJECT_ROOT / raw).resolve()
        root_resolved = server._PROJECT_ROOT.resolve()
        if not str(file_path).startswith(str(root_resolved)):
            raise HTTPException(status_code=403, detail="Path outside project root")
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "lines": content.count("\n") + 1, "path": str(file_path)}
    except HTTPException:
        raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@workflows_router.get("/api/workflow_graph_labels")
def get_workflow_graph_labels() -> dict:
    """Return ``(from_cortex_node_id, to_cortex_node_id, step_number)`` triples."""
    from axiom_graph.viz import server

    if server._DB_PATH is None:
        return {"available": False, "labels": []}

    conn = _connect()
    try:
        env_rows = conn.execute(
            """
            SELECT n.id FROM nodes n
            JOIN tags t ON t.node_id = n.id
            WHERE n.node_type = 'composite_process'
              AND n.subtype = 'workflow'
              AND t.tag = 'envelope'
            """
        ).fetchall()

        labels: list[dict] = []
        for env in env_rows:
            annotated = _annotated_function_id(conn, env["id"])
            if annotated is None:
                continue
            step_rows = _steps_for_envelope(conn, env["id"])
            for sr in step_rows:
                target_id = _delegates_target(conn, sr["id"])
                if target_id is None:
                    continue
                meta: dict = {}
                if sr["dflow_meta"]:
                    try:
                        meta = json.loads(sr["dflow_meta"])
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                step_num = meta.get("step_num_raw") or ""
                labels.append(
                    {
                        "from_cortex_node_id": annotated,
                        "to_cortex_node_id": target_id,
                        "step_number": step_num,
                    }
                )
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    conn.close()

    return {"available": bool(labels), "labels": labels}


@workflows_router.post("/api/tests/refresh")
def refresh_tests() -> dict:
    """Re-run axiom-graph build to discover new/changed test workflows."""
    from axiom_graph.index import builder  # noqa: PLC0415
    from axiom_graph.viz import server

    root = server._PROJECT_ROOT
    errors: list[str] = []
    cortex_summary: dict | None = None

    try:
        cortex_summary = builder.build(root, discovery_only=True)
    except Exception as exc:
        errors.append(f"axiom-graph build error: {exc}")

    return {
        "ok": len(errors) == 0,
        "dflow_ok": True,
        "cortex_summary": cortex_summary,
        "errors": errors,
    }
