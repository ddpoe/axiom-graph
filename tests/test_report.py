"""Tests for axiom-graph report: get_history_since() DB query and cmd_report CLI."""

from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner

from axiom_annotations import workflow

from axiom_graph.index import db
from axiom_graph.models import AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(node_id: str, code_hash: str = "abc123") -> AxiomNode:
    return AxiomNode(
        id=node_id,
        node_type="atomic_process",
        subtype="function",
        title=node_id.split("::")[-1],
        location="src/mod.py",
        source="ast",
        code_hash=code_hash,
        desc_hash=None,
        level_0=node_id,
        level_1=node_id,
    )


def _seed_history(
    db_path: Path,
    node_id: str,
    change_type: str,
    git_sha: str | None = None,
    meta: str | None = None,
    preserved: bool = False,
) -> None:
    """Insert a history row with a tiny sleep to ensure timestamp ordering."""
    time.sleep(0.015)
    db.insert_history_row(db_path, node_id, change_type, git_sha=git_sha, meta=meta, preserved=preserved)


# ---------------------------------------------------------------------------
# Tier 1 — get_history_since unit tests
# ---------------------------------------------------------------------------


def test_get_history_since_returns_all_when_no_checkpoint(db_path: Path):
    """With no checkpoint and no args, all rows are returned."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CONTENT_ONLY")
    _seed_history(db_path, n.id, "DESC_ONLY")

    rows = db.get_history_since(db_path)
    # Should include INITIAL (from upsert) + 2 manual rows
    assert len(rows) >= 2
    change_types = {r["change_type"] for r in rows}
    assert "CONTENT_ONLY" in change_types
    assert "DESC_ONLY" in change_types


def test_get_history_since_respects_checkpoint_cutoff(db_path: Path):
    """Rows before the checkpoint are excluded; rows after are included."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CONTENT_ONLY")

    # Insert checkpoint
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="aaa111", preserved=True)

    # Events after checkpoint
    _seed_history(db_path, n.id, "DESC_ONLY")
    _seed_history(db_path, n.id, "AGENT_VERIFIED")

    rows = db.get_history_since(db_path)
    change_types = [r["change_type"] for r in rows]
    assert "DESC_ONLY" in change_types
    assert "AGENT_VERIFIED" in change_types
    # CONTENT_ONLY was before checkpoint — should be excluded
    assert "CONTENT_ONLY" not in change_types
    # CHECKPOINT itself is at the cutoff, not after — excluded
    assert "CHECKPOINT" not in change_types


def test_get_history_since_by_sha(db_path: Path):
    """--since-sha matches checkpoint by git_sha prefix."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)

    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="aaa111bbb", preserved=True)
    _seed_history(db_path, n.id, "CONTENT_ONLY")

    # Second checkpoint
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="ccc222ddd", preserved=True)
    _seed_history(db_path, n.id, "DESC_ONLY")

    # Query since first checkpoint
    rows = db.get_history_since(db_path, since_sha="aaa111")
    change_types = [r["change_type"] for r in rows]
    # Should include everything after first checkpoint
    assert "CONTENT_ONLY" in change_types
    assert "DESC_ONLY" in change_types


def test_get_history_since_by_timestamp(db_path: Path):
    """--since with a timestamp filters correctly."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CONTENT_ONLY")

    # Capture a cutoff timestamp
    from axiom_graph.index.db import _now_utc

    cutoff = _now_utc()
    time.sleep(0.02)

    _seed_history(db_path, n.id, "DESC_ONLY")

    rows = db.get_history_since(db_path, since_timestamp=cutoff)
    change_types = [r["change_type"] for r in rows]
    assert "DESC_ONLY" in change_types
    assert "CONTENT_ONLY" not in change_types


def test_get_history_since_sha_takes_priority_over_timestamp(db_path: Path):
    """When both since_sha and since_timestamp are given, SHA wins."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)

    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="sha111", preserved=True)
    _seed_history(db_path, n.id, "CONTENT_ONLY")
    _seed_history(db_path, n.id, "DESC_ONLY")

    # Use a far-future timestamp that would exclude everything
    rows = db.get_history_since(db_path, since_sha="sha111", since_timestamp="2099-01-01T00:00:00+00:00")
    # SHA match should win, so we get rows after the checkpoint
    assert len(rows) >= 2


def test_get_history_since_empty_when_nothing_after(db_path: Path):
    """If checkpoint is the latest event, result is empty."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="latest", preserved=True)

    rows = db.get_history_since(db_path)
    assert rows == []


# ---------------------------------------------------------------------------
# Tier 2 — cmd_report CLI integration test
# ---------------------------------------------------------------------------


@workflow(purpose="Verify axiom-graph report CLI produces correct impact summary across all event categories")
def test_cmd_report_text_output(mini_project: Path, db_path: Path):
    """Seed varied history events and verify the text report output."""
    from axiom_graph.cli import main

    # Seed nodes
    fn1 = _node("proj::mod::fn1", code_hash="h1")
    fn2 = _node("proj::mod::fn2", code_hash="h2")
    db.upsert_node(db_path, fn1, discovery_only=False)
    db.upsert_node(db_path, fn2, discovery_only=False)

    # Checkpoint as reference point
    _seed_history(db_path, fn1.id, "CHECKPOINT", git_sha="base00", preserved=True)

    # Content change
    _seed_history(db_path, fn1.id, "CONTENT_ONLY")

    # Staleness transition
    _seed_history(db_path, fn2.id, "BECAME_CONTENT_UPDATED", meta=json.dumps({"from": "VERIFIED"}))

    # Link change
    _seed_history(
        db_path,
        fn1.id,
        "LINK_ADDED",
        meta=json.dumps({"edge_type": "documents", "target": "proj::docs::x", "actor": "agent"}),
    )

    # Verification
    _seed_history(db_path, fn2.id, "AGENT_VERIFIED", meta=json.dumps({"reason": "looks good"}))

    runner = CliRunner()
    result = runner.invoke(main, ["report", str(mini_project)])
    assert result.exit_code == 0

    output = result.output
    # Summary line should contain all categories
    assert "nodes changed" in output
    assert "became stale" in output
    assert "verified" in output
    assert "links modified" in output


@workflow(purpose="Verify axiom-graph report --format json returns valid structured JSON with all sections")
def test_cmd_report_json_output(mini_project: Path, db_path: Path):
    """JSON output should be parseable and contain expected keys."""
    from axiom_graph.cli import main

    fn = _node("proj::mod::fn", code_hash="h1")
    db.upsert_node(db_path, fn, discovery_only=False)

    # No checkpoint → all history included
    _seed_history(db_path, fn.id, "CONTENT_ONLY")
    _seed_history(db_path, fn.id, "BECAME_CONTENT_UPDATED", meta=json.dumps({"from": "VERIFIED"}))

    runner = CliRunner()
    result = runner.invoke(main, ["report", "--format", "json", str(mini_project)])
    assert result.exit_code == 0

    data = json.loads(result.output)
    assert "summary" in data
    assert "content_changes" in data
    assert "staleness_transitions" in data
    assert "link_changes" in data
    assert "verifications" in data
    assert data["summary"]["nodes_changed"] >= 1


# ---------------------------------------------------------------------------
# Tier 3 — filter_history_rows unit tests
# ---------------------------------------------------------------------------


def test_filter_history_rows(db_path: Path):
    """Glob filters on change_type, node_id, and node_type all work individually and combined."""
    fn = _node("proj::viz::render")
    fn.node_type = "atomic_process"
    mod = AxiomNode(
        id="proj::viz",
        node_type="composite_process",
        subtype="module",
        title="viz",
        location="src/viz.py",
        source="ast",
        code_hash="xyz",
        desc_hash=None,
        level_0="proj::viz",
        level_1="proj::viz",
    )
    other = _node("proj::cli::main")
    db.upsert_node(db_path, fn, discovery_only=False)
    db.upsert_node(db_path, mod, discovery_only=False)
    db.upsert_node(db_path, other, discovery_only=False)
    _seed_history(db_path, fn.id, "BECAME_CONTENT_UPDATED")
    _seed_history(db_path, fn.id, "LINK_ADDED")
    _seed_history(db_path, mod.id, "BECAME_DESC_UPDATED")
    _seed_history(db_path, other.id, "AGENT_VERIFIED")

    rows = db.get_history_since(db_path)

    # change_type glob
    stale = db.filter_history_rows(rows, change_type_pattern="*UPDATED*")
    assert {r["change_type"] for r in stale} == {"BECAME_CONTENT_UPDATED", "BECAME_DESC_UPDATED"}

    # node_id glob
    viz_only = db.filter_history_rows(rows, node_pattern="proj::viz*")
    assert all(r["node_id"].startswith("proj::viz") for r in viz_only)
    assert not any(r["node_id"] == other.id for r in viz_only)

    # node_type
    nt_map = db.build_node_types_map(db_path)
    funcs = db.filter_history_rows(rows, node_type="atomic_process", node_types_map=nt_map)
    assert all(r["node_id"] in (fn.id, other.id) for r in funcs)

    # combined: updated + viz namespace → only fn's BECAME_CONTENT_UPDATED
    combined = db.filter_history_rows(
        rows,
        change_type_pattern="*UPDATED*",
        node_pattern="*render*",
    )
    assert len(combined) == 1
    assert combined[0]["node_id"] == fn.id

    # no match → empty
    assert db.filter_history_rows(rows, change_type_pattern="DELETED") == []


# ---------------------------------------------------------------------------
# Tier 4 — list_reference_points unit tests
# ---------------------------------------------------------------------------


def test_list_reference_points(db_path: Path):
    """Checkpoints and build SHAs are listed, deduped, with checkpoint priority."""
    # Empty DB → empty list
    assert db.list_reference_points(db_path) == []

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False, git_sha="build_sha")
    _seed_history(
        db_path, n.id, "CHECKPOINT", git_sha="cp_sha", meta=json.dumps({"message": "v1 release"}), preserved=True
    )
    # Checkpoint with same SHA as build — should not be duplicated
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="build_sha", preserved=True)

    refs = db.list_reference_points(db_path)
    shas = [r["git_sha"] for r in refs]

    # Checkpoints listed, with message
    cp = next(r for r in refs if r["git_sha"] == "cp_sha")
    assert cp["type"] == "checkpoint"
    assert cp["message"] == "v1 release"

    # build_sha appears once as checkpoint (not duplicated as build)
    assert shas.count("build_sha") == 1
    assert next(r for r in refs if r["git_sha"] == "build_sha")["type"] == "checkpoint"


# ---------------------------------------------------------------------------
# Tier 5 — CLI integration: --list-refs, --change-type, --node, --node-type
# ---------------------------------------------------------------------------


@workflow(purpose="Verify --list-refs shows available reference points")
def test_cmd_report_list_refs(mini_project: Path, db_path: Path):
    """--list-refs lists checkpoints and build SHAs."""
    from axiom_graph.cli import main

    fn = _node("proj::mod::fn")
    db.upsert_node(db_path, fn, discovery_only=False, git_sha="ref_sha_1")
    _seed_history(
        db_path, fn.id, "CHECKPOINT", git_sha="cp_sha_2", meta=json.dumps({"message": "weekly"}), preserved=True
    )

    runner = CliRunner()
    result = runner.invoke(main, ["report", "--list-refs", str(mini_project)])
    assert result.exit_code == 0
    assert "cp_sha_2" in result.output
    assert "checkpoint" in result.output
    assert "weekly" in result.output


@workflow(purpose="Verify --change-type glob filters report output")
def test_cmd_report_change_type_filter(mini_project: Path, db_path: Path):
    """--change-type glob filters the report to matching event types."""
    from axiom_graph.cli import main

    fn = _node("proj::mod::fn")
    db.upsert_node(db_path, fn, discovery_only=False)
    _seed_history(db_path, fn.id, "BECAME_CONTENT_UPDATED")
    _seed_history(db_path, fn.id, "LINK_ADDED")

    runner = CliRunner()
    result = runner.invoke(main, ["report", "--change-type", "LINK_*", str(mini_project)])
    assert result.exit_code == 0
    assert "LINK" in result.output
    assert "STALE" not in result.output


@workflow(purpose="Verify --node glob filters report to matching node IDs")
def test_cmd_report_node_filter(mini_project: Path, db_path: Path):
    """--node glob restricts report to matching node IDs."""
    from axiom_graph.cli import main

    fn1 = _node("proj::viz::render")
    fn2 = _node("proj::cli::main")
    db.upsert_node(db_path, fn1, discovery_only=False)
    db.upsert_node(db_path, fn2, discovery_only=False)
    _seed_history(db_path, fn1.id, "BECAME_CONTENT_UPDATED")
    _seed_history(db_path, fn2.id, "BECAME_CONTENT_UPDATED")

    runner = CliRunner()
    result = runner.invoke(main, ["report", "--node", "proj::viz*", str(mini_project)])
    assert result.exit_code == 0
    assert "proj::viz::render" in result.output
    assert "proj::cli::main" not in result.output
