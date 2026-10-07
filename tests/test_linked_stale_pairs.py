"""Behavioural tests for LINKED_STALE decided by verified-against hash pairs.

A verification records, for each dependency target, the hash of the target
it was checked against.  A dependent is LINKED_STALE while a recorded hash
differs from the target's live hash, and settled while they match; a
dependency with no recorded hash keeps the change-time rule.  Scenarios
enter through ``axiom_graph.lifecycle.api`` and the doc-tool api, and
assert the stored statuses straight after ``build`` and again after
``check``; each also checks that the DB-only reader behind ``drift_query``
and ``mark_clean``'s classification agrees with what ``check`` stored.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.docjson.api import axiom_graph_add_link, axiom_graph_update_section
from axiom_graph.index import db
from axiom_graph.index.dependency_set import load_dependency_graph
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.index.staleness import _get_linked_stale_ids, db_live_view
from axiom_graph.lifecycle.api import (
    build_index,
    compute_check_summary,
    mark_clean_nodes,
    reverify_nodes,
)
from axiom_graph.models import hash16
from axiom_graph.query.api import compute_drift_query
from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER

F_ID = "proj::src.mod::f"
G_ID = "proj::src.mod::g"
MOVED_G_ID = "proj::src.other::g"
MODULE_ID = "proj::src.mod"
TEST_ID = "proj::tests.test_mod::test_f"
RUN_ID = "proj::src.flow::run"
RUN_ENV_ID = "proj::src.flow::run@workflow"
LEAF_ID = "proj::src.flow::leaf"
DOC_ID = "proj::docs/spec"
OTHER_DOC_ID = "proj::docs/other"
SECTION_ID = f"{DOC_ID}::overview"
MODULE_SECTION_ID = f"{DOC_ID}::module"
BOTH_SECTION_ID = f"{DOC_ID}::both"
TARGETS_SECTION_ID = f"{DOC_ID}::targets"

_MOD_SRC = """\
def f():
    return 0


def g():
    return 1


class A:
    def transform(self):
        return 2


class B:
    pass
"""

_FLOW_SRC = '''\
from axiom_annotations import AutoStep, task, workflow


@task(purpose="Leaf work")
def leaf():
    return 1


@workflow(purpose="Run the leaf")
def run():
    """Run doc."""
    口 = AutoStep(step_num=1, name="Leaf")
    return leaf()
'''

_TEST_SRC = "from src.mod import f\n\n\ndef test_f():\n    assert f() is not None\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _edit(path: Path, old: str, new: str) -> None:
    """Replace *old* with *new* in a file, moving its mtime on."""
    time.sleep(0.02)
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _row(root: Path, node_id: str, columns: str) -> tuple:
    with sqlite3.connect(_db_path(str(root))) as conn:
        row = conn.execute(f"SELECT {columns} FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, node_id
    return tuple(row)


def _link(root: Path, node_id: str) -> str:
    return _row(root, node_id, "link_status")[0]


def _build(root: Path) -> None:
    build_index(_db_path(str(root)), root)


def _check(root: Path) -> dict[str, tuple[str, str, list[str]]]:
    summary = compute_check_summary(_db_path(str(root)), root)
    assert summary is not None
    return summary.statuses


def _verify(root: Path, *node_ids: str) -> None:
    mark_clean_nodes(_db_path(str(root)), root, list(node_ids), reason="reviewed", verified_by="human")


def _drift_query_linked_stale(root: Path) -> dict[str, list[str]]:
    """The LINKED_STALE rows ``drift_query`` lists: node id -> the offenders it shows as ``via``."""
    text = compute_drift_query(_db_path(str(root)), root, filter="LINKED_STALE", format="full", limit=10_000)
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("id="):
            continue
        parts = line.split()
        via = next((part[len("via=") :].split(",") for part in parts if part.startswith("via=")), [])
        rows[parts[0][len("id=") :]] = via
    return rows


def _composes_roots(db_file: Path, node_ids) -> set[str]:
    """The topmost ``composes`` ancestors of *node_ids* (documents and modules)."""
    with db._connect(db_file) as conn:
        parents: dict[str, list[str]] = {}
        for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'composes'"):
            parents.setdefault(r["to_id"], []).append(r["from_id"])
    roots: set[str] = set()
    for node_id in node_ids:
        stack, seen = list(parents.get(node_id, [])), set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current in parents:
                stack.extend(parents[current])
            else:
                roots.add(current)
    return roots


def _mark_clean_classified_stale(root: Path, node_ids) -> set[str]:
    """The descendants ``mark_clean`` names as still LINKED_STALE when the parents of *node_ids* are marked clean.

    Runs on a copy of the index, so the scenario's own state is untouched.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "graph.db"
        with closing(sqlite3.connect(_db_path(str(root)))) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)
        parents = sorted(_composes_roots(copy, node_ids))
        result = mark_clean_nodes(copy, root, parents, reason="parity", verified_by="human")
        return {d for stale in (*result.inherited.values(), *result.mixed.values()) for d in stale}


def _assert_links(root: Path, expected: dict[str, str], *, after: str) -> None:
    """Stored link statuses equal *expected*, and every reader agrees with them.

    The readers: the DB-only stale map, ``drift_query`` (the rows it lists
    and the offenders it shows) and ``mark_clean``'s inherited / mixed
    classification of the dependents' parents.
    """
    dbp = _db_path(str(root))
    stale_map = _get_linked_stale_ids(dbp)
    listed = _drift_query_linked_stale(root)
    with db._connect(dbp) as conn:
        stored = {r["id"] for r in conn.execute("SELECT id FROM nodes WHERE link_status = 'LINKED_STALE'")}
    assert set(listed) == stored, (after, "drift_query lists other LINKED_STALE rows than the stored ones")
    classified = _mark_clean_classified_stale(root, expected)
    for node_id, link in expected.items():
        flagged = link == "LINKED_STALE"
        assert _link(root, node_id) == link, (after, node_id)
        assert (node_id in stale_map) == flagged, (after, node_id, "DB-only reader disagrees")
        assert (node_id in listed) == flagged, (after, node_id, "drift_query disagrees")
        assert (node_id in classified) == flagged, (after, node_id, "mark_clean classification disagrees")
        if flagged:
            assert listed[node_id] == stale_map[node_id], (after, node_id, "drift_query shows other offenders")


def _assert_via(root: Path, statuses: dict, node_id: str, via: list[str]) -> None:
    _own, link, got = statuses[node_id]
    assert link == "LINKED_STALE" and got == via, (node_id, link, got)
    assert _get_linked_stale_ids(_db_path(str(root)))[node_id] == via, node_id


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A module, a workflow, a test of ``f`` and docs linking ``f``, ``g`` and the module, all built."""
    return make_project(tmp_path)


def make_project(tmp_path: Path) -> Path:
    """Write and build the :func:`project` fixture's tree under *tmp_path* (other test modules reuse it)."""
    toml = '[axiom_graph]\nproject_id = "proj"\n'
    if HAS_TREE_SITTER:
        toml += '\n[axiom_graph.scan]\njs_paths = ["web/*.ts"]\n'
        _write(tmp_path / "web" / "app.ts", "export function greet(): number {\n  return 1;\n}\n")
    _write(tmp_path / "axiom-graph.toml", toml)
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", _MOD_SRC)
    _write(tmp_path / "src" / "flow.py", _FLOW_SRC)
    _write(tmp_path / "tests" / "test_mod.py", _TEST_SRC)
    _write(
        tmp_path / "docs" / "spec.json",
        json.dumps(
            {
                "title": "Spec",
                "sections": [
                    {"id": "overview", "heading": "Overview", "content": "F.", "links": [{"node_id": F_ID}]},
                    {"id": "module", "heading": "Module", "content": "Mod.", "links": [{"node_id": MODULE_ID}]},
                    {
                        "id": "both",
                        "heading": "Both",
                        "content": "F and G.",
                        "links": [{"node_id": F_ID}, {"node_id": G_ID}],
                    },
                    {"id": "targets", "heading": "Targets", "content": "Every kind of target."},
                ],
            }
        ),
    )
    _write(tmp_path / "docs" / "other.json", json.dumps({"title": "Other", "sections": []}))
    _build(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Live hashes (Tier 1)
# ---------------------------------------------------------------------------


def test_staleness_pass_stores_live_hash_and_mark_clean_resets_the_baseline_to_it(project: Path) -> None:
    """A re-hashed node keeps its baseline and stores the hash it was found at; mark_clean resets both: the
    baseline moves to that hash, and the live hash its closing refresh stores is the one ``check --full`` stores."""
    from tests.fixtures.full_recompute import assert_matches_full_recompute

    baseline = _row(project, F_ID, "code_hash")[0]

    _edit(project / "src" / "mod.py", "return 0", "return 10")
    _build(project)
    code, live, own = _row(project, F_ID, "code_hash, live_code_hash, own_status")
    assert own == "CONTENT_UPDATED"
    assert code == baseline
    assert live and live != baseline

    _verify(project, F_ID)
    code, live_code, live_desc, own = _row(project, F_ID, "code_hash, live_code_hash, live_desc_hash, own_status")
    assert code == live and live_code == live and live_desc is None and own == "VERIFIED"
    assert_matches_full_recompute(_db_path(str(project)), project)


def test_rehash_fingerprint_keeps_a_file_back_at_its_scanned_content_off_the_fast_pass(project: Path) -> None:
    """A re-hash stores the fingerprint of the content it read on the module anchor; a file put back at its scanned
    content with its mtime rolled back is re-hashed again, also after the module itself was marked clean."""
    mod = project / "src" / "mod.py"
    scanned = _row(project, MODULE_ID, "code_hash")[0]
    _edit(mod, "return 0", "return 10")
    _build(project)
    edited = mod.read_text(encoding="utf-8")
    assert _row(project, MODULE_ID, "code_hash, live_code_hash") == (scanned, hash16(edited))

    _verify(project, F_ID)
    _check(project)
    _verify(project, MODULE_ID)
    # The module keeps the fingerprint of the bytes its file's last re-hash read (what check --full stores).
    assert _row(project, MODULE_ID, "live_code_hash")[0] == hash16(edited)

    stat = mod.stat()
    mod.write_text(edited.replace("return 10", "return 0", 1), encoding="utf-8")
    os.utime(mod, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert hash16(mod.read_text(encoding="utf-8")) == scanned
    assert _check(project)[F_ID][0] == "CONTENT_UPDATED"
    assert _row(project, MODULE_ID, "live_code_hash")[0] == scanned


def test_reset_marker_keeps_a_file_off_the_fast_pass_until_it_is_rehashed(project: Path) -> None:
    """A function marked clean at an unbuilt edit makes the next pass re-hash its file, even when the file is back
    at the content the index scanned and last re-hashed."""
    mod = project / "src" / "mod.py"
    stat = mod.stat()
    original = mod.read_text(encoding="utf-8")
    _edit(mod, "return 0", "return 10")
    _verify(project, F_ID)

    mod.write_text(original, encoding="utf-8")
    os.utime(mod, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert _check(project)[F_ID][0] == "CONTENT_UPDATED"


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@workflow(
    purpose="A second edit to a function flags the doc section and the test that were verified after the first "
    "edit, although the function never left CONTENT_UPDATED and wrote no new change"
)
def test_second_edit_flags_dependents_verified_after_the_first(project: Path) -> None:
    mod = project / "src" / "mod.py"
    口 = Step(step_num=1, name="First edit, then verify", purpose="Dependents are re-checked against the edited f")
    _edit(mod, "return 0", "return 10")
    _build(project)
    _verify(project, SECTION_ID, TEST_ID)
    _check(project)
    _assert_links(project, {SECTION_ID: "VERIFIED", TEST_ID: "VERIFIED"}, after="verify")

    口 = Step(step_num=2, name="Second edit", purpose="f changes again while it is still CONTENT_UPDATED")
    _edit(mod, "return 10", "return 20")
    _build(project)
    assert _row(project, F_ID, "own_status")[0] == "CONTENT_UPDATED"

    口 = Step(step_num=3, name="Assert flagged", purpose="Both dependents are LINKED_STALE via f after build and check")
    _assert_links(project, {SECTION_ID: "LINKED_STALE", TEST_ID: "LINKED_STALE"}, after="build")
    statuses = _check(project)
    for dependent in (SECTION_ID, TEST_ID):
        _assert_via(project, statuses, dependent, [F_ID])
    _assert_links(project, {SECTION_ID: "LINKED_STALE", TEST_ID: "LINKED_STALE"}, after="check")


@workflow(
    purpose="A doc section updated with a doc tool and a test marked clean while f's edit is on disk but not yet "
    "built stay VERIFIED through the build that records the edit: they were checked against it"
)
def test_dependents_verified_before_the_build_stay_verified(project: Path) -> None:
    口 = Step(step_num=1, name="Edit f on disk", purpose="No build runs, so the index has not seen the edit")
    _edit(project / "src" / "mod.py", "return 0", "return 10")

    口 = Step(
        step_num=2,
        name="Verify before the build",
        purpose="The doc tool names f, whose edit its pre-write refresh finds, so the section's receipt for f is "
        "recorded at the edit; the test is marked clean",
    )
    result = axiom_graph_update_section(str(project), SECTION_ID, content="F returns ten.", addresses=[F_ID])
    assert not result.startswith("ERROR"), result
    _verify(project, TEST_ID)

    口 = Step(step_num=3, name="Build and check", purpose="The edit is recorded after the verifications")
    _build(project)
    assert _row(project, F_ID, "own_status")[0] == "CONTENT_UPDATED"
    _assert_links(project, {SECTION_ID: "VERIFIED", TEST_ID: "VERIFIED"}, after="build")
    _check(project)
    _assert_links(project, {SECTION_ID: "VERIFIED", TEST_ID: "VERIFIED"}, after="check")


@workflow(
    purpose="A function reverted to an old version after its doc section was re-verified at the new one flags the "
    "section, although the revert cancels the function's change"
)
def test_revert_after_reverification_flags_the_section(project: Path) -> None:
    mod = project / "src" / "mod.py"
    口 = Step(step_num=1, name="Change f and re-verify", purpose="The section is checked against f's new version")
    _edit(mod, "return 0", "return 10")
    _build(project)
    _verify(project, SECTION_ID)

    口 = Step(step_num=2, name="Revert f", purpose="f is back at its baseline; its change no longer counts")
    _edit(mod, "return 10", "return 0")
    _build(project)
    assert _row(project, F_ID, "own_status")[0] == "VERIFIED"

    口 = Step(step_num=3, name="Assert flagged", purpose="The section saw a version f no longer has")
    _assert_links(project, {SECTION_ID: "LINKED_STALE"}, after="build")
    _assert_via(project, _check(project), SECTION_ID, [F_ID])
    _assert_links(project, {SECTION_ID: "LINKED_STALE"}, after="check")


def _reverify_function(root: Path) -> None:
    reverify_nodes(_db_path(str(root)), root, [F_ID], "reviewed", verified_by="human")


def _mark_clean_function_and_dependents(root: Path) -> None:
    _verify(root, F_ID, SECTION_ID, TEST_ID)


@pytest.mark.parametrize(
    "settle",
    [
        pytest.param(_reverify_function, id="reverify-the-function"),
        pytest.param(_mark_clean_function_and_dependents, id="mark-clean-function-and-dependents"),
    ],
)
@workflow(
    purpose="A function that was edited, settled together with its dependents and then reverted to its first version "
    "after a later staleness pass reads CONTENT_UPDATED, and the doc section and the test checked against the edited "
    "version go LINKED_STALE, straight after the build and again after check"
)
def test_revert_after_settling_and_a_staleness_pass_flags_function_and_dependents(project: Path, settle) -> None:
    mod = project / "src" / "mod.py"
    dependents = {SECTION_ID: "LINKED_STALE", TEST_ID: "LINKED_STALE"}
    口 = Step(
        step_num=1, name="Verify the dependents", purpose="Section and test are checked against f's first version"
    )
    _verify(project, SECTION_ID, TEST_ID)

    口 = Step(step_num=2, name="Edit f and build", purpose="The edit flags both dependents")
    _edit(mod, "return 0", "return 10")
    _build(project)
    _assert_links(project, dependents, after="edit")

    口 = Step(step_num=3, name="Settle f", purpose="Reverify f, or mark f and both dependents clean")
    settle(project)

    口 = Step(step_num=4, name="Run a staleness pass", purpose="check re-hashes the file at the edited version")
    statuses = _check(project)
    assert statuses[F_ID][0] == "VERIFIED"
    _assert_links(project, {SECTION_ID: "VERIFIED", TEST_ID: "VERIFIED"}, after="check before the revert")

    口 = Step(step_num=5, name="Revert f and build", purpose="The file is back at the content the index first scanned")
    _edit(mod, "return 10", "return 0")
    _build(project)

    口 = Step(
        step_num=6,
        name="Assert flagged",
        purpose="f no longer matches the version it was settled at, and both dependents saw a version f no longer has",
    )
    assert _row(project, F_ID, "own_status")[0] == "CONTENT_UPDATED"
    _assert_links(project, dependents, after="build")
    statuses = _check(project)
    assert statuses[F_ID][0] == "CONTENT_UPDATED"
    assert _row(project, F_ID, "own_status")[0] == "CONTENT_UPDATED"
    for dependent in dependents:
        _assert_via(project, statuses, dependent, [F_ID])
    _assert_links(project, dependents, after="check")


@workflow(
    purpose="Without recorded pairs a dependent keeps the change-time rule: a pair-less verification is flagged by a "
    "later change, and a link added after the verification to an earlier change does not flag"
)
def test_dependency_without_a_pair_uses_the_change_time(project: Path) -> None:
    dbp = _db_path(str(project))
    _edit(project / "src" / "mod.py", "return 1", "return 11")
    _build(project)
    _verify(project, SECTION_ID, TEST_ID)
    with db._connect(dbp) as conn:
        conn.execute("DELETE FROM node_verification_targets WHERE node_id = ?", (TEST_ID,))

    result = axiom_graph_add_link(str(project), SECTION_ID, node_id=G_ID)
    assert not result.startswith("ERROR"), result
    assert G_ID not in db.get_verification_targets(dbp, SECTION_ID)
    _build(project)
    _assert_links(project, {SECTION_ID: "VERIFIED", TEST_ID: "VERIFIED"}, after="build")

    _edit(project / "src" / "mod.py", "return 0", "return 10")
    _build(project)
    _assert_links(project, {SECTION_ID: "LINKED_STALE", TEST_ID: "LINKED_STALE"}, after="build")
    _check(project)
    _assert_links(project, {SECTION_ID: "LINKED_STALE", TEST_ID: "LINKED_STALE"}, after="check")


@workflow(
    purpose="An envelope pairs its annotated function's code and docstring: a docstring-only second edit flags it "
    "even after the function itself is marked clean"
)
def test_docstring_edit_flags_envelope_verified_in_between(project: Path) -> None:
    flow = project / "src" / "flow.py"
    _edit(flow, '"""Run doc."""', '"""Run doc, first edit."""')
    _build(project)
    _verify(project, RUN_ID, RUN_ENV_ID)
    _check(project)
    _assert_links(project, {RUN_ENV_ID: "VERIFIED"}, after="verify")

    _edit(flow, '"""Run doc, first edit."""', '"""Run doc, second edit."""')
    _build(project)
    assert _row(project, RUN_ID, "own_status")[0] == "DESC_UPDATED"
    assert _link(project, RUN_ENV_ID) == "LINKED_STALE"

    _verify(project, RUN_ID)
    _assert_via(project, _check(project), RUN_ENV_ID, [RUN_ID])
    _assert_links(project, {RUN_ENV_ID: "LINKED_STALE"}, after="check")


def _change_rows(root: Path, node_id: str) -> int:
    with sqlite3.connect(_db_path(str(root))) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM node_history WHERE node_id = ? AND change_type LIKE 'BECAME_%'", (node_id,)
        ).fetchone()[0]


@workflow(
    purpose="An envelope verified while its function is still DESC_UPDATED is flagged by a second docstring edit "
    "that writes no new change, once the function itself is marked clean: the recorded docstring hash differs"
)
def test_docstring_edit_without_a_new_change_flags_envelope(project: Path) -> None:
    flow = project / "src" / "flow.py"
    口 = Step(step_num=1, name="First docstring edit", purpose="run goes DESC_UPDATED; only its envelope is verified")
    _edit(flow, '"""Run doc."""', '"""Run doc, first edit."""')
    _build(project)
    assert _row(project, RUN_ID, "own_status")[0] == "DESC_UPDATED"
    _verify(project, RUN_ENV_ID)

    口 = Step(step_num=2, name="Second docstring edit", purpose="run stays DESC_UPDATED, so no change row is written")
    changes = _change_rows(project, RUN_ID)
    _edit(flow, '"""Run doc, first edit."""', '"""Run doc, second edit."""')
    _build(project)
    assert _row(project, RUN_ID, "own_status")[0] == "DESC_UPDATED"
    assert _change_rows(project, RUN_ID) == changes

    口 = Step(
        step_num=3,
        name="Mark run clean and assert flagged",
        purpose="The envelope's last change time predates its verification, but its recorded docstring hash differs",
    )
    _verify(project, RUN_ID)
    _build(project)
    _assert_links(project, {RUN_ENV_ID: "LINKED_STALE"}, after="build")
    _assert_via(project, _check(project), RUN_ENV_ID, [RUN_ID])
    _assert_links(project, {RUN_ENV_ID: "LINKED_STALE"}, after="check")


@pytest.mark.parametrize(
    ("verify_first", "expect"),
    [
        pytest.param(True, "LINKED_STALE", id="task-changed-after-verification-flags"),
        pytest.param(False, "VERIFIED", id="task-changed-on-disk-before-verification-does-not"),
    ],
)
@workflow(
    purpose="An envelope pairs every task its delegates_to closure reaches: a task changed after the envelope's "
    "verification flags it, and one changed on disk before the verification (not yet built) does not"
)
def test_delegated_task_change_flags_by_verification_order(project: Path, verify_first: bool, expect: str) -> None:
    flow = project / "src" / "flow.py"
    if verify_first:
        _verify(project, RUN_ENV_ID)
    _edit(flow, "def leaf():\n    return 1", "def leaf():\n    return 2")
    if not verify_first:
        _verify(project, RUN_ENV_ID)
    _build(project)
    # The envelope stays flagged while the task itself is own-stale.
    assert _link(project, RUN_ENV_ID) == "LINKED_STALE"

    _verify(project, LEAF_ID)
    _build(project)
    _assert_links(project, {RUN_ENV_ID: expect}, after="build")
    statuses = _check(project)
    _assert_links(project, {RUN_ENV_ID: expect}, after="check")
    if expect == "LINKED_STALE":
        _assert_via(project, statuses, RUN_ENV_ID, [LEAF_ID])


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        pytest.param("return 1", "return 11", "LINKED_STALE", id="body-change-flags"),
        pytest.param(
            "class A:\n    def transform(self):\n        return 2\n\n\nclass B:\n    pass\n",
            "class A:\n    pass\n\n\nclass B:\n    def transform(self):\n        return 2\n",
            "VERIFIED",
            id="move-between-classes-does-not",
        ),
        pytest.param(
            "class B:\n    pass\n",
            "class B:\n    pass\n\n\ndef h():\n    return 3\n",
            "LINKED_STALE",
            id="added-function-flags",
        ),
        pytest.param("def g():", "def g2():", "LINKED_STALE", id="name-change-flags"),
    ],
)
@workflow(
    purpose="A doc section linking a module pairs a digest of its functions: a body change, an added function and a "
    "renamed function flag it, while moving a method between classes with the same name and body does not"
)
def test_module_link_pairs_by_function_digest(project: Path, old: str, new: str, expect: str) -> None:
    _verify(project, MODULE_SECTION_ID)
    _edit(project / "src" / "mod.py", old, new)
    _build(project)
    _assert_links(project, {MODULE_SECTION_ID: expect}, after="build")
    statuses = _check(project)
    _assert_links(project, {MODULE_SECTION_ID: expect}, after="check")
    if expect == "LINKED_STALE":
        _assert_via(project, statuses, MODULE_SECTION_ID, [MODULE_ID])


def _assert_build_detected_rename(dbp: Path, old_id: str, new_id: str) -> None:
    """The build parsed the file the function left, so it detected the move itself (what a full build does)."""
    with db._connect(dbp) as conn:
        row = conn.execute("SELECT 1 FROM node_renames WHERE old_id = ? AND new_id = ?", (old_id, new_id)).fetchone()
    assert row is not None


@workflow(
    purpose="Moving a linked function to another module with the same body, a move the build detects as a rename, "
    "keeps the doc section VERIFIED: its pair and its stamp's verified_against entry move to the new id with the "
    "hash unchanged"
)
def test_rename_moves_pairs_and_stamp_key(project: Path) -> None:
    dbp = _db_path(str(project))
    口 = Step(
        step_num=1,
        name="Write the section with a doc tool, then verify it",
        purpose="The doc tool stamps it; mark_clean records its pairs",
    )
    result = axiom_graph_update_section(str(project), BOTH_SECTION_ID, content="F and G, documented.")
    assert not result.startswith("ERROR"), result
    _verify(project, BOTH_SECTION_ID)
    pair = db.get_verification_targets(dbp, BOTH_SECTION_ID)[G_ID]

    口 = Step(
        step_num=2,
        name="Move g and build",
        purpose="Same name and body, another module; the build detects the move as a rename",
    )
    _edit(project / "src" / "mod.py", "def g():\n    return 1\n\n\n", "")
    _write(project / "src" / "other.py", "def g():\n    return 1\n")
    _build(project)
    _assert_build_detected_rename(dbp, G_ID, MOVED_G_ID)

    口 = Step(step_num=3, name="Assert carried", purpose="Pair and stamp key follow the rename; nothing is flagged")
    targets = db.get_verification_targets(dbp, BOTH_SECTION_ID)
    assert targets.get(MOVED_G_ID) == pair and G_ID not in targets
    sections = json.loads((project / "docs" / "spec.json").read_text(encoding="utf-8"))["sections"]
    stamp = next(s for s in sections if s["id"] == "both")["axiom_stamp"]["verified_against"]
    assert MOVED_G_ID in stamp and G_ID not in stamp
    _build(project)
    _assert_links(project, {BOTH_SECTION_ID: "VERIFIED"}, after="build")
    _check(project)
    _assert_links(project, {BOTH_SECTION_ID: "VERIFIED"}, after="check")


@workflow(
    purpose="With recorded pairs, marking an offender clean never clears its dependent; reverifying one of two "
    "offenders reports the other, and reverifying the second clears the dependent"
)
def test_offender_mark_clean_and_reverify_compose_with_pairs(project: Path) -> None:
    dbp = _db_path(str(project))
    mod = project / "src" / "mod.py"
    _verify(project, BOTH_SECTION_ID)
    _edit(mod, "return 0", "return 10")
    _edit(mod, "def g():\n    return 1", "def g():\n    return 11")
    _build(project)
    _assert_links(project, {BOTH_SECTION_ID: "LINKED_STALE"}, after="build")

    _verify(project, F_ID)
    _check(project)
    _assert_links(project, {BOTH_SECTION_ID: "LINKED_STALE"}, after="offender mark_clean")

    first = reverify_nodes(dbp, project, [F_ID], "reviewed", verified_by="human")
    assert first.skipped == {BOTH_SECTION_ID: [G_ID]}
    _assert_links(project, {BOTH_SECTION_ID: "LINKED_STALE"}, after="first reverify")

    second = reverify_nodes(dbp, project, [G_ID], "reviewed", verified_by="human")
    assert BOTH_SECTION_ID in second.cleared
    _assert_links(project, {BOTH_SECTION_ID: "VERIFIED"}, after="second reverify")
    _build(project)
    _assert_links(project, {BOTH_SECTION_ID: "VERIFIED"}, after="build")


@workflow(
    purpose="A reverify never absorbs an edit to another dependency that is on disk but not yet built: the refresh "
    "it starts with sees the edit, so the dependent is skipped with that dependency named, never verified at its "
    "old hash, and stays flagged through the next build"
)
def test_reverify_cascade_does_not_absorb_an_unbuilt_edit(project: Path) -> None:
    dbp = _db_path(str(project))
    mod = project / "src" / "mod.py"
    _verify(project, BOTH_SECTION_ID)
    _edit(mod, "return 0", "return 10")
    _build(project)
    _edit(mod, "def g():\n    return 1", "def g():\n    return 11")

    pairs_before = db.get_verification_targets(dbp, BOTH_SECTION_ID)
    result = reverify_nodes(dbp, project, [F_ID], "reviewed", verified_by="human")
    assert BOTH_SECTION_ID not in result.verified
    assert result.skipped[BOTH_SECTION_ID] == [G_ID]
    assert db.get_verification_targets(dbp, BOTH_SECTION_ID) == pairs_before
    _build(project)
    _assert_links(project, {BOTH_SECTION_ID: "LINKED_STALE"}, after="build")
    _assert_via(project, _check(project), BOTH_SECTION_ID, [F_ID, G_ID])


@workflow(
    purpose="The hash a verification records equals the hash the staleness engine compares and the one the "
    "DB-only readers compare, code and docstring hash alike, for a Python function, a JS/TS function, a workflow "
    "envelope, a doc envelope, a module digest and an envelope's annotated function"
)
def test_recorded_hash_matches_engine_and_db_readers(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from axiom_graph.index import staleness
    from axiom_graph.index.dependency_set import dependency_set, pair_hashes

    dbp = _db_path(str(project))
    targets = [F_ID, RUN_ENV_ID, OTHER_DOC_ID, MODULE_ID]
    if HAS_TREE_SITTER:
        with db._connect(dbp) as conn:
            row = conn.execute(
                "SELECT id FROM nodes WHERE location = 'web/app.ts' AND node_type = 'atomic_process'"
            ).fetchone()
        assert row is not None
        targets.append(row["id"])
    result = axiom_graph_add_link(str(project), TARGETS_SECTION_ID, node_ids=targets)
    assert not result.startswith("ERROR"), result
    # The envelope's own verification pairs its annotated function on code and docstring.
    _verify(project, TARGETS_SECTION_ID, RUN_ENV_ID)
    recorded = db.get_verification_targets(dbp, TARGETS_SECTION_ID)
    assert set(recorded) == set(targets)
    recorded_by = {TARGETS_SECTION_ID: recorded, RUN_ENV_ID: db.get_verification_targets(dbp, RUN_ENV_ID)}
    assert recorded_by[RUN_ENV_ID][RUN_ID][1] is not None

    # Every file takes the per-node hash ladder, so the engine compares hashes it computes now.
    monkeypatch.setattr(staleness, "file_unchanged_since", lambda *_args: False)
    fresh: dict = {}
    nodes = db.all_nodes(dbp)
    pass_statuses = staleness.compute_staleness(dbp, project, nodes, live_hashes_out=fresh)
    own_now = {node_id: own for node_id, (own, _link, _via) in pass_statuses.items()}
    engine_view = staleness._engine_live_view(dbp, fresh, own_now)
    _build(project)
    with db._connect(dbp) as conn:
        graph = load_dependency_graph(conn)
        reader_view = db_live_view(conn)
    for target in targets:
        assert recorded[target][0] == engine_view.value(graph, target)[0], target
        assert recorded[target][0] == reader_view.value(graph, target)[0], target
    for dependent, pairs in recorded_by.items():
        kinds = dependency_set(graph, dependent)
        for target, pair in pairs.items():
            assert pair == pair_hashes(kinds[target], engine_view.value(graph, target)), (dependent, target)
            assert pair == pair_hashes(kinds[target], reader_view.value(graph, target)), (dependent, target)
    _assert_links(project, {TARGETS_SECTION_ID: "VERIFIED", RUN_ENV_ID: "VERIFIED"}, after="build")
    _check(project)
    _assert_links(project, {TARGETS_SECTION_ID: "VERIFIED", RUN_ENV_ID: "VERIFIED"}, after="check")


@workflow(
    purpose="Moving a verified workflow function to another module with the same name and body, a move the build "
    "detects as a rename, carries its envelope's verification row and pairs to the new ids, and the envelope stays "
    "VERIFIED"
)
def test_rename_of_verified_workflow_function_carries_envelope_verification(project: Path) -> None:
    dbp = _db_path(str(project))
    new_run, new_env = "proj::src.other::run", "proj::src.other::run@workflow"
    with db._connect(dbp) as conn:
        steps = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE id LIKE ?", (f"{RUN_ID}::step-%",))]
    assert steps
    口 = Step(step_num=1, name="Verify the envelope and its steps", purpose="The envelope records its pairs")
    _verify(project, RUN_ENV_ID, *steps)
    assert set(db.get_verification_targets(dbp, RUN_ENV_ID)) == {RUN_ID, LEAF_ID}

    口 = Step(
        step_num=2,
        name="Move run and build",
        purpose="Same name, decorator and body; the build detects the move as a rename",
    )
    flow = project / "src" / "flow.py"
    head, run_src = flow.read_text(encoding="utf-8").split('@workflow(purpose="Run the leaf")')
    _write(flow, head.rstrip() + "\n")
    _write(
        project / "src" / "other.py",
        "from axiom_annotations import AutoStep, workflow\n\nfrom src.flow import leaf\n\n\n"
        '@workflow(purpose="Run the leaf")' + run_src,
    )
    _build(project)
    _assert_build_detected_rename(dbp, RUN_ID, new_run)

    口 = Step(step_num=3, name="Assert carried", purpose="Verification rows and pairs sit under the new ids")
    assert db.get_verification(dbp, new_env) is not None
    assert db.get_verification(dbp, RUN_ENV_ID) is None
    # The build that saw run leave flow.py retired its step rows (and their
    # verification) before the rename was applied; the old steps are gone.
    assert all(db.get_node(dbp, step) is None for step in steps)
    assert set(db.get_verification_targets(dbp, new_env)) == {new_run, LEAF_ID}
    _build(project)
    _assert_links(project, {new_env: "VERIFIED"}, after="build")
    _check(project)
    _assert_links(project, {new_env: "VERIFIED"}, after="check")


def test_code_rename_moves_verified_envelope_and_step_rows(project: Path) -> None:
    """Renaming a verified workflow function whose new ids are not indexed yet moves every verification row."""
    dbp = _db_path(str(project))
    with db._connect(dbp) as conn:
        steps = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE id LIKE ?", (f"{RUN_ID}::step-%",))]
    _verify(project, RUN_ENV_ID, *steps)
    pairs = db.get_verification_targets(dbp, RUN_ENV_ID)
    new_run = "proj::src.flow::run_renamed"

    db.record_code_rename(dbp, RUN_ID, new_run, "src/flow.py")

    assert db.get_verification(dbp, f"{new_run}@workflow") is not None
    for step in steps:
        assert db.get_verification(dbp, new_run + step[len(RUN_ID) :]) is not None, step
        assert db.get_verification(dbp, step) is None, step
    assert db.get_verification_targets(dbp, f"{new_run}@workflow") == {
        (new_run if target == RUN_ID else target): value for target, value in pairs.items()
    }


def test_viz_bulk_verify_records_pairs_with_one_recorder(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The viz bulk verify records every node's pairs with one recorder per request, so each file is parsed once."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom_graph.index import mark_clean
    from axiom_graph.viz import server

    dbp = _db_path(str(project))
    monkeypatch.setattr(server, "_PROJECT_ROOT", project)
    monkeypatch.setattr(server, "_DB_PATH", dbp)
    loads = {"n": 0}
    real_init = mark_clean.PairRecorder.__init__

    def _counting_init(self, *args, **kwargs):
        loads["n"] += 1
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(mark_clean.PairRecorder, "__init__", _counting_init)
    _edit(project / "src" / "mod.py", "return 0", "return 10")
    _build(project)
    f_live = _row(project, F_ID, "live_code_hash")[0]

    ids = [SECTION_ID, TEST_ID, BOTH_SECTION_ID, "proj::missing"]
    response = TestClient(server.app).post(
        "/api/nodes/bulk-verify", json={"node_ids": ids, "reason": "reviewed", "verified_by": "human"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {"node_id": SECTION_ID, "ok": True},
            {"node_id": TEST_ID, "ok": True},
            {"node_id": BOTH_SECTION_ID, "ok": True},
            {"node_id": "proj::missing", "ok": False, "error": "Node not found"},
        ]
    }
    assert loads["n"] == 1
    for node_id in (SECTION_ID, TEST_ID, BOTH_SECTION_ID):
        assert db.get_verification_targets(dbp, node_id)[F_ID] == (f_live, None), node_id


def test_doc_identity_rekey_moves_pairs_targeting_the_doc_and_its_sections(project: Path) -> None:
    """Cloning a document onto a new id points pairs that target it, or one of its sections, at the new ids."""
    dbp = _db_path(str(project))
    _write(
        project / "docs" / "other.json",
        json.dumps({"title": "Other", "sections": [{"id": "intro", "heading": "Intro", "content": "I."}]}),
    )
    _build(project)
    result = axiom_graph_add_link(str(project), TARGETS_SECTION_ID, node_ids=[OTHER_DOC_ID])
    assert not result.startswith("ERROR"), result
    _verify(project, TARGETS_SECTION_ID)
    envelope_pair = db.get_verification_targets(dbp, TARGETS_SECTION_ID)[OTHER_DOC_ID]
    old_section, new_doc = f"{OTHER_DOC_ID}::intro", "proj::docs/renamed"
    with db._connect(dbp) as conn:
        conn.execute(
            "INSERT INTO node_verification_targets (node_id, target_id, code_hash, desc_hash) VALUES (?, ?, ?, ?)",
            (TARGETS_SECTION_ID, old_section, "section-hash", None),
        )

    with db._connect(dbp) as conn:
        db.rekey_doc_identity(conn, OTHER_DOC_ID, new_doc)

    moved = db.get_verification_targets(dbp, TARGETS_SECTION_ID)
    assert moved[new_doc] == envelope_pair
    assert moved[f"{new_doc}::intro"] == ("section-hash", None)
    assert OTHER_DOC_ID not in moved and old_section not in moved
