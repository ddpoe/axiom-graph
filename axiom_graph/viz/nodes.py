"""Node / history / edges routes.

Extracted from ``viz/server.py`` during Phase 4 split.  Route bodies copied
verbatim; module-globals live in ``viz.server`` and are accessed via the
``server`` module attribute (Option A — minimal diff).
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from axiom_annotations import workflow, AutoStep, Step
from axiom_graph.lifecycle.api import get_node_diff
from axiom_graph.index import builder, db
from axiom_graph.index.status import (
    VERIFIED,
    CONTENT_UPDATED,
    DESC_UPDATED,
    RENAMED,
    NOT_FOUND,
    LINKED_STALE,
    BROKEN_LINK,
)

from axiom_graph.viz._core import (
    _parse_level3_lines,
    _node_to_dict,
    _edge_to_dict,
)

logger = logging.getLogger(__name__)

nodes_router = APIRouter()


_STALENESS_CAUSE_MESSAGES: dict[str, str] = {
    VERIFIED: "Code changed but a reviewer verified the documentation is still accurate.",
    CONTENT_UPDATED: "Primary content changed (hash mismatch) since the baseline was set.",
    DESC_UPDATED: "Descriptor/heading changed but the primary content was not modified.",
    RENAMED: "This node's identity moved — a scoped-similarity rename was applied, migrating history and edges from the old ID.",
    NOT_FOUND: "The source file or function no longer exists at the indexed location.",
    LINKED_STALE: "A linked code node changed after this documentation or test was last written.",
    BROKEN_LINK: "This node has a documents or validates edge pointing to a node that no longer exists in the index.",
}


class _VerifyRequest(BaseModel):
    reason: str | None = None
    verified_by: str = "human"


class _BulkVerifyRequest(BaseModel):
    node_ids: list[str]
    reason: str | None = None
    verified_by: str = "human"


@nodes_router.get("/api/nodes")
def get_nodes(type: str | None = None, tag: str | None = None, status: str | None = None) -> dict:
    """Filtered node list.  All params optional."""
    from axiom_graph.viz import server

    nodes = db.query_nodes(server._db(), node_type=type, tag=tag)
    if status and status != "all":
        nodes = [n for n in nodes if n.status == status]
    server._hydrate_tags(nodes)
    return {"nodes": [_node_to_dict(n) for n in nodes]}


@nodes_router.get("/api/nodes/{node_id}/neighborhood")
def get_neighborhood(node_id: str, depth: int = 1, direction: str = "both") -> dict:
    """Ego-graph: all nodes and edges reachable from node_id within `depth` hops."""
    from axiom_graph.viz import server

    edges = db.query_edges(server._db(), node_id, direction=direction, depth=depth)

    node_ids: set[str] = {node_id}
    for e in edges:
        node_ids.add(e.from_id)
        node_ids.add(e.to_id)

    nodes = [db.get_node(server._db(), nid) for nid in node_ids]
    nodes = [n for n in nodes if n is not None]
    server._hydrate_tags(nodes)
    staleness = server._compute_staleness_for_viz(nodes)

    return {
        "nodes": [_node_to_dict(n) for n in nodes],
        "edges": [_edge_to_dict(e) for e in edges],
        "staleness": server._staleness_to_dicts(staleness),
    }


@nodes_router.get("/api/nodes/{node_id}/history")
def get_node_history(node_id: str) -> dict:
    """Change history for a single node (up to 20 rows)."""
    from axiom_graph.viz import server

    rows = db.get_history(server._db(), node_id, limit=20)

    shas = [r["git_sha"] for r in rows if r.get("git_sha")]
    if shas:
        subjects = _batch_commit_subjects(shas)
        for r in rows:
            sha = r.get("git_sha")
            r["commit_subject"] = subjects.get(sha) if sha else None
    else:
        for r in rows:
            r["commit_subject"] = None

    return {"history": rows}


@nodes_router.get("/api/nodes/{node_id}/tests")
def get_node_tests(node_id: str) -> dict:
    """Return all tests that cover (validate) a given production node."""
    from axiom_graph.viz import server

    conn = server._connect()
    try:
        rows = conn.execute(
            """
            SELECT e.from_id AS test_node_id, n.title, n.location,
                   n.level_0, n.level_1, n.level_3_location
            FROM edges e
            JOIN nodes n ON n.id = e.from_id
            WHERE e.to_id = ? AND e.edge_type = 'validates'
            ORDER BY n.location, n.title
            """,
            (node_id,),
        ).fetchall()
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    conn.close()

    tests = []
    for r in rows:
        line_start, _ = _parse_level3_lines(r["level_3_location"])
        tests.append(
            {
                "test_node_id": r["test_node_id"],
                "name": r["title"],
                "location": r["location"],
                "line_start": line_start,
                "summary": r["level_1"],
                "source": "validates_edge",
            }
        )

    return {"node_id": node_id, "tests": tests, "count": len(tests)}


@nodes_router.get("/api/nodes/{node_id}")
def get_node(node_id: str) -> dict:
    """Single node by ID."""
    from axiom_graph.viz import server

    node = db.get_node(server._db(), node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")
    node.tags = server._get_tags_bulk([node.id]).get(node.id, [])
    return _node_to_dict(node)


@nodes_router.post("/api/nodes/{node_id}/verify")
@workflow(
    purpose="Validate node and dispatch to mark_node_clean",
    inputs="node_id path param, VerifyRequest body (reason, verified_by)",
    outputs="dict with node_id, status, previous own_status/link_status",
)
def verify_node(node_id: str, body: _VerifyRequest) -> dict:
    """Mark a node as verified."""
    from axiom_graph.viz import server

    node = db.get_node(server._db(), node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")
    if not node.code_hash:
        raise HTTPException(status_code=422, detail="Node has no code_hash — cannot verify")
    from axiom_graph.index.mark_clean import mark_node_clean

    mark_node_clean(server._db(), server._PROJECT_ROOT, node, body.reason or "", body.verified_by)
    return {"ok": True, "node_id": node_id, "verified_by": body.verified_by}


@nodes_router.get("/api/nodes/{node_id}/staleness-cause")
@workflow(
    purpose="Diagnose staleness with two-column cause breakdown and verification info",
    inputs="node_id path param",
    outputs="dict with own_status, link_status, cause messages, details, verification metadata",
)
def get_staleness_cause(node_id: str) -> dict:
    """Return a human-readable explanation of why a node is stale (or clean)."""
    from axiom_graph.viz import server

    node = db.get_node(server._db(), node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")

    staleness_map = db.get_all_staleness(server._db())
    pair = staleness_map.get(node_id, (VERIFIED, VERIFIED))
    if isinstance(pair, tuple):
        own_status, link_status = pair
    else:
        own_status, link_status = pair, VERIFIED

    from axiom_graph.index.staleness import _OWN_SEVERITY, _LINK_SEVERITY

    if _OWN_SEVERITY.get(own_status, 0) > 0:
        status = own_status
    elif _LINK_SEVERITY.get(link_status, 0) > 0:
        status = link_status
    else:
        status = own_status

    own_cause = _STALENESS_CAUSE_MESSAGES.get(own_status, "Unknown own status.")
    link_cause = _STALENESS_CAUSE_MESSAGES.get(link_status, "Unknown link status.")
    cause_parts = []
    if own_status != VERIFIED:
        cause_parts.append(f"Own: {own_cause}")
    if link_status != VERIFIED:
        cause_parts.append(f"Link: {link_cause}")
    if not cause_parts:
        cause_parts.append(own_cause)
    cause = " ".join(cause_parts)

    details: dict[str, Any] = {"stored_code_hash": node.code_hash, "stored_desc_hash": node.desc_hash}

    if node.location and own_status in (CONTENT_UPDATED, DESC_UPDATED, RENAMED):
        from axiom_graph.scanners.node_hashing import current_node_hash

        cur_code, cur_desc = current_node_hash(node, server._PROJECT_ROOT)
        details["current_code_hash"] = cur_code
        details["current_desc_hash"] = cur_desc

    if link_status == LINKED_STALE:
        try:
            stale_rows = db.get_stale_doc_sections(server._db())
            linked: list[str] = [
                r["code_node_id"] for r in stale_rows if r.get("section_id") == node_id or r.get("doc_id") == node_id
            ]
            if not linked:
                stale_tests = db.get_stale_tests(server._db())
                linked = [r["code_node_id"] for r in stale_tests if r.get("test_node_id") == node_id]
            if linked:
                details["stale_linked_nodes"] = linked
        except Exception as exc:
            logger.debug("LINKED_STALE enrichment failed: %s", exc)

    verification = None
    v = db.get_verification(server._db(), node_id)
    if v:
        verification = v

    return {
        "node_id": node_id,
        "status": status,
        "own_status": own_status,
        "link_status": link_status,
        "cause": cause,
        "details": details,
        "verification": verification,
    }


@nodes_router.post("/api/nodes/bulk-verify")
@workflow(
    purpose="Batch-verify multiple nodes in one request",
    inputs="BulkVerifyRequest body (node_ids list, reason, verified_by)",
    outputs="dict with results list (per-node status) and counts",
)
def bulk_verify(body: _BulkVerifyRequest) -> dict:
    """Batch-verify multiple nodes in one request."""
    from axiom_graph.viz import server

    results: list[dict] = []
    for nid in body.node_ids:
        node = db.get_node(server._db(), nid)
        if node is None:
            results.append({"node_id": nid, "ok": False, "error": "Node not found"})
            continue
        if not node.code_hash:
            results.append({"node_id": nid, "ok": False, "error": "Node has no code_hash"})
            continue
        from axiom_graph.index.mark_clean import mark_node_clean

        mark_node_clean(server._db(), server._PROJECT_ROOT, node, body.reason or "", body.verified_by)
        results.append({"node_id": nid, "ok": True})
    return {"results": results}


@nodes_router.get("/api/nodes/{node_id}/source")
def get_node_source(node_id: str) -> dict:
    """Return source code for a node with a focused line range."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    node = db.get_node(server._db(), node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")

    if builder.rescan_file_if_needed(server._db(), server._PROJECT_ROOT, node):
        node = db.get_node(server._db(), node_id) or node

    rel_path = node.location
    if not rel_path:
        raise HTTPException(status_code=422, detail="Node has no source location")

    abs_path = (server._PROJECT_ROOT / rel_path).resolve()
    root_resolved = server._PROJECT_ROOT.resolve()
    if not str(abs_path).startswith(str(root_resolved)):
        raise HTTPException(status_code=403, detail="Path outside project root")

    try:
        content = abs_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {rel_path}")

    focus_start, focus_end = _parse_level3_lines(node.level_3_location)

    return {
        "node_id": node_id,
        "path": rel_path,
        "content": content,
        "total_lines": content.count("\n") + 1,
        "focus_start": focus_start,
        "focus_end": focus_end,
    }


@nodes_router.get("/api/nodes/{node_id}/diff")
def get_node_diff_endpoint(node_id: str, sha: str | None = None) -> dict:
    """Return a diff of the node's source against a baseline SHA."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    result = get_node_diff(server._db(), server._PROJECT_ROOT, node_id, baseline_sha=sha)
    if "error" in result:
        return result
    return {
        "node_id": node_id,
        **result,
    }


@nodes_router.get("/api/history/since")
@workflow(
    purpose="Return node IDs that changed since a reference point, with ghost nodes for deleted entries",
    inputs="optional sha (git SHA prefix), optional timestamp (ISO-8601), optional until_sha, optional until_timestamp",
    outputs="dict with node_ids, baseline_sha, baseline_timestamp, until_timestamp, deleted_nodes",
)
def get_history_since_endpoint(
    sha: str | None = None,
    timestamp: str | None = None,
    until_sha: str | None = None,
    until_timestamp: str | None = None,
) -> dict:
    """Return node IDs that changed since a reference point."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    口 = AutoStep(step_num=1, name="Resolve reference point")
    cutoff_ts, baseline_sha = db.resolve_since_cutoff(server._db(), since_sha=sha, since_timestamp=timestamp)

    口 = Step(
        step_num=2,
        name="Last resort: git log date fallback",
        purpose="If SHA not in history at all, resolve its commit date via git log",
    )
    if sha and baseline_sha is None and cutoff_ts is None:
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%aI", sha],
                capture_output=True,
                text=True,
                cwd=str(server._PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0 and result.stdout.strip():
                cutoff_ts = result.stdout.strip()
                baseline_sha = sha
        except Exception as exc:
            logger.debug("git log for baseline sha failed: %s", exc)

    resolved_until_ts: str | None = until_timestamp
    if until_sha and not resolved_until_ts:
        until_cutoff_ts, _ = db.resolve_since_cutoff(
            server._db(),
            since_sha=until_sha,
        )
        if until_cutoff_ts:
            resolved_until_ts = until_cutoff_ts

    口 = Step(
        step_num=3,
        name="Query history rows after cutoff",
        purpose="Fetch all node_history rows with scanned_at > cutoff_timestamp, bounded by until if given",
    )
    if cutoff_ts:
        rows = db.get_history_since(
            server._db(),
            since_timestamp=cutoff_ts,
            until_timestamp=resolved_until_ts,
        )
    else:
        rows = db.get_history_since(server._db(), until_timestamp=resolved_until_ts)

    node_ids = sorted(set(r["node_id"] for r in rows))

    口 = Step(
        step_num=4,
        name="Synthesize ghost nodes from DELETED rows",
        purpose="Reconstruct phantom entries for purged nodes so the viz can show them as dimmed strikethrough rows",
        critical="Ghost nodes depend on preserved=1 DELETED history rows — if those are missing, deleted nodes silently disappear from the since view",
    )
    deleted_nodes = []
    deleted_rows = [r for r in rows if r["change_type"] == "DELETED"]
    if deleted_rows:
        seen_deleted: set[str] = set()
        for r in deleted_rows:
            nid = r["node_id"]
            if nid in seen_deleted:
                continue
            seen_deleted.add(nid)
            meta = json.loads(r["meta"]) if r.get("meta") else {}
            deleted_nodes.append(
                {
                    "id": nid,
                    "title": meta.get("title", nid.split("::")[-1]),
                    "node_type": meta.get("node_type", "unknown"),
                    "subtype": meta.get("subtype"),
                    "location": meta.get("location", ""),
                    "tags": meta.get("tags", []),
                    "deleted_at": r["scanned_at"],
                    "_deleted": True,
                }
            )

    口 = Step(
        step_num=5,
        name="Resolve baseline SHA for timestamp-only cutoffs",
        purpose="When cutoff came from a timestamp (no SHA), find the nearest git commit for scoped diff",
    )
    if baseline_sha is None and cutoff_ts is not None:
        try:
            result = subprocess.run(
                ["git", "rev-list", "-1", f"--before={cutoff_ts}", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(server._PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0 and result.stdout.strip():
                baseline_sha = result.stdout.strip()
        except Exception as exc:
            logger.debug("git rev-list for baseline resolution failed: %s", exc)

    return {
        "node_ids": node_ids,
        "baseline_sha": baseline_sha,
        "baseline_timestamp": cutoff_ts,
        "until_timestamp": resolved_until_ts,
        "deleted_nodes": deleted_nodes,
    }


def _batch_commit_subjects(shas: list[str]) -> dict[str, str]:
    """Resolve commit subjects for a list of SHAs via a single git log call."""
    info = _batch_commit_info(shas)
    return {sha: v["subject"] for sha, v in info.items() if v.get("subject")}


def _batch_commit_info(shas: list[str]) -> dict[str, dict[str, str]]:
    """Resolve commit subjects and bodies for a list of SHAs via a single git log call."""
    from axiom_graph.viz import server

    if not shas or server._PROJECT_ROOT is None:
        return {}
    unique = list(dict.fromkeys(shas))
    try:
        result = subprocess.run(
            ["git", "log", "--no-walk", "--format=%H%x00%s%x00%b%x1e", *unique],
            capture_output=True,
            text=True,
            cwd=str(server._PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return {}
        out: dict[str, dict[str, str]] = {}
        for record in result.stdout.split("\x1e"):
            record = record.strip()
            if not record:
                continue
            parts = record.split("\x00", 2)
            if len(parts) < 2:
                continue
            full_sha = parts[0].strip()
            subject = parts[1].strip() if len(parts) > 1 else ""
            body = parts[2].strip() if len(parts) > 2 else ""
            for s in unique:
                if full_sha.startswith(s) or s.startswith(full_sha[: len(s)]):
                    out[s] = {"subject": subject, "body": body}
        return out
    except Exception as exc:
        logger.debug("git commit info lookup failed: %s", exc)
        return {}


@nodes_router.get("/api/history/recent-shas")
def get_recent_shas() -> dict:
    """Return recent git commits for the commit picker."""
    from axiom_graph.viz import server

    if server._PROJECT_ROOT is None:
        raise HTTPException(status_code=503, detail="Server not initialized")

    with server._connect() as conn:
        checkpoint_rows = conn.execute(
            "SELECT DISTINCT git_sha FROM node_history WHERE change_type = 'CHECKPOINT' AND git_sha IS NOT NULL",
        ).fetchall()
    checkpoint_shas = {r["git_sha"] for r in checkpoint_rows}

    try:
        result = subprocess.run(
            ["git", "log", "-50", "--format=%H%x00%aI%x00%s%x00%b%x1e"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(server._PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return {"shas": []}
    except Exception:
        return {"shas": []}

    entries: list[dict] = []
    for record in result.stdout.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x00", 3)
        if len(parts) < 3:
            continue
        sha = parts[0].strip()
        date = parts[1].strip()
        subject = parts[2].strip()
        body = parts[3].strip() if len(parts) > 3 else ""
        is_cp = sha in checkpoint_shas
        entries.append(
            {
                "sha": sha,
                "date": date,
                "change_type": "CHECKPOINT" if is_cp else "BUILD",
                "commit_subject": subject or None,
                "commit_body": body if body else None,
                "is_checkpoint": is_cp,
            }
        )

    return {"shas": entries}


@nodes_router.get("/api/nodes/{node_id}/edges")
def get_node_edges(node_id: str) -> dict:
    """Return inbound and outbound edges for a node (single hop, no traversal)."""
    from axiom_graph.viz import server

    node = db.get_node(server._db(), node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node not found: {node_id!r}")

    out_edges = db.query_edges(server._db(), node_id, direction="out", depth=1)
    in_edges = db.query_edges(server._db(), node_id, direction="in", depth=1)

    return {
        "node_id": node_id,
        "inbound": [_edge_to_dict(e) for e in in_edges],
        "outbound": [_edge_to_dict(e) for e in out_edges],
    }
