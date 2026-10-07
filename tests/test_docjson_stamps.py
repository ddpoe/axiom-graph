"""Behavioural tests for DocJSON tool-write stamps and raw DocJSON edit detection.

Doc tools stamp the sections they write.  A build (or a write, or a rescan)
verifies a stamped section that arrives by merge when its linked code is the
code it was written against, and records a section edited outside the doc
tools once as a raw DocJSON edit, never verifying it.  Writes enter through
``axiom_graph.docjson.api``; staleness is read back through the lifecycle api.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from axiom_annotations import Step, workflow

from axiom_graph.docjson.api import (
    axiom_graph_accept_doc_edits,
    axiom_graph_patch_section,
    axiom_graph_update_section,
    axiom_graph_write_doc,
)
from axiom_graph.index import db, doc_stamps
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import (
    build_index,
    checkout_db,
    compute_check_summary,
    compute_report,
    fetch_history,
    mark_clean_nodes,
)
from tests.fixtures.full_recompute import assert_matches_full_recompute

CODE_ID = "proj::src.mod::foo"
DOC_ID = "proj::docs/spec"
S = {i: f"{DOC_ID}::s{i}" for i in range(1, 10)}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose function ``foo`` already has a content-change history row."""
    _write(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    (tmp_path / "docs").mkdir()
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 0\n")
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)
    time.sleep(0.02)
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 42\n")
    build_index(dbp, tmp_path)
    return tmp_path


def _doc(slug: str = "spec", n: int = 3) -> dict:
    return {
        "id": slug,
        "title": slug.title(),
        "sections": [
            {"id": f"s{i}", "heading": f"S{i}", "content": f"Section {i}.", "links": [{"node_id": CODE_ID}]}
            for i in range(1, n + 1)
        ],
    }


def _file(root: Path, slug: str = "spec") -> Path:
    return root / "docs" / f"{slug}.docjson"


def _sections(root: Path, slug: str = "spec") -> dict[str, dict]:
    return doc_stamps.load_section_dicts(_file(root, slug))


def _hand_edit(root: Path, edits: dict[str, str | None], add: dict | None = None, slug: str = "spec") -> None:
    """Edit section contents in the file the way an editor would (``None`` drops the stamp only)."""
    time.sleep(0.02)
    path = _file(root, slug)
    data = json.loads(path.read_text(encoding="utf-8"))
    flat = doc_stamps.flatten_section_dicts(data["sections"])
    for dot, content in edits.items():
        if content is None:
            flat[dot].pop(doc_stamps.STAMP_KEY, None)
        else:
            flat[dot]["content"] = content
    if add:
        data["sections"].append(add)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _raw_rows(root: Path, node_id: str) -> list[dict]:
    rows = db.get_history(_db_path(str(root)), node_id, limit=100)
    return [r for r in rows if r["change_type"] == "RAW_DOCJSON_EDIT"]


def _status(root: Path, node_id: str) -> tuple[str, str, list[str]]:
    summary = compute_check_summary(_db_path(str(root)), root)
    assert summary is not None
    return summary.statuses[node_id]


def _verified_at(root: Path, node_id: str) -> str | None:
    v = db.get_verification(_db_path(str(root)), node_id)
    return v["verified_at"] if v else None


def _setup_written(root: Path) -> None:
    assert "Wrote" in axiom_graph_write_doc(str(root), _doc())
    build_index(_db_path(str(root)), root)


def _copy_project(src: Path, dst: Path) -> None:
    for name in ("axiom-graph.toml", "src", "docs"):
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name, copy_function=shutil.copy)
        else:
            shutil.copy(src / name, dst / name)


def _two_functions(root: Path, foo: int, bar: int) -> None:
    """Rewrite ``src/mod.py`` with ``foo`` and ``bar`` returning the given values."""
    time.sleep(0.02)
    _write(root / "src" / "mod.py", f"def foo():\n    return {foo}\n\n\ndef bar():\n    return {bar}\n")


def _merge_from_copy(project: Path, tmp_path_factory, monkeypatch, *, edit_code_after_write: bool) -> str:
    """Write a doc in a copied checkout and carry its files back, as a merge would."""
    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    copy = tmp_path_factory.mktemp("branch")
    _copy_project(project, copy)
    assert checkout_db(_db_path(str(project)), copy).copied
    assert "Wrote" in axiom_graph_write_doc(str(copy), _doc("merged", 1))
    if edit_code_after_write:
        time.sleep(0.02)
        _write(copy / "src" / "mod.py", "def foo():\n    return 7\n")
        shutil.copy(copy / "src" / "mod.py", project / "src" / "mod.py")
    shutil.copy(_file(copy, "merged"), _file(project, "merged"))
    return "proj::docs/merged::s1"


@workflow(
    purpose="A doc tool stamps only the sections it writes; a hand-edited sibling keeps its old, now mismatched, stamp"
)
def test_tool_writes_stamp_only_the_sections_they_touch(project: Path) -> None:
    axiom_graph_write_doc(str(project), _doc())
    before = {k: v[doc_stamps.STAMP_KEY] for k, v in _sections(project).items()}
    assert all(doc_stamps.stamp_state(v) == doc_stamps.STAMP_VALID for v in _sections(project).values())
    current = doc_stamps.current_code_hashes(_db_path(str(project)), project, [CODE_ID])
    assert before["s1"]["verified_against"] == current and current[CODE_ID]

    axiom_graph_update_section(str(project), S[1], content="Rewritten.")
    after = {k: v[doc_stamps.STAMP_KEY] for k, v in _sections(project).items()}
    assert after["s1"] != before["s1"]
    assert after["s2"] == before["s2"] and after["s3"] == before["s3"]

    _hand_edit(project, {"s2": "Edited by hand."})
    axiom_graph_patch_section(str(project), S[3], "More.", anchor="$")
    final = _sections(project)
    assert final["s3"][doc_stamps.STAMP_KEY] != after["s3"]
    assert final["s2"][doc_stamps.STAMP_KEY] == before["s2"]
    assert doc_stamps.stamp_state(final["s2"]) == doc_stamps.STAMP_MISMATCHED


@workflow(
    purpose="A tool-written section carried onto another checkout by a merge is VERIFIED there after the build and "
    "the next check when its linked code is unchanged since the write"
)
def test_merged_tool_written_section_is_verified_when_code_unchanged(project, tmp_path_factory, monkeypatch) -> None:
    sid = _merge_from_copy(project, tmp_path_factory, monkeypatch, edit_code_after_write=False)

    result = build_index(_db_path(str(project)), project)

    assert sid in result.stamp_verified
    assert result.raw_docjson_edits == []
    assert db.get_verification(_db_path(str(project)), sid)["verified_by"] == "agent:tool-stamp"
    assert _status(project, sid)[:2] == ("VERIFIED", "VERIFIED")


@workflow(
    purpose="A tool-written section whose linked code changed after the write has its text verified after the merge, "
    "and stays LINKED_STALE through that code: its new verification holds the link open rather than settling it"
)
def test_merged_section_text_verified_but_linked_stale_when_code_moved(project, tmp_path_factory, monkeypatch) -> None:
    sid = _merge_from_copy(project, tmp_path_factory, monkeypatch, edit_code_after_write=True)
    dbp = _db_path(str(project))

    result = build_index(dbp, project)

    assert sid not in result.stamp_verified
    assert sid in result.stamp_text_verified
    assert db.get_verification(dbp, sid)["verified_by"] == "agent:tool-stamp"
    assert db.get_verification_targets(dbp, sid) == {CODE_ID: (db.OPEN_RECEIPT_HASH, None)}
    assert _status(project, sid) == ("VERIFIED", "LINKED_STALE", [CODE_ID])
    assert_matches_full_recompute(dbp, project)


@pytest.mark.parametrize("doc_changed_on_main", [False, True], ids=["doc-unchanged", "doc-changed-on-main"])
@workflow(
    purpose="A merged tool-written section that also links a doc the stamp holds no entry for pins the code that moved "
    "after the write, and pins the doc link only when that doc has a change on main the new verification would "
    "otherwise settle"
)
def test_merged_section_holds_open_only_its_open_offenders(
    project, tmp_path_factory, monkeypatch, doc_changed_on_main: bool
) -> None:
    dbp = _db_path(str(project))
    _setup_written(project)
    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    copy = tmp_path_factory.mktemp("branch")
    _copy_project(project, copy)
    assert checkout_db(dbp, copy).copied
    if doc_changed_on_main:
        # Main edits the linked doc by hand before the merge lands: a change
        # nobody verified, which the merged section's new row must not settle.
        _hand_edit(project, {"s1": "Section 1, revised on main."})
        build_index(dbp, project)
        assert DOC_ID in db.get_effective_change_rows(dbp, [DOC_ID])
    merged = {
        "id": "merged",
        "title": "Merged",
        "sections": [
            {
                "id": "s1",
                "heading": "S1",
                "content": "foo, per the spec.",
                "links": [{"node_id": CODE_ID}, {"node_id": DOC_ID}],
            }
        ],
    }
    assert "Wrote" in axiom_graph_write_doc(str(copy), merged)
    time.sleep(0.02)
    _write(copy / "src" / "mod.py", "def foo():\n    return 7\n")
    shutil.copy(copy / "src" / "mod.py", project / "src" / "mod.py")
    shutil.copy(_file(copy, "merged"), _file(project, "merged"))
    sid = "proj::docs/merged::s1"

    result = build_index(dbp, project)

    assert sid in result.stamp_text_verified
    pairs = db.get_verification_targets(dbp, sid)
    assert pairs.get(CODE_ID) == (db.OPEN_RECEIPT_HASH, None)
    own, link, offenders = _status(project, sid)
    assert (own, link) == ("VERIFIED", "LINKED_STALE")
    if doc_changed_on_main:
        assert pairs[DOC_ID][0] == db.OPEN_RECEIPT_HASH
        assert sorted(offenders) == sorted([CODE_ID, DOC_ID])
    else:
        assert DOC_ID not in pairs
        assert offenders == [CODE_ID]
    assert_matches_full_recompute(dbp, project)


@workflow(
    purpose="A merged section a tool rewrote while it was LINKED_STALE through code nobody re-checked arrives with its "
    "text verified and stays stale through that code only, as on the branch; a section hand-edited after its tool "
    "write still arrives unverified and is listed for acceptance"
)
def test_merged_section_keeps_the_branch_link_state_per_link(project, tmp_path_factory, monkeypatch) -> None:
    dbp = _db_path(str(project))
    a_id, b_id = CODE_ID, "proj::src.mod::bar"
    _two_functions(project, 42, 1)
    build_index(dbp, project)
    # Verify foo so its next edit is recorded as a new change.
    mark_clean_nodes(dbp, project, [a_id], reason="reviewed", verified_by="human")
    doc = {
        "id": "pair",
        "title": "Pair",
        "sections": [
            {"id": "p1", "heading": "P1", "content": "foo and bar.", "links": [{"node_id": a_id}, {"node_id": b_id}]},
            {"id": "p2", "heading": "P2", "content": "foo alone.", "links": [{"node_id": a_id}]},
        ],
    }
    assert "Wrote" in axiom_graph_write_doc(str(project), doc)
    build_index(dbp, project)
    p1, p2 = "proj::docs/pair::p1", "proj::docs/pair::p2"
    _two_functions(project, 43, 1)
    build_index(dbp, project)
    assert _status(project, p1) == ("VERIFIED", "LINKED_STALE", [a_id])

    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    branch = tmp_path_factory.mktemp("branch")
    _copy_project(project, branch)
    assert checkout_db(dbp, branch).copied
    _two_functions(branch, 43, 2)
    build_index(_db_path(str(branch)), branch)
    res = axiom_graph_patch_section(str(branch), p1, " bar now returns two.", anchor="$", addresses=[b_id])
    assert not res.startswith("ERROR"), res
    assert _status(branch, p1) == ("VERIFIED", "LINKED_STALE", [a_id])
    axiom_graph_update_section(str(branch), p2, content="foo, rewritten.")
    _hand_edit(branch, {"p2": "foo, then edited by hand."}, slug="pair")

    time.sleep(0.02)
    shutil.copy(branch / "src" / "mod.py", project / "src" / "mod.py")
    shutil.copy(_file(branch, "pair"), _file(project, "pair"))
    result = build_index(dbp, project)

    assert p1 in result.stamp_text_verified and p1 not in result.stamp_verified
    assert _status(project, p1) == ("VERIFIED", "LINKED_STALE", [a_id])
    assert result.raw_docjson_edits == [p2]
    assert _status(project, p2)[0] == "CONTENT_UPDATED"
    assert p2 in axiom_graph_accept_doc_edits(str(project), dry_run=True)
    assert_matches_full_recompute(dbp, project)


@workflow(
    purpose="A branch that edits code and writes a doc section against the edit with a tool, merged together, leaves "
    "the section VERIFIED after the build that first records the code change and at the next check"
)
def test_merged_code_and_doc_written_against_it_land_verified(project, tmp_path_factory, monkeypatch) -> None:
    dbp = _db_path(str(project))
    # Verify foo so the merged edit is recorded as a new content change by the build below.
    mark_clean_nodes(dbp, project, [CODE_ID], reason="reviewed", verified_by="human")
    changes_before = db.get_latest_code_change_times(dbp, [CODE_ID])
    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    copy = tmp_path_factory.mktemp("branch")
    _copy_project(project, copy)
    assert checkout_db(dbp, copy).copied
    time.sleep(0.02)
    _write(copy / "src" / "mod.py", "def foo():\n    return 7\n")
    assert "Wrote" in axiom_graph_write_doc(str(copy), _doc("merged", 1))
    shutil.copy(copy / "src" / "mod.py", project / "src" / "mod.py")
    shutil.copy(_file(copy, "merged"), _file(project, "merged"))
    sid = "proj::docs/merged::s1"

    result = build_index(dbp, project)

    assert db.get_latest_code_change_times(dbp, [CODE_ID]) != changes_before, "the build must record foo's edit"
    assert sid in result.stamp_verified
    with sqlite3.connect(dbp) as conn:
        persisted = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (sid,)).fetchone()
    assert tuple(persisted) == ("VERIFIED", "VERIFIED")
    assert _status(project, sid)[:2] == ("VERIFIED", "VERIFIED")


@workflow(
    purpose="Hand-edited and hand-added sections are recorded once each and reported in one summary line; later "
    "builds and a tool write elsewhere in the doc never repeat them; untouched unstamped sections stay quiet"
)
def test_raw_docjson_edits_are_reported_once(project: Path) -> None:
    _setup_written(project)
    dbp = _db_path(str(project))
    s1_verified = _verified_at(project, S[1])
    new_sec = {"id": "s9", "heading": "S9", "content": "Hand-added.", "links": [{"node_id": CODE_ID}]}
    _hand_edit(project, {"s1": "Edited by hand.", "s3": None}, add=new_sec)

    result = build_index(dbp, project)

    assert sorted(result.raw_docjson_edits) == [S[1], S[9]]
    summaries = [w for w in result.warnings if "edited outside the doc tools" in w]
    assert len(summaries) == 1 and summaries[0].startswith("2 DocJSON section(s)")
    for sid in (S[1], S[9]):
        rows = _raw_rows(project, sid)
        assert len(rows) == 1 and json.loads(rows[0]["meta"])["section_hash"]
    assert _verified_at(project, S[1]) == s1_verified
    assert _verified_at(project, S[9]) is None
    assert _raw_rows(project, S[3]) == []

    again = build_index(dbp, project)
    assert again.raw_docjson_edits == [] and not [w for w in again.warnings if "doc tools" in w]

    res = axiom_graph_update_section(str(project), S[2], content="Tool edit.")
    assert "edited outside the doc tools" not in res
    build_index(dbp, project)
    assert len(_raw_rows(project, S[1])) == 1 and len(_raw_rows(project, S[9])) == 1


@workflow(
    purpose="A hand edit absorbed by a tool write to a sibling is caught by that write: one record, the summary "
    "naming both fixes, the sibling stamped and verified, the edited section not"
)
def test_write_to_a_sibling_catches_an_absorbed_hand_edit(project: Path) -> None:
    _setup_written(project)
    s2_verified = _verified_at(project, S[2])
    _hand_edit(project, {"s2": "Edited by hand."})

    res = axiom_graph_update_section(str(project), S[1], content="Tool edit.")

    assert "1 DocJSON section(s) were edited outside the doc tools" in res
    for fix in ("axiom_graph_accept_doc_edits", "axiom-graph stamps accept", "update_section", "dry_run=True"):
        assert fix in res
    assert 'raw_docjson_edits = "off"' in res
    assert len(_raw_rows(project, S[2])) == 1
    assert doc_stamps.stamp_state(_sections(project)["s1"]) == doc_stamps.STAMP_VALID
    assert _status(project, S[1])[0] == "VERIFIED"
    assert _verified_at(project, S[2]) == s2_verified
    assert build_index(_db_path(str(project)), project).raw_docjson_edits == []


@workflow(
    purpose="Accepting a flagged raw DocJSON edit stamps the file without changing the section's content or hashes, "
    "verifies it with the accept op, silences later builds, and the event shows in report and history"
)
def test_accept_stamps_and_verifies_a_flagged_edit(project: Path) -> None:
    _setup_written(project)
    dbp = _db_path(str(project))
    _hand_edit(project, {"s2": "Edited by hand."})
    assert build_index(dbp, project).raw_docjson_edits == [S[2]]
    raw_bytes = _file(project).read_bytes()

    listing = axiom_graph_accept_doc_edits(str(project), dry_run=True)
    assert S[2] in listing and _file(project).read_bytes() == raw_bytes

    node_before = db.get_node(dbp, S[2])
    out = axiom_graph_accept_doc_edits(str(project), section_ids=[S[2]])
    assert "Accepted 1 section(s)" in out
    assert doc_stamps.stamp_state(_sections(project)["s2"]) == doc_stamps.STAMP_VALID
    node_after = db.get_node(dbp, S[2])
    assert (node_after.level_2, node_after.desc_hash) == (node_before.level_2, node_before.desc_hash)
    assert _status(project, S[2])[:2] == ("VERIFIED", "VERIFIED")
    latest = [r for r in db.get_history(dbp, S[2], limit=100) if r["change_type"] == "AGENT_VERIFIED"][0]
    assert json.loads(latest["meta"])["verification_op"] == "accept_raw_docjson_edit"
    assert build_index(dbp, project).raw_docjson_edits == []
    assert axiom_graph_accept_doc_edits(str(project), dry_run=True).startswith("No sections")

    assert S[2] in {r["node_id"] for r in compute_report(dbp).raw_docjson_edits}
    assert "RAW_DOCJSON_EDIT" in {r.change_type for r in fetch_history(dbp, S[2]).rows}


@pytest.mark.parametrize("new_content", ["Different tool text.", "Edited by hand."])
@workflow(
    purpose="A hand edit re-applied through update_section -- with different or identical content -- leaves the "
    "section stamped, verified, and not flagged by the next build"
)
def test_reapplying_a_hand_edit_with_a_tool_settles_it(project: Path, new_content: str) -> None:
    _setup_written(project)
    _hand_edit(project, {"s2": "Edited by hand."})

    axiom_graph_update_section(str(project), S[2], content=new_content)

    assert doc_stamps.stamp_state(_sections(project)["s2"]) == doc_stamps.STAMP_VALID
    assert build_index(_db_path(str(project)), project).raw_docjson_edits == []
    assert _status(project, S[2])[:2] == ("VERIFIED", "VERIFIED")
    assert _raw_rows(project, S[2]) == []


@workflow(
    purpose='Under raw_docjson_edits = "off" a hand edit is neither reported nor recorded and stays stale under the '
    "normal rules, while a merged tool-written section is still verified"
)
def test_off_switch_silences_detection_only(project: Path, tmp_path_factory, monkeypatch) -> None:
    _setup_written(project)
    sid = _merge_from_copy(project, tmp_path_factory, monkeypatch, edit_code_after_write=False)
    _write(
        project / "axiom-graph.toml",
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.docjson]\nraw_docjson_edits = "off"\n',
    )
    _hand_edit(project, {"s1": "Edited by hand."})

    result = build_index(_db_path(str(project)), project)

    assert result.raw_docjson_edits == []
    assert not [w for w in result.warnings if "doc tools" in w]
    assert _raw_rows(project, S[1]) == []
    assert _status(project, S[1])[0] == "CONTENT_UPDATED"
    assert sid in result.stamp_verified
    assert _status(project, sid)[:2] == ("VERIFIED", "VERIFIED")
    with sqlite3.connect(_db_path(str(project))) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM node_history WHERE change_type = 'RAW_DOCJSON_EDIT'").fetchone()[0] == 0
        )


# ---------------------------------------------------------------------------
# Adoption on drift: a tool write an earlier build indexed without adopting
# ---------------------------------------------------------------------------


def _stored(root: Path, node_id: str) -> tuple[str, str]:
    """The stored (own, link) statuses, read without running a check (a check adopts)."""
    with sqlite3.connect(_db_path(str(root))) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, node_id
    return row[0], row[1]


def _build_without_adoption(root: Path, monkeypatch) -> None:
    """Build once with the classifier's tool-write verdicts withheld.

    This reproduces an ingest that indexed merged tool-written text without
    adopting it (the stamp check of that build did not verify it): the
    sections land CONTENT_UPDATED with their file's mtime recorded, so no
    later rescan sees them.  Raw edits are still recorded as usual.
    """
    real = doc_stamps.classify_section

    def _withheld(section: dict, **kwargs) -> doc_stamps.StampVerdict:
        decided = real(section, **kwargs)
        if decided.verdict in (doc_stamps.VERDICT_TOOL_WRITE, doc_stamps.VERDICT_TOOL_WRITE_PARTIAL):
            return doc_stamps.StampVerdict(doc_stamps.VERDICT_QUIET)
        return decided

    with monkeypatch.context() as patched:
        patched.setattr(doc_stamps, "classify_section", _withheld)
        build_index(_db_path(str(root)), root)


def _branch_copy(project: Path, tmp_path_factory, monkeypatch) -> Path:
    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    copy = tmp_path_factory.mktemp("branch")
    _copy_project(project, copy)
    assert checkout_db(_db_path(str(project)), copy).copied
    return copy


@pytest.mark.parametrize("operation", ["build", "check"])
@workflow(
    purpose="A section an agent rewrote with a doc tool on another branch, merged and indexed by a build that did not "
    "adopt it, is verified by the next build with no file change, or by the next check; a section hand-edited on "
    "that branch stays CONTENT_UPDATED and listed as a raw DocJSON edit"
)
def test_merged_tool_write_indexed_unadopted_is_adopted_by_the_next_build_or_check(
    project: Path, tmp_path_factory, monkeypatch, operation: str
) -> None:
    dbp = _db_path(str(project))
    口 = Step(step_num=1, name="Write the doc on main", purpose="Three tool-written sections linked to foo, verified")
    _setup_written(project)

    口 = Step(
        step_num=2,
        name="Rewrite on a branch",
        purpose="In a copied checkout S1 is extended with patch_section; S2 is rewritten with update_section and then "
        "edited by hand",
    )
    copy = _branch_copy(project, tmp_path_factory, monkeypatch)
    axiom_graph_patch_section(str(copy), S[1], "Added on the branch.", anchor="$")
    axiom_graph_update_section(str(copy), S[2], content="Branch text.")
    _hand_edit(copy, {"s2": "Branch text, then edited by hand."})

    口 = Step(
        step_num=3,
        name="Merge and index without adopting",
        purpose="The file is copied back and indexed by one build whose stamp check does not adopt (disclosed above)",
    )
    time.sleep(0.02)
    shutil.copy(_file(copy), _file(project))
    _build_without_adoption(project, monkeypatch)
    assert _stored(project, S[1])[0] == "CONTENT_UPDATED"
    assert _stored(project, S[2])[0] == "CONTENT_UPDATED"

    口 = Step(step_num=4, name="Run the operation", purpose="A build with no file change, or a check")
    if operation == "build":
        result = build_index(dbp, project)
        assert S[1] in result.stamp_verified and S[2] not in result.stamp_verified
        assert result.raw_docjson_edits == []
    else:
        summary = compute_check_summary(dbp, project)
        assert summary is not None and summary.statuses[S[1]][:2] == ("VERIFIED", "VERIFIED")

    口 = Step(
        step_num=5,
        name="Read the outcome",
        purpose="S1 is verified in full by its stamp; S2 is still a raw edit; the stored statuses equal a full "
        "recompute",
    )
    assert _stored(project, S[1]) == ("VERIFIED", "VERIFIED")
    assert db.get_verification(dbp, S[1])["verified_by"] == "agent:tool-stamp"
    assert _stored(project, S[2])[0] == "CONTENT_UPDATED"
    assert doc_stamps.list_raw_docjson_edits(dbp, project) == [S[2]]
    assert S[2] in axiom_graph_accept_doc_edits(str(project), dry_run=True)
    assert_matches_full_recompute(dbp, project)


def test_drift_sweep_reads_each_file_once_and_batches_its_reads(project: Path, tmp_path_factory, monkeypatch) -> None:
    slugs = ("alpha", "beta", "gamma")
    for slug in slugs:
        assert "Wrote" in axiom_graph_write_doc(str(project), _doc(slug, 2))
    dbp = _db_path(str(project))
    build_index(dbp, project)
    copy = _branch_copy(project, tmp_path_factory, monkeypatch)
    drifted = sorted(f"proj::docs/{slug}::s{i}" for slug in slugs for i in (1, 2))
    for sid in drifted:
        axiom_graph_update_section(str(copy), sid, content=f"Branch text for {sid}.")
    time.sleep(0.02)
    for slug in slugs:
        shutil.copy(_file(copy, slug), _file(project, slug))
    _build_without_adoption(project, monkeypatch)
    assert {_stored(project, sid)[0] for sid in drifted} == {"CONTENT_UPDATED"}

    calls: dict[str, list] = {"files": [], "drift_queries": [], "node_reads": [], "status_reads": []}

    def _counting(name: str, real):
        def _wrapper(*args, **kwargs):
            calls[name].append(args[-1] if name == "files" else None)
            return real(*args, **kwargs)

        return _wrapper

    monkeypatch.setattr(doc_stamps, "load_section_dicts", _counting("files", doc_stamps.load_section_dicts))
    monkeypatch.setattr(
        doc_stamps.db, "get_own_drifted_sections_conn", _counting("drift_queries", db.get_own_drifted_sections_conn)
    )
    monkeypatch.setattr(doc_stamps.db, "get_nodes_conn", _counting("node_reads", db.get_nodes_conn))
    monkeypatch.setattr(doc_stamps.db, "get_staleness_for_conn", _counting("status_reads", db.get_staleness_for_conn))

    result = doc_stamps.adopt_drifted_stamps(dbp, project)

    assert sorted(result.verified) == drifted
    assert sorted(calls["files"]) == sorted(_file(project, slug) for slug in slugs)
    assert len(calls["drift_queries"]) == 1
    # One batched node read for the sweep and at most one per file for the
    # linked code's hashes: never one per section.
    assert len(calls["node_reads"]) <= len(slugs) + 1 < len(drifted)
    # The sweep judges drift itself: reconcile never re-reads the statuses.
    assert calls["status_reads"] == []


@pytest.mark.parametrize(
    ("stamp", "text_changed", "own_drifted", "verdict"),
    [
        ("valid", False, False, doc_stamps.VERDICT_QUIET),
        ("valid", False, True, doc_stamps.VERDICT_TOOL_WRITE),
        ("valid", True, False, doc_stamps.VERDICT_TOOL_WRITE),
        ("missing", False, True, doc_stamps.VERDICT_QUIET),
        ("mismatched", False, True, doc_stamps.VERDICT_RAW_EDIT),
    ],
)
def test_classifier_treats_own_drift_like_new_text_only_for_a_valid_stamp(
    stamp: str, text_changed: bool, own_drifted: bool, verdict: str
) -> None:
    section = {"id": "s1", "heading": "S1", "content": "Text.", "links": [{"node_id": CODE_ID}]}
    if stamp != "missing":
        section[doc_stamps.STAMP_KEY] = doc_stamps.make_stamp(section, {CODE_ID: "h1"})
    if stamp == "mismatched":
        section["content"] = "Edited by hand."

    decided = doc_stamps.classify_section(
        section, text_changed=text_changed, legacy_gate=True, current_hashes={CODE_ID: "h1"}, own_drifted=own_drifted
    )

    assert decided.verdict == verdict


def test_a_section_linking_a_module_reads_verified_in_a_fresh_index(project: Path, tmp_path_factory) -> None:
    """A doc write stamps a linked module at its file's hash, so a fresh index of a copy agrees the section is current."""
    doc = {
        "id": "mods",
        "title": "Mods",
        "sections": [
            {"id": "m", "heading": "M", "content": "About the module.", "links": [{"node_id": "proj::src.mod"}]}
        ],
    }
    assert "Wrote" in axiom_graph_write_doc(str(project), doc)
    clone = tmp_path_factory.mktemp("clone")
    _copy_project(project, clone)

    build_index(_db_path(str(clone)), clone)

    assert _status(clone, "proj::docs/mods::m")[:2] == ("VERIFIED", "VERIFIED")
