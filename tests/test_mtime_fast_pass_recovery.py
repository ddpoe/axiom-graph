"""The ``file_mtime`` scan cache: recovery, the freeze rule, and its readers.

``file_mtime`` records a file's on-disk modification time as observed when the
build last scanned its bytes.  These tests cover the three guarantees that
makes it load-bearing: a file rejoins the mtime fast-pass after its mtime
moves, only the builder's scan advances the column, and both readers of the
column agree.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import compute_staleness
from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER

_SKIP_COUNTERS = ("files_skipped_mtime", "docs_skipped_mtime", "config_skipped_mtime", "js_skipped_mtime")


def _bump_mtime() -> None:
    """Let the filesystem clock advance past mtime granularity."""
    time.sleep(0.05)


def _write_all_five_types(root: Path) -> dict[str, Path]:
    """Populate *root* with one file per scanned type and return them by type."""
    (root / "docs").mkdir(exist_ok=True)
    (root / ".claude").mkdir(exist_ok=True)
    (root / "web").mkdir(exist_ok=True)

    files = {
        "python": root / "mod.py",
        "markdown": root / "docs" / "guide.md",
        "docjson": root / "docs" / "spec.json",
        "config": root / ".claude" / "settings.json",
        "js": root / "web" / "app.js",
    }
    files["python"].write_text('def greet():\n    """Say hello."""\n    return "hello"\n', encoding="utf-8")
    files["markdown"].write_text("# Guide\n\n## Setup\n\nInstall it.\n", encoding="utf-8")
    files["docjson"].write_text(
        json.dumps({"title": "Spec", "sections": [{"id": "intro", "heading": "Intro", "content": "Words."}]}),
        encoding="utf-8",
    )
    files["config"].write_text('{"model": "opus"}\n', encoding="utf-8")
    files["js"].write_text("export function add(a, b) { return a + b; }\n", encoding="utf-8")
    (root / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["web/**/*.js"]\n',
        encoding="utf-8",
    )
    return files


def _shift_mtimes_forward(root: Path, seconds: float = 5.0) -> None:
    """Move every source file's mtime forward without touching a byte."""
    future = time.time() + seconds
    for path in root.rglob("*"):
        if path.is_file() and ".axiom_graph" not in path.parts:
            os.utime(path, (future, future))


def _total_skipped(summary: dict) -> int:
    return sum(summary[key] for key in _SKIP_COUNTERS)


# ---------------------------------------------------------------------------
# Tier 3 — a content-neutral mtime shift costs one rebuild, not every rebuild
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="JS/TS scanning requires the js extra")
@workflow(
    purpose=(
        "A branch switch or fresh worktree checkout moves file mtimes without changing bytes; "
        "the build after that re-scans, and every build after that skips again — repeatably."
    ),
)
def test_fast_pass_recovers_after_content_neutral_mtime_shift(mini_project: Path):
    口 = Step(
        step_num=1,
        name="Index a project covering every scanned file type",
        purpose="Establish stored mtimes for Python, Markdown, DocJSON, config, and JS files",
    )
    _write_all_five_types(mini_project)
    builder.build(mini_project, project_id="proj")

    口 = Step(
        step_num=2,
        name="Move every mtime forward without editing a byte",
        purpose="Simulate git worktree add / branch switch / rebase rewriting the working tree",
    )
    _shift_mtimes_forward(mini_project)

    口 = Step(
        step_num=3,
        name="Build once — the moved mtimes cannot be trusted, so everything re-scans",
        purpose="A file whose mtime moved must be re-read; the build cannot know the bytes are the same",
        outputs="A build that skips nothing",
    )
    after_shift = builder.build(mini_project, project_id="proj")
    assert _total_skipped(after_shift) == 0

    口 = Step(
        step_num=4,
        name="Build again — the fast-pass is back",
        purpose="The previous build recorded what it read, so every file type is now skippable",
        outputs="Non-zero skip counters for all five scanned types",
    )
    recovered = builder.build(mini_project, project_id="proj")
    assert recovered["files_skipped_mtime"] >= 1
    assert recovered["docs_skipped_mtime"] == 2
    assert recovered["config_skipped_mtime"] == 1
    assert recovered["js_skipped_mtime"] == 1

    口 = Step(
        step_num=5,
        name="Shift the mtimes a second time and confirm recovery repeats",
        purpose="Run the whole shift-and-rebuild cycle a second time",
        critical="Recovery must not be a one-shot that only works on the first shift",
    )
    _shift_mtimes_forward(mini_project, seconds=10.0)
    assert _total_skipped(builder.build(mini_project, project_id="proj")) == 0
    assert _total_skipped(builder.build(mini_project, project_id="proj")) > 0


# ---------------------------------------------------------------------------
# Tier 3 — an edit costs one rebuild per file type, not every rebuild
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="JS/TS scanning requires the js extra")
@workflow(
    purpose=(
        "Editing a file must not drop it out of the mtime fast-pass forever: after the build "
        "that picks the edit up, the next build skips it again — for every scanned file type."
    ),
)
def test_edited_files_rejoin_the_fast_pass_for_every_scanned_type(mini_project: Path):
    口 = Step(
        step_num=1,
        name="Index a project covering every scanned file type",
        purpose="Establish stored mtimes for Python, Markdown, DocJSON, config, and JS files",
    )
    files = _write_all_five_types(mini_project)
    builder.build(mini_project, project_id="proj")

    口 = Step(
        step_num=2,
        name="Edit one file of each type",
        purpose="Every type must be exercised — a fix that lands in one scanner and misses the others fails here",
    )
    _bump_mtime()
    files["python"].write_text('def greet():\n    """Say hello."""\n    return "goodbye"\n', encoding="utf-8")
    files["markdown"].write_text("# Guide\n\n## Setup\n\nInstall it twice.\n", encoding="utf-8")
    files["docjson"].write_text(
        json.dumps({"title": "Spec", "sections": [{"id": "intro", "heading": "Intro", "content": "More words."}]}),
        encoding="utf-8",
    )
    files["config"].write_text('{"model": "sonnet"}\n', encoding="utf-8")
    files["js"].write_text("export function add(a, b) { return b + a; }\n", encoding="utf-8")

    口 = Step(
        step_num=3,
        name="Build in the default discovery-only mode",
        purpose="The mode every CLI and MCP build uses — the edits are picked up, the baseline stays frozen",
    )
    picked_up = builder.build(mini_project, project_id="proj")
    assert _total_skipped(picked_up) == 0

    口 = Step(
        step_num=4,
        name="Build again with no further edits",
        purpose="Nothing has changed since the previous build, so the fast-pass should fire",
        outputs="Every edited file is skipped the second time",
    )
    settled = builder.build(mini_project, project_id="proj")
    assert settled["files_skipped_mtime"] >= 1
    assert settled["docs_skipped_mtime"] == 2
    assert settled["config_skipped_mtime"] == 1
    assert settled["js_skipped_mtime"] == 1


# ---------------------------------------------------------------------------
# Tier 3 — a faster build never costs a missed change
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "A file edited but not yet verified carries a current stored mtime, so the staleness "
        "fast-pass fires on it — and the content gate still reports the drift as CONTENT_UPDATED."
    ),
)
def test_edited_file_still_reports_content_updated_once_its_mtime_is_current(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="Index a Python file to set the hash baseline",
        purpose="A full build records code_hash / desc_hash as the staleness baseline",
    )
    src = mini_project / "mod.py"
    src.write_text('def greet():\n    """Say hello."""\n    return "hello"\n', encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=2,
        name="Edit the body so it diverges from the indexed baseline",
        purpose="Create real drift for the staleness computation to find",
    )
    _bump_mtime()
    src.write_text('def greet():\n    """Say hello."""\n    return "goodbye"\n', encoding="utf-8")

    口 = Step(
        step_num=3,
        name="Build, then confirm the stored mtime now matches the file on disk",
        purpose="Arm the staleness fast-pass so the content gate is the only thing left standing",
        critical="This is what arms the fast-pass — without it the rest of the test proves nothing",
    )
    builder.build(mini_project, project_id="proj")
    assert db.get_file_mtime(db_path, "mod.py") == pytest.approx(src.stat().st_mtime)

    口 = Step(
        step_num=4,
        name="Compute staleness and assert the drift is still reported",
        purpose="The content gate hashes the file, mismatches the baseline, and falls through to the per-node ladder",
        outputs="greet() own_status == CONTENT_UPDATED",
    )
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)
    greet = [n for n in nodes if n.title == "greet"][0]
    assert statuses[greet.id][0] == "CONTENT_UPDATED"


# ---------------------------------------------------------------------------
# Tier 2 — only a full per-file index pass may advance the column
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "The read path's single-file rescan re-derives structural metadata but discards edges, "
        "so it must leave file_mtime frozen and let the next full build re-scan the file."
    ),
)
def test_read_path_rescan_does_not_advance_the_stored_mtime(mini_project: Path, db_path: Path):
    src = mini_project / "mod.py"
    src.write_text('def greet():\n    """Say hello."""\n    return "hello"\n', encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    mtime_before = db.get_file_mtime(db_path, "mod.py")
    assert mtime_before is not None

    _bump_mtime()
    src.write_text(
        'def helper():\n    """I push greet down."""\n    return 42\n\n\n'
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )

    stale_node = [n for n in db.all_nodes(db_path) if n.title == "greet"][0]
    assert builder.rescan_file_if_needed(db_path, mini_project, stale_node)

    assert db.get_file_mtime(db_path, "mod.py") == mtime_before


@workflow(
    purpose=(
        "A build whose scanned-set-gated passes failed has not completed the per-file index "
        "pass, so it must leave the files it read out of the fast-pass for the next build to retry."
    ),
)
def test_a_build_whose_per_file_pass_failed_leaves_the_files_unstamped(mini_project: Path, monkeypatch):
    docs = mini_project / "docs"
    docs.mkdir()
    (docs / "spec.json").write_text(
        json.dumps({"title": "Spec", "sections": [{"id": "intro", "heading": "Intro", "content": "Words."}]}),
        encoding="utf-8",
    )
    (mini_project / "mod.py").write_text('def greet():\n    """Say hello."""\n    return "hello"\n', encoding="utf-8")
    builder.build(mini_project, project_id="proj")
    _shift_mtimes_forward(mini_project)

    def _locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(builder.db, "get_outbound_documents_targets_conn", _locked)
    failed = builder.build(mini_project, project_id="proj")
    assert any("documents-edge reconciliation failed" in w for w in failed["warnings"])
    assert failed["file_mtimes_stamped"] == 0

    monkeypatch.undo()
    retried = builder.build(mini_project, project_id="proj")
    assert _total_skipped(retried) == 0, "the failed pass must be retried, not skipped past"
    assert retried["file_mtimes_stamped"] > 0

    # The guard releases as soon as the pass succeeds — it must not be a
    # one-way switch that leaves the fast-pass off for good.
    assert _total_skipped(builder.build(mini_project, project_id="proj")) > 0


# ---------------------------------------------------------------------------
# Tier 2 — the point reader and the bulk reader agree
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "A Markdown file stores its mtime on the file node and on every section node; the "
        "per-location lookup and the batch lookup must select the same row for that location."
    ),
)
def test_both_mtime_readers_agree_on_a_location_stored_on_several_rows(mini_project: Path, db_path: Path):
    docs = mini_project / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# Guide\n\n## Setup\n\nInstall it.\n\n## Usage\n\nRun it.\n\n## Tips\n\nRead it.\n",
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj")

    location = "docs/guide.md"
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, file_mtime FROM nodes WHERE location = ? AND file_mtime IS NOT NULL",
            (location,),
        ).fetchall()
        assert len(rows) > 1, "expected the file node and its section nodes to each carry a stored mtime"
        # Leave one row behind so the two readers have something to disagree about.
        conn.execute(
            "UPDATE nodes SET file_mtime = ? WHERE id = ?",
            (rows[0]["file_mtime"] - 100.0, rows[0]["id"]),
        )

    assert db.get_file_mtime(db_path, location) == db.get_all_file_mtimes(db_path)[location]


# ---------------------------------------------------------------------------
# Tier 2 — status parity through the batched mtime lookup
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Reading stored mtimes in one batch instead of once per location must not change any "
        "status: unchanged, edited, and deleted files still resolve VERIFIED / CONTENT_UPDATED / NOT_FOUND."
    ),
)
def test_staleness_statuses_survive_the_batched_mtime_lookup(mini_project: Path, db_path: Path):
    untouched = mini_project / "untouched.py"
    edited = mini_project / "edited.py"
    removed = mini_project / "removed.py"
    untouched.write_text('def stay():\n    """Stay put."""\n    return 1\n', encoding="utf-8")
    edited.write_text('def drift():\n    """Drift away."""\n    return 1\n', encoding="utf-8")
    removed.write_text('def vanish():\n    """Vanish."""\n    return 1\n', encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    _bump_mtime()
    edited.write_text('def drift():\n    """Drift away."""\n    return 2\n', encoding="utf-8")
    removed.unlink()

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    by_title = {n.title: n for n in nodes}
    assert statuses[by_title["stay"].id][0] == "VERIFIED"
    assert statuses[by_title["drift"].id][0] == "CONTENT_UPDATED"
    assert statuses[by_title["vanish"].id][0] == "NOT_FOUND"
