"""Behavioural tests for carrying a merged worktree's verifications into the main index.

A worktree is simulated the way a merge is: copy the project, copy its index
in with ``checkout_db``, work and verify in the copy, then copy the changed
files back and build the original.  The carry runs from the original
through ``axiom_graph.lifecycle.api`` (and through the CLI command and the
registered MCP tool), and statuses are read back from the stored index.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli_main
from axiom_graph.docjson.api import axiom_graph_add_link, axiom_graph_patch_section, axiom_graph_write_doc
from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import (
    CarryForwardRefusedError,
    build_index,
    carry_forward_verifications,
    checkout_db,
    compute_check_summary,
    mark_clean_nodes,
    render_carry_forward_report,
    reverify_nodes,
)
from axiom_graph.mcp.server import mcp
from tests.fixtures.full_recompute import assert_matches_full_recompute

FOO = "proj::src.mod::foo"
BAR = "proj::src.mod::bar"
MODULE = "proj::src.mod"
TEST = "proj::tests.test_mod::test_foo"
SECTION = "proj::docs/spec::overview"
GUIDE_A1 = "proj::docs/guide::a1"
NOTES_B1 = "proj::docs/notes::b1"

_TRANSITIVE_CONFIG = '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n'

_MOD = "def foo():\n    return {value}\n\n\ndef bar():\n    return 1\n"
_TEST = "from src.mod import foo\n\n\ndef test_foo():\n    assert foo() is not None\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _set_foo(root: Path, value: int) -> None:
    time.sleep(0.02)
    _write(root / "src" / "mod.py", _MOD.format(value=value))


def _build(root: Path) -> None:
    build_index(_db_path(str(root)), root)


def _statuses(root: Path, *node_ids: str) -> dict[str, tuple[str, str]]:
    with sqlite3.connect(_db_path(str(root))) as conn:
        rows = conn.execute(
            f"SELECT id, own_status, link_status FROM nodes WHERE id IN ({','.join('?' * len(node_ids))})", node_ids
        ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _verification_rows(root: Path, node_id: str) -> list[dict]:
    rows = db.get_history(_db_path(str(root)), node_id, limit=100)
    return [r for r in rows if r["change_type"] in ("AGENT_VERIFIED", "MANUAL_VERIFIED")]


def _index_snapshot(root: Path) -> tuple:
    """Every verification record, recorded pair, verification history row and stored status."""
    with sqlite3.connect(_db_path(str(root))) as conn:
        return (
            conn.execute("SELECT * FROM node_verification ORDER BY node_id").fetchall(),
            conn.execute("SELECT * FROM node_verification_targets ORDER BY node_id, target_id").fetchall(),
            conn.execute(
                "SELECT id FROM node_history WHERE change_type IN ('AGENT_VERIFIED', 'MANUAL_VERIFIED') ORDER BY id"
            ).fetchall(),
            conn.execute("SELECT id, own_status, link_status FROM nodes ORDER BY id").fetchall(),
        )


def _whole_index(root: Path) -> dict[str, list]:
    """Every row of every stored table in the index (virtual tables aside), by table name."""
    with closing(sqlite3.connect(_db_path(str(root)))) as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' "
                "ORDER BY name"
            )
        ]
        return {name: conn.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with ``foo``, a test of it and a doc section documenting it, all VERIFIED."""
    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    root = tmp_path / "main"
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "mod.py", _MOD.format(value=0))
    _write(root / "tests" / "test_mod.py", _TEST)
    (root / "docs").mkdir()
    _build(root)
    doc = {
        "id": "spec",
        "title": "Spec",
        "sections": [
            {"id": "overview", "heading": "Overview", "content": "foo returns a number.", "links": [{"node_id": FOO}]}
        ],
    }
    assert "Wrote" in axiom_graph_write_doc(str(root), doc)
    _build(root)
    summary = compute_check_summary(_db_path(str(root)), root)
    assert summary is not None and summary.all_clean
    return root


def _branch(project: Path, tmp_path: Path) -> Path:
    """A copy of the project with the project's index checked out into it."""
    copy = tmp_path / "branch"
    for name in ("axiom-graph.toml", "src", "tests", "docs"):
        source = project / name
        if source.is_dir():
            shutil.copytree(source, copy / name, copy_function=shutil.copy)
        else:
            copy.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, copy / name)
    assert checkout_db(_db_path(str(project)), copy).copied
    return copy


def _edit_in_branch(project: Path, tmp_path: Path, *, value: int = 42) -> Path:
    """Edit ``foo`` in a branch copy and build it there."""
    branch = _branch(project, tmp_path)
    _set_foo(branch, value)
    _build(branch)
    return branch


def _merge(branch: Path, project: Path) -> None:
    """Carry the branch's code back, as a fast-forward would, and build the project."""
    shutil.copy(branch / "src" / "mod.py", project / "src" / "mod.py")
    _build(project)


@workflow(
    purpose="After a merge, the code, test and doc verifications a worktree made carry into the main index with the "
    "pairs they recorded: all three read VERIFIED, each with one carry_forward verification naming branch, SHA and "
    "verifier, and none of the worktree's history"
)
def test_carry_forward_settles_code_test_and_doc_verified_in_the_worktree(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    口 = Step(
        step_num=1, name="Edit and verify in the worktree", purpose="Reverify foo there, clearing its test and section"
    )
    branch = _edit_in_branch(project, tmp_path)
    reverify_nodes(_db_path(str(branch)), branch, [FOO], "reviewed in the branch", verified_by="agent")
    compute_check_summary(_db_path(str(branch)), branch)
    assert set(_statuses(branch, FOO, TEST, SECTION).values()) == {("VERIFIED", "VERIFIED")}

    口 = Step(step_num=2, name="Merge", purpose="The merged build on main sees foo changed and its dependents stale")
    _merge(branch, project)
    assert _statuses(project, FOO, TEST, SECTION) == {
        FOO: ("CONTENT_UPDATED", "VERIFIED"),
        TEST: ("VERIFIED", "LINKED_STALE"),
        SECTION: ("VERIFIED", "LINKED_STALE"),
    }
    rows_before = {nid: len(_verification_rows(project, nid)) for nid in (FOO, TEST, SECTION)}

    口 = Step(step_num=3, name="Carry forward", purpose="Copy the worktree's verifications into main")
    monkeypatch.setattr("axiom_graph.index.git_utils.get_git_branch", lambda _root: "feature/foo")
    monkeypatch.setattr("axiom_graph.index.git_utils.get_git_sha", lambda _root: "abcdef1234567890")
    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    口 = Step(
        step_num=4,
        name="Assert settled with provenance",
        purpose="All three VERIFIED with the worktree's pairs and one carry_forward row each; no history imported",
    )
    assert {FOO, TEST, SECTION} <= set(result.carried)
    assert result.not_carried == {}
    assert result.after_stale == 0
    assert set(_statuses(project, FOO, TEST, SECTION).values()) == {("VERIFIED", "VERIFIED")}
    for nid in (FOO, TEST, SECTION):
        assert db.get_verification_targets(_db_path(str(project)), nid) == db.get_verification_targets(
            _db_path(str(branch)), nid
        )
        rows = _verification_rows(project, nid)
        assert len(rows) == rows_before[nid] + 1
        meta = json.loads(rows[0]["meta"])
        assert meta["verification_op"] == "carry_forward"
        assert meta["reason"].startswith("[carry_forward:feature/foo@abcdef123456] verified by agent")
        assert meta["carried_from"]["sha"] == "abcdef1234567890"


@workflow(
    purpose="A carried dependent is judged by its carried pair: it settles while its dependency is at the verified "
    "version, and a further edit to the dependency re-opens it, which the change time alone would miss"
)
def test_carried_dependent_is_judged_by_its_carried_pair(project: Path, tmp_path: Path) -> None:
    branch = _edit_in_branch(project, tmp_path)
    mark_clean_nodes(_db_path(str(branch)), branch, [TEST, SECTION], reason="checked against foo", verified_by="agent")
    _merge(branch, project)

    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    assert set(result.carried) >= {TEST, SECTION}
    assert result.not_carried["not_verified"] == [(FOO, None)]
    assert _statuses(project, FOO, TEST) == {FOO: ("CONTENT_UPDATED", "VERIFIED"), TEST: ("VERIFIED", "VERIFIED")}

    _set_foo(project, 7)
    statuses = compute_check_summary(_db_path(str(project)), project).statuses
    assert statuses[TEST][1] == "LINKED_STALE" and FOO in statuses[TEST][2]
    # foo's last recorded change still predates the carried verification: only the pair re-opens the test.
    dbp = _db_path(str(project))
    assert db.get_latest_code_change_times(dbp, [FOO])[FOO] < db.get_verification(dbp, TEST)["verified_at"]


@workflow(
    purpose="A node whose merged content matches neither side stays stale, and so do its dependents verified "
    "against the worktree's version of it"
)
def test_node_whose_content_differs_stays_stale_with_its_dependents(project: Path, tmp_path: Path) -> None:
    branch = _edit_in_branch(project, tmp_path)
    reverify_nodes(_db_path(str(branch)), branch, [FOO], "reviewed in the branch", verified_by="agent")
    _set_foo(project, 141)  # the merge of this branch's edit with another one: neither side's content
    _build(project)

    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    assert result.carried == []
    assert result.not_carried["content_differs"] == [(FOO, None)]
    assert sorted(result.not_carried["linked_node_differs"]) == [(SECTION, FOO), (TEST, FOO)]
    assert _statuses(project, FOO, TEST, SECTION) == {
        FOO: ("CONTENT_UPDATED", "VERIFIED"),
        TEST: ("VERIFIED", "LINKED_STALE"),
        SECTION: ("VERIFIED", "LINKED_STALE"),
    }


@workflow(
    purpose="A dependent with a link here that the worktree's index lacks is not carried in full: it carries the "
    "receipt the worktree recorded for the link that holds it stale, and the link the worktree lacks keeps this "
    "index's state"
)
def test_dependent_with_a_link_the_worktree_lacks_carries_in_part(project: Path, tmp_path: Path) -> None:
    branch = _edit_in_branch(project, tmp_path)
    reverify_nodes(_db_path(str(branch)), branch, [FOO], "reviewed in the branch", verified_by="agent")
    _merge(branch, project)
    added = axiom_graph_add_link(str(project), SECTION, node_id=BAR)
    assert not added.startswith("ERROR"), added
    bar_pair_before = db.get_verification_targets(_db_path(str(project)), SECTION).get(BAR)

    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    assert "link_absent" not in result.not_carried
    assert {FOO, TEST} <= set(result.carried)
    assert result.partial == [SECTION]
    assert result.partial_detail[SECTION] == (False, [FOO], [])
    pairs = db.get_verification_targets(_db_path(str(project)), SECTION)
    assert pairs[FOO] == db.get_verification_targets(_db_path(str(branch)), SECTION)[FOO]
    assert pairs.get(BAR) == bar_pair_before
    assert_matches_full_recompute(_db_path(str(project)), project)


@workflow(
    purpose="A dry run reports every stale node with its verdict from the statuses this index's last build stored, "
    "and writes nothing to either index, even with a file changed since that build, reading the worktree index "
    "through one read-only connection"
)
def test_dry_run_reports_each_node_and_writes_nothing(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = _edit_in_branch(project, tmp_path)
    mark_clean_nodes(_db_path(str(branch)), branch, [TEST, SECTION], reason="checked against foo", verified_by="agent")
    _merge(branch, project)
    # bar changes after main's build: a status refresh would now have rows to write.
    time.sleep(0.02)
    _write(project / "src" / "mod.py", _MOD.format(value=42).replace("return 1", "return 2"))
    before = _whole_index(project)
    wt_db = _db_path(str(branch))
    wt_bytes = hashlib.sha256(wt_db.read_bytes()).hexdigest()
    opened: list[tuple[Path, bool]] = []
    real_open = db.open_connection

    def _counting_open(path, *, read_only=False):
        opened.append((Path(path).resolve(), read_only))
        return real_open(path, read_only=read_only)

    monkeypatch.setattr(db, "open_connection", _counting_open)

    result = carry_forward_verifications(_db_path(str(project)), project, branch, dry_run=True)

    after = _whole_index(project)
    for table in ("node_history", "node_verification", "node_verification_targets", "nodes", "index_meta"):
        assert after[table] == before[table], table
    assert after == before
    assert hashlib.sha256(wt_db.read_bytes()).hexdigest() == wt_bytes
    assert opened == [(wt_db.resolve(), True)]
    assert result.dry_run and result.after_stale is None
    assert set(result.carried) == {TEST, SECTION}
    assert result.not_carried == {"not_verified": [(FOO, None)]}
    assert MODULE in result.follows
    assert result.before_stale == len(result.carried) + result.not_carried_count + len(result.follows)


@workflow(
    purpose="A section verified in the worktree that also links, doc-to-doc, a section still stale here carries, "
    "stays stale through that link until its target settles, and is counted and listed as carried but still stale"
)
def test_carried_section_held_by_a_doc_to_doc_link_is_reported_still_stale(project: Path, tmp_path: Path) -> None:
    _write(project / "axiom-graph.toml", _TRANSITIVE_CONFIG)
    notes = {
        "id": "notes",
        "title": "Notes",
        "sections": [{"id": "b1", "heading": "B1", "content": "bar returns one.", "links": [{"node_id": BAR}]}],
    }
    guide = {
        "id": "guide",
        "title": "Guide",
        "tags": ["consumer"],
        "sections": [
            {
                "id": "a1",
                "heading": "A1",
                "content": "foo returns a number; B1 covers bar.",
                "links": [{"node_id": FOO}, {"node_id": NOTES_B1}],
            }
        ],
    }
    for doc in (notes, guide):
        assert "Wrote" in axiom_graph_write_doc(str(project), doc)
    _build(project)
    assert compute_check_summary(_db_path(str(project)), project).all_clean
    branch = _edit_in_branch(project, tmp_path)
    mark_clean_nodes(
        _db_path(str(branch)), branch, [TEST, SECTION, GUIDE_A1], reason="checked against foo", verified_by="agent"
    )
    assert _statuses(branch, GUIDE_A1, NOTES_B1) == {
        GUIDE_A1: ("VERIFIED", "VERIFIED"),
        NOTES_B1: ("VERIFIED", "VERIFIED"),
    }
    # The merge brings the branch's foo and an edit to bar made here meanwhile, which leaves B1 stale.
    time.sleep(0.02)
    _write(project / "src" / "mod.py", _MOD.format(value=42).replace("return 1", "return 2"))
    _build(project)
    assert _statuses(project, NOTES_B1)[NOTES_B1] == ("VERIFIED", "LINKED_STALE")

    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    assert {TEST, SECTION, GUIDE_A1} <= set(result.carried)
    assert NOTES_B1 not in result.carried
    assert result.carried_still_stale == [GUIDE_A1]
    statuses = compute_check_summary(_db_path(str(project)), project).statuses
    assert statuses[GUIDE_A1] == ("VERIFIED", "LINKED_STALE", [NOTES_B1])
    assert statuses[TEST][:2] == statuses[SECTION][:2] == ("VERIFIED", "VERIFIED")
    short = render_carry_forward_report(result)
    assert (
        "1 carried node(s) stay stale until nodes they depend on settle "
        "(a doc-to-doc link, or an annotated function or delegated task still stale)." in short
    )
    assert f"- {GUIDE_A1}" not in short
    listed = render_carry_forward_report(result, list_nodes=True)
    assert f"Carried, still stale until nodes they depend on settle (1):\n- {GUIDE_A1}" in listed


TEST_BOTH = "proj::tests.test_both::test_both"
GUIDE = "proj::docs/guide"
GUIDE_BOTH = "proj::docs/guide::both"
_TEST_BOTH = "from src.mod import bar, foo\n\n\ndef test_both():\n    assert foo() is not None\n    assert bar() > 0\n"


def _add_two_offender_dependents(project: Path) -> None:
    """Add a test of foo and bar and a doc section linking both, then verify everything."""
    _write(project / "tests" / "test_both.py", _TEST_BOTH)
    guide = {
        "id": "guide",
        "title": "Guide",
        "sections": [
            {
                "id": "both",
                "heading": "Both",
                "content": "foo returns a number and bar a positive one.",
                "links": [{"node_id": FOO}, {"node_id": BAR}],
            }
        ],
    }
    assert "Wrote" in axiom_graph_write_doc(str(project), guide)
    _build(project)
    stale = compute_check_summary(_db_path(str(project)), project).statuses
    mark_clean_nodes(_db_path(str(project)), project, sorted(stale), reason="setup", verified_by="agent")
    assert compute_check_summary(_db_path(str(project)), project).all_clean


def _set_mod(root: Path, foo: int, bar: int) -> None:
    time.sleep(0.02)
    _write(root / "src" / "mod.py", _MOD.format(value=foo).replace("return 1", f"return {bar}"))


@workflow(
    purpose="With unrelated drift on both sides, the code, test, section and doc envelope a worktree touched arrive "
    "on main with the worktree's own status and the same offenders; the envelope, still flagged there through bar, "
    "is carried in part and its history names the worktree's latest verification, the doc write"
)
def test_carry_forward_carries_each_dimension_the_worktree_verified(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    口 = Step(step_num=1, name="Unrelated drift on main", purpose="bar changes on main and nobody re-checks it")
    _add_two_offender_dependents(project)
    _set_mod(project, foo=0, bar=2)
    _build(project)

    口 = Step(
        step_num=2,
        name="Edit and re-check against foo in the worktree",
        purpose="foo changes; the test is reverified against foo only and the section is rewritten addressing foo, "
        "which text-verifies the doc envelope; bar stays open in both",
    )
    branch = _branch(project, tmp_path)
    _set_mod(branch, foo=42, bar=2)
    _build(branch)
    reverify_nodes(_db_path(str(branch)), branch, [FOO], "reviewed in the branch", verified_by="agent")
    patched = axiom_graph_patch_section(
        str(branch), GUIDE_BOTH, anchor="$", new_string=" foo now returns 42.", addresses=[FOO]
    )
    assert not patched.startswith("ERROR"), patched
    wt = compute_check_summary(_db_path(str(branch)), branch).statuses
    # reverify settles no dependent another offender (bar) still holds, so the test keeps foo open too.
    assert wt[TEST_BOTH] == ("VERIFIED", "LINKED_STALE", [BAR, FOO])
    assert wt[GUIDE][0] == "VERIFIED"

    口 = Step(step_num=3, name="Merge", purpose="Copy the code and docs back and build main")
    shutil.copy(branch / "src" / "mod.py", project / "src" / "mod.py")
    shutil.copytree(branch / "docs", project / "docs", dirs_exist_ok=True, copy_function=shutil.copy)
    _build(project)
    assert _statuses(project, GUIDE)[GUIDE][0] != "VERIFIED"

    口 = Step(step_num=4, name="Carry forward", purpose="Copy what the worktree verified, one dimension at a time")
    monkeypatch.setattr("axiom_graph.index.git_utils.get_git_branch", lambda _root: "feature/foo")
    monkeypatch.setattr("axiom_graph.index.git_utils.get_git_sha", lambda _root: "abcdef1234567890")
    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    口 = Step(
        step_num=5,
        name="Assert the worktree's statuses, counts and provenance",
        purpose="Same own status and offenders as the worktree; partial carries counted once each; history names "
        "the worktree's latest verification; parity with a full recompute",
    )
    assert GUIDE in result.partial and result.partial_detail[GUIDE][0] is True
    assert f"- {GUIDE} (own text" in render_carry_forward_report(result, list_nodes=True)
    assert FOO in result.carried and TEST_BOTH not in result.partial
    assert result.before_stale == (
        len(result.carried) + len(result.partial) + result.not_carried_count + len(result.follows)
    )
    main = compute_check_summary(_db_path(str(project)), project).statuses
    for nid in (FOO, TEST_BOTH, GUIDE, GUIDE_BOTH):
        assert main[nid] == wt[nid], nid
    guide_meta = json.loads(_verification_rows(project, GUIDE)[0]["meta"])
    assert guide_meta["verifies"] == "text"
    assert guide_meta["carried_from"]["verification_op"] == "doc_edit"
    assert guide_meta["reason"].startswith("[carry_forward:feature/foo@abcdef123456] verified by agent")
    assert_matches_full_recompute(_db_path(str(project)), project)


@workflow(
    purpose="A dependent the worktree verified against two linked nodes carries only the receipt for the one at the "
    "same version here; the other stays an offender, and the dry run reports the same partial carry"
)
def test_partial_carry_takes_only_receipts_that_match_here(project: Path, tmp_path: Path) -> None:
    _add_two_offender_dependents(project)
    branch = _branch(project, tmp_path)
    _set_mod(branch, foo=42, bar=1)
    _build(branch)
    mark_clean_nodes(_db_path(str(branch)), branch, [TEST_BOTH], reason="checked", verified_by="agent")
    _set_mod(project, foo=42, bar=2)  # the merge: foo from the branch, bar edited on main meanwhile
    _build(project)

    dry = carry_forward_verifications(_db_path(str(project)), project, branch, dry_run=True)
    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    assert TEST_BOTH in result.partial
    assert result.partial_detail[TEST_BOTH] == (False, [FOO], [(BAR, "linked_node_differs")])
    assert (dry.partial, dry.partial_detail) == (result.partial, result.partial_detail)
    n = len(result.carried)
    preview = render_carry_forward_report(dry)
    assert preview.startswith(f"Dry run: would carry {n} verification(s) in full and 1 in part from ")
    assert f"Would carry in part (1):\n- {TEST_BOTH} (receipts: {FOO}; open: {BAR} (differs))" in preview
    report = render_carry_forward_report(result)
    assert report.startswith(f"Carried {n} verification(s) in full and 1 in part from ")
    assert "0 of the 1 carried in part now read VERIFIED; the rest stay stale through what did not carry" in report
    assert f"- {TEST_BOTH}" not in report
    assert f"Carried in part (1):\n- {TEST_BOTH} (receipts: {FOO}; open: {BAR} (differs))" in (
        render_carry_forward_report(result, list_nodes=True)
    )
    assert compute_check_summary(_db_path(str(project)), project).statuses[TEST_BOTH] == (
        "VERIFIED",
        "LINKED_STALE",
        [BAR],
    )
    pairs = db.get_verification_targets(_db_path(str(project)), TEST_BOTH)
    assert pairs[FOO] == db.get_verification_targets(_db_path(str(branch)), TEST_BOTH)[FOO]
    assert_matches_full_recompute(_db_path(str(project)), project)


BAZ = "proj::src.mod::baz"
TEST_THREE = "proj::tests.test_three::test_three"
_MOD3 = "def foo():\n    return {foo}\n\n\ndef bar():\n    return {bar}\n\n\ndef baz():\n    return 1\n"
_TEST_THREE = (
    "from src.mod import bar, baz, foo\n\n\n"
    "def test_three():\n    assert foo() is not None\n    assert bar() > 0\n    assert baz() == 1\n"
)


def _set_mod3(root: Path, foo: int, bar: int) -> None:
    time.sleep(0.02)
    _write(root / "src" / "mod.py", _MOD3.format(foo=foo, bar=bar))


@workflow(
    purpose="A test with no verification row here, carried in part, arrives with the worktree's offenders and no "
    "others: the row the carry creates pins only the link still open, and takes the worktree's receipts for the "
    "links that do not hold it stale"
)
def test_partial_carry_onto_a_node_with_no_row_pins_only_its_open_offenders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    project = tmp_path / "main"
    _write(project / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(project / "src" / "mod.py", _MOD3.format(foo=0, bar=1))
    _write(project / "tests" / "test_three.py", _TEST_THREE)
    (project / "docs").mkdir()
    _build(project)  # the first build baselines no test: TEST_THREE has no verification row
    assert db.get_verification(_db_path(str(project)), TEST_THREE) is None

    branch = _branch(project, tmp_path)
    _set_mod3(branch, foo=42, bar=1)
    _build(branch)
    mark_clean_nodes(_db_path(str(branch)), branch, [TEST_THREE], reason="checked", verified_by="agent")
    _set_mod3(branch, foo=42, bar=2)  # bar moves after the check: the worktree holds the test open through bar
    _build(branch)
    wt = compute_check_summary(_db_path(str(branch)), branch).statuses
    assert wt[TEST_THREE] == ("VERIFIED", "LINKED_STALE", [BAR])

    shutil.copy(branch / "src" / "mod.py", project / "src" / "mod.py")
    _build(project)
    assert compute_check_summary(_db_path(str(project)), project).statuses[TEST_THREE] == (
        "VERIFIED",
        "LINKED_STALE",
        [BAR, FOO],
    )

    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    assert result.partial == [TEST_THREE]
    assert compute_check_summary(_db_path(str(project)), project).statuses[TEST_THREE] == wt[TEST_THREE]
    assert result.partial_detail[TEST_THREE] == (False, [BAZ, FOO], [(BAR, "linked_node_differs")])
    report = render_carry_forward_report(result, list_nodes=True)
    assert f"- {TEST_THREE} (receipts: {BAZ}, {FOO}; open: {BAR} (differs))" in report
    assert "0 of the 1 carried in part now read VERIFIED" in report
    pairs = db.get_verification_targets(_db_path(str(project)), TEST_THREE)
    wt_pairs = db.get_verification_targets(_db_path(str(branch)), TEST_THREE)
    assert pairs[BAR][0] == db.OPEN_RECEIPT_HASH
    assert (pairs[FOO], pairs[BAZ]) == (wt_pairs[FOO], wt_pairs[BAZ])
    assert_matches_full_recompute(_db_path(str(project)), project)


SPEC = "proj::docs/spec"


@workflow(
    purpose="A full carry names the worktree's latest verification of the node: a doc write's text-only verification "
    "made after the verification record, not the older verifier the record still names"
)
def test_full_carry_names_the_worktrees_latest_verification(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = _edit_in_branch(project, tmp_path)
    reverify_nodes(_db_path(str(branch)), branch, [FOO], "reviewed in the branch", verified_by="agent")
    record_before = db.get_verification(_db_path(str(branch)), SPEC)
    patched = axiom_graph_patch_section(str(branch), SECTION, anchor="$", new_string=" It returns 42.")
    assert not patched.startswith("ERROR"), patched
    assert compute_check_summary(_db_path(str(branch)), branch).statuses[SPEC][:2] == ("VERIFIED", "VERIFIED")
    wt_latest = _verification_rows(branch, SPEC)[0]
    assert json.loads(wt_latest["meta"])["verification_op"] == "doc_edit"
    assert db.get_verification(_db_path(str(branch)), SPEC)["verified_at"] == record_before["verified_at"]
    shutil.copy(branch / "docs" / "spec.docjson", project / "docs" / "spec.docjson")
    _merge(branch, project)
    assert _statuses(project, SPEC)[SPEC][0] != "VERIFIED"

    monkeypatch.setattr("axiom_graph.index.git_utils.get_git_branch", lambda _root: "feature/foo")
    monkeypatch.setattr("axiom_graph.index.git_utils.get_git_sha", lambda _root: "abcdef1234567890")
    result = carry_forward_verifications(_db_path(str(project)), project, branch)

    assert SPEC in result.carried
    meta = json.loads(_verification_rows(project, SPEC)[0]["meta"])
    assert meta["verification_op"] == "carry_forward" and "verifies" not in meta
    assert meta["carried_from"]["verification_op"] == "doc_edit"
    assert meta["carried_from"]["verified_at"] == wt_latest["scanned_at"] != record_before["verified_at"]
    assert _statuses(project, SPEC)[SPEC] == ("VERIFIED", "VERIFIED")


@pytest.mark.parametrize("mismatch", ["schema", "project", "same_index"])
def test_refuses_indexes_that_cannot_be_compared(project: Path, tmp_path: Path, mismatch: str) -> None:
    """A worktree index with another schema version or project id, or the index itself, is refused untouched."""
    branch = _branch(project, tmp_path)
    if mismatch == "schema":
        with sqlite3.connect(_db_path(str(branch))) as conn:
            conn.execute("PRAGMA user_version = 4")
        expected = "schema versions differ: this index is v5, the worktree index is v4"
    elif mismatch == "project":
        with sqlite3.connect(_db_path(str(branch))) as conn:
            conn.execute("UPDATE index_meta SET value = 'other' WHERE key = 'project_id'")
        expected = "project ids differ: this index is 'proj', the worktree index is 'other'"
    else:
        branch = project
        expected = "this checkout's own index"
    before = _index_snapshot(project)

    with pytest.raises(CarryForwardRefusedError, match=expected):
        carry_forward_verifications(_db_path(str(project)), project, branch)

    assert _index_snapshot(project) == before


def test_cli_carry_forward_prints_counts_and_lists_on_dry_run(project: Path, tmp_path: Path) -> None:
    """The command prints the counts and stale counts; a dry run lists what it would carry, --list every verdict."""
    branch = _edit_in_branch(project, tmp_path)
    mark_clean_nodes(_db_path(str(branch)), branch, [TEST, SECTION], reason="checked against foo", verified_by="agent")
    _merge(branch, project)
    runner = CliRunner()

    dry = runner.invoke(cli_main, ["carry-forward", str(branch), "-p", str(project), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert dry.output.startswith("Dry run: would carry 2 verification(s)")
    assert "(statuses as this index's last build or check stored them)." in dry.output
    assert "Not carried (1): 1 not verified in the worktree." in dry.output
    assert "Would carry (2):" in dry.output and f"- {TEST}" in dry.output and f"- {SECTION}" in dry.output
    assert f"- {FOO}" not in dry.output and "Not carried, " not in dry.output

    listed = runner.invoke(cli_main, ["carry-forward", str(branch), "-p", str(project), "--dry-run", "--list"])
    assert listed.exit_code == 0, listed.output
    assert f"- {TEST}" in listed.output
    assert f"Not carried, not verified in the worktree (1):\n- {FOO}" in listed.output

    real = runner.invoke(cli_main, ["carry-forward", str(branch), "-p", str(project)])
    assert real.exit_code == 0, real.output
    lines = real.output.splitlines()
    assert lines[0].startswith("Carried 2 verification(s) from ")
    assert lines[1].startswith("Stale here: ") and " before -> " in lines[1]
    assert f"- {TEST}" not in real.output
    assert len(lines) <= 6


@workflow(purpose="The registered MCP tool carries the worktree's verifications and refuses an index it cannot compare")
def test_mcp_tool_carries_through_its_registered_function(project: Path, tmp_path: Path) -> None:
    branch = _edit_in_branch(project, tmp_path)
    reverify_nodes(_db_path(str(branch)), branch, [FOO], "reviewed in the branch", verified_by="agent")
    _merge(branch, project)

    def _call(**args) -> str:
        blocks = list(asyncio.run(mcp.call_tool("axiom_graph_carry_forward", {"project_root": str(project), **args})))
        return blocks[0].text

    dry = _call(worktree_path=str(branch), dry_run=True)
    assert dry.startswith("Dry run: would carry 3 verification(s)")
    assert "Would carry (3):" in dry and f"- {FOO}" in dry
    assert f"- {MODULE}" not in dry.splitlines() and "linked sections (3):" not in dry
    dry_listed = _call(worktree_path=str(branch), dry_run=True, list_nodes=True)
    assert "Stale only through their children or linked sections (3):" in dry_listed
    assert f"- {MODULE}" in dry_listed.splitlines()

    blocks = list(
        asyncio.run(
            mcp.call_tool("axiom_graph_carry_forward", {"project_root": str(project), "worktree_path": str(branch)})
        )
    )
    text = blocks[0].text

    assert text.startswith("Carried 3 verification(s) from ")
    assert "Stale here: " in text and "-> 0 after." in text
    assert set(_statuses(project, FOO, TEST, SECTION).values()) == {("VERIFIED", "VERIFIED")}

    refused = list(
        asyncio.run(
            mcp.call_tool("axiom_graph_carry_forward", {"project_root": str(project), "worktree_path": str(project)})
        )
    )
    assert refused[0].text.startswith("ERROR: carry-forward refused:")


ADR_SECTION = "proj::docs/adr::context"


@workflow(
    purpose="A frozen doc's section that carries LINKED_STALE here and has no verification row, edited as plain "
    "text in a worktree, keeps its LINKED_STALE when the worktree's verification carries in part: the row the "
    "carry creates pins every link, so its time settles none of them"
)
def test_partial_carry_onto_a_carried_frozen_section_keeps_its_linked_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from axiom_graph.docjson.api import axiom_graph_update_section

    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    project = tmp_path / "main"
    config = '[axiom_graph]\nproject_id = "proj"\n'

    口 = Step(
        step_num=1,
        name="A frozen section carrying LINKED_STALE with no verification row",
        purpose="An adr-tagged section links foo and has never been verified; foo changes, the section goes "
        "LINKED_STALE, then the doc is frozen",
    )
    _write(project / "axiom-graph.toml", config)
    _write(project / "src" / "mod.py", _MOD.format(value=0))
    (project / "tests").mkdir()
    _write(
        project / "docs" / "adr.json",
        json.dumps(
            {
                "title": "ADR",
                "tags": ["adr"],
                "sections": [
                    {"id": "context", "heading": "Context", "content": "foo returns 0.", "links": [{"node_id": FOO}]}
                ],
            }
        ),
    )
    _build(project)
    _set_foo(project, 1)
    _build(project)
    _write(project / "axiom-graph.toml", config + '\n[axiom_graph.staleness]\nfrozen_tags = ["adr"]\n')
    compute_check_summary(_db_path(str(project)), project)
    assert _statuses(project, ADR_SECTION)[ADR_SECTION] == ("VERIFIED", "LINKED_STALE")
    assert db.get_verification(_db_path(str(project)), ADR_SECTION) is None

    口 = Step(
        step_num=2,
        name="Edit the section's text in a worktree and merge it without its stamp",
        purpose="A plain doc write verifies only the text there; the merged text, its write stamp lost to the merge, "
        "reads as an own change here",
    )
    branch = _branch(project, tmp_path)
    out = axiom_graph_update_section(str(branch), ADR_SECTION, content="foo returns 1 now.")
    assert not out.startswith("ERROR"), out
    assert _statuses(branch, ADR_SECTION)[ADR_SECTION] == ("VERIFIED", "LINKED_STALE")
    merged = json.loads((branch / "docs" / "adr.json").read_text(encoding="utf-8"))
    for section in merged["sections"]:
        section.pop("axiom_stamp", None)
    _write(project / "docs" / "adr.json", json.dumps(merged))
    _build(project)
    assert _statuses(project, ADR_SECTION)[ADR_SECTION] == ("CONTENT_UPDATED", "LINKED_STALE")

    口 = Step(
        step_num=3,
        name="Carry the worktree's verification",
        purpose="The text carries in part; the section keeps LINKED_STALE until a verification of its links",
    )
    result = carry_forward_verifications(_db_path(str(project)), project, branch)
    assert result.partial == [ADR_SECTION]
    compute_check_summary(_db_path(str(project)), project)
    assert _statuses(project, ADR_SECTION)[ADR_SECTION] == ("VERIFIED", "LINKED_STALE")
    assert db.get_verification_targets(_db_path(str(project)), ADR_SECTION)[FOO][0] == db.OPEN_RECEIPT_HASH
    assert_matches_full_recompute(_db_path(str(project)), project)
