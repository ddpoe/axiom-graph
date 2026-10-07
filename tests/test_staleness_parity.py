"""Incremental and scoped refreshes store what ``check --full`` stores.

Three harnesses, each comparing stored own status, link status and live
hashes with a full recompute run on a copy of the index:

* the scenarios of ``tests/test_linked_stale_pairs.py`` re-run with every
  ``build`` and ``check`` followed by the comparison (their own assertions
  are unchanged);
* the revert, revert-after-mark-clean and reset-marker sequences, where
  after every step a normal pass with the fast-pass gate on and a forced
  re-hash, each on its own copy, must agree;
* one case per refresh caller: write tools, ``reverify``, ``drift_query``,
  ``build`` and the node-naming reads.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow
from click.testing import CliRunner

import tests.test_linked_stale_pairs as pairs
from axiom_graph.cli import main as cli
from axiom_graph.docjson.api import axiom_graph_accept_doc_edits, axiom_graph_read_doc, axiom_graph_update_section
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes, reverify_nodes
from axiom_graph.query.api import compute_drift_query, fetch_graph, fetch_source, search_nodes
from tests.fixtures.full_recompute import assert_matches_full_recompute, full_recompute_rows, stored_rows


def _normal_pass_equals_forced_rehash(root: Path, after: str = "") -> None:
    """A plain ``check`` (fast pass on) and ``check --full``, each on its own copy, store the same rows."""
    dbp = _db_path(str(root))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "normal.db"
        with closing(sqlite3.connect(dbp)) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)
        compute_check_summary(copy, root)
        normal = stored_rows(copy)
    full = full_recompute_rows(dbp, root)
    assert normal == full, (
        after
        + ": "
        + str({k: (normal.get(k), full.get(k)) for k in set(normal) | set(full) if normal.get(k) != full.get(k)})
    )


def _cases(fn: Callable, scenario: str) -> list:
    """One ``pytest.param`` per parametrisation of a scenario test (read from its own ``parametrize`` mark)."""
    every_step = scenario in _EVERY_STEP
    marks = [m for m in getattr(fn, "pytestmark", []) if m.name == "parametrize"]
    if not marks:
        return [pytest.param(fn, {}, every_step, id=scenario)]
    names, values = marks[0].args[0], marks[0].args[1]
    names = [n.strip() for n in names.split(",")] if isinstance(names, str) else list(names)
    return [
        pytest.param(fn, dict(zip(names, p.values, strict=True)), every_step, id=f"{scenario}-{p.id}") for p in values
    ]


# Scenario -> the test in test_linked_stale_pairs.py that walks it.
_SCENARIOS = [
    ("second-edit-after-verification", pairs.test_second_edit_flags_dependents_verified_after_the_first),
    ("verified-before-the-build", pairs.test_dependents_verified_before_the_build_stay_verified),
    ("revert-after-reverification", pairs.test_revert_after_reverification_flags_the_section),
    ("docstring-edit-envelope-verified-in-between", pairs.test_docstring_edit_flags_envelope_verified_in_between),
    ("docstring-edit-envelope-no-new-change", pairs.test_docstring_edit_without_a_new_change_flags_envelope),
    ("delegated-task-verification-order", pairs.test_delegated_task_change_flags_by_verification_order),
    ("module-link-function-digest", pairs.test_module_link_pairs_by_function_digest),
    ("rename-moves-pairs", pairs.test_rename_moves_pairs_and_stamp_key),
    ("offender-mark-clean-and-reverify", pairs.test_offender_mark_clean_and_reverify_compose_with_pairs),
    ("unbuilt-edit-under-reverify", pairs.test_reverify_cascade_does_not_absorb_an_unbuilt_edit),
    ("revert-after-settling", pairs.test_revert_after_settling_and_a_staleness_pass_flags_function_and_dependents),
    (
        "rehash-fingerprint-back-at-scanned-content",
        pairs.test_rehash_fingerprint_keeps_a_file_back_at_its_scanned_content_off_the_fast_pass,
    ),
    (
        "reset-marker-keeps-file-off-fast-pass",
        pairs.test_reset_marker_keeps_a_file_off_the_fast_pass_until_it_is_rehashed,
    ),
]
# The sequences whose every step is also checked with the gate on vs a forced re-hash.
_EVERY_STEP = {
    "revert-after-settling",
    "rehash-fingerprint-back-at-scanned-content",
    "reset-marker-keeps-file-off-fast-pass",
}


def _install_parity(monkeypatch: pytest.MonkeyPatch, *, every_step: bool) -> dict[str, int]:
    """Wrap the scenario helpers: compare with check --full after each build and check (and each step)."""
    counts = {"build": 0, "check": 0, "step": 0}
    real_build, real_check, real_verify, real_edit = pairs._build, pairs._check, pairs._verify, pairs._edit
    real_reverify = pairs.reverify_nodes
    roots: list[Path] = []

    def build(root: Path) -> None:
        roots[:] = [root]
        real_build(root)
        assert_matches_full_recompute(_db_path(str(root)), root)
        counts["build"] += 1

    def check(root: Path):
        statuses = real_check(root)
        assert_matches_full_recompute(_db_path(str(root)), root)
        counts["check"] += 1
        return statuses

    monkeypatch.setattr(pairs, "_build", build)
    monkeypatch.setattr(pairs, "_check", check)
    if every_step:

        def step(after: str) -> None:
            _normal_pass_equals_forced_rehash(roots[0], after)
            counts["step"] += 1

        def verify(root: Path, *node_ids: str) -> None:
            real_verify(root, *node_ids)
            step(f"verify {node_ids}")

        def edit(path: Path, old: str, new: str) -> None:
            real_edit(path, old, new)
            step(f"edit {path.name}: {old!r} -> {new!r}")

        def reverify(*args, **kwargs):
            result = real_reverify(*args, **kwargs)
            step("reverify")
            return result

        monkeypatch.setattr(pairs, "_verify", verify)
        monkeypatch.setattr(pairs, "_edit", edit)
        monkeypatch.setattr(pairs, "reverify_nodes", reverify)
    return counts


@pytest.mark.parametrize(
    ("scenario_test", "kwargs", "every_step"), [case for scenario, fn in _SCENARIOS for case in _cases(fn, scenario)]
)
@workflow(
    purpose="Every linked-staleness scenario, re-run with each build and each incremental check followed by check "
    "--full on a copy: own status, link status and live hashes are equal at every point; for the revert and "
    "reset-marker sequences a normal pass and a forced re-hash also agree after every step"
)
def test_incremental_build_and_check_store_what_check_full_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario_test, kwargs: dict, every_step: bool
) -> None:
    counts = _install_parity(monkeypatch, every_step=every_step)
    project = pairs.make_project(tmp_path)
    scenario_test(project, **kwargs)
    assert counts["build"] + counts["check"] >= 2
    assert counts["step"] > 0 or not every_step


@workflow(
    purpose="An edit accepted by mark_clean and then reverted, with a normal pass and a forced re-hash compared "
    "on copies after every step; the dependents end VERIFIED"
)
def test_revert_after_mark_clean_agrees_with_a_forced_rehash_at_every_step(tmp_path: Path) -> None:
    project = pairs.make_project(tmp_path)
    dbp = _db_path(str(project))
    mod = project / "src" / "mod.py"
    steps = [
        ("verify the dependents", lambda: pairs._verify(project, pairs.SECTION_ID, pairs.TEST_ID)),
        ("edit f", lambda: pairs._edit(mod, "return 0", "return 10")),
        ("build", lambda: build_index(dbp, project)),
        ("mark f clean at the edit", lambda: pairs._verify(project, pairs.F_ID)),
        ("revert f", lambda: pairs._edit(mod, "return 10", "return 0")),
        ("build", lambda: build_index(dbp, project)),
    ]
    口 = Step(step_num=1, name="Walk the sequence", purpose="Each step, then the gate compared with a forced re-hash")
    for _name, run in steps:
        口 = Step(step_num=1.1, name="Run a step", purpose="One step of the edit, accept, revert sequence")
        run()
        口 = Step(step_num=1.2, name="Gate vs forced re-hash", purpose="A normal pass and check --full agree")
        _normal_pass_equals_forced_rehash(project)

    口 = Step(step_num=2, name="Net change only", purpose="The dependents saw the version f is back at")
    statuses = compute_check_summary(dbp, project).statuses
    assert statuses[pairs.SECTION_ID][1] == statuses[pairs.TEST_ID][1] == "VERIFIED"
    assert_matches_full_recompute(dbp, project)


# ---------------------------------------------------------------------------
# One case per refresh caller
# ---------------------------------------------------------------------------

_F_ID = "demo::pkg.mod::f"
_S_ID = "demo::docs/guide::f"
_OTHER_ID = "demo::docs/guide::other"
_T_ID = "demo::tests.test_mod::test_f"
_DOC_ID = "demo::docs/guide"


def _project(root: Path) -> Path:
    """S documents ``f``, T validates it, a second section documents ``h0``; init, then check."""
    (root / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "demo"\n', encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    for i in range(2):
        (pkg / f"other{i}.py").write_text(f"def h{i}():\n    return {i}\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text(
        "from pkg.mod import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    (root / "docs").mkdir()
    doc = {
        "title": "Guide",
        "tags": [],
        "sections": [
            {"id": "f", "heading": "f", "content": "f returns one.", "links": [{"node_id": _F_ID}]},
            {"id": "other", "heading": "Other", "content": "h0.", "links": [{"node_id": "demo::pkg.other0::h0"}]},
        ],
    }
    (root / "docs" / "guide.docjson").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(root)])
    assert result.exit_code == 0, result.output
    db_path = root / ".axiom_graph" / "graph.db"
    compute_check_summary(db_path, root)
    return db_path


def _edit_f(root: Path, value: int = 2) -> None:
    (root / "pkg" / "mod.py").write_text(f"def f():\n    return {value}\n", encoding="utf-8")


def _raw_edit_s_then_build(root: Path) -> None:
    """Edit section S by hand (a raw DocJSON edit) and build, so accept_doc_edits has something to accept."""
    path = root / "docs" / "guide.docjson"
    path.write_text(path.read_text(encoding="utf-8").replace("f returns one.", "f returns one, by hand."), "utf-8")
    build_index(_db_path(str(root)), root)


def _viz(root: Path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from axiom_graph.viz import server  # noqa: PLC0415

    server._PROJECT_ROOT = root
    server._DB_PATH = root / ".axiom_graph" / "graph.db"
    server._DFLOW_DB_PATH = None
    server._TEST_PATHS = []
    server._EXCLUDE_DIRS = []
    return fastapi_testclient.TestClient(server.app)


def _viz_bulk_verify(root: Path, ids: list[str]) -> None:
    resp = _viz(root).post("/api/nodes/bulk-verify", json={"node_ids": ids, "reason": "reviewed"})
    assert resp.status_code == 200 and all(r["ok"] for r in resp.json()["results"]), resp.text


def _viz_verify(root: Path, node_id: str) -> None:
    resp = _viz(root).post(f"/api/nodes/{node_id}/verify", json={"reason": "reviewed"})
    assert resp.status_code == 200, resp.text


def _ok(reply: str) -> None:
    assert not reply.startswith("ERROR"), reply


def _build_after_edit_purge_and_link(root: Path) -> None:
    """A one-file edit, a deleted file (its nodes purged) and a link added by hand, then one build."""
    _edit_f(root)
    (root / "pkg" / "other1.py").unlink()
    path = root / "docs" / "guide.docjson"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["sections"][1]["links"].append({"node_id": _F_ID})
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    build_index(_db_path(str(root)), root)


def _mark_clean(root: Path, *ids: str) -> None:
    mark_clean_nodes(_db_path(str(root)), root, list(ids), reason="reviewed", verified_by="human")


# caller -> (setup before the unbuilt edit of f, the call, the nodes it shows or None for the whole index)
_CALLERS: dict[str, tuple[Callable | None, Callable[[Path], object], list[str] | None]] = {
    "mark_clean": (None, lambda r: _mark_clean(r, _S_ID), None),
    "update_section": (None, lambda r: _ok(axiom_graph_update_section(str(r), _S_ID, content="f returns two.")), None),
    "accept_doc_edits": (
        _raw_edit_s_then_build,
        lambda r: _ok(axiom_graph_accept_doc_edits(str(r), section_ids=[_S_ID], verified_by="human")),
        None,
    ),
    "viz_bulk_verify": (None, lambda r: _viz_bulk_verify(r, [_S_ID, _T_ID]), None),
    "reverify": (None, lambda r: reverify_nodes(_db_path(str(r)), r, [_F_ID], "reviewed", verified_by="human"), None),
    "drift_query": (None, lambda r: compute_drift_query(_db_path(str(r)), r), None),
    "build_edit_purge_link": (None, None, None),
    "read_doc": (None, lambda r: axiom_graph_read_doc(str(r), section_ids=[_S_ID]), [_S_ID, _F_ID, _DOC_ID]),
    "read_doc_outline": (
        None,
        lambda r: axiom_graph_read_doc(str(r), doc_id=_DOC_ID, outline=True),
        [_S_ID, _OTHER_ID, _DOC_ID],
    ),
    "graph": (
        None,
        lambda r: fetch_graph(_db_path(str(r)), _F_ID, direction="in", with_locations=True, root=r),
        [_F_ID, _S_ID, _T_ID],
    ),
    "search": (None, lambda r: search_nodes(_db_path(str(r)), "f", scope="code", root=r), [_F_ID]),
    "source": (None, lambda r: fetch_source(_db_path(str(r)), r, _F_ID), [_F_ID]),
}


@pytest.mark.parametrize("caller", sorted(_CALLERS))
@workflow(
    purpose="Each refresh caller, run with an edit on disk that no build has seen, stores what check --full stores "
    "for what it refreshed (the whole index, or the nodes a read shows), and the next incremental check then "
    "matches check --full over the whole index"
)
def test_each_refresh_caller_stores_what_check_full_stores(tmp_path: Path, caller: str) -> None:
    db_path = _project(tmp_path)
    setup, call, shown = _CALLERS[caller]
    if setup is not None:
        setup(tmp_path)
    if call is None:
        _build_after_edit_purge_and_link(tmp_path)
    else:
        _edit_f(tmp_path)
        call(tmp_path)
    if shown is None:
        assert_matches_full_recompute(db_path, tmp_path)
    else:
        assert stored_rows(db_path, shown) == full_recompute_rows(db_path, tmp_path, shown)
    compute_check_summary(db_path, tmp_path)
    assert_matches_full_recompute(db_path, tmp_path)


# The written node, the link status the write leaves it with (a doc edit
# verifies the text only; naming f in addresses= reconciles the link), and the
# dependent left LINKED_STALE, per write.
_WRITES: dict[str, tuple[Callable | None, Callable[[Path], object], str, str, str]] = {
    "mark_clean": (None, lambda r: _mark_clean(r, _S_ID), _S_ID, "VERIFIED", _T_ID),
    "viz_verify": (None, lambda r: _viz_verify(r, _T_ID), _T_ID, "VERIFIED", _S_ID),
    "viz_bulk_verify": (None, lambda r: _viz_bulk_verify(r, [_S_ID]), _S_ID, "VERIFIED", _T_ID),
    "update_section": (
        None,
        lambda r: _ok(axiom_graph_update_section(str(r), _S_ID, content="f returns two.")),
        _S_ID,
        "LINKED_STALE",
        _T_ID,
    ),
    "update_section_addresses": (
        None,
        lambda r: _ok(axiom_graph_update_section(str(r), _S_ID, content="f returns two.", addresses=[_F_ID])),
        _S_ID,
        "VERIFIED",
        _T_ID,
    ),
    "accept_doc_edits": (
        _raw_edit_s_then_build,
        lambda r: _ok(axiom_graph_accept_doc_edits(str(r), section_ids=[_S_ID], verified_by="human")),
        _S_ID,
        "LINKED_STALE",
        _T_ID,
    ),
}


@pytest.mark.parametrize("write", sorted(_WRITES))
@workflow(
    purpose="A write tool leaves the index current: with f edited and not built, writing one of f's dependents "
    "leaves f CONTENT_UPDATED, the other dependent LINKED_STALE, and the written one's own status VERIFIED with "
    "the link status the write earns (a plain doc edit verifies the text only), as soon as it returns"
)
def test_write_tools_leave_the_index_current(tmp_path: Path, write: str) -> None:
    口 = Step(step_num=1, name="Index, then edit f", purpose="S documents f and T validates it; f changes, no build")
    db_path = _project(tmp_path)
    setup, run, verified, verified_link, flagged = _WRITES[write]
    if setup is not None:
        setup(tmp_path)
    _edit_f(tmp_path)

    口 = Step(step_num=2, name="Verify one dependent", purpose="mark_clean, viz verify, a doc-tool update or accept")
    run(tmp_path)

    口 = Step(step_num=3, name="Read the stored statuses", purpose="No check or build runs before the assertions")
    rows = stored_rows(db_path, [_F_ID, verified, flagged])
    assert rows[_F_ID][0] == "CONTENT_UPDATED"
    assert rows[verified][:2] == ("VERIFIED", verified_link)
    assert rows[flagged][1] == "LINKED_STALE"
    assert_matches_full_recompute(db_path, tmp_path)
