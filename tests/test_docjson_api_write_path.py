"""Behavioural tests for the docjson write pipeline.

Every DocJSON write tool funnels through ``axiom_graph.docjson.api``'s
shared save pipeline.  These tests pin what that pipeline preserves (the
sections a write did not touch), what it verifies (the sections it created
or edited), how it serialises concurrent writers, how it batches, and what
it refuses to store.  All writes enter through ``axiom_graph.docjson.api``;
staleness is read back through the lifecycle api's build and check.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from axiom_annotations import Step, workflow

from axiom_graph.docjson.api import (
    axiom_graph_add_link,
    axiom_graph_add_section,
    axiom_graph_delete_link,
    axiom_graph_delete_section,
    axiom_graph_patch_section,
    axiom_graph_update_section,
    axiom_graph_write_doc,
    content_hash,
)
from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes

CODE_ID = "proj::src.mod::foo"
DOC_ID = "proj::docs/spec"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose code node ``foo`` has a content-change history.

    A section linking to ``foo`` is LINKED_STALE unless it carries a
    verification newer than that change, which makes verification loss
    observable at the next check.
    """
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    (tmp_path / "docs").mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("def foo():\n    return 0\n", encoding="utf-8")
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)
    time.sleep(0.02)
    (src / "mod.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
    build_index(dbp, tmp_path)
    assert db.get_latest_code_change_times(dbp, [CODE_ID]), "fixture: foo needs a content-change history row"
    return tmp_path


def _linked_doc(n_sections: int = 3) -> dict:
    """A doc whose sections all link to ``foo``."""
    return {
        "id": "spec",
        "title": "Spec",
        "sections": [
            {
                "id": f"s{i}",
                "heading": f"S{i}",
                "content": f"Section {i}.",
                "links": [{"node_id": CODE_ID}],
            }
            for i in range(1, n_sections + 1)
        ],
    }


def _seed_verified_doc(project: Path, doc: dict | None = None) -> list[str]:
    """Write *doc* and stamp every section with a human verification."""
    doc = doc or _linked_doc()
    res = axiom_graph_write_doc(str(project), json.loads(json.dumps(doc)))
    assert "Wrote" in res, res
    dbp = _db_path(str(project))
    ids = [s["id"] for s in db.get_doc_sections(dbp, DOC_ID)]
    mark_clean_nodes(dbp, project, ids, reason="seed", verified_by="human")
    return ids


def _snapshot(dbp: Path, node_id: str) -> dict:
    """Verification timestamp, INITIAL-row count, and edge ids for a node."""
    with sqlite3.connect(dbp) as conn:
        v = conn.execute("SELECT verified_at FROM node_verification WHERE node_id = ?", (node_id,)).fetchone()
        initial = conn.execute(
            "SELECT COUNT(*) FROM node_history WHERE node_id = ? AND change_type = 'INITIAL'", (node_id,)
        ).fetchone()[0]
        edges = {r[0] for r in conn.execute("SELECT id FROM edges WHERE from_id = ? OR to_id = ?", (node_id, node_id))}
    return {"verified_at": v[0] if v else None, "initial_rows": initial, "edges": edges}


def _link_status(project: Path) -> dict[str, str]:
    """Run a check and return ``node_id -> link_status``."""
    summary = compute_check_summary(_db_path(str(project)), project)
    assert summary is not None
    return {nid: link for nid, (_own, link, _via) in summary.statuses.items()}


def _build_and_check(project: Path) -> dict[str, str]:
    build_index(_db_path(str(project)), project)
    return _link_status(project)


# ---------------------------------------------------------------------------
# US-1: plain-path writers leave siblings alone through the next build
# ---------------------------------------------------------------------------


def _write_add_link(project: Path) -> None:
    assert "Added" in axiom_graph_add_link(str(project), f"{DOC_ID}::s1", node_id="proj::src.mod")


def _write_patch(project: Path) -> None:
    assert "Patched" in axiom_graph_patch_section(str(project), f"{DOC_ID}::s1", "More.", anchor="$")


def _write_add_section(project: Path) -> None:
    assert "Added" in axiom_graph_add_section(str(project), DOC_ID, "s4", heading="S4", content="New.")


def _write_update(project: Path) -> None:
    assert "Updated" in axiom_graph_update_section(str(project), f"{DOC_ID}::s1", content="Rewritten.")


@workflow(
    purpose="Plain-path doc writes (add_link, patch_section, add_section, update_section) leave every untouched "
    "sibling's verification, history and link status intact through the next build and check",
)
@pytest.mark.parametrize(
    "writer",
    [_write_add_link, _write_patch, _write_add_section, _write_update],
    ids=["add_link", "patch_section", "add_section", "update_section"],
)
def test_plain_writers_leave_siblings_verified_through_next_build(project: Path, writer) -> None:
    dbp = _db_path(str(project))
    _seed_verified_doc(project)
    siblings = [f"{DOC_ID}::s2", f"{DOC_ID}::s3"]
    before = {sid: _snapshot(dbp, sid) for sid in siblings}
    assert all(_link_status(project)[sid] == "VERIFIED" for sid in siblings)

    time.sleep(0.02)
    writer(project)
    statuses = _build_and_check(project)

    for sid in siblings:
        after = _snapshot(dbp, sid)
        assert after["verified_at"] == before[sid]["verified_at"], sid
        assert after["initial_rows"] == before[sid]["initial_rows"], sid
        assert after["edges"] == before[sid]["edges"], sid
        assert statuses[sid] == "VERIFIED", (sid, statuses[sid])


# ---------------------------------------------------------------------------
# US-1: link removal, section deletion and rename touch only their target
# ---------------------------------------------------------------------------


@workflow(
    purpose="Removing a link from one section of a verified linked doc leaves the other sections' verification, "
    "history and edges untouched, and none of them turns LINKED_STALE at the next check",
)
def test_delete_link_leaves_siblings_verified(project: Path) -> None:
    dbp = _db_path(str(project))

    口 = Step(step_num=1, name="Seed a verified linked doc", purpose="Three sections link to foo and are verified")
    _seed_verified_doc(project)
    siblings = [f"{DOC_ID}::s2", f"{DOC_ID}::s3"]
    before = {sid: _snapshot(dbp, sid) for sid in siblings}

    口 = Step(step_num=2, name="Remove one link", purpose="delete_link on s1 only")
    time.sleep(0.02)
    res = axiom_graph_delete_link(str(project), f"{DOC_ID}::s1", node_id=CODE_ID)
    assert "Removed" in res, res

    口 = Step(step_num=3, name="Check", purpose="Siblings keep verification, history, edges and link status")
    statuses = _link_status(project)
    for sid in siblings:
        assert _snapshot(dbp, sid) == before[sid], sid
        assert statuses[sid] == "VERIFIED", (sid, statuses[sid])
    s1_edges = _snapshot(dbp, f"{DOC_ID}::s1")["edges"]
    assert not any("::documents::" in e for e in s1_edges), s1_edges


def _remap_edge(edge_id: str, moved: dict[str, str]) -> str:
    """Rewrite both endpoints of an ``{from}::{type}::{to}`` edge id through *moved*."""
    for edge_type in ("composes", "documents"):
        sep = f"::{edge_type}::"
        if sep in edge_id:
            src, dst = edge_id.split(sep, 1)
            return f"{moved.get(src, src)}{sep}{moved.get(dst, dst)}"
    return edge_id


def _nested_linked_doc() -> dict:
    doc = _linked_doc()
    doc["sections"][0]["sections"] = [
        {"id": "c", "heading": "C", "content": "Child.", "links": [{"node_id": CODE_ID}]},
    ]
    return doc


@workflow(
    purpose="Deleting or renaming a parent section touches only that subtree: siblings keep their verification, "
    "history and edges, and a renamed section and its cascaded child carry verification, history and edges "
    "to their new ids",
)
@pytest.mark.parametrize("op", ["delete_section", "rename"])
def test_section_delete_and_rename_touch_only_their_subtree(project: Path, op: str) -> None:
    dbp = _db_path(str(project))
    _seed_verified_doc(project, _nested_linked_doc())
    siblings = [f"{DOC_ID}::s2", f"{DOC_ID}::s3"]
    moved = {f"{DOC_ID}::s1": f"{DOC_ID}::r1", f"{DOC_ID}::s1.c": f"{DOC_ID}::r1.c"}
    before = {sid: _snapshot(dbp, sid) for sid in siblings + list(moved)}

    time.sleep(0.02)
    if op == "delete_section":
        res = axiom_graph_delete_section(str(project), f"{DOC_ID}::s1")
        assert "Deleted" in res, res
    else:
        res = axiom_graph_update_section(str(project), f"{DOC_ID}::s1", new_id="r1")
        assert "Updated" in res, res
    statuses = _link_status(project)

    for sid in siblings:
        assert _snapshot(dbp, sid) == before[sid], sid
        assert statuses[sid] == "VERIFIED", (sid, statuses[sid])

    for old, new in moved.items():
        assert db.get_node(dbp, old) is None
        if op == "delete_section":
            assert _snapshot(dbp, old)["edges"] == set()
            continue
        after = _snapshot(dbp, new)
        assert after["verified_at"] == before[old]["verified_at"], new
        assert after["initial_rows"] == before[old]["initial_rows"], new
        assert after["edges"] == {_remap_edge(e, moved) for e in before[old]["edges"]}, new
        assert f"{new}::documents::{CODE_ID}" in after["edges"]
        assert statuses[new] == "VERIFIED", (new, statuses[new])


@workflow(
    purpose="A new linked doc from write_doc, and a section added by add_section and then linked, come out VERIFIED "
    "and not LINKED_STALE at the next check although their target has a content-change history",
)
def test_created_sections_are_verified_by_their_writer(project: Path) -> None:
    口 = Step(step_num=1, name="Create a linked doc", purpose="write_doc with sections linking to drifted foo")
    res = axiom_graph_write_doc(str(project), _linked_doc())
    assert "Wrote" in res, res
    assert f"s1  content_hash: {content_hash('Section 1.')}" in res, res

    口 = Step(step_num=2, name="Add a section, then link it", purpose="add_section followed by add_link")
    time.sleep(0.02)
    assert "Added" in axiom_graph_add_section(str(project), DOC_ID, "s4", heading="S4", content="New.")
    assert "Added" in axiom_graph_add_link(str(project), f"{DOC_ID}::s4", node_id=CODE_ID)

    口 = Step(step_num=3, name="Check", purpose="Every section of the doc is VERIFIED on both dimensions")
    summary = compute_check_summary(_db_path(str(project)), project)
    assert summary is not None
    for sid in [f"{DOC_ID}::s{i}" for i in range(1, 5)]:
        assert summary.statuses[sid][:2] == ("VERIFIED", "VERIFIED"), (sid, summary.statuses[sid])


@workflow(
    purpose="A documents edge from another doc into this doc survives an unrelated link removal here, and follows "
    "a rename of its target section both in the index and in the other doc's file",
)
def test_inbound_documents_edges_from_other_docs_survive_writes(project: Path) -> None:
    dbp = _db_path(str(project))
    _seed_verified_doc(project)
    other = {
        "id": "other",
        "title": "Other",
        "sections": [
            {"id": "x1", "heading": "X1", "content": "Refers to spec.", "links": [{"node_id": f"{DOC_ID}::s2"}]}
        ],
    }
    assert "Wrote" in axiom_graph_write_doc(str(project), other)
    inbound = f"proj::docs/other::x1::documents::{DOC_ID}::s2"

    assert "Removed" in axiom_graph_delete_link(str(project), f"{DOC_ID}::s1", node_id=CODE_ID)
    assert inbound in _snapshot(dbp, f"{DOC_ID}::s2")["edges"]

    assert "Updated" in axiom_graph_update_section(str(project), f"{DOC_ID}::s2", new_id="t2")
    assert f"proj::docs/other::x1::documents::{DOC_ID}::t2" in _snapshot(dbp, f"{DOC_ID}::t2")["edges"]
    other_json = json.loads((project / "docs" / "other.docjson").read_text(encoding="utf-8"))
    assert other_json["sections"][0]["links"] == [{"node_id": f"{DOC_ID}::t2"}]


@workflow(
    purpose="Deleting a section that another doc links to keeps that doc's inbound documents edge, so the next "
    "check reports the linking section as BROKEN_LINK instead of the link silently disappearing",
)
def test_deleting_a_linked_section_surfaces_broken_link(project: Path) -> None:
    dbp = _db_path(str(project))

    口 = Step(step_num=1, name="Seed two docs", purpose="A verified spec doc and another doc linking to spec::s2")
    _seed_verified_doc(project)
    other = {
        "id": "other",
        "title": "Other",
        "sections": [
            {"id": "x1", "heading": "X1", "content": "Refers to spec.", "links": [{"node_id": f"{DOC_ID}::s2"}]}
        ],
    }
    assert "Wrote" in axiom_graph_write_doc(str(project), other)
    linker = "proj::docs/other::x1"
    inbound = f"{linker}::documents::{DOC_ID}::s2"
    assert _link_status(project)[linker] == "VERIFIED"

    口 = Step(step_num=2, name="Delete the linked section", purpose="delete_section on spec::s2")
    res = axiom_graph_delete_section(str(project), f"{DOC_ID}::s2")
    assert "Deleted" in res, res

    口 = Step(step_num=3, name="Check", purpose="The inbound edge is kept and the linking section is BROKEN_LINK")
    assert db.get_node(dbp, f"{DOC_ID}::s2") is None
    assert inbound in _snapshot(dbp, linker)["edges"]
    assert _link_status(project)[linker] == "BROKEN_LINK"
    assert _build_and_check(project)[linker] == "BROKEN_LINK"


def _hand_edit_section(project: Path, sec_id: str, content: str) -> None:
    """Change one section's content directly in the doc file, bypassing the write tools."""
    doc_file = project / "docs" / "spec.docjson"
    data = json.loads(doc_file.read_text(encoding="utf-8"))
    for sec in data["sections"]:
        if sec["id"] == sec_id:
            sec["content"] = content
    doc_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@workflow(
    purpose="A section already drifted from its verification (hand-edited on disk, with or without a build since) "
    "stays CONTENT_UPDATED when a write elsewhere in the same doc saves the file; only the written section is "
    "verified",
)
@pytest.mark.parametrize("rebuilt", [True, False], ids=["drift-indexed", "drift-on-disk-only"])
def test_write_elsewhere_does_not_verify_an_already_stale_section(project: Path, rebuilt: bool) -> None:
    dbp = _db_path(str(project))

    口 = Step(step_num=1, name="Seed a verified doc", purpose="Every section verified by a human")
    _seed_verified_doc(project)
    stale = f"{DOC_ID}::s2"
    before = _snapshot(dbp, stale)

    口 = Step(step_num=2, name="Drift one section", purpose="Hand-edit s2 on disk, optionally rebuilding the index")
    time.sleep(0.02)
    _hand_edit_section(project, "s2", "Edited by hand.")
    if rebuilt:
        build_index(dbp, project)
        summary = compute_check_summary(dbp, project)
        assert summary is not None and summary.statuses[stale][0] == "CONTENT_UPDATED"

    口 = Step(step_num=3, name="Write elsewhere", purpose="patch_section appends to s1 of the same doc")
    time.sleep(0.02)
    assert "Patched" in axiom_graph_patch_section(str(project), f"{DOC_ID}::s1", "More.", anchor="$")

    口 = Step(step_num=4, name="Check", purpose="s2 keeps its old verification and is CONTENT_UPDATED; s1 is VERIFIED")
    assert _snapshot(dbp, stale)["verified_at"] == before["verified_at"]
    for build_first in (False, True):
        if build_first:
            build_index(dbp, project)
        summary = compute_check_summary(dbp, project)
        assert summary is not None
        assert summary.statuses[stale][0] == "CONTENT_UPDATED", (build_first, summary.statuses[stale])
        written = summary.statuses[f"{DOC_ID}::s1"]
        assert written[:2] == ("VERIFIED", "VERIFIED"), (build_first, written)


@workflow(
    purpose="Renaming a section and changing its content in one update_section call re-verifies it at its new "
    "content under the new id, and it stays VERIFIED through the next build",
)
def test_rename_with_content_edit_reverifies_under_new_id(project: Path) -> None:
    dbp = _db_path(str(project))
    _seed_verified_doc(project)
    before = _snapshot(dbp, f"{DOC_ID}::s1")

    time.sleep(0.02)
    res = axiom_graph_update_section(str(project), f"{DOC_ID}::s1", new_id="r1", content="Renamed and rewritten.")
    assert "Updated" in res, res

    new_id = f"{DOC_ID}::r1"
    assert db.get_node(dbp, f"{DOC_ID}::s1") is None
    assert db.get_node(dbp, new_id).level_2 == "Renamed and rewritten."
    with sqlite3.connect(dbp) as conn:
        verified_at, verified_by, code_hash_at, desc_hash_at = conn.execute(
            "SELECT verified_at, verified_by, code_hash_at, desc_hash_at FROM node_verification WHERE node_id = ?",
            (new_id,),
        ).fetchone()
        code_hash, desc_hash = conn.execute("SELECT code_hash, desc_hash FROM nodes WHERE id = ?", (new_id,)).fetchone()
    # The edit verifies the text (the snapshot below); the row's time still
    # describes the last verification of the section's links.
    assert verified_at == before["verified_at"]
    assert (code_hash_at, desc_hash_at) == (code_hash, desc_hash)
    assert _build_and_check(project)[new_id] == "VERIFIED"
    summary = compute_check_summary(dbp, project)
    assert summary is not None and summary.statuses[new_id][:2] == ("VERIFIED", "VERIFIED")


# ---------------------------------------------------------------------------
# US-3: concurrent writers are serialised; stale reads are refused
# ---------------------------------------------------------------------------

_APPENDER = """
import sys
from axiom_graph.docjson.api import axiom_graph_patch_section
root, sid, tag, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
for i in range(n):
    res = axiom_graph_patch_section(root, sid, f"{tag}-{i}", anchor="$")
    assert res.startswith("Patched"), res
"""


@workflow(
    purpose="Concurrent patch_section appends to one section from two threads and a separate process all land; "
    "none is lost to a read-modify-write race",
)
def test_concurrent_appends_to_one_section_all_land(project: Path) -> None:
    import subprocess
    import sys
    import threading

    _seed_verified_doc(project)
    sid = f"{DOC_ID}::s1"
    n = 4

    def _append(tag: str) -> None:
        for i in range(n):
            res = axiom_graph_patch_section(str(project), sid, f"{tag}-{i}", anchor="$")
            assert res.startswith("Patched"), res

    proc = subprocess.Popen([sys.executable, "-c", _APPENDER, str(project), sid, "proc", str(n)])
    threads = [threading.Thread(target=_append, args=(f"t{k}",)) for k in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert proc.wait(timeout=120) == 0

    content = json.loads((project / "docs" / "spec.docjson").read_text(encoding="utf-8"))["sections"][0]["content"]
    for tag in ("t0", "t1", "proc"):
        for i in range(n):
            assert f"{tag}-{i}" in content.splitlines(), (tag, i, content)


@workflow(
    purpose="update_section refuses a write based on an outdated expected_hash and reports the current content and "
    "hash without touching the file, accepts a matching hash, and returns ERROR without writing on a lock timeout",
)
def test_expected_hash_and_lock_timeout_refuse_without_writing(project: Path, monkeypatch) -> None:
    from axiom_graph.docjson.api import content_hash
    from axiom_graph.index import doc_lock

    _seed_verified_doc(project)
    sid = f"{DOC_ID}::s1"
    doc_file = project / "docs" / "spec.docjson"

    first = axiom_graph_update_section(str(project), sid, content="Version two.")
    assert first.startswith("Updated"), first
    assert f"content_hash: {content_hash('Version two.')}" in first

    before = doc_file.read_bytes()
    stale = axiom_graph_update_section(
        str(project), sid, content="Lost edit.", expected_hash=content_hash("Section 1.")
    )
    assert stale.startswith("ERROR:"), stale
    assert content_hash("Version two.") in stale and "Version two." in stale
    assert doc_file.read_bytes() == before

    ok = axiom_graph_update_section(
        str(project), sid, content="Version three.", expected_hash=content_hash("Version two.")
    )
    assert ok.startswith("Updated"), ok

    monkeypatch.setattr(doc_lock, "DEFAULT_LOCK_TIMEOUT", 0.2)
    before = doc_file.read_bytes()
    with doc_lock.doc_write_lock(project.resolve(), doc_file):
        res = axiom_graph_update_section(str(project), sid, content="Blocked.")
    assert res.startswith("ERROR:"), res
    assert doc_file.read_bytes() == before


# ---------------------------------------------------------------------------
# US-4: batched writes apply every item or none, with one save
# ---------------------------------------------------------------------------


def _count_saves(monkeypatch) -> list[int]:
    """Count calls to the shared save pipeline made by the api tools."""
    import axiom_graph.docjson.api as api

    calls: list[int] = []
    real = api.save_and_reindex

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "save_and_reindex", _counting)
    return calls


@workflow(
    purpose="add_section with a list of sections writes nothing when one item is invalid, and otherwise adds every "
    "item -- including one nested under an item added earlier in the same list -- in one save, all VERIFIED",
)
def test_add_section_batch_is_all_or_nothing(project: Path, monkeypatch) -> None:
    dbp = _db_path(str(project))
    _seed_verified_doc(project)
    doc_file = project / "docs" / "spec.docjson"

    before_bytes = doc_file.read_bytes()
    before_ids = {s["id"] for s in db.get_doc_sections(dbp, DOC_ID)}
    res = axiom_graph_add_section(
        str(project),
        DOC_ID,
        sections=[
            {"section_id": "n1", "heading": "N1", "content": "Fine."},
            {"section_id": "Not A Slug", "heading": "Bad"},
        ],
    )
    assert res.startswith("ERROR:") and "sections[1]" in res, res
    assert doc_file.read_bytes() == before_bytes
    assert {s["id"] for s in db.get_doc_sections(dbp, DOC_ID)} == before_ids

    saves = _count_saves(monkeypatch)
    time.sleep(0.02)
    res = axiom_graph_add_section(
        str(project),
        DOC_ID,
        sections=[
            {"section_id": "n1", "heading": "N1", "content": "First."},
            {"section_id": "n2", "heading": "N2", "content": "Child of the first.", "parent_id": "n1"},
            {"section_id": "n3", "heading": "N3", "content": "After s1.", "after": "s1"},
        ],
    )
    assert res.startswith("Added 3 section(s)"), res
    assert len(saves) == 1
    added = [f"{DOC_ID}::n1", f"{DOC_ID}::n1.n2", f"{DOC_ID}::n3"]
    statuses = compute_check_summary(dbp, project).statuses
    for sid in added:
        assert db.get_node(dbp, sid) is not None, sid
        assert statuses[sid][:2] == ("VERIFIED", "VERIFIED"), (sid, statuses[sid])
    top = [s["id"] for s in json.loads(doc_file.read_text(encoding="utf-8"))["sections"]]
    assert top == ["s1", "n3", "s2", "s3", "n1"]


@workflow(
    purpose="add_link with a list of {section_id, node_id} items links several sections of one doc in one save, "
    "and refuses items spanning two docs without writing either file",
)
def test_add_link_batch_across_sections_of_one_doc(project: Path, monkeypatch) -> None:
    dbp = _db_path(str(project))
    _seed_verified_doc(project)
    mod_id = "proj::src.mod"

    saves = _count_saves(monkeypatch)
    res = axiom_graph_add_link(
        str(project),
        links=[
            {"section_id": f"{DOC_ID}::s1", "node_id": mod_id},
            {"section_id": f"{DOC_ID}::s2", "node_id": mod_id},
        ],
    )
    assert res.startswith("Added 2 link(s) across 2 section(s)"), res
    assert len(saves) == 1
    for sid in (f"{DOC_ID}::s1", f"{DOC_ID}::s2"):
        assert f"{sid}::documents::{mod_id}" in _snapshot(dbp, sid)["edges"], sid

    assert "Wrote" in axiom_graph_write_doc(
        str(project), {"id": "other", "title": "Other", "sections": [{"id": "x1", "heading": "X1", "content": "X."}]}
    )
    files = [project / "docs" / "spec.docjson", project / "docs" / "other.docjson"]
    before = [f.read_bytes() for f in files]
    res = axiom_graph_add_link(
        str(project),
        links=[
            {"section_id": f"{DOC_ID}::s3", "node_id": mod_id},
            {"section_id": "proj::docs/other::x1", "node_id": mod_id},
        ],
    )
    assert res.startswith("ERROR:") and "nothing was written" in res, res
    assert [f.read_bytes() for f in files] == before


@workflow(
    purpose="content_file stores a UTF-8 file's text byte-for-byte without its BOM; a path outside the project and "
    "temp roots, a '..' escape, or a content_file combined with inline content is refused; write_doc reads doc_file",
)
def test_content_file_and_doc_file_inputs(project: Path, monkeypatch, tmp_path_factory) -> None:
    import tempfile

    scratch = project / "scratch-tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(scratch))
    _seed_verified_doc(project)
    sid = f"{DOC_ID}::s1"
    spec = project / "docs" / "spec.docjson"

    text = 'Ünïcødé — "quotes" and \\backslashes\\\nsecond line\n'
    body = scratch / "body.md"
    body.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    res = axiom_graph_update_section(str(project), sid, content_file=str(body))
    assert res.startswith("Updated"), res
    assert json.loads(spec.read_text(encoding="utf-8"))["sections"][0]["content"] == text

    elsewhere = tmp_path_factory.mktemp("elsewhere")
    outside = elsewhere / "outside.md"
    outside.write_text("Outside.", encoding="utf-8")
    escape = project / ".." / elsewhere.name / "outside.md"
    for bad in (str(outside), str(escape)):
        res = axiom_graph_update_section(str(project), sid, content_file=bad)
        assert res.startswith("ERROR:") and "outside" in res, res
    res = axiom_graph_update_section(str(project), sid, content="Inline.", content_file=str(body))
    assert res.startswith("ERROR:") and "not both" in res, res
    assert json.loads(spec.read_text(encoding="utf-8"))["sections"][0]["content"] == text

    doc_src = scratch / "doc.json"
    doc_src.write_text(
        json.dumps(
            {"id": "from-file", "title": "From File", "sections": [{"id": "a", "heading": "A", "content": "Ä."}]}
        ),
        encoding="utf-8",
    )
    res = axiom_graph_write_doc(str(project), doc_file=str(doc_src))
    assert res.startswith("Wrote"), res
    written = json.loads((project / "docs" / "from-file.docjson").read_text(encoding="utf-8"))
    assert written["sections"][0]["content"] == "Ä."


# ---------------------------------------------------------------------------
# US-5: pasted read_doc footers are not stored as prose
# ---------------------------------------------------------------------------

_MARKED = (
    "Body one.\n\n<!-- axiom:linked-nodes -->\n**Linked nodes:**\n- `proj::src.mod::foo` -- foo\n"
    "<!-- /axiom:linked-nodes -->\n"
)
_UNMARKED = "Body two.\n\n**Linked nodes:**\n- `proj::src.mod::foo` -- foo\n"
_UNMARKED_THEN_PROSE = "Body three.\n\n**Linked nodes:**\n- `proj::src.mod::foo`\n\nProse written after the list."
_INLINE = "See the **Linked nodes:** list that read_doc shows for this section."
#: (incoming content, stored content)
_FOOTER_CASES = [
    (_MARKED, "Body one."),
    (_UNMARKED, "Body two."),
    (_UNMARKED_THEN_PROSE, _UNMARKED_THEN_PROSE),
    (_INLINE, _INLINE),
]


def _stored_contents(project: Path) -> dict[str, str]:
    data = json.loads((project / "docs" / "spec.docjson").read_text(encoding="utf-8"))
    return {s["id"]: s.get("content", "") for s in data["sections"]}


@workflow(
    purpose="update_section, patch_section, add_section and write_doc store content without a pasted marked "
    "linked-nodes footer or a trailing unmarked one, keep prose after an unmarked block and inline mentions, "
    "and say so in the result",
)
@pytest.mark.parametrize("writer", ["update_section", "patch_section", "add_section", "write_doc"])
def test_pasted_linked_nodes_footers_are_stripped_on_write(project: Path, writer: str) -> None:
    note = "stripped a pasted linked-nodes footer"
    numbered = list(enumerate(_FOOTER_CASES, 1))
    if writer == "write_doc":
        doc = {
            "id": "spec",
            "title": "Spec",
            "sections": [{"id": f"f{i}", "heading": f"F{i}", "content": incoming} for i, (incoming, _) in numbered],
        }
        res = axiom_graph_write_doc(str(project), doc)
        assert "from 2 sections" in res, res
        assert _stored_contents(project) == {f"f{i}": stored for i, (_, stored) in numbered}
        return

    _seed_verified_doc(project, _linked_doc(n_sections=4))
    if writer == "add_section":
        res = axiom_graph_add_section(
            str(project),
            DOC_ID,
            sections=[
                {"section_id": f"f{i}", "heading": f"F{i}", "content": incoming} for i, (incoming, _) in numbered
            ],
        )
        assert "from 2 sections" in res, res
        stored = _stored_contents(project)
        for i, (_, expected) in numbered:
            assert stored[f"f{i}"] == expected, i
        return

    for i, (incoming, expected) in numbered:
        sid = f"{DOC_ID}::s{i}"
        if writer == "update_section":
            res = axiom_graph_update_section(str(project), sid, content=incoming)
        else:
            res = axiom_graph_patch_section(str(project), sid, incoming, anchor="$")
            expected = f"Section {i}.\n{expected}"
        assert (note in res) == (i <= 2), (i, res)
        assert _stored_contents(project)[f"s{i}"] == expected, i


def test_patch_section_strips_footers_from_incoming_text_only(project: Path) -> None:
    _seed_verified_doc(project)
    _hand_edit_section(project, "s1", _UNMARKED)
    res = axiom_graph_patch_section(str(project), f"{DOC_ID}::s1", "Body 2.", old_string="Body two.")
    assert "Patched" in res and "stripped" not in res, res
    assert _stored_contents(project)["s1"] == _UNMARKED.replace("Body two.", "Body 2.")


def test_patch_section_without_new_string_is_an_error(project: Path) -> None:
    _seed_verified_doc(project)
    doc_file = project / "docs" / "spec.docjson"
    before = doc_file.read_bytes()
    res = axiom_graph_patch_section(str(project), f"{DOC_ID}::s1", anchor="$")
    assert res.startswith("ERROR:"), res
    assert doc_file.read_bytes() == before
