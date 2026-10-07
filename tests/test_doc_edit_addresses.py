"""A doc edit reconciles only the offenders it names in ``addresses=``.

``update_section`` / ``patch_section`` take ``addresses=[...]``: the current
offenders of the section this edit reconciles.  Each named offender's
receipt is refreshed at the index-live hash; the section clears once no
offender is left open, and the reply names the ones that are.  A name that is
not a current offender writes nothing.  The tool-write stamp mirrors the
receipts, so a merge carrying the file verifies exactly what the edit did.
Scenarios enter through the docjson api and compare the stored rows with
``check --full`` after each write.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.docjson import parse as docjson_parse
from axiom_graph.docjson.api import (
    _stamp_verified_against,
    axiom_graph_patch_section,
    axiom_graph_update_section,
)
from axiom_graph.index import db, dependency_set, doc_stamps, staleness
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.index.staleness import CurrentOffenders, _get_linked_stale_ids
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes, reverify_nodes
from axiom_graph.scanners import node_hashing
from tests.fixtures.full_recompute import assert_matches_full_recompute

F_ID = "proj::src.mod::f"
G_ID = "proj::src.mod::g"
TYPO_ID = "proj::src.mod::typo"
SPEC_ID = "proj::docs/spec"
S_ID = f"{SPEC_ID}::s"
T_ID = f"{SPEC_ID}::t"
OTHER_ID = "proj::docs/other"
OTHER_SECTION_ID = f"{OTHER_ID}::e1"
DOC_SECTION_ID = f"{SPEC_ID}::d"
CONSUMER_ID = "proj::docs/guide::r"
_CONFIG = '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n'
_MOD = "def f():\n    return 0\n\n\ndef g():\n    return 1\n"
_MOD_CHANGED = "def f():\n    return 10\n\n\ndef g():\n    return 11\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _section(sid: str, *links: str, content: str | None = None) -> dict:
    return {
        "id": sid,
        "heading": sid.upper(),
        "content": content or f"{sid} text.",
        "links": [{"node_id": nid} for nid in links],
    }


def _project(root: Path, sections: list[dict], *, guide: list[dict] | None = None) -> Path:
    """Write and build a project: ``src/mod.py`` (f, g), ``docs/spec.json`` and an optional consumer guide."""
    _write(root / "axiom-graph.toml", _CONFIG)
    _write(root / "src" / "__init__.py", "")
    _write(root / "src" / "mod.py", _MOD)
    _write(root / "docs" / "spec.json", json.dumps({"title": "Spec", "sections": sections}, indent=2))
    _write(
        root / "docs" / "other.json",
        json.dumps({"title": "Other", "sections": [_section("e1", content="Other text.")]}, indent=2),
    )
    if guide is not None:
        _write(root / "docs" / "guide.json", json.dumps({"title": "Guide", "tags": ["consumer"], "sections": guide}))
    dbp = _db_path(str(root))
    build_index(dbp, root)
    return dbp


def _change_code(root: Path, dbp: Path) -> None:
    """f and g change, and the change is built and checked."""
    time.sleep(0.05)
    _write(root / "src" / "mod.py", _MOD_CHANGED)
    build_index(dbp, root)
    compute_check_summary(dbp, root)


def _statuses(dbp: Path, node_id: str) -> tuple[str, str]:
    with closing(sqlite3.connect(dbp)) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row[0], row[1]


def _latest_doc_edit_meta(dbp: Path, node_id: str) -> dict:
    with closing(sqlite3.connect(dbp)) as conn:
        row = conn.execute(
            "SELECT meta FROM node_history WHERE node_id = ? AND change_type = 'AGENT_VERIFIED' ORDER BY id DESC LIMIT 1",
            (node_id,),
        ).fetchone()
    return json.loads(row[0])


def _receipts(dbp: Path, node_id: str) -> list[tuple]:
    with closing(sqlite3.connect(dbp)) as conn:
        return conn.execute(
            "SELECT target_id, code_hash, desc_hash FROM node_verification_targets WHERE node_id = ? "
            "ORDER BY target_id",
            (node_id,),
        ).fetchall()


def _stamp_entries(root: Path, sid: str) -> dict:
    sections = json.loads((root / "docs" / "spec.json").read_text(encoding="utf-8"))["sections"]
    section = next(s for s in sections if s["id"] == sid)
    return section[doc_stamps.STAMP_KEY]["verified_against"]


# ---------------------------------------------------------------------------
# Offenders clear one at a time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verified_before", [True, False], ids=["verified-before", "never-verified"])
@workflow(
    purpose="A section documenting two changed functions clears one offender per named address: naming f leaves "
    "it LINKED_STALE via g and the reply says so, naming g in a later patch clears it, the history records what each "
    "edit named, and each step matches check --full"
)
def test_offenders_clear_one_at_a_time(tmp_path: Path, verified_before: bool) -> None:
    口 = Step(
        step_num=1,
        name="Two changed offenders",
        purpose="S documents f and g; both change and are built, so S is LINKED_STALE via f and g",
    )
    dbp = _project(tmp_path, [_section("s", F_ID, G_ID)])
    if verified_before:
        mark_clean_nodes(dbp, tmp_path, [S_ID], reason="reviewed", verified_by="human")
    _change_code(tmp_path, dbp)
    assert _get_linked_stale_ids(dbp)[S_ID] == [F_ID, G_ID]

    口 = Step(step_num=2, name="Name f", purpose="update_section with new content and addresses=[f]")
    result = axiom_graph_update_section(str(tmp_path), S_ID, content="S covers the new f.", addresses=[F_ID])
    assert result.startswith(f"Updated content, addresses for section: {S_ID}"), result
    assert result.endswith(f"\n  still LINKED_STALE via: {G_ID}"), result
    assert _statuses(dbp, S_ID) == ("VERIFIED", "LINKED_STALE")
    assert _get_linked_stale_ids(dbp)[S_ID] == [G_ID]
    assert _latest_doc_edit_meta(dbp, S_ID)["addresses"] == [F_ID]
    assert_matches_full_recompute(dbp, tmp_path)

    口 = Step(step_num=3, name="Name g", purpose="patch_section appending text, with addresses=[g]")
    result = axiom_graph_patch_section(str(tmp_path), S_ID, new_string="And the new g.", anchor="$", addresses=[G_ID])
    assert result.startswith(f"Patched (appended to, addresses) section: {S_ID}"), result
    assert "still LINKED_STALE" not in result, result
    assert _statuses(dbp, S_ID) == ("VERIFIED", "VERIFIED")
    assert _statuses(dbp, SPEC_ID)[1] == "VERIFIED"
    assert _latest_doc_edit_meta(dbp, S_ID)["addresses"] == [G_ID]
    assert_matches_full_recompute(dbp, tmp_path)


# ---------------------------------------------------------------------------
# A bad name writes nothing
# ---------------------------------------------------------------------------


def _snapshot(root: Path, dbp: Path, node_id: str) -> tuple:
    """Everything a refused call must leave as it was: the file, the indexed text, history and verification."""
    with closing(sqlite3.connect(dbp)) as conn:
        node = conn.execute(
            "SELECT level_2, code_hash, desc_hash, own_status, link_status FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        history = conn.execute(
            "SELECT id, change_type, meta FROM node_history WHERE node_id = ?", (node_id,)
        ).fetchall()
        verification = conn.execute("SELECT * FROM node_verification WHERE node_id = ?", (node_id,)).fetchall()
    return (root / "docs" / "spec.json").read_bytes(), node, history, verification, _receipts(dbp, node_id)


@workflow(
    purpose="An addresses list naming anything that is not a current offender of the section is refused with an "
    "error listing the current offenders, and nothing is written: file bytes, indexed text, history and receipts "
    "are unchanged; on a VERIFIED section the error says there are none"
)
def test_a_name_that_is_not_an_offender_writes_nothing(tmp_path: Path) -> None:
    dbp = _project(tmp_path, [_section("s", F_ID), _section("t")])
    mark_clean_nodes(dbp, tmp_path, [S_ID], reason="reviewed", verified_by="human")
    _change_code(tmp_path, dbp)
    before = {nid: _snapshot(tmp_path, dbp, nid) for nid in (S_ID, T_ID)}

    result = axiom_graph_update_section(str(tmp_path), S_ID, content="Changed.", addresses=[F_ID, TYPO_ID])
    assert result == (
        f"ERROR: addresses must name current offenders of {S_ID}; not offenders: {TYPO_ID}. "
        f"Current offenders: {F_ID}. Nothing was written."
    )
    result = axiom_graph_patch_section(str(tmp_path), S_ID, new_string="More.", anchor="$", addresses=[TYPO_ID])
    assert result.startswith(f"ERROR: addresses must name current offenders of {S_ID}; not offenders: {TYPO_ID}.")
    result = axiom_graph_update_section(str(tmp_path), T_ID, content="Changed.", addresses=[F_ID])
    assert result == (
        f"ERROR: addresses must name current offenders of {T_ID}; not offenders: {F_ID}. "
        "Current offenders: none. Nothing was written."
    )
    assert {nid: _snapshot(tmp_path, dbp, nid) for nid in (S_ID, T_ID)} == before


def test_a_name_on_a_section_carried_on_a_frozen_doc_says_mark_clean_clears_it(tmp_path: Path) -> None:
    """A section that went LINKED_STALE before its doc was frozen has no offender to name; the error says so."""
    dbp = _project(tmp_path, [_section("s", F_ID)])
    mark_clean_nodes(dbp, tmp_path, [S_ID], reason="reviewed", verified_by="human")
    _change_code(tmp_path, dbp)
    _write(tmp_path / "axiom-graph.toml", _CONFIG + 'frozen_tags = ["frozen"]\n')
    _write(
        tmp_path / "docs" / "spec.json",
        json.dumps({"title": "Spec", "tags": ["frozen"], "sections": [_section("s", F_ID)]}, indent=2),
    )
    build_index(dbp, tmp_path)
    compute_check_summary(dbp, tmp_path)
    assert _statuses(dbp, S_ID)[1] == "LINKED_STALE"
    before = _snapshot(tmp_path, dbp, S_ID)

    result = axiom_graph_update_section(str(tmp_path), S_ID, content="Changed.", addresses=[F_ID])
    assert result == (
        f"ERROR: addresses must name current offenders of {S_ID}; not offenders: {F_ID}. "
        "Current offenders: none (LINKED_STALE carried on a frozen doc; mark_clean clears it). Nothing was written."
    )
    assert _snapshot(tmp_path, dbp, S_ID) == before


# ---------------------------------------------------------------------------
# Doc offenders
# ---------------------------------------------------------------------------


@workflow(
    purpose="A section documenting a whole doc is LINKED_STALE via that doc when the doc's text changes; naming the "
    "doc in addresses clears it, and a later change to the doc flags it again, each step matching check --full"
)
def test_a_section_documenting_a_doc_clears_when_it_names_the_doc(tmp_path: Path) -> None:
    dbp = _project(tmp_path, [_section("d", OTHER_ID)])
    mark_clean_nodes(dbp, tmp_path, [DOC_SECTION_ID], reason="reviewed", verified_by="human")
    assert not axiom_graph_update_section(str(tmp_path), OTHER_SECTION_ID, content="Other, changed.").startswith(
        "ERROR"
    )
    assert _get_linked_stale_ids(dbp)[DOC_SECTION_ID] == [OTHER_ID]

    result = axiom_graph_update_section(str(tmp_path), DOC_SECTION_ID, content="D re-read.", addresses=[OTHER_ID])
    assert "still LINKED_STALE" not in result, result
    assert _statuses(dbp, DOC_SECTION_ID) == ("VERIFIED", "VERIFIED")
    assert_matches_full_recompute(dbp, tmp_path)

    assert not axiom_graph_update_section(str(tmp_path), OTHER_SECTION_ID, content="Other, again.").startswith("ERROR")
    assert _get_linked_stale_ids(dbp)[DOC_SECTION_ID] == [OTHER_ID]
    assert_matches_full_recompute(dbp, tmp_path)


def _reverify_f(root: Path) -> str:
    reverify_nodes(_db_path(str(root)), root, [F_ID], "reviewed", verified_by="human")
    return ""


def _address_f_on_s(root: Path) -> str:
    return axiom_graph_update_section(str(root), S_ID, content="S covers the new f.", addresses=[F_ID])


@pytest.mark.parametrize("clear", [_reverify_f, _address_f_on_s], ids=["reverify-f", "addresses-f-on-s"])
@workflow(
    purpose="A consumer section stale through a documented section may name that section: the call is accepted, "
    "refreshes no receipt, and replies that it clears when the section does; reconciling the section's own offender "
    "clears both, each step matching check --full"
)
def test_a_consumer_section_clears_when_the_section_it_reads_does(tmp_path: Path, clear) -> None:
    dbp = _project(tmp_path, [_section("s", F_ID)], guide=[_section("r", S_ID)])
    _change_code(tmp_path, dbp)
    assert _get_linked_stale_ids(dbp, transitive_tags=["consumer"])[CONSUMER_ID] == [S_ID]
    receipts = _receipts(dbp, CONSUMER_ID)

    result = axiom_graph_update_section(str(tmp_path), CONSUMER_ID, addresses=[S_ID])
    assert result.startswith(f"Updated addresses for section: {CONSUMER_ID}"), result
    assert result.endswith(f"\n  still LINKED_STALE via: {S_ID} (clears when it does)"), result
    assert _statuses(dbp, CONSUMER_ID)[1] == "LINKED_STALE"
    assert _receipts(dbp, CONSUMER_ID) == receipts
    assert_matches_full_recompute(dbp, tmp_path)

    assert not clear(tmp_path).startswith("ERROR")
    assert _statuses(dbp, S_ID)[1] == "VERIFIED"
    assert _statuses(dbp, CONSUMER_ID)[1] == "VERIFIED"
    assert_matches_full_recompute(dbp, tmp_path)


# ---------------------------------------------------------------------------
# Unbuilt edit
# ---------------------------------------------------------------------------


@workflow(
    purpose="With f edited on disk and not built, naming f is accepted because the pre-write refresh finds the "
    "edit; the section is VERIFIED straight away and after the build and check that record the edit, matching "
    "check --full"
)
def test_naming_an_unbuilt_edit_verifies_the_section_through_the_build(tmp_path: Path) -> None:
    dbp = _project(tmp_path, [_section("s", F_ID)])
    time.sleep(0.05)
    _write(tmp_path / "src" / "mod.py", _MOD_CHANGED)

    result = axiom_graph_update_section(str(tmp_path), S_ID, content="S covers the new f.", addresses=[F_ID])
    assert not result.startswith("ERROR"), result
    assert "still LINKED_STALE" not in result, result
    assert _statuses(dbp, F_ID)[0] == "CONTENT_UPDATED"
    assert _statuses(dbp, S_ID) == ("VERIFIED", "VERIFIED")
    assert_matches_full_recompute(dbp, tmp_path)

    build_index(dbp, tmp_path)
    compute_check_summary(dbp, tmp_path)
    assert _statuses(dbp, S_ID) == ("VERIFIED", "VERIFIED")
    assert_matches_full_recompute(dbp, tmp_path)


# ---------------------------------------------------------------------------
# A merge cannot clear what the edit did not
# ---------------------------------------------------------------------------


def _fresh_copy(root: Path, dest: Path) -> Path:
    """Copy the project's files (not its index) to *dest* and build them from scratch."""
    for part in ("src", "docs"):
        shutil.copytree(root / part, dest / part)
    shutil.copy(root / "axiom-graph.toml", dest / "axiom-graph.toml")
    (dest / ".axiom_graph").mkdir()
    dbp = dest / ".axiom_graph" / "graph.db"
    db.init_db(dbp)
    build_index(dbp, dest)
    return dbp


def _verification_count(dbp: Path, node_id: str) -> int:
    with closing(sqlite3.connect(dbp)) as conn:
        return conn.execute("SELECT COUNT(*) FROM node_verification WHERE node_id = ?", (node_id,)).fetchone()[0]


@pytest.mark.parametrize("named", [False, True], ids=["plain-edit", "addresses-f"])
@workflow(
    purpose="The stamp a doc edit writes mirrors what the edit verified: a plain edit of a section stale via f keeps "
    "f's old stamp entry, so neither a fresh build of the files nor a merge into an index where the section is stale "
    "verifies its link to f (only its text); naming f records f's current hash, and both verify the link too"
)
def test_a_merge_verifies_only_what_the_edit_named(tmp_path: Path, named: bool) -> None:
    口 = Step(
        step_num=1,
        name="A stamped section goes stale",
        purpose="A doc tool writes S while f is at its first version, then f changes and is built",
    )
    branch = tmp_path / "branch"
    dbp = _project(branch, [_section("s", F_ID)])
    assert not axiom_graph_update_section(str(branch), S_ID, content="S documents the first f.").startswith("ERROR")
    first = _stamp_entries(branch, "s")[F_ID]
    _change_code(branch, dbp)
    main = tmp_path / "main"
    shutil.copytree(branch, main)
    assert _statuses(dbp, S_ID)[1] == "LINKED_STALE"

    口 = Step(step_num=2, name="Edit S", purpose="update_section, naming f or not")
    kwargs = {"addresses": [F_ID]} if named else {}
    assert not axiom_graph_update_section(str(branch), S_ID, content="S documents the new f.", **kwargs).startswith(
        "ERROR"
    )
    current = doc_stamps.current_code_hashes(dbp, branch, [F_ID])[F_ID]
    assert current != first
    assert _stamp_entries(branch, "s")[F_ID] == (current if named else first)

    口 = Step(
        step_num=3,
        name="Build the files elsewhere",
        purpose="A fresh build of the files verifies S's text from its stamp either way, and takes f's receipt from it "
        "only when the edit named f; otherwise the new verification holds f open",
    )
    fresh_db = _fresh_copy(branch, tmp_path / "fresh")
    assert _verification_count(fresh_db, S_ID) == 1
    receipt = db.get_verification_targets(fresh_db, S_ID)[F_ID]
    assert receipt[0] == (current if named else db.OPEN_RECEIPT_HASH)

    口 = Step(
        step_num=4,
        name="Merge into the stale index",
        purpose="The edited doc arrives in a copy taken before the edit, where S is LINKED_STALE: only the named edit "
        "clears it",
    )
    shutil.copy(branch / "docs" / "spec.json", main / "docs" / "spec.json")
    main_db = _db_path(str(main))
    build_index(main_db, main)
    compute_check_summary(main_db, main)
    assert _statuses(main_db, S_ID)[1] == ("VERIFIED" if named else "LINKED_STALE")
    assert_matches_full_recompute(main_db, main)


# ---------------------------------------------------------------------------
# The stamp entries and the batched hash lookup (Tier 1)
# ---------------------------------------------------------------------------


def test_stamp_keeps_the_previous_entry_of_an_open_offender_and_drops_a_missing_one() -> None:
    found = CurrentOffenders(vias=[F_ID, G_ID], roots=[], receipt_targets=frozenset({F_ID, G_ID}))
    current = {F_ID: "f-now", G_ID: "g-now", "proj::src.mod::h": "h-now"}
    out = _stamp_verified_against([F_ID, G_ID, "proj::src.mod::h", OTHER_ID], current, {F_ID: "f-old"}, found, set())
    assert out == {F_ID: "f-old", "proj::src.mod::h": "h-now"}


def test_stamp_records_the_current_hash_of_a_named_offender_and_of_every_link_when_none_is_open() -> None:
    found = CurrentOffenders(vias=[F_ID, G_ID], roots=[], receipt_targets=frozenset({F_ID, G_ID}))
    current = {F_ID: "f-now", G_ID: "g-now"}
    previous = {F_ID: "f-old", G_ID: "g-old"}
    assert _stamp_verified_against([F_ID, G_ID], current, previous, found, {F_ID}) == {F_ID: "f-now", G_ID: "g-old"}
    assert _stamp_verified_against([F_ID, G_ID], current, previous, None, set()) == current


def test_stamp_of_a_section_carried_on_a_frozen_doc_keeps_every_previous_entry() -> None:
    carried = CurrentOffenders(vias=[], roots=[], receipt_targets=frozenset(), carried=True)
    current = {F_ID: "f-now", G_ID: "g-now"}
    assert _stamp_verified_against([F_ID, G_ID], current, {F_ID: "f-old"}, carried, set()) == {F_ID: "f-old"}


def test_current_code_hashes_runs_one_query_and_parses_each_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dbp = _project(tmp_path, [_section("s", F_ID, G_ID)])
    _write(tmp_path / "src" / "other.py", "def h():\n    return 2\n")
    build_index(dbp, tmp_path)
    queries = {"n": 0}
    parses: Counter = Counter()
    real_get = db.get_nodes_conn
    real_parse = node_hashing.current_node_hashes_for_file

    def counting_get(*args, **kwargs):
        queries["n"] += 1
        return real_get(*args, **kwargs)

    def counting_parse(path, *args, **kwargs):
        parses[Path(path).name] += 1
        return real_parse(path, *args, **kwargs)

    monkeypatch.setattr(db, "get_nodes_conn", counting_get)
    monkeypatch.setattr(node_hashing, "current_node_hashes_for_file", counting_parse)
    h_id = "proj::src.other::h"
    hashes = doc_stamps.current_code_hashes(dbp, tmp_path, [G_ID, S_ID, h_id, F_ID, "proj::src.mod::missing", G_ID])
    assert list(hashes) == [G_ID, h_id, F_ID]
    assert all(hashes.values())
    assert queries["n"] == 1
    assert parses == {"mod.py": 1, "other.py": 1}


# ---------------------------------------------------------------------------
# Work count
# ---------------------------------------------------------------------------


def _fn(i: int) -> str:
    return f"proj::src.{'a' if i < 3 else 'b'}::f{i}"


def _measure_named_edit(root: Path, offenders: int, monkeypatch: pytest.MonkeyPatch) -> dict:
    """One update_section naming *offenders* changed functions of a section linking five; the work it did."""
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "__init__.py", "")
    src = {"a": range(3), "b": range(3, 5)}
    for name, ids in src.items():
        _write(root / "src" / f"{name}.py", "".join(f"def f{i}():\n    return {i}\n\n\n" for i in ids))
    section = _section("s", *(_fn(i) for i in range(5)))
    _write(root / "docs" / "spec.json", json.dumps({"title": "Spec", "sections": [section]}))
    dbp = _db_path(str(root))
    build_index(dbp, root)
    mark_clean_nodes(dbp, root, [S_ID], reason="reviewed", verified_by="human")
    time.sleep(0.05)
    for name, ids in src.items():
        body = "".join(f"def f{i}():\n    return {i + 10 if i < offenders else i}\n\n\n" for i in ids)
        _write(root / "src" / f"{name}.py", body)
    build_index(dbp, root)
    compute_check_summary(dbp, root)
    assert len(_get_linked_stale_ids(dbp)[S_ID]) == offenders

    work: dict = {"connections": 0, "selects": 0, "parses": Counter()}
    real_connect = sqlite3.connect
    real_parse = node_hashing.current_node_hashes_for_file

    def count_select(sql: str) -> None:
        work["selects"] += sql.lstrip().upper().startswith("SELECT")

    def counting_connect(*args, **kwargs):
        work["connections"] += 1
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(count_select)
        return conn

    def counting_parse(path, *args, **kwargs):
        work["parses"][Path(path).name] += 1
        return real_parse(path, *args, **kwargs)

    def whole_graph(*_args, **_kwargs):
        raise AssertionError("whole-graph load on a doc write")

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", counting_connect)
        m.setattr(node_hashing, "current_node_hashes_for_file", counting_parse)
        m.setattr(dependency_set, "load_dependency_graph", whole_graph)
        m.setattr(staleness, "load_dependency_graph", whole_graph)
        result = axiom_graph_update_section(
            str(root), S_ID, content="S covers the new code.", addresses=[_fn(i) for i in range(offenders)]
        )
    assert "still LINKED_STALE" not in result, result
    assert _statuses(dbp, S_ID) == ("VERIFIED", "VERIFIED")
    return work


@workflow(
    purpose="One update_section naming one or five offenders of a section linking five functions does the same "
    "work: one connection, the same number of queries, each code file parsed at most once, and no whole-graph load"
)
def test_naming_more_offenders_does_no_more_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one = _measure_named_edit(tmp_path / "one", 1, monkeypatch)
    five = _measure_named_edit(tmp_path / "five", 5, monkeypatch)
    assert one["connections"] == five["connections"] == 1
    assert one["selects"] == five["selects"], (one, five)
    assert one["parses"] == five["parses"], (one, five)
    assert five["parses"]["a.py"] <= 1 and five["parses"]["b.py"] <= 1, five


def _measure_doc_write(root: Path, links: int, monkeypatch: pytest.MonkeyPatch) -> dict:
    """One update_section naming one changed function of a section linking *links*; the scans and queries it made."""
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "__init__.py", "")
    fns = [f"proj::src.a::f{i}" for i in range(links)]
    _write(root / "src" / "a.py", "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(links)))
    _write(root / "docs" / "spec.json", json.dumps({"title": "Spec", "sections": [_section("s", *fns)]}))
    dbp = _db_path(str(root))
    build_index(dbp, root)
    mark_clean_nodes(dbp, root, [S_ID], reason="reviewed", verified_by="human")
    time.sleep(0.05)
    _write(
        root / "src" / "a.py", "".join(f"def f{i}():\n    return {i + 10 if i == 0 else i}\n\n\n" for i in range(links))
    )
    build_index(dbp, root)
    compute_check_summary(dbp, root)

    work: dict = {"selects": 0, "scans": Counter()}
    real_connect = sqlite3.connect
    real_scan = docjson_parse.scan_single_json_doc

    def count_select(sql: str) -> None:
        work["selects"] += sql.lstrip().upper().startswith("SELECT")

    def counting_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(count_select)
        return conn

    def counting_scan(path, *args, **kwargs):
        work["scans"][Path(path).name] += 1
        return real_scan(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", counting_connect)
        m.setattr(docjson_parse, "scan_single_json_doc", counting_scan)
        result = axiom_graph_update_section(str(root), S_ID, content="S covers the new code.", addresses=[fns[0]])
    assert "still LINKED_STALE" not in result, result
    assert _statuses(dbp, S_ID) == ("VERIFIED", "VERIFIED")
    assert_matches_full_recompute(dbp, root)
    return work


@workflow(
    purpose="One update_section scans the doc file once, and makes the same number of queries whether its section "
    "links one function or twelve"
)
def test_a_doc_write_scans_its_file_once_whatever_its_link_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = _measure_doc_write(tmp_path / "one", 1, monkeypatch)
    twelve = _measure_doc_write(tmp_path / "twelve", 12, monkeypatch)
    assert one["scans"] == twelve["scans"] == {"spec.json": 1}, (one, twelve)
    assert one["selects"] == twelve["selects"], (one, twelve)


def test_a_cached_scan_is_never_served_for_changed_text(tmp_path: Path) -> None:
    """Inside one operation an unchanged DocJSON file is scanned once; once its text changes it is scanned again."""
    _project(tmp_path, [_section("s", F_ID)])
    doc = tmp_path / "docs" / "spec.json"
    calls = Counter()
    real_scan = docjson_parse.scan_single_json_doc

    def counting_scan(path, *args, **kwargs):
        calls[Path(path).name] += 1
        return real_scan(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(docjson_parse, "scan_single_json_doc", counting_scan)
        with node_hashing.parse_cache():
            first = node_hashing._scan_docjson_sections(doc, tmp_path, "proj")
            again = node_hashing._scan_docjson_sections(doc, tmp_path, "proj")
            _write(doc, json.dumps({"title": "Spec", "sections": [_section("s", F_ID, content="Changed.")]}))
            changed = node_hashing._scan_docjson_sections(doc, tmp_path, "proj")
        outside = node_hashing._scan_docjson_sections(doc, tmp_path, "proj")
    assert calls["spec.json"] == 3
    assert first[S_ID].level_2 == again[S_ID].level_2 == "s text."
    assert changed[S_ID].level_2 == outside[S_ID].level_2 == "Changed."
