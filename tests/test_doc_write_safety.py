"""Behavioural tests for the shared doc write-safety paths.

Multi-doc locking (sorted, one deadline, release-all on timeout, re-entrant
per thread), cross-doc link patching under the write locks, all-or-nothing
``accept_doc_edits`` and ``write_doc``'s ``expected_hash`` guard.  Tool calls
enter through ``axiom_graph.docjson.api``; link patching through
``axiom_graph.index.link_maintenance``, the function every rename path calls.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from axiom_graph.docjson.api import (
    axiom_graph_accept_doc_edits,
    axiom_graph_clone_doc,
    axiom_graph_update_section,
    axiom_graph_write_doc,
)
from axiom_graph.index import db, doc_lock
from axiom_graph.index.doc_io import lock_docs, save_doc_json
from axiom_graph.index.link_maintenance import patch_doc_links_batch
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import apply_rename, build_index, revert_rename

A = "proj::docs/a"
B = "proj::docs/b"
DOC_A = {"title": "A", "sections": [{"id": "s1", "heading": "S1", "content": "One."}]}
DOC_B = {
    "title": "B",
    "sections": [{"id": "t1", "heading": "T1", "content": "Bee.", "links": [{"node_id": f"{A}::s1"}]}],
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.docjson").write_text(json.dumps(DOC_A, indent=2), encoding="utf-8")
    (tmp_path / "docs" / "b.docjson").write_text(json.dumps(DOC_B, indent=2), encoding="utf-8")
    build_index(_db_path(str(tmp_path)), tmp_path)
    return tmp_path.resolve()


def _files(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((root / "docs").glob("*.docjson"))}


def _lock_is_free(root: Path, doc: Path) -> bool:
    """Whether another thread can take *doc*'s write lock right now."""
    got: list[bool] = []

    def probe() -> None:
        try:
            with doc_lock.doc_write_lock(root, doc, timeout=0.2):
                got.append(True)
        except doc_lock.DocLockTimeout:
            got.append(False)

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return got[0]


def test_lock_docs_releases_everything_on_timeout_and_is_reentrant(project: Path) -> None:
    """A timeout part-way leaves no lock held; a nested lock_docs on a held doc does not wait for itself."""
    a, b = project / "docs" / "a.docjson", project / "docs" / "b.docjson"
    with doc_lock.doc_write_lock(project, b):
        with pytest.raises(doc_lock.DocLockTimeout):
            with lock_docs(project, [b, a], timeout=0.3):
                pass
        assert _lock_is_free(project, a)
    with lock_docs(project, [a], timeout=0.3):
        with lock_docs(project, [a, project / "docs" / "." / "a.docjson", b], timeout=0.3):
            assert not _lock_is_free(project, b)
        assert not _lock_is_free(project, a)  # the outer hold survives the inner block
        assert _lock_is_free(project, b)
    assert _lock_is_free(project, a)


@pytest.mark.parametrize("failure", ["lock-held", "unreadable"])
def test_link_patching_never_writes_unlocked_and_reports_what_it_skipped(project: Path, failure: str) -> None:
    """A rename map is applied under the write locks of every affected doc: a held lock leaves every file
    unchanged and names it; an unparseable file is named instead of silently skipped."""
    mapping = {f"{A}::s1": f"{A}::s9"}
    if failure == "lock-held":
        before = _files(project)
        with doc_lock.doc_write_lock(project, project / "docs" / "b.docjson"):
            result = patch_doc_links_batch(project, mapping)
        assert result.files_patched == 0 and [Path(f).name for f in result.not_patched] == ["b.docjson"]
        assert _files(project) == before
        return
    (project / "docs" / "broken.docjson").write_text("{not json", encoding="utf-8")
    result = patch_doc_links_batch(project, mapping)
    assert [Path(f).name for f in result.unreadable] == ["broken.docjson"]
    assert result.files_patched == 1
    links = json.loads((project / "docs" / "b.docjson").read_text(encoding="utf-8"))["sections"][0]["links"]
    assert links == [{"node_id": f"{A}::s9"}]


@pytest.mark.parametrize("failure", ["unknown-section", "lock-held"])
def test_accept_doc_edits_accepts_every_section_or_none(project: Path, failure: str) -> None:
    """One section that cannot be accepted -- unknown, or its doc locked by another writer -- refuses the whole
    call: the valid section's doc is not stamped and its index rows are unchanged."""
    before = _files(project)
    with db._connect(_db_path(str(project))) as conn:
        rows = conn.execute("SELECT id, own_status FROM nodes ORDER BY id").fetchall()
    if failure == "unknown-section":
        res = axiom_graph_accept_doc_edits(str(project), section_ids=[f"{A}::s1", f"{B}::nope"])
    else:
        with doc_lock.doc_write_lock(project, project / "docs" / "b.docjson"):
            doc_lock_timeout = doc_lock.DEFAULT_LOCK_TIMEOUT
            doc_lock.DEFAULT_LOCK_TIMEOUT = 0.3
            try:
                res = axiom_graph_accept_doc_edits(str(project), section_ids=[f"{A}::s1", f"{B}::t1"])
            finally:
                doc_lock.DEFAULT_LOCK_TIMEOUT = doc_lock_timeout
    assert res.startswith("ERROR:") and "nothing was written" in res, res
    assert _files(project) == before
    with db._connect(_db_path(str(project))) as conn:
        assert conn.execute("SELECT id, own_status FROM nodes ORDER BY id").fetchall() == rows
    ok = axiom_graph_accept_doc_edits(str(project), section_ids=[f"{A}::s1", f"{B}::t1"])
    assert ok.startswith("Accepted 2 section(s)"), ok


def _doc_hash(reply: str) -> str:
    return next(line.split(":", 1)[1].strip() for line in reply.splitlines() if "doc_hash" in line)


def test_write_doc_expected_hash_guards_an_overwrite(project: Path) -> None:
    """write_doc reports a doc_hash; an overwrite carrying a stale one (or naming a doc that does not exist) is
    refused with the current hash and changes nothing, and the right one overwrites."""

    def doc(**over) -> dict:
        return {"id": "c", "title": "C", "sections": [{"id": "x", "heading": "X", "content": "v1"}], **over}

    first = axiom_graph_write_doc(str(project), doc_json=doc())
    seen = _doc_hash(first)
    second = axiom_graph_write_doc(str(project), doc_json=doc(title="C2"))
    assert not second.startswith("ERROR"), second
    before = _files(project)

    stale = axiom_graph_write_doc(str(project), doc_json=doc(title="C3"), expected_hash=seen)
    assert stale.startswith("ERROR:") and _doc_hash(second) in stale and "nothing was written" in stale, stale
    assert _files(project) == before
    missing = axiom_graph_write_doc(str(project), doc_json=doc(id="new"), expected_hash=seen)
    assert missing.startswith("ERROR:") and not (project / "docs" / "new.docjson").exists(), missing

    ok = axiom_graph_write_doc(str(project), doc_json=doc(title="C3"), expected_hash=_doc_hash(second))
    assert not ok.startswith("ERROR"), ok
    assert json.loads((project / "docs" / "c.docjson").read_text(encoding="utf-8"))["title"] == "C3"


@pytest.mark.parametrize("tool", ["write_doc", "clone_doc"])
def test_a_new_doc_write_waits_for_the_destination_lock(project: Path, tool: str, monkeypatch) -> None:
    """write_doc and clone_doc take the destination's write lock before checking or writing it: another holder
    makes the call fail with nothing written -- no file, no doc node."""
    monkeypatch.setattr(doc_lock, "DEFAULT_LOCK_TIMEOUT", 0.3)
    dest = project / "docs" / "c.docjson"
    with doc_lock.doc_write_lock(project, dest):
        if tool == "write_doc":
            doc = {"id": "c", "title": "C", "sections": [{"id": "x", "heading": "X", "content": "v1"}]}
            res = axiom_graph_write_doc(str(project), doc_json=doc)
        else:
            res = axiom_graph_clone_doc(str(project), A, "c")
    assert res.startswith("ERROR:") and "nothing was written" in res, res
    assert not dest.exists() and not (project / "docs" / "c.json").exists()
    assert db.get_node(_db_path(str(project)), "proj::docs/c") is None


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_a_section_rename_locks_the_docs_linking_it_before_writing(project: Path, mode: str, monkeypatch) -> None:
    """Renaming a section takes its doc's lock and the locks of the docs that link it in one acquisition: one of
    them held elsewhere refuses the rename with nothing written; once free, the rename re-points the link."""
    monkeypatch.setattr(doc_lock, "DEFAULT_LOCK_TIMEOUT", 0.3)

    def rename() -> str:
        if mode == "single":
            return axiom_graph_update_section(str(project), section_id=f"{A}::s1", new_id="s9")
        return axiom_graph_update_section(str(project), edits=[{"section_id": f"{A}::s1", "new_id": "s9"}])

    before = _files(project)
    with db._connect(_db_path(str(project))) as conn:
        rows = conn.execute("SELECT id, own_status, link_status FROM nodes ORDER BY id").fetchall()
    with doc_lock.doc_write_lock(project, project / "docs" / "b.docjson"):
        res = rename()
    assert res.startswith("ERROR:") and "nothing was written" in res, res
    assert _files(project) == before
    with db._connect(_db_path(str(project))) as conn:
        assert conn.execute("SELECT id, own_status, link_status FROM nodes ORDER BY id").fetchall() == rows

    ok = rename()
    assert not ok.startswith("ERROR") and "WARNING" not in ok, ok
    links = json.loads((project / "docs" / "b.docjson").read_text(encoding="utf-8"))["sections"][0]["links"]
    assert links == [{"node_id": f"{A}::s9"}]


F = "proj::mod_a::f"


@pytest.fixture
def code_project(project: Path) -> Path:
    """The two-doc project plus a module function ``mod_a.f`` that doc ``c`` links."""
    (project / "mod_a.py").write_text("def f():\n    return sum([40, 1])\n", encoding="utf-8")
    c = {"title": "C", "sections": [{"id": "u", "heading": "U", "content": "f.", "links": [{"node_id": F}]}]}
    (project / "docs" / "c.docjson").write_text(json.dumps(c, indent=2), encoding="utf-8")
    build_index(_db_path(str(project)), project)
    return project


@pytest.mark.parametrize("path", ["update_section", "apply_rename", "revert_rename", "build"])
def test_renames_name_the_docs_whose_links_they_could_not_repoint(code_project: Path, path: str, monkeypatch) -> None:
    """Every rename path that rewrites other docs' links reports the files it could not rewrite (a busy write
    lock) as still linking the old id, and an unreadable file -- which may not link it at all -- only as not
    checked: a section rename in its reply, apply_rename / revert_rename in their results, an automatic rename
    during build in the build warnings."""
    monkeypatch.setattr(doc_lock, "DEFAULT_LOCK_TIMEOUT", 0.3)
    root, dbp = code_project, _db_path(str(code_project))
    c_file = root / "docs" / "c.docjson"
    if path == "update_section":
        # Its locks are all taken up front, so what is left is a file it cannot read, unrelated to the rename.
        (root / "docs" / "broken.docjson").write_text("{not json", encoding="utf-8")
        res = axiom_graph_update_section(str(root), section_id=f"{A}::s1", new_id="s9")
        assert not res.startswith("ERROR") and "broken.docjson" in res, res
        assert "WARNING: 1 DocJSON file(s) could not be checked" in res and "still link" not in res, res
        return
    G = "proj::mod_a::g"
    if path in ("apply_rename", "revert_rename"):
        (root / "mod_a.py").write_text("def g():\n    while True:\n        print('other')\n        break\n")
        build_index(dbp, root)
        (root / "docs" / "broken.docjson").write_text("{not json", encoding="utf-8")
        if path == "apply_rename":
            with doc_lock.doc_write_lock(root, c_file):
                result = apply_rename(dbp, root, F, G)
            assert result.applied, result
            stays = F
        else:
            assert apply_rename(dbp, root, F, G).applied
            with doc_lock.doc_write_lock(root, c_file):
                result = revert_rename(dbp, root, G)
            assert result.reverted, result
            stays = G
        assert [Path(f).name for f in result.links_not_patched] == ["c.docjson"]
        assert [Path(f).name for f in result.links_unreadable] == ["broken.docjson"]
    else:
        stays = F
        (root / "mod_b.py").write_text("def f():\n    return sum([40, 1])\n", encoding="utf-8")
        (root / "mod_a.py").unlink()
        with doc_lock.doc_write_lock(root, c_file):
            summary = build_index(dbp, root)
        assert any(F in w and "c.docjson" in w and "still link" in w for w in summary.warnings), summary.warnings
    links = json.loads(c_file.read_text(encoding="utf-8"))["sections"][0]["links"]
    assert links == [{"node_id": stays}]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_an_atomic_save_keeps_the_file_mode(tmp_path: Path) -> None:
    """A saved doc keeps the permission bits it had; a new doc gets the umask default, not mkstemp's 0o600."""
    kept = tmp_path / "kept.docjson"
    kept.write_text("{}", encoding="utf-8")
    kept.chmod(0o640)
    save_doc_json(kept, DOC_A)
    assert stat.S_IMODE(kept.stat().st_mode) == 0o640

    umask = os.umask(0)
    os.umask(umask)
    fresh = tmp_path / "fresh.docjson"
    save_doc_json(fresh, DOC_A)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o666 & ~umask
