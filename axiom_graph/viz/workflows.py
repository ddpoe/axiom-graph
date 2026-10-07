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

    return db.open_connection(server._DB_PATH)


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
        depth: Nesting depth for expanded rows: the number of dots in the
            rendered step number, so ``1`` and ``2`` are ``0`` and ``2.1.1``
            is ``2``.  This is the depth the export bundle carries, so the
            live outline and the exported page indent a step the same way.
            Passing any value (including
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
                depth=entry.rendered_step_num.count("."),
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


@workflows_router.get("/api/workflow-export")
def export_workflows(ids: str = "", format: str = "json"):
    """Export the selected workflows as a JSON bundle or a standalone page.

    The page is the same one ``axiom-graph workflows export`` and the MCP
    ``axiom_graph_workflow_export`` tool write: the selection comes from
    :func:`axiom_graph.workflows.api.select_export_bundle` and the page from
    :func:`axiom_graph.workflows.export.render_export_html`.

    Args:
        ids: Comma-separated envelope node IDs to include.
        format: ``"json"`` for the bundle itself, ``"html"`` for the
            bundle rendered into a self-contained document with the JSON
            embedded.

    Returns:
        The bundle dict, or an ``HTMLResponse`` when ``format="html"``.

    Raises:
        HTTPException: 400 when no ids are given; 404 naming every id that
            matches no workflow or task.
    """
    from fastapi.responses import HTMLResponse

    from axiom_graph.viz import server
    from axiom_graph.workflows.api import UnknownWorkflowError, select_export_bundle
    from axiom_graph.workflows.export import render_export_html

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    workflow_ids = [part for part in (p.strip() for p in ids.split(",")) if part]
    if not workflow_ids:
        raise HTTPException(status_code=400, detail="No workflows selected")

    try:
        bundle = select_export_bundle(server._PROJECT_ROOT, workflow_ids)
    except UnknownWorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if format == "html":
        return HTMLResponse(content=render_export_html(bundle))
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
        # Internal: the walk's per-file observations, for the in-process refresh only.
        cortex_summary.pop("discovery_observed", None)
    except Exception as exc:
        errors.append(f"axiom-graph build error: {exc}")

    return {
        "ok": len(errors) == 0,
        "dflow_ok": True,
        "cortex_summary": cortex_summary,
        "errors": errors,
    }
