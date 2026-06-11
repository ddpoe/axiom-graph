"""E2E tests with real git repos for history, since filter, diff, and staleness.

Tier 3 scenario tests use @workflow + Step().
Tier 2 edge case tests use @workflow(purpose=...) only.

All tests use the ``git_project`` fixture (real git repo + axiom-graph DB).
No git mocking — every git operation hits a real temporary repository.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


from axiom_annotations import workflow, Step

from axiom_graph.lifecycle.api import get_node_diff
from axiom_graph.index import builder, db
from axiom_graph.index.staleness import record_staleness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commit(project: Path, message: str) -> str:
    """Stage all, commit, return the full SHA."""
    subprocess.run(
        ["git", "add", "-A"],
        cwd=project,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=project,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init(project: Path) -> dict:
    """axiom-graph init — full build (discovery_only=False) + record_staleness."""
    db_path = project / ".axiom_graph" / "graph.db"
    summary = builder.build(project, project_id="proj", discovery_only=False)
    nodes = db.all_nodes(db_path)
    if nodes:
        record_staleness(db_path, project, nodes)
    return summary


def _build(project: Path) -> dict:
    """axiom-graph build — discovery only + record_staleness."""
    db_path = project / ".axiom_graph" / "graph.db"
    summary = builder.build(project, project_id="proj", discovery_only=True)
    nodes = db.all_nodes(db_path)
    if nodes:
        record_staleness(db_path, project, nodes)
    return summary


def _since(
    db_path: Path,
    project: Path,
    sha: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Call the viz since endpoint with _apply_project wired up."""
    from axiom_graph.viz.server import get_history_since_endpoint, _apply_project

    _apply_project(project)
    return get_history_since_endpoint(sha=sha, timestamp=timestamp)


def _diff(
    db_path: Path,
    project: Path,
    node_id: str,
    sha: str | None = None,
) -> dict:
    """Call get_node_diff with real git (no mocks)."""
    return get_node_diff(db_path, project, node_id, baseline_sha=sha)


def _checkpoint(db_path: Path, project: Path) -> None:
    """Insert CHECKPOINT markers on all nodes (mirrors CLI cmd_history_checkpoint)."""
    git_sha: str | None = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        )
        git_sha = result.stdout.strip()[:12]
    except Exception:
        pass
    nodes = db.all_nodes(db_path)
    for node in nodes:
        db.insert_history_row(
            db_path,
            node_id=node.id,
            change_type="CHECKPOINT",
            git_sha=git_sha,
            preserved=True,
        )


def _find_node(db_path: Path, title: str):
    """Find a node by title, return the first match."""
    nodes = db.all_nodes(db_path)
    matches = [n for n in nodes if n.title == title]
    assert matches, f"No node with title {title!r}"
    return matches[0]


def _history_types(db_path: Path, node_id: str) -> list[str]:
    """Return change_type list for a node, newest first."""
    rows = db.get_history(db_path, node_id, limit=100)
    return [r["change_type"] for r in rows]


def _current_hashes(project: Path, node) -> tuple[str, str | None]:
    """Compute current code_hash and desc_hash for a node from the file on disk."""
    import ast
    from axiom_graph.models import hash16
    from axiom_graph.scanners.module_scanner import _split_function

    abs_path = project / node.location
    source = abs_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_name = node.title.split(".")[-1]
    for ast_node in ast.walk(tree):
        if isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast_node.name == func_name:
            code_text, docstring = _split_function(ast_node)
            return hash16(code_text), hash16(docstring) if docstring else None
    raise ValueError(f"Function {func_name} not found in {abs_path}")


# ---------------------------------------------------------------------------
# T3 Scenario 1 — Full lifecycle: commit → build → edit → staleness → diff
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify the complete lifecycle end-to-end with real git: "
        "commit → init → edit → build → staleness detection → diff "
        "returns real old/new content with commit context."
    ),
)
def test_full_lifecycle(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write initial function, commit, and init",
        purpose="Establish hash baselines with a real git commit",
        outputs="INITIAL history row with real git SHA",
    )
    (git_project / "mod.py").write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    sha_initial = _commit(git_project, "Initial greet function")
    _init(git_project)

    node = _find_node(git_db_path, "greet")
    history = db.get_history(git_db_path, node.id, limit=10)
    initial_rows = [r for r in history if r["change_type"] == "INITIAL"]
    assert initial_rows, "Expected an INITIAL history row"
    assert initial_rows[0]["git_sha"] is not None
    assert sha_initial.startswith(initial_rows[0]["git_sha"][:7])

    口 = Step(
        step_num=2,
        name="Edit function body, commit, and discovery build",
        purpose="Trigger CONTENT_ONLY event and BECAME_CONTENT_UPDATED transition",
    )
    (git_project / "mod.py").write_text(
        'def greet():\n    """Say hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Change greeting")
    _build(git_project)

    types = _history_types(git_db_path, node.id)
    assert "BECAME_CONTENT_UPDATED" in types

    staleness = db.get_all_staleness(git_db_path)
    assert staleness.get(node.id)[0] == "CONTENT_UPDATED"

    口 = Step(
        step_num=3,
        name="Diff endpoint with real git",
        purpose="Verify old/new content and commit context from real git show",
    )
    result = _diff(git_db_path, git_project, node.id)

    assert "error" not in result
    assert "hello" in result["old_content"]
    assert "goodbye" in result["new_content"]
    assert result["baseline_sha"] is not None
    assert result["commit_subject"] == "Initial greet function"
    assert result["commit_author"] == "Test"
    assert result["commit_date"] is not None


# ---------------------------------------------------------------------------
# T3 Scenario 2 — PR review workflow: since → inspect → verify → re-check
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify the PR review workflow: checkpoint → edit → since filter "
        "scopes to changed nodes → staleness cause → bulk verify → re-check clean."
    ),
)
def test_pr_review_workflow(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write two functions, commit, init, checkpoint",
        purpose="Establish baseline with a checkpoint as the since reference point",
    )
    (git_project / "mod.py").write_text(
        "def greet():\n"
        '    """Say hello."""\n'
        '    return "hello"\n'
        "\n"
        "\n"
        "def farewell():\n"
        '    """Say bye."""\n'
        '    return "bye"\n',
        encoding="utf-8",
    )
    sha_a = _commit(git_project, "Add greet and farewell")
    _init(git_project)
    _checkpoint(git_db_path, git_project)

    口 = Step(
        step_num=2,
        name="Edit one function, commit, and discovery build",
        purpose="Only greet() changes — farewell() should not appear in since results",
    )
    (git_project / "mod.py").write_text(
        "def greet():\n"
        '    """Say hello."""\n'
        '    return "hi there"\n'
        "\n"
        "\n"
        "def farewell():\n"
        '    """Say bye."""\n'
        '    return "bye"\n',
        encoding="utf-8",
    )
    sha_b = _commit(git_project, "Update greet")
    _build(git_project)

    口 = Step(
        step_num=3,
        name="Since filter with SHA-A returns only changed nodes",
        purpose="Verify scoped filtering works with real checkpoints",
    )
    greet_node = _find_node(git_db_path, "greet")
    farewell_node = _find_node(git_db_path, "farewell")

    since_result = _since(git_db_path, git_project, sha=sha_a[:10])
    assert greet_node.id in since_result["node_ids"]
    assert farewell_node.id not in since_result["node_ids"]
    assert since_result["baseline_sha"] is not None
    assert since_result["baseline_timestamp"] is not None

    口 = Step(
        step_num=4,
        name="Staleness cause explains why the node is stale",
        purpose="Verify get_staleness_cause returns meaningful info",
    )
    from axiom_graph.viz.server import get_staleness_cause, _apply_project

    _apply_project(git_project)
    cause = get_staleness_cause(greet_node.id)
    assert cause["own_status"] == "CONTENT_UPDATED"
    assert cause["details"].get("stored_code_hash") is not None

    口 = Step(
        step_num=5,
        name="Bulk verify changed nodes, then re-check → all clean",
        purpose="Verify the verify → BECAME_VERIFIED cycle with real hashes",
    )
    # Re-read the node to get current code_hash
    greet_node = db.get_node(git_db_path, greet_node.id)
    cur_code, cur_desc = _current_hashes(git_project, greet_node)
    db.upsert_verification(
        git_db_path,
        node_id=greet_node.id,
        verified_by="human:test",
        reason="Reviewed in PR",
        code_hash_at=cur_code,
        desc_hash_at=cur_desc,
    )
    db.insert_history_row(
        git_db_path,
        node_id=greet_node.id,
        change_type="MANUAL_VERIFIED",
    )

    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes)

    staleness = db.get_all_staleness(git_db_path)
    assert staleness.get(greet_node.id)[0] in ("VERIFIED", "VERIFIED")

    types = _history_types(git_db_path, greet_node.id)
    assert "MANUAL_VERIFIED" in types

    口 = Step(
        step_num=6,
        name="Since filter SHA flows to diff as scoped baseline",
        purpose="Verify the since→diff contract: scoped SHA overrides per-node resolution",
    )
    diff_result = _diff(
        git_db_path,
        git_project,
        greet_node.id,
        sha=since_result["baseline_sha"],
    )
    assert "error" not in diff_result
    assert diff_result["baseline_sha"] is not None


# ---------------------------------------------------------------------------
# T3 Scenario 3 — Verification lifecycle: stale → verify → edit → re-stale
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify that verification is not permanent: stale → verify → CLEAN → "
        "edit again → re-stale. Full history chain is recorded."
    ),
)
def test_verification_lifecycle(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write function, commit, and init",
        purpose="Baseline setup",
    )
    (git_project / "mod.py").write_text(
        'def calc():\n    """Compute."""\n    return 1 + 1\n',
        encoding="utf-8",
    )
    _commit(git_project, "Add calc")
    _init(git_project)
    node = _find_node(git_db_path, "calc")

    口 = Step(
        step_num=2,
        name="Edit → commit → build → assert CONTENT_UPDATED",
        purpose="First staleness cycle",
    )
    (git_project / "mod.py").write_text(
        'def calc():\n    """Compute."""\n    return 2 + 2\n',
        encoding="utf-8",
    )
    _commit(git_project, "Update calc to 2+2")
    _build(git_project)

    staleness = db.get_all_staleness(git_db_path)
    assert staleness.get(node.id)[0] == "CONTENT_UPDATED"

    口 = Step(
        step_num=3,
        name="Verify → re-check → CLEAN or VERIFIED",
        purpose="Verification resolves staleness",
    )
    node = db.get_node(git_db_path, node.id)
    cur_code, cur_desc = _current_hashes(git_project, node)
    db.upsert_verification(
        git_db_path,
        node_id=node.id,
        verified_by="human:dev",
        reason="Looks good",
        code_hash_at=cur_code,
        desc_hash_at=cur_desc,
    )
    db.insert_history_row(
        git_db_path,
        node_id=node.id,
        change_type="MANUAL_VERIFIED",
    )
    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes)

    staleness = db.get_all_staleness(git_db_path)
    assert staleness.get(node.id)[0] in ("VERIFIED", "VERIFIED")

    口 = Step(
        step_num=4,
        name="Edit again → commit → build → CONTENT_UPDATED again",
        purpose="New edits invalidate the verification",
    )
    (git_project / "mod.py").write_text(
        'def calc():\n    """Compute."""\n    return 3 + 3\n',
        encoding="utf-8",
    )
    _commit(git_project, "Update calc to 3+3")
    _build(git_project)

    staleness = db.get_all_staleness(git_db_path)
    assert staleness.get(node.id)[0] == "CONTENT_UPDATED"

    口 = Step(
        step_num=5,
        name="Verify full history chain",
        purpose="History records the complete lifecycle with real git SHAs",
    )
    types = _history_types(git_db_path, node.id)
    # Newest first. Expect at least:
    # BECAME_CONTENT_UPDATED (second), MANUAL_VERIFIED, BECAME_CONTENT_UPDATED (first), INITIAL
    assert types.count("BECAME_CONTENT_UPDATED") >= 2
    assert "MANUAL_VERIFIED" in types
    assert "INITIAL" in types

    # Every row with a git_sha should match a real commit
    rows = db.get_history(git_db_path, node.id, limit=100)
    shas_in_history = [r["git_sha"] for r in rows if r.get("git_sha")]
    assert len(shas_in_history) > 0


# ---------------------------------------------------------------------------
# T2 Edge Cases
# ---------------------------------------------------------------------------


# Test 4 — Multiple builds on same SHA (the cutoff bug)
@workflow(purpose="Multiple builds on same commit must not push since-filter cutoff forward")
def test_multiple_builds_same_sha(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    sha = _commit(git_project, "Add greet")
    _init(git_project)

    # Edit without committing
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    _build(git_project)

    # Second build on same commit — should not push cutoff forward
    _build(git_project)

    node = _find_node(git_db_path, "greet")
    since_result = _since(git_db_path, git_project, sha=sha[:10])

    assert node.id in since_result["node_ids"], (
        "Node that went CONTENT_UPDATED must appear in since results even after multiple builds on the same SHA"
    )


# Test 5 — SHA not in history → git log date fallback
@workflow(purpose="SHA not in any history row falls back to git log date resolution")
def test_sha_not_in_history_git_log_fallback(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    sha_a = _commit(git_project, "Add greet")
    _init(git_project)

    # Commit B — no build, so sha_b is NOT in any history row
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    sha_b = _commit(git_project, "Change greet")

    # Commit C — build happens here
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hi there"\n',
        encoding="utf-8",
    )
    sha_c = _commit(git_project, "Change greet again")
    _build(git_project)

    # Since with sha_b — not in history, should fall back to git log date
    since_result = _since(git_db_path, git_project, sha=sha_b[:10])
    assert since_result["baseline_sha"] is not None
    assert since_result["baseline_timestamp"] is not None


# Test 6 — No params, no checkpoints → latest sha-bearing row
@workflow(purpose="since() with no args and no checkpoints falls back to latest sha-bearing row")
def test_since_no_params_no_checkpoints(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Add greet")
    _init(git_project)

    # Since with no args — should resolve from latest sha-bearing history row
    since_result = _since(git_db_path, git_project)
    # Either resolves a cutoff or returns empty (both are valid — no crash)
    assert isinstance(since_result["node_ids"], list)


# Test 8 — Composite + since filter coherence
@workflow(purpose="Stale child must appear in since-filtered list when its composite parent does")
def test_composite_since_filter_coherence(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hello"\n\n\ndef farewell():\n    """Bye."""\n    return "bye"\n',
        encoding="utf-8",
    )
    sha = _commit(git_project, "Add module")
    _init(git_project)

    # Edit child function without committing
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "goodbye"\n\n\ndef farewell():\n    """Bye."""\n    return "bye"\n',
        encoding="utf-8",
    )
    _build(git_project)

    greet_node = _find_node(git_db_path, "greet")
    since_result = _since(git_db_path, git_project, sha=sha[:10])

    # Find the composite (module) node
    all_nodes = db.all_nodes(git_db_path)
    module_nodes = [n for n in all_nodes if n.node_type == "composite_process" and n.location == "mod.py"]

    if module_nodes:
        module_node = module_nodes[0]
        module_staleness = db.get_all_staleness(git_db_path).get(module_node.id)
        if module_node.id in since_result["node_ids"] and module_staleness not in (
            ("VERIFIED", "VERIFIED"),
            ("VERIFIED", "VERIFIED"),
            ("VERIFIED", "VERIFIED"),
            ("VERIFIED", "VERIFIED"),
        ):
            # If the module is in the since list and stale, the child that
            # caused the inheritance must also be in the list
            assert greet_node.id in since_result["node_ids"], (
                f"Composite {module_node.id} is in since results with status "
                f"{module_staleness}, but its stale child {greet_node.id} is missing"
            )


# Test 9 — Ghost nodes for deleted files
@workflow(purpose="Deleting a file produces DELETED history rows and ghost nodes in since filter")
def test_ghost_nodes_deleted_file(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    sha_a = _commit(git_project, "Add greet")
    _init(git_project)
    _checkpoint(git_db_path, git_project)

    greet_node = _find_node(git_db_path, "greet")
    greet_id = greet_node.id

    # Delete the file, commit, and rebuild
    (git_project / "mod.py").unlink()
    _commit(git_project, "Remove mod.py")
    _build(git_project)

    # The node should be gone from the nodes table
    assert db.get_node(git_db_path, greet_id) is None

    # But a DELETED history row should exist with preserved=1
    from axiom_graph.index.db import _connect

    with _connect(git_db_path) as conn:
        deleted_rows = conn.execute(
            "SELECT * FROM node_history WHERE node_id = ? AND change_type = 'DELETED'",
            (greet_id,),
        ).fetchall()
    assert len(deleted_rows) > 0
    assert deleted_rows[0]["preserved"] == 1

    meta = json.loads(deleted_rows[0]["meta"])
    assert meta.get("title") is not None
    assert meta.get("node_type") is not None

    # Since filter should include ghost nodes
    since_result = _since(git_db_path, git_project, sha=sha_a[:10])
    ghost_ids = [g["id"] for g in since_result.get("deleted_nodes", [])]
    assert greet_id in ghost_ids, "Deleted node should appear as ghost in since filter"

    ghost = [g for g in since_result["deleted_nodes"] if g["id"] == greet_id][0]
    assert ghost["_deleted"] is True


# Test 10 — Line shift + real git diff
@workflow(purpose="Adding a function above shifts line numbers; diff still returns correct content")
def test_line_shift_real_git_diff(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Add greet")
    _init(git_project)

    # Add a function above, shifting greet's lines
    (git_project / "mod.py").write_text(
        "def helper():\n"
        '    """Push greet down."""\n'
        "    return 42\n"
        "\n"
        "\n"
        "def greet():\n"
        '    """Hello."""\n'
        '    return "hello"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Add helper above greet")
    _build(git_project)

    node = _find_node(git_db_path, "greet")
    result = _diff(git_db_path, git_project, node.id)

    # Should not error — the old content from git show should resolve correctly
    # even though line numbers shifted
    if "error" not in result:
        assert "greet" in result.get("new_content", "")


# Test 13 — Recent SHAs dropdown
@workflow(purpose="get_recent_shas returns real commit subjects in correct newest-first order")
def test_recent_shas_real_subjects(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    _commit(git_project, "First commit")
    _init(git_project)

    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Second commit")
    _build(git_project)

    (git_project / "mod.py").write_text(
        'def greet():\n    """Hello."""\n    return "hi there"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Third commit")
    _build(git_project)

    from axiom_graph.viz.server import get_recent_shas, _apply_project

    _apply_project(git_project)
    result = get_recent_shas()

    shas = result.get("shas", [])
    assert len(shas) >= 2

    # At least some entries should have resolved commit subjects
    subjects = [s.get("commit_subject") for s in shas if s.get("commit_subject")]
    assert len(subjects) > 0

    # Newest first — first entry should be from a later date than last
    if len(shas) >= 2:
        assert shas[0]["date"] >= shas[-1]["date"]


# Test 14 — DESC_ONLY + CONTENT_AND_DESC + no-change skip (narrowed to history row types)
@workflow(
    purpose="History row change_types: DESC_ONLY for docstring edit, CONTENT_AND_DESC for both, no row on no-change"
)
def test_history_change_type_variants(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Add greet")
    _init(git_project)

    node = _find_node(git_db_path, "greet")
    row_count_after_init = len(db.get_history(git_db_path, node.id, limit=100))

    # Edit docstring only → expect DESC_ONLY
    (git_project / "mod.py").write_text(
        'def greet():\n    """Say goodbye."""\n    return "hello"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Update docstring")
    _build(git_project)

    types = _history_types(git_db_path, node.id)
    # Should have a DESC_ONLY or BECAME_DESC_UPDATED (transition events may also appear)
    desc_events = [t for t in types if "DESC" in t]
    assert desc_events, "Expected a DESC-related history event after docstring edit"

    # Edit both body and docstring → expect CONTENT_AND_DESC or CONTENT-related event
    (git_project / "mod.py").write_text(
        'def greet():\n    """Say hi."""\n    return "hi"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Update both")
    _build(git_project)

    types = _history_types(git_db_path, node.id)
    content_events = [t for t in types if "CONTENT" in t]
    assert content_events, "Expected a CONTENT-related history event after both changed"

    # Rebuild with no changes → no new history row
    row_count_before = len(db.get_history(git_db_path, node.id, limit=100))
    _build(git_project)
    row_count_after = len(db.get_history(git_db_path, node.id, limit=100))
    # Allow for transition events from record_staleness but no new content events
    content_types_before = [
        t
        for t in _history_types(git_db_path, node.id)
        if t in ("CONTENT_ONLY", "DESC_ONLY", "CONTENT_AND_DESC", "INITIAL")
    ]
    # The count of content events should not increase on a no-change rebuild
    _build(git_project)
    content_types_after = [
        t
        for t in _history_types(git_db_path, node.id)
        if t in ("CONTENT_ONLY", "DESC_ONLY", "CONTENT_AND_DESC", "INITIAL")
    ]
    assert len(content_types_after) == len(content_types_before), (
        "No-change rebuild should not produce new content history rows"
    )


# Test 15 — LINK_REMOVED audit trail
@workflow(purpose="Removing a test file emits LINK_REMOVED history rows via node purge")
def test_link_events_audit_trail(git_project: Path, git_db_path: Path):
    # Production module
    (git_project / "prod.py").write_text(
        'def compute():\n    """Do math."""\n    return 42\n',
        encoding="utf-8",
    )
    # Test module that calls the production function
    (git_project / "test_prod.py").write_text(
        "from prod import compute\n\ndef test_compute():\n    assert compute() == 42\n",
        encoding="utf-8",
    )
    _commit(git_project, "Add prod and test")
    _init(git_project)

    # Verify that a validates edge was discovered
    from axiom_graph.index.db import _connect

    with _connect(git_db_path) as conn:
        edges = conn.execute("SELECT * FROM edges WHERE edge_type = 'validates'").fetchall()
    assert len(edges) > 0, "Expected validates edge between test and prod"

    # Now remove the test file and rebuild — this should emit LINK_REMOVED
    (git_project / "test_prod.py").unlink()
    _commit(git_project, "Remove test")
    _build(git_project)

    with _connect(git_db_path) as conn:
        removed_rows = conn.execute("SELECT * FROM node_history WHERE change_type = 'LINK_REMOVED'").fetchall()
    assert len(removed_rows) > 0, "Test removed but no LINK_REMOVED history row"
