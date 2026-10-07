"""Incremental ``check``: only what changed is recomputed, and the stored result equals ``check --full``'s."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli
from axiom_graph.index import db
from axiom_graph.lifecycle.api import compute_check_summary, read_annotation_findings
from axiom_graph.lifecycle.mcp_tools import axiom_graph_check

_TOML = '[axiom_graph]\nproject_id = "demo"\n'
_F_ID = "demo::pkg.mod::f"
_SECTION_ID = "demo::docs/guide::f"
_TEST_F_ID = "demo::tests.test_mod::test_f"


def _project(root: Path, unrelated: int = 1) -> Path:
    """A doc section documents ``f``, a test validates it, and *unrelated* modules nothing links to."""
    (root / "axiom-graph.toml").write_text(_TOML, encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    for i in range(unrelated):
        (pkg / f"other{i}.py").write_text(f"def h{i}():\n    return {i}\n", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mod.py").write_text(
        "from pkg.mod import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    docs_dir = root / "docs"
    docs_dir.mkdir()
    doc = {
        "title": "Guide",
        "tags": [],
        "sections": [{"id": "f", "heading": "f", "content": "f returns one.", "links": [{"node_id": _F_ID}]}],
    }
    (docs_dir / "guide.docjson").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(root)])
    assert result.exit_code == 0, result.output
    return root / ".axiom_graph" / "graph.db"


def _stored(db_path: Path) -> dict[str, tuple]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id, own_status, link_status, live_code_hash, live_desc_hash FROM nodes")
        return {r[0]: tuple(r[1:]) for r in rows}


def _full_copy(db_path: Path, root: Path, tmp: Path) -> dict[str, tuple]:
    """What ``check --full`` stores, run on a copy of the index."""
    copy = tmp / "full.db"
    with sqlite3.connect(db_path) as src, sqlite3.connect(copy) as dst:
        src.backup(dst)
    compute_check_summary(copy, root, full=True)
    return _stored(copy)


@workflow(
    purpose="An incremental check after a one-file edit re-hashes only that file, whatever the size of the rest "
    "of the project, and stores exactly what check --full stores"
)
def test_incremental_check_rehashes_only_the_edit_and_stores_what_full_stores(tmp_path: Path) -> None:
    hashed: list[set[str]] = []
    for size in (1, 6):
        root = tmp_path / f"p{size}"
        root.mkdir()
        口 = Step(step_num=1, name="Index and settle", purpose="init, then a first check that records the watermark")
        db_path = _project(root, unrelated=size)
        compute_check_summary(db_path, root)

        口 = Step(step_num=2, name="Edit one function", purpose="change f's body without building")
        (root / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        cs = compute_check_summary(db_path, root)
        assert cs is not None
        hashed.append(set(cs.refresh.files_hashed))

        口 = Step(step_num=3, name="Same result as a full check", purpose="compare every stored status and live hash")
        stored = _stored(db_path)
        assert stored[_F_ID][0] == "CONTENT_UPDATED"
        assert stored[_SECTION_ID][1] == "LINKED_STALE"
        assert stored[_TEST_F_ID][1] == "LINKED_STALE"
        assert _full_copy(db_path, root, tmp_path / f"p{size}") == stored
    assert hashed[0] == hashed[1] == {"pkg/mod.py"}


def test_idle_check_hashes_no_file_and_writes_no_row(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    compute_check_summary(db_path, tmp_path)
    compute_check_summary(db_path, tmp_path)
    cs = compute_check_summary(db_path, tmp_path)
    assert cs is not None
    assert cs.refresh.mode == "idle"
    assert cs.refresh.files_hashed == set()
    assert cs.refresh.rows_written == 0


def test_check_runs_in_full_when_asked_without_a_watermark_or_on_a_stamp_mismatch(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    # The build's own refresh records the watermark; remove it to model an index without one.
    assert compute_check_summary(db_path, tmp_path).refresh.mode != "full"
    with db._connect(db_path) as conn:
        db.delete_index_meta_conn(conn, db.STALENESS_WATERMARK_META_KEY)
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "full"
    assert compute_check_summary(db_path, tmp_path).refresh.mode != "full"
    assert compute_check_summary(db_path, tmp_path, full=True).refresh.mode == "full"
    with db._connect(db_path) as conn:
        db.set_index_meta_conn(conn, db.STALENESS_SCHEME_META_KEY, "h0.r0.cold")
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "full"
    with db._connect(db_path) as conn:
        db.delete_index_meta_conn(conn, db.STALENESS_WATERMARK_META_KEY)
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "full"
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "idle"


def test_summary_counts_match_the_rows_without_loading_them(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    cs = compute_check_summary(db_path, tmp_path)
    assert cs is not None
    assert cs._statuses is None and cs._problems is None
    problems = cs.problem_statuses
    assert cs._statuses is None
    statuses = cs.statuses
    own = {k: sum(1 for o, _l, _v in statuses.values() if o == k) for k in cs.own_counts}
    link = {k: sum(1 for _o, lk, _v in statuses.values() if lk == k) for k in cs.link_counts}
    assert own == cs.own_counts
    assert link == cs.link_counts
    assert cs.clean_count == sum(1 for o, lk, _v in statuses.values() if o == lk == "VERIFIED")
    assert problems == {nid: t for nid, t in statuses.items() if t[0] != "VERIFIED" or t[1] != "VERIFIED"}
    assert statuses[_SECTION_ID] == ("VERIFIED", "LINKED_STALE", [_F_ID])
    assert list(cs.ordered_ids) == list(db.get_all_staleness(db_path))


def test_check_reports_structure_the_index_lacks_in_text_json_and_mcp(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    runner = CliRunner()
    text = runner.invoke(cli, ["check", str(tmp_path), "--full"])
    assert text.exit_code == 0, text.output
    assert "pkg/mod.py has 1 new function — run build" in text.output.splitlines()

    payload = json.loads(runner.invoke(cli, ["check", str(tmp_path), "--format", "json"]).output)
    assert payload["structural_changes"] == {"pkg/mod.py": {"new": ["g"], "missing": []}}
    assert "pkg/mod.py has 1 new function — run build" in axiom_graph_check(str(tmp_path)).splitlines()

    assert runner.invoke(cli, ["build", str(tmp_path)]).exit_code == 0
    compute_check_summary(db_path, tmp_path)
    assert "structural_changes" in json.loads(runner.invoke(cli, ["check", str(tmp_path), "--format", "json"]).output)
    assert compute_check_summary(db_path, tmp_path).structure == {}
    assert "run build" not in axiom_graph_check(str(tmp_path), full=True)


def test_annotation_findings_rescan_the_files_discovery_saw_change_since_the_parse(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    cs = compute_check_summary(db_path, tmp_path, full=True)
    observed = cs.refresh.observed
    with db._connect(db_path) as conn:
        db.record_parsed_files_conn(conn, {loc: obs.as_record() for loc, obs in observed.items()})
    mod = tmp_path / "pkg" / "mod.py"
    st = mod.stat()
    os.utime(mod, (st.st_atime, st.st_mtime + 50))

    touched = compute_check_summary(db_path, tmp_path).refresh.observed
    assert read_annotation_findings(db_path, tmp_path, observed=touched).files_rescanned == 0
    assert read_annotation_findings(db_path, tmp_path).files_rescanned == 1

    mod.write_text("def f():\n    return 3\n", encoding="utf-8")
    edited = compute_check_summary(db_path, tmp_path).refresh.observed
    assert read_annotation_findings(db_path, tmp_path, observed=edited).files_rescanned == 1


@workflow(
    purpose="An idle check hashes nothing: after an edit leaves a function CONTENT_UPDATED and a build, the second "
    "of two checks re-hashes no file, neither the edited module nor an anchor-only __init__.py, and stores what the "
    "first stored"
)
def test_second_idle_check_hashes_nothing_beside_a_content_updated_node(tmp_path: Path) -> None:
    # Edit and build: f stays CONTENT_UPDATED after the build.
    db_path = _project(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    assert CliRunner().invoke(cli, ["build", str(tmp_path)]).exit_code == 0

    # Two checks: the second sees both files and re-hashes neither.
    compute_check_summary(db_path, tmp_path)
    first = _stored(db_path)
    assert first[_F_ID][0] == "CONTENT_UPDATED"
    second = compute_check_summary(db_path, tmp_path)
    assert {"pkg/mod.py", "pkg/__init__.py"} <= set(second.refresh.observed)
    assert second.refresh.files_hashed == set()
    assert _stored(db_path) == first


@workflow(
    purpose="Structure is reported, never changed: a function added without a build is named by check and by a "
    "refreshing drift_query, with node and edge counts unchanged; a deleted function reads NOT_FOUND and is named"
)
def test_structure_is_reported_by_check_and_drift_query_and_never_changed(tmp_path: Path) -> None:
    from axiom_graph.query.api import compute_drift_query  # noqa: PLC0415

    def _counts() -> tuple[int, ...]:
        with sqlite3.connect(db_path) as conn:
            return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("nodes", "edges"))

    # Add g with no build: the index lacks g.
    db_path = _project(tmp_path)
    compute_check_summary(db_path, tmp_path)
    before = _counts()
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")

    # Reported: check and drift_query name the new function; nothing is added.
    assert compute_check_summary(db_path, tmp_path).structure == {"pkg/mod.py": {"new": ["g"], "missing": []}}
    assert "[pkg/mod.py has 1 new function — run build]" in compute_drift_query(db_path, tmp_path).splitlines()
    assert _counts() == before

    # Delete f: f reads NOT_FOUND and the structure names the missing function.
    (tmp_path / "pkg" / "mod.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    cs = compute_check_summary(db_path, tmp_path)
    assert cs.structure == {"pkg/mod.py": {"new": ["g"], "missing": [_F_ID]}}
    assert _stored(db_path)[_F_ID][0] == "NOT_FOUND"
    assert _counts() == before


@workflow(
    purpose="A purged function is no longer reported as missing: after the NOT_FOUND functions of unchanged files "
    "are purged, check's text, its JSON structural_changes and the MCP check drop them, and a new function in the "
    "same file is still reported"
)
def test_purged_functions_leave_the_structural_lines(tmp_path: Path) -> None:
    from axiom_graph.lifecycle.api import purge_nodes  # noqa: PLC0415

    g_id = "demo::pkg.mod::g"
    h0_id = "demo::pkg.other0::h0"

    # Index g beside f, then remove g, and swap h0 for an unindexed k0.
    db_path = _project(tmp_path)
    mod = tmp_path / "pkg" / "mod.py"
    mod.write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(cli, ["build", str(tmp_path)]).exit_code == 0
    compute_check_summary(db_path, tmp_path)
    mod.write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "other0.py").write_text("def k0():\n    return 0\n", encoding="utf-8")
    assert compute_check_summary(db_path, tmp_path).structure == {
        "pkg/mod.py": {"new": [], "missing": [g_id]},
        "pkg/other0.py": {"new": ["k0"], "missing": [h0_id]},
    }

    # Purge the two removed functions; neither file changes afterwards.
    assert all(r.purged for r in purge_nodes(db_path, tmp_path, [g_id, h0_id], "removed", actor="human"))

    # Every surface drops them and keeps the new function.
    remaining = {"pkg/other0.py": {"new": ["k0"], "missing": []}}
    assert compute_check_summary(db_path, tmp_path).structure == remaining
    text = runner.invoke(cli, ["check", str(tmp_path)]).output
    assert [line for line in text.splitlines() if "run build" in line] == [
        "pkg/other0.py has 1 new function — run build"
    ]
    payload = json.loads(runner.invoke(cli, ["check", str(tmp_path), "--format", "json"]).output)
    assert payload["structural_changes"] == remaining
    mcp = axiom_graph_check(str(tmp_path))
    assert "pkg/other0.py has 1 new function — run build" in mcp.splitlines()
    assert "no longer found" not in mcp


@workflow(
    purpose="check --full through the CLI and through axiom_graph_check(full=True) hashes every tracked file and "
    "prints what a plain check prints; a frozen_tags change makes the next plain check run in full once and record "
    "the new scheme stamp"
)
def test_full_check_through_every_surface_and_a_config_stamp_change(tmp_path: Path) -> None:
    # Full through the api: every tracked file is hashed.
    db_path = _project(tmp_path)
    cs = compute_check_summary(db_path, tmp_path, full=True)
    assert cs.refresh.mode == "full"
    assert cs.refresh.files_hashed == set(cs.refresh.observed)

    # Same output: the CLI and the MCP tool print a full check as a plain one.
    runner = CliRunner()
    plain = runner.invoke(cli, ["check", str(tmp_path)])
    full = runner.invoke(cli, ["check", str(tmp_path), "--full"])
    assert plain.exit_code == full.exit_code == 0
    assert full.output == plain.output
    assert axiom_graph_check(str(tmp_path), full=True) == axiom_graph_check(str(tmp_path))

    # Config stamp: a frozen_tags change runs one full check, then incremental.
    with db._connect(db_path) as conn:
        stamp = db.get_index_meta_conn(conn, db.STALENESS_SCHEME_META_KEY)
    (tmp_path / "axiom-graph.toml").write_text(
        _TOML + '\n[axiom_graph.staleness]\nfrozen_tags = ["archived"]\n', encoding="utf-8"
    )
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "full"
    with db._connect(db_path) as conn:
        assert db.get_index_meta_conn(conn, db.STALENESS_SCHEME_META_KEY) not in (None, stamp)
    assert compute_check_summary(db_path, tmp_path).refresh.mode != "full"


def _data_version(db_path: Path) -> int:
    """The DB's data version as another connection sees it: it moves when any other connection commits a write."""
    with sqlite3.connect(db_path) as conn:
        return conn.execute("PRAGMA data_version").fetchone()[0]


def test_check_after_a_changing_check_is_idle(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    compute_check_summary(db_path, tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "incremental"
    with sqlite3.connect(db_path) as conn, sqlite3.connect(db_path) as probe:
        before = probe.execute("PRAGMA data_version").fetchone()[0]
        cs = compute_check_summary(db_path, tmp_path)
        assert probe.execute("PRAGMA data_version").fetchone()[0] == before
        assert conn.execute("SELECT 1").fetchone() == (1,)
    assert cs.refresh.mode == "idle"
    assert _stored(db_path) == _full_copy(db_path, tmp_path, tmp_path)


def test_watermark_stays_before_a_row_another_writer_added_among_the_passes_rows(tmp_path: Path) -> None:
    from axiom_graph.index.refresh import _watermark_after  # noqa: PLC0415

    db_path = _project(tmp_path)
    with db._connect(db_path) as conn:
        last = db.max_history_id_conn(conn)
        ids = []
        for change in ("BECAME_CONTENT_UPDATED", "VERIFIED", "BECAME_LINKED_STALE"):
            cur = conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, preserved) VALUES (?, '2026', ?, 0)",
                (_F_ID, change),
            )
            ids.append(cur.lastrowid)
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type, preserved) VALUES (?, '2026', 'CHECKPOINT', 0)",
            (_F_ID,),
        )
        own = [ids[0], ids[2]]
        assert _watermark_after(conn, last, own) == last
        assert _watermark_after(conn, last, ids) == ids[2]
        assert _watermark_after(conn, last, []) == last


def test_check_after_a_refresh_that_consumed_the_deletion_log_writes_nothing(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    mod = tmp_path / "pkg" / "mod.py"
    mod.write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    assert CliRunner().invoke(cli, ["build", str(tmp_path)]).exit_code == 0
    mod.write_text("def f():\n    return 1\n", encoding="utf-8")
    compute_check_summary(db_path, tmp_path)
    purged = CliRunner().invoke(cli, ["purge", "demo::pkg.mod::g", str(tmp_path)])
    assert purged.exit_code == 0, purged.output
    # This check reads the deletion log, stores its mark and drops the rows it read.
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "incremental"
    with db._connect(db_path) as conn:
        mark = db.get_index_meta_conn(conn, db.DELETION_MARK_META_KEY)
        assert int(mark) > 0
        assert conn.execute("SELECT COUNT(*) FROM node_deletion_log").fetchone()[0] == 0

    before = _data_version(db_path)
    with sqlite3.connect(db_path) as probe:
        start = probe.execute("PRAGMA data_version").fetchone()[0]
        cs = compute_check_summary(db_path, tmp_path)
        assert probe.execute("PRAGMA data_version").fetchone()[0] == start
    assert cs.refresh.mode == "idle"
    assert before == _data_version(db_path)
    with db._connect(db_path) as conn:
        assert db.get_index_meta_conn(conn, db.DELETION_MARK_META_KEY) == mark


_NOTES_ID = "demo::docs/notes.md"
_INTRO_ID = f"{_NOTES_ID}#intro"
_USAGE_ID = f"{_NOTES_ID}#usage"


_NOTES_TEXT = "# Notes\n\n## Intro\n\nThe introduction.\n\n## Usage\n\nCall it with two numbers.\n"


def _markdown_project(root: Path, text: str = _NOTES_TEXT) -> Path:
    """A project whose only document is a Markdown file with two H2 sections, indexed by ``init``."""
    (root / "axiom-graph.toml").write_text(_TOML, encoding="utf-8")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    (docs_dir / "notes.md").write_text(text, encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(root)])
    assert result.exit_code == 0, result.output
    return root / ".axiom_graph" / "graph.db"


def _markdown_statuses(db_path: Path, root: Path) -> dict[str, tuple[str, str]]:
    """Own and link status of the Markdown document and its sections after a check."""
    statuses = compute_check_summary(db_path, root).statuses
    return {nid: statuses[nid][:2] for nid in (_NOTES_ID, _INTRO_ID, _USAGE_ID)}


@workflow(
    purpose="A Markdown document stays VERIFIED through init and check --full, and an edit to one of its "
    "sections reads CONTENT_UPDATED on that section alone"
)
def test_markdown_sections_stay_verified_through_a_full_check_and_an_edit_flags_only_its_section(
    tmp_path: Path,
) -> None:
    db_path = _markdown_project(tmp_path)
    full = CliRunner().invoke(cli, ["check", str(tmp_path), "--full"])
    assert full.exit_code == 0, full.output
    assert "NOT_FOUND" not in full.output.replace("0 NOT_FOUND", "")
    assert "no longer found" not in full.output
    verified = ("VERIFIED", "VERIFIED")
    assert _markdown_statuses(db_path, tmp_path) == {_NOTES_ID: verified, _INTRO_ID: verified, _USAGE_ID: verified}

    notes = tmp_path / "docs" / "notes.md"
    notes.write_text(notes.read_text(encoding="utf-8").replace("two numbers", "three numbers"), encoding="utf-8")
    statuses = _markdown_statuses(db_path, tmp_path)
    assert statuses[_USAGE_ID][0] == "CONTENT_UPDATED"
    assert statuses[_INTRO_ID] == verified
    assert all(own != "NOT_FOUND" for own, _link in statuses.values())
    assert compute_check_summary(db_path, tmp_path).structure == {}


@workflow(purpose="An edit after a code fence in one Markdown section flags that section and not the one after it")
def test_markdown_edit_after_a_code_fence_flags_only_its_section(tmp_path: Path) -> None:
    fenced = "# Notes\n\n## Intro\n\n```\nrun()\n```\n\nIntro tail.\n\n## Usage\n\nCall it with two numbers.\n"
    db_path = _markdown_project(tmp_path, fenced)
    notes = tmp_path / "docs" / "notes.md"
    notes.write_text(fenced.replace("Intro tail.", "Intro tail, edited."), encoding="utf-8")

    statuses = _markdown_statuses(db_path, tmp_path)
    assert statuses[_INTRO_ID][0] == "CONTENT_UPDATED"
    assert statuses[_USAGE_ID] == ("VERIFIED", "VERIFIED")


def test_hashing_scheme_change_heals_markdown_sections_a_failed_rehash_left_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    from axiom_graph.scanners import doc_scanner, node_hashing  # noqa: PLC0415

    db_path = _markdown_project(tmp_path)

    # Under an earlier scheme the hasher could not read Markdown: a full re-hash leaves the sections NOT_FOUND.
    def unreadable(*_args, **_kwargs):
        raise ValueError("not a DocJSON document")

    monkeypatch.setattr(node_hashing, "HASHING_SCHEME", "1")
    monkeypatch.setattr(doc_scanner, "scan_markdown_file", unreadable)
    assert compute_check_summary(db_path, tmp_path, full=True).refresh.mode == "full"
    assert _markdown_statuses(db_path, tmp_path)[_INTRO_ID][0] == "NOT_FOUND"

    # The unchanged file is not re-hashed while the scheme stays the same, so the NOT_FOUND persists.
    monkeypatch.undo()
    monkeypatch.setattr(node_hashing, "HASHING_SCHEME", "1")
    assert _markdown_statuses(db_path, tmp_path)[_INTRO_ID][0] == "NOT_FOUND"

    # The current scheme differs from the stored stamp: the next check re-hashes in full and heals the sections.
    monkeypatch.undo()
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "full"
    verified = ("VERIFIED", "VERIFIED")
    assert _markdown_statuses(db_path, tmp_path) == {_NOTES_ID: verified, _INTRO_ID: verified, _USAGE_ID: verified}
    assert compute_check_summary(db_path, tmp_path).structure == {}
