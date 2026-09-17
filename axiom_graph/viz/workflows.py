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
    _envelopes_by_subtype,
    _parse_envelope_line_start,
    _parse_level3_lines,
    _sort_step_rows,
    _step_count_for_envelope,
    _step_rows_by_ids,
    _steps_for_envelope,
)
from axiom_graph.workflows.api import (
    WorkflowGraph,
    load_workflow_graph,
    step_delegate_target,
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


def _step_row_to_dict(
    step_row: sqlite3.Row,
    graph: WorkflowGraph,
    *,
    rendered_step_num: str | None = None,
    depth: int | None = None,
    note: str | None = None,
) -> dict:
    """Transform a step node row into a frontend-facing step dict.

    Delegation is resolved through
    :func:`axiom_graph.workflows.api.step_delegate_target`, the project's
    single delegate accessor, so this surface names the same target and
    reports the same inherited intent as the MCP tools and the export
    bundle.

    Args:
        step_row: Row for the step/autostep node.
        graph: An already-loaded workflow graph, shared across every step
            in the response.
        rendered_step_num: Dotted step number from transitive expansion
            (e.g. ``"2.3.1"``).  When ``None`` the authored ``step_num_raw``
            is used, which is the single-envelope behaviour.
        depth: Nesting depth for expanded rows — ``0`` for steps declared on
            the requested envelope itself.  Passing any value (including
            ``0``) adds ``depth`` and ``note`` to the payload; ``None``
            leaves the payload in the unexpanded shape.
        note: Expansion annotation (cycle detected / target not annotated).

    Returns:
        The frontend-facing step dict.  It carries two independent
        file/line pairs, which are not interchangeable:

        - ``location`` / ``line`` — where the step *marker itself* is
          written.  Under transitive expansion this is frequently a
          different module from the envelope that was requested, because
          an AutoStep's delegate target declares its own steps.
        - ``target`` — the nested delegate target: where the callee is
          defined and what intent it declares.  ``None`` for a step that
          delegates to nothing.
    """
    meta: dict = {}
    if step_row["dflow_meta"]:
        try:
            meta = json.loads(step_row["dflow_meta"])
        except (json.JSONDecodeError, TypeError):
            meta = {}

    subtype = step_row["subtype"] or meta.get("subtype") or "step"
    is_auto = subtype == "autostep"

    target = step_delegate_target(step_row["id"], graph)

    purpose = meta.get("purpose")
    inputs = meta.get("inputs")
    outputs = meta.get("outputs")
    critical = meta.get("critical")
    if is_auto and target is not None:
        # An AutoStep marker has nowhere to hold intent; the intent lives on
        # the function it calls.  Authored values still win over inherited.
        purpose = purpose or target.purpose or None
        inputs = inputs or target.inputs or None
        outputs = outputs or target.outputs or None
        critical = critical or target.critical or None

    line = _parse_envelope_line_start(step_row["level_3_location"])

    step_num_raw = meta.get("step_num_raw") or ""
    payload = {
        "step_number": rendered_step_num if rendered_step_num is not None else step_num_raw,
        "name": meta.get("name"),
        "purpose": purpose,
        "inputs": inputs,
        "outputs": outputs,
        "critical": critical,
        "calls_function": target.name if target else None,
        "is_auto": is_auto,
        "cortex_node_id": target.id if target else None,
        "target": (
            {
                "id": target.id,
                "name": target.name,
                "location": target.location,
                "line": target.line,
            }
            if target
            else None
        ),
        "location": step_row["location"],
        "line": line,
    }
    if depth is not None:
        payload["depth"] = depth
        payload["note"] = note
    return payload


def _workflow_graph() -> WorkflowGraph:
    """Load the workflow graph once for the current request.

    Returns:
        The loaded graph, or an empty one when the server has no project
        root configured.
    """
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        return WorkflowGraph()
    return load_workflow_graph(server._PROJECT_ROOT)


def _expanded_step_dicts(conn: sqlite3.Connection, envelope_id: str, graph: WorkflowGraph) -> list[dict]:
    """Frontend-facing step dicts for an envelope's transitive AutoStep tree.

    Delegates the walk to :func:`workflow_expanded_steps`, then hydrates each
    returned node ID into the same dict shape the single-envelope path emits,
    plus ``depth`` and ``note``.  The expander's ordering is authoritative and
    is deliberately not re-sorted — dotted numbers like ``"2.10"`` do not
    survive the numeric sort the flat path uses.
    """
    from axiom_graph.viz import server
    from axiom_graph.workflows.api import workflow_expanded_steps

    if server._PROJECT_ROOT is None:
        return []

    expanded = workflow_expanded_steps(server._PROJECT_ROOT, envelope_id, graph=graph)
    if not expanded:
        return []

    rows_by_id = _step_rows_by_ids(conn, [e.step_node_id for e in expanded])
    out: list[dict] = []
    for entry in expanded:
        row = rows_by_id.get(entry.step_node_id)
        if row is None:
            continue
        out.append(
            _step_row_to_dict(
                row,
                graph,
                rendered_step_num=entry.rendered_step_num,
                depth=max(len(entry.context_chain) - 1, 0),
                note=entry.note,
            )
        )
    return out


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
        graph = _workflow_graph()
        steps = [_step_row_to_dict(sr, graph) for sr in steps_sorted]
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


def _envelope_steps_payload(envelope_id: str, *, expand: bool = False) -> dict:
    """Build the ``{func, steps}`` payload for an envelope node id.

    Args:
        envelope_id: Node ID of the envelope to describe.
        expand: When ``True``, ``steps`` carries the transitive AutoStep tree
            with dotted step numbers, a ``depth`` per row, and AutoStep
            purposes resolved from their delegate targets.  When ``False``
            (the default) only the envelope's own direct step children are
            returned, in the shape callers have always received.
    """
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

        graph = _workflow_graph()
        if expand:
            steps = _expanded_step_dicts(conn, env_row["id"], graph)
        else:
            step_rows = _steps_for_envelope(conn, env_row["id"])
            step_rows = _sort_step_rows(list(step_rows))
            steps = [_step_row_to_dict(sr, graph) for sr in step_rows]
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
def get_workflow_steps(func_id: str, expand: bool = False) -> dict:
    """Ordered steps for a @workflow envelope (graph.db-backed).

    Set ``?expand=true`` to receive the transitive AutoStep tree instead of
    only the envelope's own step markers.
    """
    return _envelope_steps_payload(func_id, expand=expand)


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


_LIGHT_PALETTE = (
    "--bg:#ffffff;--bg-alt:#f6f8fa;--bg-sunk:#eef1f4;--fg:#1f2328;--fg-dim:#656d76;"
    "--border:#d0d7de;--accent:#0969da;--warn:#9a6700;--warn-bg:#fff8c5;"
    "--hit:#fff3c4;--match:#dbeafe;--match-strong:#a8cbf5;"
    "--ln:#8c959f;--chip:#eaeef2;--shadow:rgba(31,35,40,.12)"
)
_DARK_PALETTE = (
    "--bg:#0d1117;--bg-alt:#161b22;--bg-sunk:#010409;--fg:#e6edf3;--fg-dim:#8b949e;"
    "--border:#30363d;--accent:#4493f8;--warn:#d29922;--warn-bg:#282215;"
    "--hit:#3f2d00;--match:#16324f;--match-strong:#26507d;"
    "--ln:#6e7681;--chip:#21262d;--shadow:rgba(1,4,9,.6)"
)


def _export_theme_css() -> str:
    """Return the two palettes and the rules that choose between them.

    The light palette is the unconditional default so every colour has a
    value before any media query or toggle is consulted.  The dark palette
    is applied when the reader's browser asks for it, and either palette
    can be forced by the toggle, which stamps ``data-theme`` on the root.

    Returns:
        A CSS fragment defining both palettes.
    """
    return (
        f":root{{{_LIGHT_PALETTE}}}"
        f'@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{{_DARK_PALETTE}}}}}'
        f':root[data-theme="dark"]{{{_DARK_PALETTE}}}'
        f':root[data-theme="light"]{{{_LIGHT_PALETTE}}}'
    )


def _dark_token_style() -> str:
    """Return the best available Pygments style for the dark palette."""
    from pygments.styles import get_all_styles

    available = set(get_all_styles())
    for name in ("github-dark", "monokai", "native"):
        if name in available:
            return name
    return "default"


def _token_css() -> str:
    """Return Pygments token rules for both themes, or ``""`` without Pygments.

    One set of token classes is emitted into the markup; this supplies two
    palettes for them, so switching theme recolours the code without
    re-rendering it.

    Returns:
        A CSS fragment, empty when Pygments is unavailable.
    """
    try:
        from pygments.formatters import HtmlFormatter
    except ImportError:
        return ""

    def defs(style: str, selector: str) -> str:
        return HtmlFormatter(style=style, classprefix="tok-").get_style_defs(selector)

    dark_style = _dark_token_style()
    return (
        defs("default", ".code")
        + "@media (prefers-color-scheme: dark){"
        + defs(dark_style, ':root:not([data-theme="light"]) .code')
        + "}"
        + defs(dark_style, ':root[data-theme="dark"] .code')
        + defs("default", ':root[data-theme="light"] .code')
    )


def _highlight_lines(path: str, text: str) -> list[str]:
    """Return one HTML fragment per line of ``text``, syntax-highlighted.

    Pygments splits multi-line tokens at line boundaries, so each returned
    fragment is self-contained and safe to wrap in its own element.  When
    Pygments is unavailable — or holds no lexer for the file — the lines
    come back escaped but uncoloured rather than failing the export.

    Args:
        path: Path the text came from, used only to choose a lexer.
        text: The file's full contents.

    Returns:
        One fragment per line, in file order.
    """
    from html import escape

    plain = [escape(line) for line in text.splitlines()]
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return plain

    try:
        lexer = get_lexer_for_filename(path, stripnl=False)
    except ClassNotFound:
        return plain

    rendered = highlight(text, lexer, HtmlFormatter(nowrap=True, classprefix="tok-"))
    lines = rendered.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    # A lexer that re-wraps would desynchronise the gutter; fall back if so.
    return lines if len(lines) == len(plain) else plain


def _render_source(path: str, text: str, slug: str) -> str:
    """Render one source file as a numbered, highlighted block.

    Each line is its own block element carrying an ``id``, which gives the
    outline something to scroll to and lets the line number come from a CSS
    counter in an absolutely-positioned gutter.  Nothing about the number
    depends on the line's width, so a long line cannot push it out of
    alignment.

    Args:
        path: Project-relative path, shown as the block's title.
        text: The file's full contents.
        slug: Short DOM id stem for this file (e.g. ``"src-0"``).

    Returns:
        The block's HTML.
    """

    rows = "".join(
        f'<span class="cl" id="{slug}-{number}">{line}</span>'
        for number, line in enumerate(_highlight_lines(path, text), start=1)
    )
    return f'<div class="src" id="{slug}" hidden><pre>{rows}</pre></div>'


_MARKER_CALLS = frozenset({"Step", "AutoStep"})


def _called_name(func: ast.expr) -> str:
    """Return the bare name a call expression names, or ``""``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _block_spans(path: str, text: str) -> dict[int, int]:
    """Map each highlightable block's first line to its last.

    Following a link should light up the whole thing it names rather than
    one line of it.  Two kinds of block earn an entry, each keyed by the
    line the export already links to, so nothing else has to change to
    find them:

    - The decorator stack above an annotated function, keyed by the
      decorator's own first line — which is where the index's stored span
      starts, and so where a target link already points.  The signature
      below it is deliberately excluded: the intent is the decorator.
    - Every ``Step`` / ``AutoStep`` marker call, keyed by the line the
      call opens on, which is the line a step's own link points at.  A
      marker written across six lines then lights up as one block.

    Only Python is read.  A file in another language, or one that will
    not parse, contributes nothing and every link into it keeps the
    single-line behaviour.

    Args:
        path: Project-relative path, used only to skip non-Python files.
        text: The file's full contents.

    Returns:
        ``{first_line: last_line}``, both 1-based and inclusive.
    """
    if not path.endswith(".py"):
        return {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}

    spans: dict[int, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
            opener, closer = node.decorator_list[0], node.decorator_list[-1]
            spans[opener.lineno] = closer.end_lineno or closer.lineno
        elif isinstance(node, ast.Call) and _called_name(node.func) in _MARKER_CALLS:
            spans[node.lineno] = node.end_lineno or node.lineno
    return spans


def _render_step(step: dict, has_children: bool = False) -> str:
    """Render one outline row.

    The row's indent comes from ``depth``, which the bundle reads off the
    dotted step number, so ``6.1.1`` sits under ``6.1`` regardless of which
    file either marker was written in.  The list itself carries no marker —
    the step number is printed, and a second machine-generated sequence
    beside it would contradict it.

    Two independent things fold here.  The arrow beside the number folds
    the steps *under* this one; the ``...`` folds the extra intent this
    step itself declares.  They are separate because a reader often wants
    the whole tree open and every step terse, or one step opened out while
    its children stay away.

    A step that any minor step sits under gets an arrow; one that nothing
    nests under gets a spacer of the same width, so the numbers stay in a
    single column either way.  The ``...`` appears only on a step that has
    something beyond its purpose to show.

    Args:
        step: One serialized step from the bundle.
        has_children: Whether a later step nests underneath this one.

    Returns:
        The row's ``<li>`` HTML.
    """
    from html import escape

    step_num = escape(str(step.get("step_num") or ""))
    location = escape(str(step.get("location") or ""))
    control = (
        '<button class="step-disclose" title="Fold the steps under this one">&#9662;</button>'
        if has_children
        else '<span class="step-disclose-gap"></span>'
    )
    parts = [
        f'<li class="step" data-step-num="{step_num}" style="--depth:{int(step.get("depth") or 0)}">',
        f'<span class="step-lead">{control}<span class="step-num">{step_num}</span></span>',
        '<div class="step-body"><div class="step-head">',
        f'<span class="lnk" data-path="{location}" data-line="{int(step.get("line") or 1)}">'
        f"{escape(str(step.get('name') or ''))}</span>",
    ]
    if step.get("is_auto"):
        parts.append('<span class="badge">auto</span>')

    target = step.get("target") or {}
    if target.get("location"):
        target_path = escape(str(target["location"]))
        parts.append(
            f'<span class="arrow">&rarr;</span><span class="lnk" data-path="{target_path}" '
            f'data-line="{int(target.get("line") or 1)}">{escape(str(target.get("name") or ""))}</span>'
        )
        if target["location"] != step.get("location"):
            parts.append(f'<span class="origin">{target_path}</span>')
    elif step.get("is_auto"):
        # An AutoStep declares that the call after it *is* the step, so one
        # that bound nothing is worth surfacing however rare: either the
        # marker sits away from the call it names, or the work is inline and
        # wants a plain Step.  The reader gets a flag, not a sentence.
        parts.append(
            '<span class="unbound" data-tip="No delegate target resolved &#8212; the marker may sit '
            'away from the call it names, or the step may be inline work that wants a plain Step">'
            "&#9888;</span>"
        )

    detail: list[str] = []
    chips = "".join(
        f'<span class="chip"><b>{label}</b>{escape(str(step[key]))}</span>'
        for key, label in (("inputs", "in"), ("outputs", "out"))
        if step.get(key)
    )
    if chips:
        detail.append(f'<div class="chips">{chips}</div>')
    if step.get("critical"):
        detail.append(f'<div class="critical">{escape(str(step["critical"]))}</div>')
    if step.get("note"):
        detail.append(f'<div class="note">{escape(str(step["note"]))}</div>')

    if detail:
        parts.append('<button class="step-more" title="Show everything this step declares">&hellip;</button>')
    parts.append("</div>")

    if step.get("purpose"):
        parts.append(f'<div class="step-purpose">{escape(str(step["purpose"]))}</div>')
    if detail:
        parts.append(f'<div class="step-detail">{"".join(detail)}</div>')

    parts.append("</div></li>")
    return "".join(parts)


def _render_workflow(index: int, wf: dict) -> str:
    """Render one workflow as a collapsible outline section.

    Args:
        index: Position in the export, used for the section's DOM id.
        wf: One serialized workflow from the bundle.

    Returns:
        The section's HTML.
    """
    from html import escape

    steps = wf.get("steps") or []
    name = escape(str(wf.get("name") or ""))
    wf_file = escape(str(wf.get("file") or ""))
    head = [
        f'<section class="wf" id="wf-{index}">',
        f'<div class="wf-head"><button class="disclose" data-wf="wf-{index}">&#9662;</button>',
        f"<h2>{name}</h2>",
        f'<span class="role">{escape(str(wf.get("role") or "workflow"))}</span>',
        f'<span class="count">{len(steps)} steps</span></div>',
        # Outside wf-body, so a collapsed workflow still says where it lives.
        f'<div class="wf-file"><span class="lnk" data-path="{wf_file}" '
        f'data-line="{int(wf.get("line") or 1)}">{wf_file}</span></div>',
        '<div class="wf-body">',
    ]
    if wf.get("purpose"):
        head.append(f'<p class="wf-purpose">{escape(str(wf["purpose"]))}</p>')

    for key, label in (("inputs", "in"), ("outputs", "out")):
        if wf.get(key):
            head.append(f'<div class="chips"><span class="chip"><b>{label}</b>{escape(str(wf[key]))}</span></div>')
    if wf.get("critical"):
        head.append(f'<div class="critical">{escape(str(wf["critical"]))}</div>')

    if steps:
        depths = [int(s.get("depth") or 0) for s in steps]
        rows = "".join(
            _render_step(
                step,
                has_children=position + 1 < len(steps) and depths[position + 1] > depths[position],
            )
            for position, step in enumerate(steps)
        )
        head.append(f'<ul class="steps">{rows}</ul>')
    else:
        head.append('<p class="empty">No steps recorded.</p>')
    head.append("</div></section>")
    return "".join(head)


def _render_toc(workflows: list) -> str:
    """Render the jump list shown above a multi-workflow export.

    A single-workflow export has nothing to jump between, so it gets no
    list at all.

    Args:
        workflows: The serialized workflows, in export order.

    Returns:
        The ``<nav>`` HTML, or ``""`` for a selection of one.
    """
    from html import escape

    if len(workflows) < 2:
        return ""
    items = "".join(
        f'<button class="toc-item" data-target="wf-{index}">'
        f'<span class="toc-name">{escape(str(wf.get("name") or ""))}</span>'
        f'<span class="role">{escape(str(wf.get("role") or "workflow"))}</span>'
        f'<span class="count">{len(wf.get("steps") or [])}</span></button>'
        for index, wf in enumerate(workflows)
    )
    return (
        '<nav class="toc"><div class="toc-head">'
        '<button class="toc-disclose" title="Collapse the contents list">&#9662;</button>'
        f'<span>Contents</span><span class="count">{len(workflows)}</span></div>'
        f'<div class="toc-items">{items}</div></nav>'
    )


_EXPORT_LAYOUT_CSS = """
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.app{display:flex;flex-direction:column;height:100%}
header{display:flex;align-items:center;gap:12px;padding:10px 14px;flex:0 0 auto;
  background:var(--bg-alt);border-bottom:1px solid var(--border)}
.brand{font-weight:600}
.meta{color:var(--fg-dim);font-size:12px}
header .spacer{flex:1}
#filter{width:230px;padding:5px 9px;border:1px solid var(--border);border-radius:6px;
  background:var(--bg);color:var(--fg);font:inherit;font-size:13px}
#theme-toggle{border:1px solid var(--border);background:var(--bg);color:var(--fg);
  border-radius:6px;padding:5px 9px;cursor:pointer;font-size:14px;line-height:1}
.ctl{display:flex;align-items:center;gap:4px}
.ctl-label{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--fg-dim)}
.ctl button{border:1px solid var(--border);background:var(--bg);color:var(--fg-dim);
  border-radius:6px;padding:4px 8px;cursor:pointer;font:inherit;font-size:12px;line-height:1.1}
.ctl button:hover,#wrap-toggle:hover{color:var(--fg);border-color:var(--accent)}
#wrap-toggle{flex:0 0 auto;border:1px solid var(--border);background:var(--bg);
  color:var(--fg-dim);border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit;font-size:12px}
#wrap-toggle.on{background:var(--chip);color:var(--fg);border-color:var(--accent)}
.seg{gap:0}
.seg button{border-radius:0;margin-left:-1px}
.seg button:first-of-type{border-radius:6px 0 0 6px;margin-left:4px}
.seg button:last-of-type{border-radius:0 6px 6px 0}
.seg button.on{background:var(--chip);color:var(--fg);border-color:var(--accent);z-index:1}
main{display:flex;flex:1;min-height:0}
.pane{overflow:auto;min-width:0}
.outline-col{flex:0 0 44%;min-width:0;display:flex;flex-direction:column}
.outline{flex:1;padding:14px 16px}
.code{flex:1;background:var(--bg-sunk);display:flex;flex-direction:column}
.splitter{flex:0 0 5px;cursor:col-resize;background:var(--border)}
.splitter:hover{background:var(--accent)}

.toc{flex:0 0 auto;max-height:38%;overflow:auto;background:var(--bg-alt);
  border-bottom:1px solid var(--border);padding:8px 10px}
.toc-head{display:flex;align-items:center;gap:6px;font-size:11px;text-transform:uppercase;
  letter-spacing:.06em;color:var(--fg-dim);padding:2px 4px 6px}
.toc-head span:first-of-type{flex:1}
.toc.collapsed{max-height:none;overflow:visible}
.toc.collapsed .toc-items{display:none}
.toc.collapsed .toc-disclose{transform:rotate(-90deg)}
.toc-disclose,.step-disclose{border:0;background:none;color:var(--fg-dim);cursor:pointer;
  font-size:11px;line-height:1;padding:0;transition:transform .15s}
.toc-items{margin:2px 0 0 5px;padding-left:12px;border-left:2px solid var(--border)}
.toc-item{display:flex;align-items:center;gap:8px;width:100%;text-align:left;
  border:0;background:none;color:var(--fg);font:inherit;padding:6px;
  cursor:pointer}
.toc-item + .toc-item{border-top:1px solid var(--border)}
.toc-item:hover{background:var(--chip)}
.toc-item.active{background:var(--chip);box-shadow:inset 3px 0 0 var(--accent)}
.toc-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* Each workflow is its own block: a rule above it separates it from the one
   before, and everything below the heading sits behind an indent rail so it
   reads as belonging to that heading rather than running on from it. */
.wf + .wf{margin-top:30px;padding-top:24px;border-top:2px solid var(--border)}
.wf:last-child{margin-bottom:40px}
.wf-body{margin-top:8px;padding-left:15px;border-left:2px solid var(--border)}
.wf.collapsed .wf-body{display:none}
.wf-head{display:flex;align-items:baseline;gap:8px}
.disclose{border:0;background:none;color:var(--fg-dim);cursor:pointer;font-size:12px;padding:0}
.wf.collapsed .disclose{transform:rotate(-90deg)}
h2{font-size:17px;margin:0}
.role,.count{font-size:11px;color:var(--fg-dim);border:1px solid var(--border);
  border-radius:10px;padding:0 7px}
.wf-file,.origin{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.wf-file{margin:4px 0 0;padding-left:17px}
.wf-purpose{margin:0 0 10px;color:var(--fg-dim)}
.empty{color:var(--fg-dim);font-style:italic}

.steps{list-style:none;margin:0;padding:0}
.step{display:flex;gap:12px;padding:5px 0;
  margin-left:calc(var(--depth,0) * 20px);border-top:1px solid var(--border)}
.step.folded{display:none}
.step.collapsed .step-disclose{transform:rotate(-90deg)}
.step-lead{flex:0 0 auto;display:flex;align-items:baseline;gap:5px}
.step-disclose{flex:0 0 15px;font-size:15px;color:var(--fg-dim)}
.step-disclose:hover{color:var(--accent)}
.step-disclose-gap{flex:0 0 15px}
.step-num{flex:0 0 auto;min-width:4.6em;padding-right:6px;white-space:nowrap;color:var(--fg-dim);
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.step-more{border:0;background:none;color:var(--fg-dim);cursor:pointer;
  font-size:15px;line-height:.6;padding:0 4px;border-radius:4px}
.step-more:hover{background:var(--chip);color:var(--accent)}
.step.detail-open .step-more{background:var(--chip);color:var(--accent)}

/* Density. The per-step override wins by being excluded from the rule
   that hides, so neither side needs !important. */
:root[data-detail="compact"] .step:not(.detail-open) .step-purpose,
:root[data-detail="compact"] .step:not(.detail-open) .step-detail,
:root[data-detail="purpose"] .step:not(.detail-open) .step-detail{display:none}
/* Own tooltip rather than the native title: that one waits over a second
   and several embedded viewers drop it entirely. */
.unbound{position:relative;color:var(--warn);cursor:help;font-size:14px;line-height:1}
.unbound::after{content:attr(data-tip);position:absolute;left:0;top:calc(100% + 7px);
  width:250px;white-space:normal;background:var(--bg-alt);color:var(--fg);
  border:1px solid var(--warn);border-radius:6px;padding:7px 9px;font-size:11px;
  line-height:1.45;box-shadow:0 4px 14px var(--shadow);z-index:30;
  opacity:0;visibility:hidden;transition:opacity .12s;pointer-events:none}
.unbound:hover::after,.unbound:focus::after{opacity:1;visibility:visible}
.step-body{min-width:0;flex:1}
.step-head{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.arrow{color:var(--fg-dim)}
.origin{color:var(--fg-dim)}
.badge{font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  border:1px solid var(--border);border-radius:9px;padding:0 6px;color:var(--fg-dim)}
.step-purpose{color:var(--fg-dim);margin-top:2px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.chip{background:var(--chip);border-radius:5px;padding:1px 7px;font-size:12px}
.chip b{color:var(--fg-dim);font-weight:600;margin-right:5px;text-transform:uppercase;font-size:10px}
.critical{margin-top:4px;padding:4px 9px;border-left:3px solid var(--warn);
  background:var(--warn-bg);color:var(--warn);border-radius:0 5px 5px 0;font-size:13px}
.note{margin-top:3px;color:var(--fg-dim);font-size:12px;font-style:italic}
.lnk{color:var(--accent);cursor:pointer;text-decoration:underline;text-underline-offset:2px}
.step.dim{display:none}

.code-bar{flex:0 0 auto;display:flex;gap:8px;align-items:center;padding:8px 12px;
  background:var(--bg-alt);border-bottom:1px solid var(--border)}
#file-picker{flex:1 1 110px;min-width:0;padding:4px 8px;border:1px solid var(--border);
  border-radius:6px;background:var(--bg);color:var(--fg);
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.code-body{flex:1;overflow:auto}
#code-search{flex:0 1 165px;min-width:80px;padding:4px 8px;border:1px solid var(--border);
  border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;font-size:12px}
#code-search-count{font-size:11px;color:var(--fg-dim);white-space:nowrap}
#code-prev,#code-next{flex:0 0 auto;border:1px solid var(--border);background:var(--bg);
  color:var(--fg-dim);border-radius:6px;padding:4px 7px;cursor:pointer;font-size:9px;line-height:1.2}
#code-prev:hover,#code-next:hover{color:var(--fg);border-color:var(--accent)}
/* Blue for search, yellow for the block a link jumped to, so both read at once. */
.cl.match{background:var(--match)}
.cl.match-current{background:var(--match-strong);box-shadow:inset 3px 0 0 var(--accent)}
.src pre{margin:0;padding:10px 0;counter-reset:ln;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;
  line-height:1.5;white-space:pre;overflow-x:auto}
/* Wrapping works only because the gutter is an absolutely-positioned counter
   in the line's own padding well: continuations land at the padding edge and
   the number stays pinned to the block's first line. */
:root[data-wrap="on"] .src pre{white-space:pre-wrap;overflow-wrap:break-word;overflow-x:visible}
.cl{counter-increment:ln;display:block;min-height:1.5em;position:relative;padding:0 12px 0 5.2em}
.cl::before{content:counter(ln);position:absolute;left:0;width:4.2em;text-align:right;
  color:var(--ln);user-select:none}
.cl.hit{background:var(--hit)}
.placeholder{padding:16px;color:var(--fg-dim)}

@media (max-width:900px){
  main{flex-direction:column}
  .outline-col{flex:0 0 auto;max-height:55%}
  .splitter{display:none}
}
"""

_EXPORT_SCRIPT = """
(function(){
var root=document.documentElement;
var slugs=JSON.parse(document.getElementById('path-slugs').textContent);
var spans=JSON.parse(document.getElementById('line-spans').textContent);
var picker=document.getElementById('file-picker');

try{var saved=localStorage.getItem('axiom-export-theme');if(saved)root.dataset.theme=saved;}catch(e){}
function effectiveTheme(){
  if(root.dataset.theme)return root.dataset.theme;
  return window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
}
document.getElementById('theme-toggle').addEventListener('click',function(){
  var next=effectiveTheme()==='dark'?'light':'dark';
  root.dataset.theme=next;
  try{localStorage.setItem('axiom-export-theme',next);}catch(e){}
});

function show(slug,line,path){
  if(!slug)return;
  var blocks=document.querySelectorAll('.src');
  for(var i=0;i<blocks.length;i++)blocks[i].hidden=blocks[i].id!==slug;
  if(picker)picker.value=slug;
  var lit=document.querySelectorAll('.cl.hit');
  for(var j=0;j<lit.length;j++)lit[j].classList.remove('hit');
  if(!line)return;
  // A decorator stack or a marker call runs over several lines; light the
  // whole block, falling back to the one line when the file yielded none.
  var last=((spans[path]||{})[line])||line;
  for(var n=+line;n<=+last;n++){
    var row=document.getElementById(slug+'-'+n);
    if(row)row.classList.add('hit');
  }
  var first=document.getElementById(slug+'-'+line);
  if(first)first.scrollIntoView({block:'center'});
}

document.addEventListener('click',function(e){
  var link=e.target.closest('[data-path]');
  if(link){e.preventDefault();show(slugs[link.dataset.path],link.dataset.line,link.dataset.path);return;}
  var jump=e.target.closest('[data-target]');
  if(jump){var sec=document.getElementById(jump.dataset.target);
    if(sec)sec.scrollIntoView({behavior:'smooth',block:'start'});return;}
  var more=e.target.closest('.step-more');
  if(more){more.closest('.step').classList.toggle('detail-open');return;}
  var fold=e.target.closest('.step-disclose');
  if(fold){var li=fold.closest('.step');
    li.classList.toggle('collapsed');refold(li.closest('.steps'));return;}
  var contents=e.target.closest('.toc-disclose');
  if(contents){contents.closest('.toc').classList.toggle('collapsed');return;}
  var toggle=e.target.closest('.disclose');
  if(toggle){document.getElementById(toggle.dataset.wf).classList.toggle('collapsed');}
});

function depthOf(step){return parseInt(step.style.getPropertyValue('--depth')||'0',10);}

// Recompute every row's visibility from the collapsed flags, so a parent
// reopening does not reveal rows under a child that is still collapsed.
function refold(list){
  if(!list)return;
  var steps=list.querySelectorAll('.step'),hideBelow=null;
  for(var i=0;i<steps.length;i++){
    var d=depthOf(steps[i]);
    if(hideBelow!==null&&d>hideBelow){steps[i].classList.add('folded');continue;}
    hideBelow=null;
    steps[i].classList.remove('folded');
    if(steps[i].classList.contains('collapsed'))hideBelow=d;
  }
}

if(picker)picker.addEventListener('change',function(){show(picker.value,0);});

// Cross-file code search.  Every carried line is already in the DOM as its
// own addressable block, so searching all files costs no more than searching
// the visible one -- which matters because the other files are hidden, and
// hidden elements are invisible to the browser's own find.
var searchBox=document.getElementById('code-search');
var countEl=document.getElementById('code-search-count');
var searchIndex=null,matches=[],cursor=-1,matchFiles=0,searchTimer=null;

function buildSearchIndex(){
  if(searchIndex)return searchIndex;
  searchIndex=[];
  var blocks=document.querySelectorAll('.src');
  for(var i=0;i<blocks.length;i++){
    var rows=blocks[i].querySelectorAll('.cl');
    for(var j=0;j<rows.length;j++)
      searchIndex.push({el:rows[j],slug:blocks[i].id,text:rows[j].textContent.toLowerCase()});
  }
  return searchIndex;
}

function clearMatches(){
  for(var i=0;i<matches.length;i++)matches[i].el.classList.remove('match','match-current');
  matches=[];cursor=-1;matchFiles=0;
}

function renderCount(){
  if(!matches.length){countEl.textContent=searchBox.value.trim()?'no matches':'';return;}
  countEl.textContent=(cursor+1)+'/'+matches.length+' in '+matchFiles+
    ' file'+(matchFiles===1?'':'s');
}

function runSearch(){
  var q=searchBox.value.trim().toLowerCase();
  clearMatches();
  if(!q){renderCount();return;}
  var rows=buildSearchIndex(),seen={};
  for(var i=0;i<rows.length;i++){
    if(rows[i].text.indexOf(q)<0)continue;
    rows[i].el.classList.add('match');
    matches.push(rows[i]);
    if(!seen[rows[i].slug]){seen[rows[i].slug]=1;matchFiles++;}
  }
  if(matches.length)stepMatch(1); else renderCount();
}

function stepMatch(dir){
  if(!matches.length)return;
  if(cursor>=0)matches[cursor].el.classList.remove('match-current');
  cursor=(cursor+dir+matches.length)%matches.length;
  var m=matches[cursor];
  m.el.classList.add('match-current');
  var blocks=document.querySelectorAll('.src');
  for(var i=0;i<blocks.length;i++)blocks[i].hidden=blocks[i].id!==m.slug;
  if(picker)picker.value=m.slug;
  m.el.scrollIntoView({block:'center'});
  renderCount();
}

if(searchBox){
  searchBox.addEventListener('input',function(){
    clearTimeout(searchTimer);searchTimer=setTimeout(runSearch,120);
  });
  searchBox.addEventListener('keydown',function(e){
    if(e.key==='Enter'){e.preventDefault();stepMatch(e.shiftKey?-1:1);}
    else if(e.key==='Escape'){searchBox.value='';runSearch();}
  });
  var prev=document.getElementById('code-prev'),next=document.getElementById('code-next');
  if(prev)prev.addEventListener('click',function(){stepMatch(-1);});
  if(next)next.addEventListener('click',function(){stepMatch(1);});
}

var wrap=document.getElementById('wrap-toggle');
try{if(localStorage.getItem('axiom-export-wrap')==='off')root.dataset.wrap='off';}catch(e){}
if(wrap){
  wrap.classList.toggle('on',root.dataset.wrap!=='off');
  wrap.addEventListener('click',function(){
    var next=root.dataset.wrap==='off'?'on':'off';
    root.dataset.wrap=next;
    wrap.classList.toggle('on',next==='on');
    try{localStorage.setItem('axiom-export-wrap',next);}catch(e){}
  });
}

var seg=document.getElementById('detail-seg');
if(seg)seg.addEventListener('click',function(e){
  var b=e.target.closest('button[data-detail]');
  if(!b)return;
  root.dataset.detail=b.dataset.detail;
  var all=seg.querySelectorAll('button');
  for(var i=0;i<all.length;i++)all[i].classList.toggle('on',all[i]===b);
});

function eachList(fn){
  var lists=document.querySelectorAll('.steps');
  for(var i=0;i<lists.length;i++)fn(lists[i]);
}
var foldAll=document.getElementById('fold-all');
if(foldAll)foldAll.addEventListener('click',function(){
  eachList(function(list){
    var steps=list.querySelectorAll('.step');
    for(var i=0;i<steps.length;i++)
      if(steps[i].querySelector('.step-disclose'))steps[i].classList.add('collapsed');
    refold(list);
  });
});
var unfoldAll=document.getElementById('unfold-all');
if(unfoldAll)unfoldAll.addEventListener('click',function(){
  eachList(function(list){
    var steps=list.querySelectorAll('.step');
    for(var i=0;i<steps.length;i++)steps[i].classList.remove('collapsed','folded');
  });
});

var filter=document.getElementById('filter');
if(filter)filter.addEventListener('input',function(){
  var q=filter.value.trim().toLowerCase();
  // A filter that leaves matches hidden inside a collapsed parent is a
  // filter that appears to have found nothing; searching unfolds the tree.
  if(q){
    var folded=document.querySelectorAll('.step.collapsed,.step.folded');
    for(var f=0;f<folded.length;f++)folded[f].classList.remove('collapsed','folded');
  }
  var sections=document.querySelectorAll('.wf');
  for(var i=0;i<sections.length;i++){
    var steps=sections[i].querySelectorAll('.step'),shown=0;
    for(var j=0;j<steps.length;j++){
      var hit=!q||steps[j].textContent.toLowerCase().indexOf(q)>=0;
      steps[j].classList.toggle('dim',!hit);
      if(hit)shown++;
    }
    var name=sections[i].querySelector('h2').textContent.toLowerCase();
    var keep=!q||shown>0||name.indexOf(q)>=0;
    sections[i].hidden=!keep;
    var entry=document.querySelector('.toc-item[data-target="'+sections[i].id+'"]');
    if(entry)entry.hidden=!keep;
  }
});

var entries=document.querySelectorAll('.toc-item');
if(entries.length&&window.IntersectionObserver){
  var spy=new IntersectionObserver(function(rows){
    for(var i=0;i<rows.length;i++){
      if(!rows[i].isIntersecting)continue;
      for(var j=0;j<entries.length;j++)
        entries[j].classList.toggle('active',entries[j].dataset.target===rows[i].target.id);
    }
  },{rootMargin:'-10% 0px -80% 0px'});
  var all=document.querySelectorAll('.wf');
  for(var k=0;k<all.length;k++)spy.observe(all[k]);
}

var bar=document.getElementById('splitter'),outline=document.querySelector('.outline-col');
if(bar)bar.addEventListener('mousedown',function(down){
  down.preventDefault();
  function drag(move){
    var box=outline.parentElement.getBoundingClientRect();
    var pct=(move.clientX-box.left)/box.width*100;
    outline.style.flexBasis=Math.min(80,Math.max(20,pct))+'%';
  }
  function stop(){document.removeEventListener('mousemove',drag);
    document.removeEventListener('mouseup',stop);}
  document.addEventListener('mousemove',drag);
  document.addEventListener('mouseup',stop);
});

var first=document.querySelector('.src');
if(first)first.hidden=false;
if(picker&&first)picker.value=first.id;
})();
"""


def _render_export_html(bundle: dict) -> str:
    """Render an export bundle as one self-contained HTML document.

    Everything the page needs is inlined: the outline, every source file as
    a numbered and syntax-highlighted block, two colour palettes, and the
    script that moves between them.  The document references no external
    ``src`` or ``href``, so it opens with no network access — which is what
    makes it something you can send to somebody.

    The embedded JSON keeps the workflow structure but drops ``sources``:
    the rendered blocks already carry every line, so a second copy would
    roughly double the file for no reader and no script.

    Args:
        bundle: The serialized bundle — ``{"workflows": [...],
            "sources": {path: text}}``.

    Returns:
        A complete HTML document as a string.
    """
    from html import escape

    workflows = bundle.get("workflows") or []
    sources = bundle.get("sources") or {}
    path_slug = {path: f"src-{index}" for index, path in enumerate(sorted(sources))}

    # The contents list sits beside the scrolling outline, not inside it, so
    # it neither scrolls away nor covers the section it is pointing at.
    toc = _render_toc(workflows)
    outline = "".join(_render_workflow(index, wf) for index, wf in enumerate(workflows))
    blocks = (
        "".join(_render_source(path, text, path_slug[path]) for path, text in sorted(sources.items()))
        or '<div class="placeholder">No source carried.</div>'
    )
    options = "".join(f'<option value="{path_slug[path]}">{escape(path)}</option>' for path in sorted(sources))

    spans = {path: found for path, text in sources.items() if (found := _block_spans(path, text))}

    structure = json.dumps({"workflows": workflows}).replace("</", "<\\/")
    slug_json = json.dumps(path_slug).replace("</", "<\\/")
    span_json = json.dumps(spans).replace("</", "<\\/")
    plural = "s" if len(workflows) != 1 else ""
    label = f"{len(workflows)} workflow{plural} &middot; {len(sources)} files"
    style = _export_theme_css() + _EXPORT_LAYOUT_CSS + _token_css()

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en" data-detail="purpose" data-wrap="on"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Workflow export</title>"
        f"<style>{style}</style></head><body>"
        '<div class="app">'
        '<header><span class="brand">Workflow export</span>'
        f'<span class="meta">{label}</span><span class="spacer"></span>'
        '<span class="ctl"><span class="ctl-label">Steps</span>'
        '<button id="fold-all" title="Fold every step">&#8863;</button>'
        '<button id="unfold-all" title="Unfold every step">&#8862;</button></span>'
        '<span class="ctl seg" id="detail-seg"><span class="ctl-label">Detail</span>'
        '<button data-detail="compact">Compact</button>'
        '<button data-detail="purpose" class="on">Purpose</button>'
        '<button data-detail="all">All</button></span>'
        '<input id="filter" type="text" placeholder="Filter steps..." spellcheck="false">'
        '<button id="theme-toggle" title="Switch between dark and light">&#9681;</button></header>'
        f'<main><div class="outline-col">{toc}<div class="pane outline">{outline}</div></div>'
        '<div class="splitter" id="splitter"></div>'
        '<div class="pane code">'
        f'<div class="code-bar"><select id="file-picker">{options}</select>'
        '<input id="code-search" type="text" placeholder="Search code..." spellcheck="false">'
        '<span id="code-search-count"></span>'
        '<button id="code-prev" title="Previous match">&#9650;</button>'
        '<button id="code-next" title="Next match">&#9660;</button>'
        '<button id="wrap-toggle" class="on" title="Wrap long lines instead of scrolling">Wrap</button></div>'
        f'<div class="code-body">{blocks}</div>'
        "</div></main></div>"
        f'<script type="application/json" id="workflow-bundle">{structure}</script>'
        f'<script type="application/json" id="path-slugs">{slug_json}</script>'
        f'<script type="application/json" id="line-spans">{span_json}</script>'
        f"<script>{_EXPORT_SCRIPT}</script>"
        "</body></html>"
    )


@workflows_router.get("/api/workflow-export")
def export_workflows(ids: str = "", format: str = "json"):
    """Export the selected workflows as a JSON bundle or a standalone page.

    Args:
        ids: Comma-separated envelope node IDs to include.
        format: ``"json"`` for the bundle itself, ``"html"`` for the
            bundle rendered into a self-contained document with the JSON
            embedded.

    Returns:
        The bundle dict, or an ``HTMLResponse`` when ``format="html"``.
    """
    from fastapi.responses import HTMLResponse

    from axiom_graph.viz import server
    from axiom_graph.workflows.api import workflow_bundle_to_dict, workflow_export_bundle

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workflow_ids = [part for part in (p.strip() for p in ids.split(",")) if part]
    if not workflow_ids:
        raise HTTPException(status_code=400, detail="No workflows selected")

    bundle = workflow_bundle_to_dict(workflow_export_bundle(server._PROJECT_ROOT, workflow_ids))
    if format == "html":
        return HTMLResponse(content=_render_export_html(bundle))
    return bundle


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

        graph = _workflow_graph()
        labels: list[dict] = []
        for env in env_rows:
            annotated = _annotated_function_id(conn, env["id"])
            if annotated is None:
                continue
            step_rows = _steps_for_envelope(conn, env["id"])
            for sr in step_rows:
                target = step_delegate_target(sr["id"], graph)
                if target is None:
                    continue
                target_id = target.id
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
