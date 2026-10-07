"""A doc edit verifies the text it wrote, never a link.

Editing a LINKED_STALE section through the doc tools marks its own text as
reviewed (own status VERIFIED) and leaves every link status as it was: the
edited section stays LINKED_STALE until its offender is reconciled
(``addresses=``, ``mark_clean`` or ``reverify``), and no other section moves.
After every step the stored statuses equal what ``check --full`` stores.

Drives the docjson API, not the build fixture path: a full build would
refresh the section rows itself and mask what the write did.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.db.staleness import get_stale_doc_sections
from axiom_graph.docjson.api import (
    axiom_graph_accept_doc_edits,
    axiom_graph_delete_section,
    axiom_graph_patch_section,
    axiom_graph_update_doc_meta,
    axiom_graph_update_section,
    axiom_graph_write_doc,
)
from axiom_graph.index import builder
from axiom_graph.index.staleness import _get_linked_stale_ids
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes
from axiom_graph.lifecycle.mcp_tools import axiom_graph_mark_clean
from tests.fixtures.full_recompute import assert_matches_full_recompute


def _statuses(db_path: Path, node_id: str) -> tuple[str, str]:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row[0], row[1]


def _latest_verification_meta(db_path: Path, node_id: str) -> dict:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT meta FROM node_history WHERE node_id = ? AND change_type IN ('AGENT_VERIFIED', 'MANUAL_VERIFIED') "
            "ORDER BY id DESC LIMIT 1",
            (node_id,),
        ).fetchone()
    return json.loads(row[0])


def _project(root: Path, section_ids: list[str]) -> None:
    """``src/mod.py::foo`` documented by every section in *section_ids*; built, then ``foo`` changed and built."""
    (root / "docs").mkdir(exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    code_path = root / "src" / "mod.py"
    code_path.write_text("def foo():\n    return 0\n", encoding="utf-8")
    sections = [
        {
            "id": sid,
            "heading": sid.title(),
            "content": f"{sid} section content (documents foo).",
            "links": [{"node_id": "proj::src.mod::foo"}],
        }
        for sid in section_ids
    ]
    (root / "docs" / "spec.json").write_text(json.dumps({"title": "Spec", "sections": sections}, indent=2), "utf-8")
    builder.build(root, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_path.write_text("def foo():\n    return 42\n", encoding="utf-8")
    builder.build(root, project_id="proj", discovery_only=False)


@workflow(
    purpose="A plain section edit verifies the section's text only: its own status is VERIFIED, it stays "
    "LINKED_STALE via the changed code, the reply says so, and only mark_clean clears it, each step matching "
    "check --full"
)
def test_a_plain_section_edit_keeps_linked_stale_until_mark_clean(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="A section documenting changed code",
        purpose="The section links foo, foo changes and is built, so the section is LINKED_STALE",
    )
    _project(mini_project, ["overview"])
    section_id = "proj::docs/spec::overview"
    assert section_id in {row["section_id"] for row in get_stale_doc_sections(db_path)}
    assert section_id in _get_linked_stale_ids(db_path)

    口 = Step(
        step_num=2,
        name="Edit the section's text",
        purpose="update_section with new content and no addresses",
    )
    result = axiom_graph_update_section(
        project_root=str(mini_project),
        section_id=section_id,
        content="Section content updated to describe the new foo.",
    )
    assert "ERROR" not in result, result
    assert "\n  still LINKED_STALE via: proj::src.mod::foo" in result

    口 = Step(
        step_num=3,
        name="The text is verified, the link is not",
        purpose="Own status VERIFIED, link status still LINKED_STALE via foo; the history row records a text-only "
        "doc edit; the stored rows equal a full recompute",
    )
    assert _statuses(db_path, section_id) == ("VERIFIED", "LINKED_STALE")
    assert _get_linked_stale_ids(db_path)[section_id] == ["proj::src.mod::foo"]
    meta = _latest_verification_meta(db_path, section_id)
    assert meta["verification_op"] == "doc_edit" and meta["verifies"] == "text"
    assert_matches_full_recompute(db_path, mini_project)

    口 = Step(
        step_num=4,
        name="mark_clean clears it",
        purpose="An explicit verification of the section's links clears the LINKED_STALE the edit left",
    )
    mark_result = axiom_graph_mark_clean(
        project_root=str(mini_project),
        node_id=section_id,
        reason="Verified prose still matches updated foo() implementation.",
    )
    assert "ERROR" not in mark_result, mark_result
    assert section_id not in _get_linked_stale_ids(db_path)
    assert _statuses(db_path, section_id) == ("VERIFIED", "VERIFIED")
    assert_matches_full_recompute(db_path, mini_project)


@workflow(
    purpose="Editing one of two sections stale through the same code leaves both LINKED_STALE: a doc edit clears "
    "no link status, the edited section's included, matching check --full"
)
def test_editing_one_stale_section_leaves_every_stale_section_stale(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="Two sections documenting changed code",
        purpose="Both sections link foo; foo changes and is built, so both are LINKED_STALE",
    )
    _project(mini_project, ["parent", "child"])
    parent_id = "proj::docs/spec::parent"
    child_id = "proj::docs/spec::child"
    before = _get_linked_stale_ids(db_path)
    assert parent_id in before and child_id in before

    口 = Step(step_num=2, name="Edit only the child", purpose="update_section on the child, no addresses")
    result = axiom_graph_update_section(
        project_root=str(mini_project),
        section_id=child_id,
        content="Child section content updated to acknowledge code change.",
    )
    assert "ERROR" not in result, result

    口 = Step(
        step_num=3,
        name="Both stay LINKED_STALE",
        purpose="The child's text is verified, its link is not; the parent is untouched; a full recompute agrees",
    )
    after = _get_linked_stale_ids(db_path)
    assert child_id in after and parent_id in after
    assert _statuses(db_path, child_id) == ("VERIFIED", "LINKED_STALE")
    assert _statuses(db_path, parent_id)[1] == "LINKED_STALE"
    stale_after = {row["section_id"] for row in get_stale_doc_sections(db_path)}
    assert {parent_id, child_id} <= stale_after
    assert_matches_full_recompute(db_path, mini_project)


def _verification_row_count(db_path: Path, node_id: str) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM node_verification WHERE node_id = ?", (node_id,)).fetchone()[0]


@workflow(
    purpose="A never-verified section documenting a deleted function stays LINKED_STALE after a plain edit, "
    "whose new verification row must not settle the deletion by its time, and clears with mark_clean, each step "
    "matching check --full"
)
def test_a_plain_edit_keeps_linked_stale_via_a_deleted_function(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="A never-verified section documenting a deleted function",
        purpose="The section links foo; foo changes and is built, then is deleted and checked, so foo is NOT_FOUND "
        "and the section is LINKED_STALE via foo with no verification row",
    )
    (mini_project / "docs").mkdir(exist_ok=True)
    (mini_project / "src").mkdir(exist_ok=True)
    code_path = mini_project / "src" / "mod.py"
    code_path.write_text("def foo():\n    return 0\n\n\ndef bar():\n    return 1\n", encoding="utf-8")
    spec = {
        "title": "Spec",
        "sections": [
            {
                "id": "overview",
                "heading": "Overview",
                "content": "Documents foo.",
                "links": [{"node_id": "proj::src.mod::foo"}],
            }
        ],
    }
    (mini_project / "docs" / "spec.json").write_text(json.dumps(spec, indent=2), "utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_path.write_text("def foo():\n    return 42\n\n\ndef bar():\n    return 1\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_path.write_text("def bar():\n    return 1\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    compute_check_summary(db_path, mini_project)
    section_id = "proj::docs/spec::overview"
    assert _statuses(db_path, "proj::src.mod::foo")[0] == "NOT_FOUND"
    assert _get_linked_stale_ids(db_path)[section_id] == ["proj::src.mod::foo"]
    assert _verification_row_count(db_path, section_id) == 0

    口 = Step(step_num=2, name="A plain edit", purpose="update_section with new content and no addresses")
    time.sleep(0.05)
    result = axiom_graph_update_section(
        project_root=str(mini_project), section_id=section_id, content="Overview rewritten; still names foo."
    )
    assert "ERROR" not in result, result
    assert "\n  still LINKED_STALE via: proj::src.mod::foo" in result

    口 = Step(
        step_num=3,
        name="Still LINKED_STALE",
        purpose="The edit created the section's verification row, but the deleted function stays an open offender",
    )
    assert _verification_row_count(db_path, section_id) == 1
    assert _statuses(db_path, section_id) == ("VERIFIED", "LINKED_STALE")
    assert_matches_full_recompute(db_path, mini_project)

    口 = Step(step_num=4, name="mark_clean clears it", purpose="An explicit verification of the links clears it")
    mark_result = axiom_graph_mark_clean(
        project_root=str(mini_project), node_id=section_id, reason="Reviewed: foo is gone, prose updated."
    )
    assert "ERROR" not in mark_result, mark_result
    assert section_id not in _get_linked_stale_ids(db_path)
    assert _statuses(db_path, section_id) == ("VERIFIED", "VERIFIED")
    assert_matches_full_recompute(db_path, mini_project)


# ---------------------------------------------------------------------------
# A plain edit clears no link status anywhere, whichever doc write makes it
# ---------------------------------------------------------------------------

_F_ID = "proj::src.mod::f"
_SPEC_ID = "proj::docs/spec"
_S1_ID = f"{_SPEC_ID}::s1"
_S2_ID = f"{_SPEC_ID}::s2"
_SIBLING_ID = f"{_SPEC_ID}::sibling"
_CONSUMER_ID = "proj::docs/guide::r"
_FROZEN_ID = "proj::docs/adr::frozen"
_STILL_VIA_F = "\n  still LINKED_STALE via: proj::src.mod::f"
_CARRIED_LINE = "\n  still LINKED_STALE (carried on a frozen doc; mark_clean clears it)"
_CONFIG = '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n'


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _doc(title: str, sections: list[dict], tags: list[str] | None = None) -> str:
    return json.dumps({"title": title, **({"tags": tags} if tags else {}), "sections": sections}, indent=2)


def _stale_everywhere_project(root: Path, db_path: Path) -> None:
    """S1 and S2 document f, a consumer section R links S1, a frozen-doc section F documents f; f changes.

    S1 is marked clean before the change, so it holds a receipt for f.  F's
    doc is frozen after it went LINKED_STALE, so F carries it.
    """
    _write_file(root / "axiom-graph.toml", _CONFIG)
    _write_file(root / "src" / "__init__.py", "")
    _write_file(root / "src" / "mod.py", "def f():\n    return 0\n")
    _write_file(
        root / "docs" / "spec.json",
        _doc(
            "Spec",
            [
                {"id": "s1", "heading": "S1", "content": "S1 documents f.", "links": [{"node_id": _F_ID}]},
                {"id": "s2", "heading": "S2", "content": "S2 documents f.", "links": [{"node_id": _F_ID}]},
                {"id": "sibling", "heading": "Sibling", "content": "Unrelated."},
            ],
        ),
    )
    consumer = [{"id": "r", "heading": "R", "content": "R reads S1.", "links": [{"node_id": _S1_ID}]}]
    _write_file(root / "docs" / "guide.json", _doc("Guide", consumer, ["consumer"]))
    frozen = [{"id": "frozen", "heading": "F", "content": "F documents f.", "links": [{"node_id": _F_ID}]}]
    _write_file(root / "docs" / "adr.json", _doc("ADR", frozen, ["adr"]))
    build_index(db_path, root)
    mark_clean_nodes(db_path, root, [_S1_ID], reason="reviewed", verified_by="human")
    time.sleep(0.05)
    _write_file(root / "src" / "mod.py", "def f():\n    return 42\n")
    build_index(db_path, root)
    compute_check_summary(db_path, root)
    _write_file(root / "axiom-graph.toml", _CONFIG + 'frozen_tags = ["adr"]\n')
    compute_check_summary(db_path, root)


def _hand_edit_s1_then_build(root: Path, db_path: Path) -> None:
    spec = root / "docs" / "spec.json"
    time.sleep(0.05)
    spec.write_text(spec.read_text(encoding="utf-8").replace("S1 documents f.", "S1 edited by hand."), "utf-8")
    build_index(db_path, root)


def _write_doc_editing_s1(root: Path) -> str:
    data = json.loads((root / "docs" / "spec.json").read_text(encoding="utf-8"))
    data["sections"][0]["content"] = "S1 rewritten through write_doc."
    return axiom_graph_write_doc(str(root), doc_json={"id": "spec", **data})


def _link_evidence(db_path: Path, node_id: str) -> tuple:
    """The section's ``verified_at`` and its receipts (target, code hash, desc hash)."""
    with closing(sqlite3.connect(db_path)) as conn:
        at = conn.execute("SELECT verified_at FROM node_verification WHERE node_id = ?", (node_id,)).fetchone()
        receipts = conn.execute(
            "SELECT target_id, code_hash, desc_hash FROM node_verification_targets WHERE node_id = ? "
            "ORDER BY target_id",
            (node_id,),
        ).fetchall()
    return at, receipts


# Per doc write: the setup it needs, the write, the node whose own status the
# write verifies, and whether its reply carries the still-LINKED_STALE line.
_PLAIN_WRITES = {
    "update_section": (
        None,
        lambda r: axiom_graph_update_section(str(r), _S1_ID, content="S1 rewritten."),
        _S1_ID,
        True,
    ),
    "patch_section": (
        None,
        lambda r: axiom_graph_patch_section(str(r), _S1_ID, new_string="More on f.", anchor="$"),
        _S1_ID,
        True,
    ),
    "write_doc": (None, _write_doc_editing_s1, _S1_ID, False),
    "accept_doc_edits": (
        _hand_edit_s1_then_build,
        lambda r: axiom_graph_accept_doc_edits(str(r), section_ids=[_S1_ID], verified_by="human"),
        _S1_ID,
        False,
    ),
    "update_doc_meta": (
        None,
        lambda r: axiom_graph_update_doc_meta(str(r), _SPEC_ID, title="Spec v2"),
        _SPEC_ID,
        False,
    ),
    "delete_section": (None, lambda r: axiom_graph_delete_section(str(r), _SIBLING_ID), _SPEC_ID, False),
}


@pytest.mark.parametrize("write", sorted(_PLAIN_WRITES))
@workflow(
    purpose="A plain doc write, whichever tool makes it, verifies only the text it wrote: the sections documenting "
    "the changed function, their doc envelope, a consumer section linking one of them and a frozen section carrying "
    "LINKED_STALE all stay LINKED_STALE, the edited section's receipts and verified_at are untouched, and editing "
    "the frozen section keeps it stale too, each step matching check --full"
)
def test_a_plain_doc_write_clears_no_link_status_anywhere(mini_project: Path, db_path: Path, write: str) -> None:
    setup, run, edited, says_still = _PLAIN_WRITES[write]
    stale = [_S1_ID, _S2_ID, _SPEC_ID, _CONSUMER_ID, _FROZEN_ID]

    口 = Step(
        step_num=1,
        name="Stale everywhere",
        purpose="f changes after S1 was verified: S1, S2, their doc, the consumer section and the frozen section "
        "are LINKED_STALE",
    )
    _stale_everywhere_project(mini_project, db_path)
    if setup is not None:
        setup(mini_project, db_path)
    for node_id in stale:
        assert _statuses(db_path, node_id)[1] == "LINKED_STALE", node_id
    evidence = _link_evidence(db_path, _S1_ID)
    assert evidence[1], "S1 holds a receipt for f"

    口 = Step(step_num=2, name="A plain doc write", purpose="The parametrised write, with no addresses")
    result = run(mini_project)
    assert not result.startswith("ERROR"), result
    assert (_STILL_VIA_F in result) == says_still, result

    口 = Step(
        step_num=3,
        name="Only the text is verified",
        purpose="The written node's own status is VERIFIED; every link status stays LINKED_STALE; S1's receipts and "
        "verified_at are unchanged; the stored rows equal a full recompute",
    )
    assert _statuses(db_path, edited)[0] == "VERIFIED"
    for node_id in stale:
        assert _statuses(db_path, node_id)[1] == "LINKED_STALE", (write, node_id)
    assert _link_evidence(db_path, _S1_ID) == evidence
    assert_matches_full_recompute(db_path, mini_project)

    口 = Step(
        step_num=4,
        name="Edit the frozen section",
        purpose="A plain edit of the frozen section keeps the LINKED_STALE it carries, and the reply says so",
    )
    frozen_result = axiom_graph_update_section(str(mini_project), _FROZEN_ID, content="F rewritten.")
    assert frozen_result.endswith(_CARRIED_LINE), frozen_result
    assert _statuses(db_path, _FROZEN_ID) == ("VERIFIED", "LINKED_STALE")
    assert_matches_full_recompute(db_path, mini_project)
