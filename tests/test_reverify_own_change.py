"""Behavioural tests: reverify clears only the LINKED_STALE its source caused.

A cascade dependent whose own content changed and was not reviewed gets a
link-only verification: its links clear, its own status stays CONTENT_UPDATED.
reverify's before and after LINKED_STALE counts are the ones ``check`` shows.
Scenarios enter through ``axiom_graph.lifecycle.api`` with real edits and
builds, reusing the project of ``tests/test_linked_stale_pairs.py``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.index import db
from axiom_graph.index.mark_clean import PairRecorder, verify_links_conn
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes, reverify_nodes
from tests.fixtures.full_recompute import assert_matches_full_recompute
from tests.test_linked_stale_pairs import BOTH_SECTION_ID, F_ID, G_ID, SECTION_ID, TEST_ID, make_project

ADR_SECTION = "proj::docs/adr::decision"
ADR_DOC = "proj::docs/adr"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path)


def _edit(path: Path, old: str, new: str) -> None:
    time.sleep(0.02)
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _hand_edit_section(root: Path, section: str, content: str) -> None:
    path = root / "docs" / "spec.json"
    time.sleep(0.02)
    data = json.loads(path.read_text(encoding="utf-8"))
    next(s for s in data["sections"] if s["id"] == section)["content"] = content
    path.write_text(json.dumps(data), encoding="utf-8")


def _status(root: Path, node_id: str) -> tuple[str, str]:
    with sqlite3.connect(_db_path(str(root))) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, node_id
    return row[0], row[1]


def _build(root: Path) -> None:
    build_index(_db_path(str(root)), root)


def _check_linked_stale(root: Path) -> int:
    summary = compute_check_summary(_db_path(str(root)), root)
    assert summary is not None
    return summary.link_counts["LINKED_STALE"]


def _latest_reason(root: Path, node_id: str) -> str:
    with db._connect(_db_path(str(root))) as conn:
        row = db.latest_verification_rows_conn(conn, [node_id])[node_id]
    meta = row["meta"] if isinstance(row["meta"], dict) else json.loads(row["meta"] or "{}")
    return meta.get("reason") or ""


@workflow(
    purpose="Reverifying a changed function clears the LINKED_STALE it caused on every dependent; a section whose "
    "text was also edited and a test edited on disk but not built keep their own change flagged and are reported "
    "as own changes kept, while a dependent with no change of its own is verified in full"
)
def test_reverify_keeps_a_dependents_own_unreviewed_change(project: Path) -> None:
    dbp = _db_path(str(project))
    mod = project / "src" / "mod.py"

    口 = Step(step_num=1, name="Change f", purpose="f's edit is built: its documenting sections and its test go stale")
    _edit(mod, "return 0", "return 10")
    _build(project)
    for node_id in (SECTION_ID, BOTH_SECTION_ID, TEST_ID):
        assert _status(project, node_id)[1] == "LINKED_STALE", node_id

    口 = Step(
        step_num=2,
        name="Own edits nobody reviewed",
        purpose="The overview section's text is edited by hand and built; the test is edited on disk, not built",
    )
    _hand_edit_section(project, "overview", "F, edited by hand.")
    _build(project)
    assert _status(project, SECTION_ID) == ("CONTENT_UPDATED", "LINKED_STALE")
    _edit(project / "tests" / "test_mod.py", "assert f() is not None", "assert f() == 10")

    口 = Step(step_num=3, name="Reverify f", purpose="One reverify of the source")
    result = reverify_nodes(dbp, project, [F_ID], "f reviewed", verified_by="human")

    口 = Step(
        step_num=4,
        name="Only the links clear on the own-changed dependents",
        purpose="Section and test read link VERIFIED and own CONTENT_UPDATED; the clean dependent is VERIFIED in full "
        "with reverify provenance; stored statuses equal a full recompute",
    )
    assert _status(project, SECTION_ID) == ("CONTENT_UPDATED", "VERIFIED")
    assert _status(project, TEST_ID) == ("CONTENT_UPDATED", "VERIFIED")
    assert set(result.own_change_kept) == {SECTION_ID, TEST_ID}
    assert {SECTION_ID, TEST_ID, BOTH_SECTION_ID} <= set(result.cleared)
    assert _status(project, BOTH_SECTION_ID) == ("VERIFIED", "VERIFIED")
    assert BOTH_SECTION_ID not in result.own_change_kept
    assert _latest_reason(project, BOTH_SECTION_ID).startswith(f"[reverify:{F_ID}]")
    assert_matches_full_recompute(dbp, project)
    _build(project)
    assert _status(project, SECTION_ID) == ("CONTENT_UPDATED", "VERIFIED")
    assert _status(project, TEST_ID) == ("CONTENT_UPDATED", "VERIFIED")


@pytest.mark.parametrize("verbose", [False, True])
def test_reverify_report_states_count_scope_and_own_changes_kept(project: Path, verbose: bool) -> None:
    """The MCP report states its counts are check's, with the scope phrase, and reports the dependents that kept
    their own change: a count line by default, their ids under verbose."""
    from axiom_graph.lifecycle.mcp_tools import REVERIFY_COUNT_SCOPE, axiom_graph_reverify

    _edit(project / "src" / "mod.py", "return 0", "return 10")
    _build(project)
    _hand_edit_section(project, "overview", "F, edited by hand.")
    _build(project)
    _edit(project / "tests" / "test_mod.py", "assert f() is not None", "assert f() == 10")

    before = _check_linked_stale(project)
    out = axiom_graph_reverify(str(project), F_ID, "f reviewed", verbose=verbose)
    after = _check_linked_stale(project)
    lines = out.splitlines()

    assert f"LINKED_STALE before: {before} -> after: {after} ({REVERIFY_COUNT_SCOPE})" in lines
    assert lines[1].endswith(" · own changes kept: 2"), lines[1]
    kept_heading = "Own change kept — links verified, own change still to review (2):"
    if verbose:
        at = lines.index(kept_heading)
        assert sorted(lines[at + 1 : at + 3]) == sorted([f"- {SECTION_ID}", f"- {TEST_ID}"])
    else:
        assert "2 cleared dependent(s) kept their own change: review them (verbose=true lists them)." in lines
        assert kept_heading not in lines
        assert f"- {SECTION_ID}" not in lines


@workflow(
    purpose="A dependent with its own unreviewed change and two offenders is skipped by the first reverify and "
    "cleared link-only by the second: composition and the link-only write combine"
)
def test_staggered_reverify_of_an_own_changed_dependent(project: Path) -> None:
    dbp = _db_path(str(project))
    mod = project / "src" / "mod.py"
    _edit(mod, "return 0", "return 10")
    _edit(mod, "def g():\n    return 1", "def g():\n    return 11")
    _build(project)
    _hand_edit_section(project, "both", "F and G, edited by hand.")
    _build(project)
    assert _status(project, BOTH_SECTION_ID) == ("CONTENT_UPDATED", "LINKED_STALE")

    first = reverify_nodes(dbp, project, [F_ID], "f reviewed", verified_by="human")
    assert first.skipped == {BOTH_SECTION_ID: [G_ID]}
    assert _status(project, BOTH_SECTION_ID) == ("CONTENT_UPDATED", "LINKED_STALE")

    second = reverify_nodes(dbp, project, [G_ID], "g reviewed", verified_by="human")
    assert BOTH_SECTION_ID in second.cleared
    assert second.own_change_kept == [BOTH_SECTION_ID]
    assert _status(project, BOTH_SECTION_ID) == ("CONTENT_UPDATED", "VERIFIED")
    assert_matches_full_recompute(dbp, project)


@workflow(
    purpose="With a frozen doc holding LINKED_STALE (section and doc), reverify's before and after counts equal the "
    "LINKED_STALE count check shows just before and just after the call"
)
def test_reverify_counts_match_check(project: Path) -> None:
    dbp = _db_path(str(project))
    (project / "docs" / "adr.json").write_text(
        json.dumps(
            {
                "title": "ADR",
                "tags": ["adr"],
                "sections": [{"id": "decision", "heading": "Decision", "content": "G.", "links": [{"node_id": G_ID}]}],
            }
        ),
        encoding="utf-8",
    )
    _build(project)
    mod = project / "src" / "mod.py"
    _edit(mod, "return 0", "return 10")
    _edit(mod, "def g():\n    return 1", "def g():\n    return 11")
    _build(project)
    assert _status(project, ADR_SECTION)[1] == "LINKED_STALE"
    toml = project / "axiom-graph.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + '\n[axiom_graph.staleness]\nfrozen_tags = ["adr"]\n', "utf-8")
    assert _status(project, ADR_DOC)[1] == "LINKED_STALE"

    before = _check_linked_stale(project)
    result = reverify_nodes(dbp, project, [F_ID], "f reviewed", verified_by="human")
    after = _check_linked_stale(project)
    assert (result.before_linked_stale, result.after_linked_stale) == (before, after)
    assert before > after
    assert _status(project, ADR_SECTION)[1] == "LINKED_STALE"
    assert ADR_DOC not in result.settled and ADR_DOC not in result.cleared


def test_link_only_write_leaves_snapshot_and_baseline(project: Path) -> None:
    """The link-only writer moves the verification time and records pairs, never the snapshot or the baseline."""
    dbp = _db_path(str(project))
    mark_clean_nodes(dbp, project, [BOTH_SECTION_ID], reason="reviewed", verified_by="human")
    _hand_edit_section(project, "both", "Changed by hand.")
    _hand_edit_section(project, "overview", "Changed by hand too.")
    _build(project)
    with db._connect(dbp) as conn:
        before = db.get_verifications_for_conn(conn, [BOTH_SECTION_ID, SECTION_ID])
        baselines = {nid: db._get_node_hashes_conn(conn, nid) for nid in (BOTH_SECTION_ID, SECTION_ID)}
    assert SECTION_ID not in before
    time.sleep(0.02)
    recorder = PairRecorder(project, from_index=True)
    with db._connect(dbp) as conn:
        nodes = db.get_nodes_conn(conn, [BOTH_SECTION_ID, SECTION_ID])
        for node in nodes.values():
            verify_links_conn(conn, recorder, node, reason="links only", verified_by="human")
    with db._connect(dbp) as conn:
        after = db.get_verifications_for_conn(conn, [BOTH_SECTION_ID, SECTION_ID])
        for nid in (BOTH_SECTION_ID, SECTION_ID):
            assert db._get_node_hashes_conn(conn, nid) == baselines[nid], nid
    old, new = before[BOTH_SECTION_ID], after[BOTH_SECTION_ID]
    assert (new["code_hash_at"], new["desc_hash_at"]) == (old["code_hash_at"], old["desc_hash_at"])
    assert new["verified_at"] > old["verified_at"]
    assert (after[SECTION_ID]["code_hash_at"], after[SECTION_ID]["desc_hash_at"]) == baselines[SECTION_ID]
    assert set(db.get_verification_targets(dbp, BOTH_SECTION_ID)) == {F_ID, G_ID}
    compute_check_summary(dbp, project)
    assert _status(project, BOTH_SECTION_ID)[0] == "CONTENT_UPDATED"
    assert _status(project, SECTION_ID)[0] == "CONTENT_UPDATED"


_BATCH = 200
_REPORT_BOUND = 1_500


def _batch_project(root: Path) -> list[str]:
    """200 functions, each documented by its own section of one doc; every function changed and built."""
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    body = "\n\n".join(f"def f{i}():\n    return {i}\n" for i in range(_BATCH))
    (root / "src" / "many.py").write_text(body, encoding="utf-8")
    sections = [
        {"id": f"s{i}", "heading": f"S{i}", "content": f"F{i}.", "links": [{"node_id": f"proj::src.many::f{i}"}]}
        for i in range(_BATCH)
    ]
    (root / "docs" / "many.json").write_text(json.dumps({"title": "Many", "sections": sections}), encoding="utf-8")
    build_index(_db_path(str(root)), root)
    time.sleep(0.02)
    (root / "src" / "many.py").write_text(body.replace("    return ", "    return 1 + "), encoding="utf-8")
    build_index(_db_path(str(root)), root)
    return [f"proj::src.many::f{i}" for i in range(_BATCH)]


@pytest.mark.parametrize("verbose", [False, True])
@workflow(
    purpose="A 200-source reverify batch through the MCP tool returns a compact report by default, within a fixed "
    "size and leading with the cleared count, while verbose lists every source and cleared node; the doc that "
    "settled by inheritance is never listed as cleared"
)
def test_batch_reverify_report_stays_compact(tmp_path: Path, verbose: bool) -> None:
    from axiom_graph.lifecycle.mcp_tools import axiom_graph_reverify

    sources = _batch_project(tmp_path)
    assert _status(tmp_path, "proj::docs/many")[1] == "LINKED_STALE"
    out = axiom_graph_reverify(str(tmp_path), "", "all reviewed", node_ids=sources, verbose=verbose)

    first = out.splitlines()[0]
    assert first.startswith(f"Cleared {_BATCH} LINKED_STALE node(s)."), first
    listed = {line[2:] for line in out.splitlines() if line.startswith("- ")}
    assert "proj::docs/many" not in listed
    if verbose:
        assert set(sources) | {f"proj::docs/many::s{i}" for i in range(_BATCH)} <= listed
        assert "Settled by inheritance (not listed as cleared): 1" in out
    else:
        assert len(out) < _REPORT_BOUND, len(out)
        assert not listed
    assert _status(tmp_path, "proj::docs/many") == ("VERIFIED", "VERIFIED")


_ROWS = 25
_PER_ROW = 5
_UNKNOWN = 30
_CAPPED_REPORT_BOUND = 4_000


def _skip_heavy_project(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    """25 sections, each documenting f and five functions of its own; every function changed, the five marked clean.

    Returns each section's own offenders (sorted) and every marked-clean offender.
    """
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    names = ["f"] + [f"g{i:02d}_{j}" for i in range(_ROWS) for j in range(_PER_ROW)]
    body = "\n\n".join(f"def {name}():\n    return 0\n" for name in names)
    (root / "src" / "many.py").write_text(body, encoding="utf-8")
    offenders = {
        f"proj::docs/many::s{i:02d}": [f"proj::src.many::g{i:02d}_{j}" for j in range(_PER_ROW)] for i in range(_ROWS)
    }
    sections = [
        {
            "id": sid.rsplit("::", 1)[1],
            "heading": sid.rsplit("::", 1)[1].upper(),
            "content": "F and its helpers.",
            "links": [{"node_id": "proj::src.many::f"}] + [{"node_id": o} for o in offs],
        }
        for sid, offs in offenders.items()
    ]
    (root / "docs" / "many.json").write_text(json.dumps({"title": "Many", "sections": sections}), encoding="utf-8")
    build_index(_db_path(str(root)), root)
    time.sleep(0.02)
    (root / "src" / "many.py").write_text(body.replace("return 0", "return 1"), encoding="utf-8")
    build_index(_db_path(str(root)), root)
    marked = sorted(o for offs in offenders.values() for o in offs)
    time.sleep(0.02)
    mark_clean_nodes(_db_path(str(root)), root, marked, reason="helpers re-read", verified_by="human")
    return offenders, marked


@pytest.mark.parametrize("verbose", [False, True])
def test_reverify_report_caps_skipped_offenders_notes_and_not_found(tmp_path: Path, verbose: bool) -> None:
    """Many skipped rows, five offenders per row, marked-clean offenders and 30 unknown ids: the default report
    stays within a fixed size (each list capped with ``+N more``, notes only for offenders it shows), while
    verbose lists every row, offender, note and unknown id."""
    from axiom_graph.lifecycle.api import REVERIFY_MARKED_CLEAN_NOTE
    from axiom_graph.lifecycle.mcp_tools import (
        REVERIFY_NOT_FOUND_CAP,
        REVERIFY_OFFENDERS_CAP,
        REVERIFY_SKIPPED_CAP,
        axiom_graph_reverify,
    )

    offenders, marked = _skip_heavy_project(tmp_path)
    unknown = [f"proj::src.nope::x{k:02d}" for k in range(_UNKNOWN)]
    out = axiom_graph_reverify(
        str(tmp_path), "", "f reviewed", node_ids=["proj::src.many::f", *unknown], verbose=verbose
    )
    lines = out.splitlines()
    assert f"Skipped — also stale via offenders outside the batch ({_ROWS}):" in lines
    assert f"Not found ({_UNKNOWN}):" in lines
    row_lines = [line for line in lines if line.startswith("- proj::docs/many::s")]
    note_count = sum(REVERIFY_MARKED_CLEAN_NOTE.format(offender=o) in out for o in marked)

    if verbose:
        assert row_lines == [f"- {sid} (other offenders: {', '.join(offs)})" for sid, offs in offenders.items()]
        assert all(f"- {nid}" in lines for nid in unknown)
        assert note_count == len(marked)
        assert "more; pass verbose=true for all" not in out
        return

    assert len(out) < _CAPPED_REPORT_BOUND, len(out)
    shown_rows = list(offenders.items())[:REVERIFY_SKIPPED_CAP]
    assert row_lines == [
        f"- {sid} (other offenders: {', '.join(offs[:REVERIFY_OFFENDERS_CAP])} +{_PER_ROW - REVERIFY_OFFENDERS_CAP} more)"
        for sid, offs in shown_rows
    ]
    assert f"(+{_ROWS - REVERIFY_SKIPPED_CAP} more; pass verbose=true for all)" in lines
    hidden = [o for _sid, offs in list(offenders.items())[REVERIFY_SKIPPED_CAP:] for o in offs]
    assert not any(o in out for o in hidden)
    assert note_count == 0
    named = sorted(o for _sid, offs in shown_rows for o in offs[:REVERIFY_OFFENDERS_CAP])
    combined = [line for line in lines if "marked clean, not reverified" in line]
    assert combined == [
        f"{len(named)} offenders named above were marked clean, not reverified — mark_clean does not settle an "
        f"offender for its dependents; reverify them to clear their dependents: "
        f"{', '.join(named[:REVERIFY_OFFENDERS_CAP])} +{len(named) - REVERIFY_OFFENDERS_CAP} more."
    ]
    assert [line for line in lines if line.startswith("- proj::src.nope::")] == [
        f"- {nid}" for nid in unknown[:REVERIFY_NOT_FOUND_CAP]
    ]
    assert f"(+{_UNKNOWN - REVERIFY_NOT_FOUND_CAP} more; pass verbose=true for all)" in lines
