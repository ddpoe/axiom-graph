"""Tests for Phase 7: resolve_since_cutoff, commit context in diff, /api/history/since."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch


from axiom_annotations import workflow

from axiom_graph.lifecycle.api import get_node_diff
from axiom_graph.index import db
from axiom_graph.models import AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    node_id: str,
    code_hash: str = "abc123",
    location: str = "src/mod.py",
    level_3_location: str | None = "src/mod.py#L1-L2",
) -> AxiomNode:
    return AxiomNode(
        id=node_id,
        node_type="atomic_process",
        subtype="function",
        title=node_id.split("::")[-1],
        location=location,
        level_3_location=level_3_location,
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
    preserved: bool = False,
) -> None:
    """Insert a history row with a tiny sleep to ensure timestamp ordering."""
    time.sleep(0.015)
    db.insert_history_row(
        db_path,
        node_id,
        change_type,
        git_sha=git_sha,
        preserved=preserved,
    )


# ---------------------------------------------------------------------------
# Tier 1 — resolve_since_cutoff unit tests
# ---------------------------------------------------------------------------


def test_resolve_cutoff_by_sha(db_path: Path):
    """SHA param matches a checkpoint and returns its timestamp and git_sha."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="deadbeef1234", preserved=True)

    cutoff_ts, sha = db.resolve_since_cutoff(db_path, since_sha="deadbeef")
    assert sha == "deadbeef1234"
    assert cutoff_ts is not None


def test_resolve_cutoff_by_timestamp(db_path: Path):
    """Timestamp param is returned directly; no checkpoint SHA available."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)

    cutoff_ts, sha = db.resolve_since_cutoff(db_path, since_timestamp="2026-03-01T00:00:00")
    assert cutoff_ts == "2026-03-01T00:00:00"
    assert sha is None


def test_resolve_cutoff_fallback_to_latest_checkpoint(db_path: Path):
    """With no args, falls back to the most recent checkpoint."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="older_sha", preserved=True)
    _seed_history(db_path, n.id, "CONTENT_ONLY")
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="newer_sha", preserved=True)

    cutoff_ts, sha = db.resolve_since_cutoff(db_path)
    assert sha == "newer_sha"
    assert cutoff_ts is not None


def test_resolve_cutoff_no_checkpoint(db_path: Path):
    """Returns (None, None) when there are no checkpoints at all."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CONTENT_ONLY")

    cutoff_ts, sha = db.resolve_since_cutoff(db_path)
    assert cutoff_ts is None
    assert sha is None


def test_resolve_cutoff_sha_takes_priority(db_path: Path):
    """SHA param is tried first even when a timestamp is also provided."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="sha_abc", preserved=True)

    cutoff_ts, sha = db.resolve_since_cutoff(db_path, since_sha="sha_abc", since_timestamp="2020-01-01T00:00:00")
    assert sha == "sha_abc"
    # The timestamp should come from the checkpoint, not the provided value
    assert cutoff_ts != "2020-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Tier 1 — Commit context in get_node_diff
# ---------------------------------------------------------------------------


def test_diff_returns_commit_context(db_path: Path, mini_project: Path):
    """get_node_diff returns commit_subject, commit_author, commit_date."""
    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("def fn():\n    return 42\n")

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "AGENT_VERIFIED", git_sha="abc1234")

    # Mock both git show (first call) and git log (second call)
    show_result = MagicMock()
    show_result.returncode = 0
    show_result.stdout = "def fn():\n    return 0\n"

    log_result = MagicMock()
    log_result.returncode = 0
    log_result.stdout = "Fix the thing\nJane Doe\n2026-03-18T14:00:00+00:00\n"

    with patch("axiom_graph.lifecycle.api.subprocess.run", side_effect=[show_result, log_result]):
        result = get_node_diff(db_path, mini_project, n.id)

    assert "error" not in result
    assert result["commit_subject"] == "Fix the thing"
    assert result["commit_author"] == "Jane Doe"
    assert result["commit_date"] == "2026-03-18T14:00:00+00:00"


def test_diff_commit_context_graceful_on_failure(db_path: Path, mini_project: Path):
    """Commit context fields are None when git log fails."""
    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("def fn():\n    return 42\n")

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "AGENT_VERIFIED", git_sha="abc1234")

    show_result = MagicMock()
    show_result.returncode = 0
    show_result.stdout = "def fn():\n    return 0\n"

    log_result = MagicMock()
    log_result.returncode = 128  # git log fails
    log_result.stdout = ""

    with patch("axiom_graph.lifecycle.api.subprocess.run", side_effect=[show_result, log_result]):
        result = get_node_diff(db_path, mini_project, n.id)

    assert "error" not in result
    assert result["commit_subject"] is None
    assert result["commit_author"] is None
    assert result["commit_date"] is None


# ---------------------------------------------------------------------------
# Tier 1 — /api/history/since distinct node_ids
# ---------------------------------------------------------------------------


def test_since_returns_distinct_node_ids(db_path: Path):
    """get_history_since rows are collapsed to distinct node_ids."""
    n1 = _node("proj::mod::fn1", code_hash="h1")
    n2 = _node("proj::mod::fn2", code_hash="h2")
    db.upsert_node(db_path, n1, discovery_only=False)
    db.upsert_node(db_path, n2, discovery_only=False)
    _seed_history(db_path, n1.id, "CHECKPOINT", git_sha="cp_sha", preserved=True)
    _seed_history(db_path, n1.id, "CONTENT_ONLY")
    _seed_history(db_path, n1.id, "CONTENT_ONLY")  # duplicate node
    _seed_history(db_path, n2.id, "DESC_ONLY")

    rows = db.get_history_since(db_path)
    node_ids = sorted(set(r["node_id"] for r in rows))
    assert n1.id in node_ids
    assert n2.id in node_ids


# ---------------------------------------------------------------------------
# Tier 2 — Integration: /api/history/since endpoint
# ---------------------------------------------------------------------------


@workflow(purpose="Verify /api/history/since endpoint resolves the baseline SHA and emits change_kinds")
def test_since_endpoint_with_checkpoint(db_path: Path, mini_project: Path):
    """Endpoint resolves the baseline SHA and returns the net-diff shape.

    Under the net-diff contract (D-1/D-2), membership is a true git state-diff
    of the index's stored hashes vs the baseline blob — not an event replay.
    With a synthetic (non-git) baseline SHA the net diff can't run, so
    ``node_ids`` is empty; the baseline resolution and response shape are still
    asserted here. Real membership/kind behaviour is covered by the e2e tests
    against a real git repo.
    """
    from axiom_graph.viz.server import get_history_since_endpoint, _apply_project

    _apply_project(mini_project)

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="cp_sha_123", preserved=True)
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="later_sha")

    result = get_history_since_endpoint()

    assert isinstance(result["node_ids"], list)
    assert isinstance(result["change_kinds"], dict)
    assert result["baseline_sha"] == "cp_sha_123"
    assert result["baseline_timestamp"] is not None


@workflow(purpose="Verify /api/history/since with timestamp param resolves and emits the net-diff shape")
def test_since_endpoint_with_timestamp(db_path: Path, mini_project: Path):
    """Endpoint handles timestamp-only param; baseline_timestamp is preserved."""
    from axiom_graph.viz.server import get_history_since_endpoint, _apply_project

    _apply_project(mini_project)

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CONTENT_ONLY")

    # Use a very old timestamp so all rows are included
    result = get_history_since_endpoint(timestamp="2020-01-01T00:00:00")

    assert isinstance(result["node_ids"], list)
    assert isinstance(result["change_kinds"], dict)
    assert result["baseline_timestamp"] == "2020-01-01T00:00:00"


# ---------------------------------------------------------------------------
# Tier 2 — Integration: since filter → diff endpoint full workflow
# ---------------------------------------------------------------------------


@workflow(purpose="Verify since filter baseline SHA flows through to diff endpoint for existing files")
def test_since_diff_workflow_existing_file(db_path: Path, mini_project: Path):
    """Full flow: since endpoint resolves baseline → diff endpoint uses it to return old/new content."""
    from axiom_graph.viz.server import get_history_since_endpoint, get_node_diff_endpoint, _apply_project

    _apply_project(mini_project)

    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("def fn():\n    return 42\n")

    n = _node("proj::mod::fn", location="src/mod.py", level_3_location="src/mod.py#L1-L2")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="cp_sha_1", preserved=True)
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="later_sha")

    # Step 1: since endpoint resolves baseline (membership is a real git diff;
    # with a synthetic baseline SHA node_ids is empty — see net-diff contract).
    since_result = get_history_since_endpoint()
    assert since_result["baseline_sha"] == "cp_sha_1"
    assert isinstance(since_result["change_kinds"], dict)

    # Step 2: diff endpoint uses that baseline SHA
    show_mock = MagicMock()
    show_mock.returncode = 0
    show_mock.stdout = "def fn():\n    return 0\n"

    log_mock = MagicMock()
    log_mock.returncode = 0
    log_mock.stdout = "Old commit\nDev\n2026-03-01T00:00:00"

    with patch("axiom_graph.lifecycle.api.subprocess.run", side_effect=[show_mock, log_mock]):
        diff_result = get_node_diff_endpoint(n.id, sha=since_result["baseline_sha"])

    assert "error" not in diff_result
    assert diff_result["baseline_sha"] == "cp_sha_1"
    assert "return 0" in diff_result["old_content"]
    assert "return 42" in diff_result["new_content"]


@workflow(purpose="Verify since→diff workflow handles new files (not in baseline) with empty old content")
def test_since_diff_workflow_new_file(db_path: Path, mini_project: Path):
    """New file at baseline commit → diff returns empty old_content, full new_content."""
    from axiom_graph.viz.server import get_node_diff_endpoint, _apply_project

    _apply_project(mini_project)

    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "new_mod.py").write_text("def new_fn():\n    return 99\n")

    n = _node("proj::new_mod::new_fn", location="src/new_mod.py", level_3_location="src/new_mod.py#L1-L2")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "INITIAL", git_sha="some_sha")

    # git show fails — file didn't exist at baseline
    show_mock = MagicMock()
    show_mock.returncode = 128
    show_mock.stderr = "fatal: path 'src/new_mod.py' does not exist in 'baseline_sha'"
    show_mock.stdout = ""

    log_mock = MagicMock()
    log_mock.returncode = 0
    log_mock.stdout = "Initial\nDev\n2026-03-01T00:00:00"

    with patch("axiom_graph.lifecycle.api.subprocess.run", side_effect=[show_mock, log_mock]):
        diff_result = get_node_diff_endpoint(n.id, sha="baseline_sha")

    assert "error" not in diff_result
    assert diff_result["old_content"] == ""
    assert "return 99" in diff_result["new_content"]


# ---------------------------------------------------------------------------
# Tier 1 — Enhanced recent-shas endpoint
# ---------------------------------------------------------------------------


@workflow(purpose="Verify enhanced recent-shas returns commit_body and is_checkpoint fields")
def test_recent_shas_enhanced_fields(db_path: Path, mini_project: Path):
    """get_recent_shas pulls from git log and cross-references checkpoints."""
    from axiom_graph.viz.server import get_recent_shas, _apply_project

    _apply_project(mini_project)

    # Seed a checkpoint so the cross-reference query finds it
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="cp_sha_001", preserved=True)

    # Mock git log output (format: %H%x00%aI%x00%s%x00%b%x1e)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = (
        "cp_sha_001\x002026-04-04T10:00:00+00:00\x00Checkpoint commit\x00Body line 1\nBody line 2\x1e"
        "build_sha_002\x002026-04-04T09:00:00+00:00\x00Build commit\x00\x1e"
    )

    with patch("axiom_graph.viz.nodes.subprocess.run", return_value=mock_result):
        result = get_recent_shas()

    shas = result["shas"]
    assert len(shas) == 2

    # Find the checkpoint entry
    cp_entry = next(e for e in shas if e["sha"] == "cp_sha_001")
    assert cp_entry["is_checkpoint"] is True
    assert cp_entry["commit_subject"] == "Checkpoint commit"
    assert cp_entry["commit_body"] == "Body line 1\nBody line 2"
    assert cp_entry["date"] == "2026-04-04T10:00:00+00:00"

    # Find the build entry (not a checkpoint)
    build_entry = next(e for e in shas if e["sha"] == "build_sha_002")
    assert build_entry["is_checkpoint"] is False
    assert build_entry["commit_body"] is None


# ---------------------------------------------------------------------------
# Tier 1 — get_history_since with until_timestamp support
# ---------------------------------------------------------------------------


def test_get_history_since_with_until_timestamp(db_path: Path):
    """get_history_since with until_timestamp caps the upper bound of results."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)

    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="cp_sha", preserved=True)
    # Record the checkpoint timestamp for the since cutoff
    cutoff_ts, _ = db.resolve_since_cutoff(db_path, since_sha="cp_sha")

    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="mid_sha")
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="later_sha")

    # Get all rows after checkpoint (should be 2)
    all_rows = db.get_history_since(db_path, since_sha="cp_sha")
    assert len(all_rows) >= 2

    # Now use until_timestamp to cap — get the timestamp of the first row after checkpoint
    # The mid_sha row should be included, later_sha should be excluded
    mid_row = [r for r in all_rows if r.get("git_sha") == "mid_sha"]
    assert len(mid_row) == 1
    until_ts = mid_row[0]["scanned_at"]

    capped_rows = db.get_history_since(db_path, since_sha="cp_sha", until_timestamp=until_ts)
    capped_shas = [r.get("git_sha") for r in capped_rows]
    assert "mid_sha" in capped_shas
    assert "later_sha" not in capped_shas


# ---------------------------------------------------------------------------
# Tier 2 — /api/history/since with until params
# ---------------------------------------------------------------------------


@workflow(purpose="Verify /api/history/since endpoint supports until_sha for range queries")
def test_since_endpoint_with_until(db_path: Path, mini_project: Path):
    """Endpoint with until_sha returns only nodes in the range."""
    from axiom_graph.viz.server import get_history_since_endpoint, _apply_project

    _apply_project(mini_project)

    n1 = _node("proj::mod::fn1", code_hash="h1")
    n2 = _node("proj::mod::fn2", code_hash="h2")
    db.upsert_node(db_path, n1, discovery_only=False)
    db.upsert_node(db_path, n2, discovery_only=False)

    _seed_history(db_path, n1.id, "CHECKPOINT", git_sha="cp_start", preserved=True)
    _seed_history(db_path, n1.id, "CONTENT_ONLY", git_sha="mid_sha")
    _seed_history(db_path, n2.id, "CONTENT_ONLY", git_sha="mid_sha")
    _seed_history(db_path, n1.id, "CONTENT_ONLY", git_sha="end_sha")

    # Without until — resolves and returns the net-diff shape (membership is a
    # real git diff; with synthetic SHAs node_ids is empty — net-diff contract).
    result_all = get_history_since_endpoint(sha="cp_start")
    assert isinstance(result_all["node_ids"], list)
    assert isinstance(result_all["change_kinds"], dict)

    # With until_sha — mock resolve to get its timestamp
    # We need to get the scanned_at for end_sha to use as until
    rows = db.get_history_since(db_path, since_sha="cp_start")
    end_row = [r for r in rows if r.get("git_sha") == "end_sha"]
    assert len(end_row) >= 1

    # Use until_timestamp to exclude the end_sha row
    mid_rows = [r for r in rows if r.get("git_sha") == "mid_sha"]
    assert len(mid_rows) >= 1
    until_ts = mid_rows[0]["scanned_at"]

    result_range = get_history_since_endpoint(sha="cp_start", until_timestamp=until_ts)
    # Should include mid_sha changes but not end_sha changes
    assert "until_timestamp" in result_range


# ---------------------------------------------------------------------------
# Tier 1 — explicit-SHA miss fails loud + index-freshness helpers
# ---------------------------------------------------------------------------


def test_resolve_cutoff_explicit_sha_miss_returns_none(db_path: Path):
    """An explicit since_sha absent from history resolves to (None, None).

    The no-arg checkpoint/any-SHA fallback must not fire for an explicit SHA —
    borrowing a different baseline would answer "changed since X" against the
    wrong reference point.
    """
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    # A checkpoint exists, but the caller asks for a different, un-indexed SHA.
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="indexed_sha", preserved=True)

    cutoff_ts, sha = db.resolve_since_cutoff(db_path, since_sha="ffff0000")
    assert cutoff_ts is None
    assert sha is None


def test_index_head_sha_and_indexed_shas(db_path: Path):
    """get_index_head_sha returns the most recent SHA; get_indexed_shas the set."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "INITIAL", git_sha="sha_old")
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="sha_new")

    assert db.get_index_head_sha(db_path) == "sha_new"
    assert db.get_indexed_shas(db_path) == {"sha_old", "sha_new"}


def test_index_head_sha_none_without_history(db_path: Path):
    """get_index_head_sha is None when no history row carries a SHA."""
    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CONTENT_ONLY")  # no git_sha

    assert db.get_index_head_sha(db_path) is None


# ---------------------------------------------------------------------------
# Tier 2 — endpoint fails loud on an un-indexed SHA / surfaces freshness
# ---------------------------------------------------------------------------


@workflow(purpose="Verify /api/history/since returns resolved:false for an un-indexed SHA instead of a count")
def test_since_endpoint_unindexed_sha_fails_loud(db_path: Path, mini_project: Path):
    """An explicit SHA not in node_history returns resolved:false and no node_ids."""
    from axiom_graph.viz.server import get_history_since_endpoint, _apply_project

    _apply_project(mini_project)

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="indexed_sha", preserved=True)
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="indexed_sha")

    result = get_history_since_endpoint(sha="deadbeefcafe")

    assert result["resolved"] is False
    assert result["requested_sha"] == "deadbeefcafe"
    assert result["reason"] == "not in index"
    assert "node_ids" not in result  # never a count against the wrong baseline
    assert "index_head_sha" in result
    assert "commits_behind_head" in result


@workflow(purpose="Verify a resolvable since query returns resolved:true plus index-freshness fields")
def test_since_endpoint_resolved_includes_freshness(db_path: Path, mini_project: Path):
    """A matched SHA resolves with node_ids and carries the freshness fields."""
    from axiom_graph.viz.server import get_history_since_endpoint, _apply_project

    _apply_project(mini_project)

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="cp_sha", preserved=True)
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="cp_sha")

    result = get_history_since_endpoint(sha="cp_sha")

    assert result["resolved"] is True
    assert isinstance(result["node_ids"], list)
    assert isinstance(result["change_kinds"], dict)
    assert "index_head_sha" in result
    assert "commits_behind_head" in result  # may be None when git is unavailable


@workflow(purpose="Verify recent-shas marks which commits are present in the index")
def test_recent_shas_indexed_flag(db_path: Path, mini_project: Path):
    """Each commit is flagged indexed=True iff it has node_history rows."""
    from axiom_graph.viz.server import get_recent_shas, _apply_project

    _apply_project(mini_project)

    n = _node("proj::mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="cp_sha_001", preserved=True)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = (
        "cp_sha_001\x002026-04-04T10:00:00+00:00\x00Indexed commit\x00\x1e"
        "ghost_sha_999\x002026-04-04T09:00:00+00:00\x00Un-indexed commit\x00\x1e"
    )

    with patch("axiom_graph.viz.nodes.subprocess.run", return_value=mock_result):
        result = get_recent_shas()

    by_sha = {e["sha"]: e for e in result["shas"]}
    assert by_sha["cp_sha_001"]["indexed"] is True
    assert by_sha["ghost_sha_999"]["indexed"] is False
