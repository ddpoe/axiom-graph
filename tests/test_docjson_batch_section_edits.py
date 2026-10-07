"""Behavioural tests for batch ``update_section`` / ``patch_section`` (``edits=[...]``).

A batch applies section edits across docs in one call: every item is checked
first, then each touched file is written and re-indexed once.  It leaves the
same files and statuses as the same edits made one call at a time, and an
invalid item, a held lock or a failed file write leaves no file truncated
and, before the write phase, nothing written at all.  All calls enter through
``axiom_graph.docjson.api``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from axiom_annotations import Step, workflow

import axiom_graph.docjson.api as api
from axiom_graph.docjson.api import axiom_graph_patch_section, axiom_graph_update_section, content_hash
from axiom_graph.index import db, doc_io, doc_lock, doc_stamps
from axiom_graph.index.doc_lock import doc_write_lock
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary

CODE_ID = "proj::src.mod::foo"
A = "proj::docs/a"
B = "proj::docs/b"

DOC_A = {
    "title": "A",
    "sections": [
        {"id": "s1", "heading": "S1", "content": "Documents foo.", "links": [{"node_id": CODE_ID}]},
        {"id": "s2", "heading": "S2", "content": "dup dup"},
        {"id": "s3", "heading": "S3", "content": "Third."},
    ],
}
DOC_B = {"title": "B", "sections": [{"id": "t1", "heading": "T1", "content": "Bee."}, {"id": "t2", "heading": "T2"}]}

TOOLS = {"update": axiom_graph_update_section, "patch": axiom_graph_patch_section}

#: Items of one cross-doc batch per tool.  Two items edit A::s1, the first
#: naming foo as the offender it reconciles; the update batch also renames a
#: section of B and then edits it under its new id.
BATCHES = {
    "update": [
        {"section_id": f"{A}::s1", "content": "First pass.", "addresses": [CODE_ID]},
        {"section_id": f"{A}::s1", "content": "Second pass.", "expected_hash": content_hash("First pass.")},
        {"section_id": f"{B}::t1", "new_id": "t9"},
        {"section_id": f"{B}::t9", "content": "Renamed."},
    ],
    "patch": [
        {"section_id": f"{A}::s1", "new_string": "one", "anchor": "$", "addresses": [CODE_ID]},
        {"section_id": f"{A}::s1", "new_string": "two", "anchor": "$"},
        {"section_id": f"{B}::t1", "new_string": "Be", "old_string": "Bee"},
    ],
}


def _make_project(root: Path, doc_b: dict = DOC_B, transitive: bool = False) -> Path:
    """Code node foo, docs a and b, built; then foo changes, so A::s1 is LINKED_STALE through it.

    With *transitive*, docs tagged ``consumer`` take LINKED_STALE through the doc sections they link.
    """
    toml = '[axiom_graph]\nproject_id = "proj"\n'
    if transitive:
        toml += '\n[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n'
    (root / "axiom-graph.toml").write_text(toml, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("def foo():\n    return 0\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "a.docjson").write_text(json.dumps(DOC_A, indent=2), encoding="utf-8")
    (root / "docs" / "b.docjson").write_text(json.dumps(doc_b, indent=2), encoding="utf-8")
    dbp = _db_path(str(root))
    build_index(dbp, root)
    compute_check_summary(dbp, root)
    time.sleep(0.02)
    (root / "src" / "mod.py").write_text("def foo():\n    return 42\n", encoding="utf-8")
    build_index(dbp, root)
    compute_check_summary(dbp, root)
    return root


@pytest.fixture
def project(tmp_path_factory) -> Path:
    return _make_project(tmp_path_factory.mktemp("batch"))


def _files(root: Path) -> dict[str, bytes]:
    return {name: (root / "docs" / name).read_bytes() for name in ("a.docjson", "b.docjson")}


def _without_stamps(value):
    if isinstance(value, dict):
        return {k: _without_stamps(v) for k, v in value.items() if k != doc_stamps.STAMP_KEY}
    if isinstance(value, list):
        return [_without_stamps(v) for v in value]
    return value


def _indexed_sections(root: Path) -> dict[str, str | None]:
    with db._connect(_db_path(str(root))) as conn:
        rows = conn.execute("SELECT id, level_2 FROM nodes WHERE subtype = 'docjson_section'").fetchall()
    return {r["id"]: r["level_2"] for r in rows}


def _doc_statuses(root: Path) -> dict[str, tuple[str, str]]:
    statuses = compute_check_summary(_db_path(str(root)), root).statuses
    return {nid: s[:2] for nid, s in statuses.items() if nid.startswith(("proj::docs/a", "proj::docs/b"))}


@pytest.mark.parametrize("tool", ["update", "patch"])
@workflow(
    purpose="An agent reconciling docs edits sections of two docs in one call -- two edits of one section, the "
    "second guarded by the first's content hash, one naming the offender it reconciles -- and gets the same files "
    "and statuses as the same edits made one call at a time, with the stale section cleared"
)
def test_a_cross_doc_batch_matches_the_same_edits_made_one_at_a_time(project: Path, tmp_path_factory, tool: str):
    twin = _make_project(tmp_path_factory.mktemp("twin"))
    items = BATCHES[tool]
    assert _doc_statuses(project)[f"{A}::s1"][1] == "LINKED_STALE"

    口 = Step(step_num=1, name="Batch", purpose="All items in one call")
    res = TOOLS[tool](str(project), edits=json.loads(json.dumps(items)))
    verb = "Updated" if tool == "update" else "Patched"
    assert res.startswith(f"{verb} {len(items)} section(s) in 2 doc(s)"), res

    口 = Step(step_num=2, name="One call per item", purpose="The same edits on a twin project")
    for item in items:
        single = TOOLS[tool](str(twin), **item)
        assert not single.startswith("ERROR"), single

    口 = Step(
        step_num=3,
        name="Compare",
        purpose="Equal files ignoring stamps, equal statuses, per-item hashes, and A::s1 cleared",
    )
    for name in ("a.docjson", "b.docjson"):
        batch_doc = json.loads((project / "docs" / name).read_text(encoding="utf-8"))
        single_doc = json.loads((twin / "docs" / name).read_text(encoding="utf-8"))
        assert _without_stamps(batch_doc) == _without_stamps(single_doc), name
    statuses = _doc_statuses(project)
    assert statuses == _doc_statuses(twin)
    assert statuses[f"{A}::s1"] == ("VERIFIED", "VERIFIED")
    final_s1 = json.loads((project / "docs" / "a.docjson").read_text(encoding="utf-8"))["sections"][0]["content"]
    assert f"{A}::s1  " in res and f"content_hash: {content_hash(final_s1)}" in res, res
    assert res.count("content_hash:") == len(items)


@pytest.mark.parametrize(
    ("tool", "bad_item", "named"),
    [
        pytest.param("update", {"section_id": f"{B}::nope", "content": "x"}, "edits[2]", id="unknown-section"),
        pytest.param(
            "update", {"section_id": f"{B}::t1", "content": "x", "expected_hash": "0" * 64}, "edits[2]", id="stale-hash"
        ),
        pytest.param(
            "patch", {"section_id": f"{B}::t1", "new_string": "x", "anchor": "$"}, None, id="valid-last-item-control"
        ),
        pytest.param(
            "patch", {"section_id": f"{A}::s2", "new_string": "x", "old_string": "dup"}, "edits[2]", id="non-unique"
        ),
        pytest.param(
            "update", {"section_id": f"{B}::t1", "content": "x", "addresses": [CODE_ID]}, f"{B}::t1", id="not-offender"
        ),
    ],
)
def test_an_invalid_item_anywhere_in_a_batch_writes_nothing(project: Path, tool: str, bad_item: dict, named) -> None:
    """The invalid last item is named, and neither doc's file nor its indexed sections change."""
    first = (
        [{"section_id": f"{A}::s3", "content": "Edited."}, {"section_id": f"{B}::t2", "content": "Edited."}]
        if tool == "update"
        else [
            {"section_id": f"{A}::s3", "new_string": "Edited.", "anchor": "$"},
            {"section_id": f"{B}::t2", "new_string": "Edited.", "anchor": "^"},
        ]
    )
    files, indexed = _files(project), _indexed_sections(project)
    res = TOOLS[tool](str(project), edits=[*first, bad_item])
    if named is None:  # control: the same batch shape with a valid last item writes both docs
        assert res.startswith("Patched 3 section(s) in 2 doc(s)"), res
        assert all(_files(project)[n] != files[n] for n in files)
        return
    assert res.startswith("ERROR:") and named in res and "othing was written" in res, res
    assert _files(project) == files
    assert _indexed_sections(project) == indexed


@pytest.mark.parametrize("failure", ["lock-held", "replace-fails"])
def test_a_batch_that_cannot_finish_never_truncates_a_file(project: Path, monkeypatch, failure: str) -> None:
    """A held lock refuses the batch within the deadline with nothing written; a failed file write leaves that file
    exactly as it was (the other doc is written whole) and is reported."""
    files, indexed = _files(project), _indexed_sections(project)
    edits = [{"section_id": f"{A}::s3", "content": "Edited."}, {"section_id": f"{B}::t2", "content": "Edited."}]
    if failure == "lock-held":
        monkeypatch.setattr(doc_lock, "DEFAULT_LOCK_TIMEOUT", 0.3)
        started = time.monotonic()
        with doc_write_lock(project, project / "docs" / "b.docjson", timeout=1):
            res = axiom_graph_update_section(str(project), edits=edits)
        assert time.monotonic() - started < 5
        assert res.startswith("ERROR:") and "nothing was written" in res, res
        assert _files(project) == files
        assert _indexed_sections(project) == indexed
        return
    real_replace = doc_io.os.replace

    def failing_replace(src, dst):
        if str(dst).endswith("b.docjson"):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(doc_io.os, "replace", failing_replace)
    res = axiom_graph_update_section(str(project), edits=edits)
    assert res.startswith("ERROR:") and "b.docjson" in res, res
    assert _files(project)["b.docjson"] == files["b.docjson"]
    assert (
        json.loads((project / "docs" / "a.docjson").read_text(encoding="utf-8"))["sections"][2]["content"] == "Edited."
    )
    assert not list((project / "docs").glob("*.tmp"))


def test_batch_work_scales_with_files_touched_not_items(project: Path, monkeypatch) -> None:
    """Two items or eight over the same two docs: one connection, one save per doc, the same doc-file reads."""
    real_connect, real_read, real_save = sqlite3.connect, Path.read_text, api.save_and_reindex
    counts = {"connections": 0, "saves": 0, "reads": 0}

    def connect(*args, **kwargs):
        counts["connections"] += 1
        return real_connect(*args, **kwargs)

    def read_text(self, *args, **kwargs):
        if self.suffix == ".docjson":
            counts["reads"] += 1
        return real_read(self, *args, **kwargs)

    def save(*args, **kwargs):
        counts["saves"] += 1
        return real_save(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(api, "save_and_reindex", save)
    seen = []
    for n in (1, 4):
        edits = [
            {"section_id": f"{doc}::{sec}", "new_string": f"line {i}", "anchor": "$"}
            for i in range(n)
            for doc, sec in ((A, "s3"), (B, "t2"))
        ]
        counts.update(connections=0, saves=0, reads=0)
        res = axiom_graph_patch_section(str(project), edits=edits)
        assert res.startswith(f"Patched {2 * n} section(s) in 2 doc(s)"), res
        seen.append(dict(counts))
    assert seen[0] == seen[1]
    assert seen[0]["connections"] == 1 and seen[0]["saves"] == 2


def test_a_batch_reads_offenders_as_they_stood_before_it(tmp_path_factory) -> None:
    """B::t1 links A::s1 (LINKED_STALE through foo; B is not transitive, so B::t1 stays VERIFIED); one batch
    edits A::s1 and then B::t1.  The offenders, stamps and open targets for B are read before the batch writes
    A; with no ``addresses`` the batch leaves the same files (stamps included) and statuses as the two edits
    made one call at a time.  Where an earlier item's ``addresses`` clears a later doc's offender, the batch is
    more lenient than single calls instead (see the next test)."""
    doc_b = json.loads(json.dumps(DOC_B))
    doc_b["sections"][0]["links"] = [{"node_id": f"{A}::s1"}]
    project = _make_project(tmp_path_factory.mktemp("chain"), doc_b)
    twin = _make_project(tmp_path_factory.mktemp("chain-twin"), doc_b)
    items = [{"section_id": f"{A}::s1", "content": "Changed."}, {"section_id": f"{B}::t1", "content": "Bee two."}]
    for item in items:
        single = axiom_graph_update_section(str(twin), **item)
        assert not single.startswith("ERROR"), single

    res = axiom_graph_update_section(str(project), edits=json.loads(json.dumps(items)))
    assert res.startswith("Updated 2 section(s) in 2 doc(s)"), res
    for name in ("a.docjson", "b.docjson"):
        batch_doc = json.loads((project / "docs" / name).read_text(encoding="utf-8"))
        single_doc = json.loads((twin / "docs" / name).read_text(encoding="utf-8"))
        assert batch_doc == single_doc, name
    assert _doc_statuses(project) == _doc_statuses(twin)


@pytest.mark.parametrize("names", ["code", "section"])
def test_a_batch_accepts_addresses_an_earlier_item_already_cleared(tmp_path_factory, names: str) -> None:
    """Consumer doc B::t1 links A::s1, which links foo, so both are LINKED_STALE.  A batch editing A::s1 with
    addresses=[foo] and then B::t1 naming foo (or A::s1) is accepted, because B's offenders are read before
    the batch writes A.  Made one call at a time, the first call's addresses already clears B::t1, so the
    second is refused ("Current offenders: none").  The statuses both ways end the same."""
    doc_b = json.loads(json.dumps(DOC_B))
    doc_b["tags"] = ["consumer"]
    doc_b["sections"][0]["links"] = [{"node_id": f"{A}::s1"}]
    project = _make_project(tmp_path_factory.mktemp("lenient"), doc_b, transitive=True)
    twin = _make_project(tmp_path_factory.mktemp("lenient-twin"), doc_b, transitive=True)
    assert _doc_statuses(project)[f"{B}::t1"] == ("VERIFIED", "LINKED_STALE")
    items = [
        {"section_id": f"{A}::s1", "content": "Changed.", "addresses": [CODE_ID]},
        {"section_id": f"{B}::t1", "content": "Bee two.", "addresses": [CODE_ID if names == "code" else f"{A}::s1"]},
    ]

    first = axiom_graph_update_section(str(twin), **json.loads(json.dumps(items[0])))
    assert not first.startswith("ERROR"), first
    second = axiom_graph_update_section(str(twin), **json.loads(json.dumps(items[1])))
    assert second.startswith("ERROR") and "Current offenders: none" in second, second

    res = axiom_graph_update_section(str(project), edits=json.loads(json.dumps(items)))
    assert res.startswith("Updated 2 section(s) in 2 doc(s)"), res
    assert _doc_statuses(project) == _doc_statuses(twin)
