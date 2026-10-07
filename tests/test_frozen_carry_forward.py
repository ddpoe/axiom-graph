"""Behavioural tests for staleness on frozen-doc sections.

A frozen doc's sections receive no new LINKED_STALE, but freezing never
clears what they already carry.  Verification does: ``mark_clean`` on a
carried section clears it on the next check, and the doc envelope clears
by inheritance once no section stays stale.  A BROKEN_LINK on a frozen
section is never hidden: ``check`` and every ``drift_query`` format count
and list it, as they drop frozen LINKED_STALE.  Scenarios enter through
``axiom_graph.lifecycle.api`` / ``axiom_graph.query.api`` and assert the
persisted ``link_status``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from axiom_annotations import Step, workflow

from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes

CODE_ID = "proj::src.mod::foo"
DOC_ID = "proj::docs/adr-001"
VERIFIED_SECTION = "proj::docs/adr-001::context"
UNVERIFIED_SECTION = "proj::docs/adr-001::decision"

_CONFIG = '[axiom_graph]\nproject_id = "proj"\n'
_FROZEN_CONFIG = _CONFIG + '\n[axiom_graph.staleness]\nfrozen_tags = ["adr"]\n'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _link_status(root: Path, node_id: str) -> str:
    with sqlite3.connect(_db_path(str(root))) as conn:
        row = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, node_id
    return row[0]


def _check(root: Path) -> None:
    assert compute_check_summary(_db_path(str(root)), root) is not None


@workflow(
    purpose="A frozen doc's sections that were LINKED_STALE before the freeze stay LINKED_STALE through freezing; "
    "mark_clean on one clears it on the next check while an unverified sibling stays stale and keeps the doc "
    "envelope stale; once both are verified the envelope clears by inheritance"
)
def test_mark_clean_clears_carried_linked_stale_on_frozen_section(tmp_path: Path) -> None:
    dbp = _db_path(str(tmp_path))

    口 = Step(
        step_num=1,
        name="Make both sections LINKED_STALE before the freeze",
        purpose="Two sections of an adr-tagged doc link a function that then changes",
    )
    _write(tmp_path / "axiom-graph.toml", _CONFIG)
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 0\n")
    _write(
        tmp_path / "docs" / "adr-001.json",
        json.dumps(
            {
                "title": "ADR 001",
                "tags": ["adr"],
                "sections": [
                    {"id": "context", "heading": "Context", "content": "Foo.", "links": [{"node_id": CODE_ID}]},
                    {"id": "decision", "heading": "Decision", "content": "Foo.", "links": [{"node_id": CODE_ID}]},
                ],
            }
        ),
    )
    build_index(dbp, tmp_path)
    time.sleep(0.02)
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 1\n")
    build_index(dbp, tmp_path)
    _check(tmp_path)
    for section in (VERIFIED_SECTION, UNVERIFIED_SECTION):
        assert _link_status(tmp_path, section) == "LINKED_STALE", section

    口 = Step(
        step_num=2,
        name="Freeze the doc",
        purpose="Adopting frozen_tags preserves the existing LINKED_STALE on every section and the envelope",
    )
    _write(tmp_path / "axiom-graph.toml", _FROZEN_CONFIG)
    _check(tmp_path)
    for node_id in (VERIFIED_SECTION, UNVERIFIED_SECTION, DOC_ID):
        assert _link_status(tmp_path, node_id) == "LINKED_STALE", node_id

    口 = Step(
        step_num=3,
        name="mark_clean one section",
        purpose="The verified section clears; the section with no newer verification and the envelope stay stale",
    )
    mark_clean_nodes(dbp, tmp_path, [VERIFIED_SECTION], reason="re-read against foo", verified_by="human")
    _check(tmp_path)
    assert _link_status(tmp_path, VERIFIED_SECTION) == "VERIFIED"
    assert _link_status(tmp_path, UNVERIFIED_SECTION) == "LINKED_STALE"
    assert _link_status(tmp_path, DOC_ID) == "LINKED_STALE"

    口 = Step(
        step_num=4,
        name="mark_clean the other section",
        purpose="With no stale section left, the envelope clears by inheritance and stays clear on the next check",
    )
    mark_clean_nodes(dbp, tmp_path, [UNVERIFIED_SECTION], reason="re-read against foo", verified_by="human")
    _check(tmp_path)
    _check(tmp_path)
    for node_id in (VERIFIED_SECTION, UNVERIFIED_SECTION, DOC_ID):
        assert _link_status(tmp_path, node_id) == "VERIFIED", node_id


BROKEN_SECTION = "proj::docs/adr-001::broken"
GONE_ID = "proj::src.gone::bar"


def _frozen_broken_and_stale_project(root: Path) -> None:
    """A frozen doc holding one BROKEN_LINK section and one LINKED_STALE section."""
    dbp = _db_path(str(root))
    _write(root / "axiom-graph.toml", _CONFIG)
    _write(root / "src" / "__init__.py", "")
    _write(root / "src" / "mod.py", "def foo():\n    return 0\n")
    _write(root / "src" / "gone.py", "def bar():\n    return 0\n")
    _write(
        root / "docs" / "adr-001.json",
        json.dumps(
            {
                "title": "ADR 001",
                "tags": ["adr"],
                "sections": [
                    {"id": "context", "heading": "Context", "content": "Foo.", "links": [{"node_id": CODE_ID}]},
                    {"id": "broken", "heading": "Broken", "content": "Bar.", "links": [{"node_id": GONE_ID}]},
                ],
            }
        ),
    )
    build_index(dbp, root)
    time.sleep(0.02)
    _write(root / "src" / "mod.py", "def foo():\n    return 1\n")
    (root / "src" / "gone.py").unlink()
    build_index(dbp, root)
    _check(root)
    _write(root / "axiom-graph.toml", _FROZEN_CONFIG)
    _check(root)
    assert _link_status(root, VERIFIED_SECTION) == "LINKED_STALE"
    assert _link_status(root, BROKEN_SECTION) == "BROKEN_LINK"


def _listed_ids(text: str) -> set[str]:
    """The node ids a drift_query projection lists, one per row (``id=`` prefix and indent dropped)."""
    ids: set[str] = set()
    for line in text.splitlines():
        token = line.strip().split("  ")[0]
        if token.startswith("id="):
            token = token[len("id=") :]
        if token.startswith("proj::"):
            ids.add(token)
    return ids


@pytest.mark.parametrize("include_frozen", [False, True])
@workflow(
    purpose="A BROKEN_LINK on a frozen doc's section is counted by check and listed by drift_query counts, ids and "
    "full alike, while a frozen LINKED_STALE section and the frozen doc's own node are dropped from all of them "
    "unless include_frozen is set"
)
def test_frozen_broken_link_agrees_across_surfaces(tmp_path: Path, include_frozen: bool) -> None:
    from axiom_graph.query.api import compute_drift_query

    _frozen_broken_and_stale_project(tmp_path)
    dbp = _db_path(str(tmp_path))
    assert _link_status(tmp_path, DOC_ID) == "LINKED_STALE"

    summary = compute_check_summary(dbp, tmp_path, include_frozen=include_frozen)
    assert summary is not None
    assert summary.link_counts["BROKEN_LINK"] == 1
    assert summary.link_counts["LINKED_STALE"] == (2 if include_frozen else 0)
    assert BROKEN_SECTION in summary.statuses
    assert (VERIFIED_SECTION in summary.statuses) is include_frozen
    assert (DOC_ID in summary.statuses) is include_frozen
    assert (DOC_ID in summary.problem_statuses) is include_frozen

    def _drift(**kwargs) -> str:
        return compute_drift_query(dbp, tmp_path, include_frozen=include_frozen, **kwargs)

    surfaces = {
        "ids": _drift(format="ids"),
        "ids-by-status": _drift(format="ids", group_by="status"),
        "ids-by-location": _drift(format="ids", group_by="location_prefix"),
        "full": _drift(format="full"),
        "full-by-status": _drift(format="full", group_by="status"),
    }
    for name, text in surfaces.items():
        assert BROKEN_SECTION in text, name
        assert (VERIFIED_SECTION in text) is include_frozen, name
        assert (DOC_ID in _listed_ids(text)) is include_frozen, name
    marker = "[frozen]" if include_frozen else "[frozen-source]"
    broken_line = next(line for line in surfaces["full"].splitlines() if BROKEN_SECTION in line)
    assert broken_line.endswith(marker)

    counts = dict(line.rsplit("  ", 1) for line in _drift(group_by="status", format="counts").splitlines())
    assert sum(int(n) for group, n in counts.items() if group.endswith("/BROKEN_LINK")) == 1


FROZEN_TWIN = "proj::docs/adr-002"
LIVE_TWIN = "proj::docs/guide"


def _twin_doc(root: Path, name: str, title: str, tags: list[str]) -> None:
    """A doc whose parent section ``part`` holds a child ``detail`` linking ``foo``."""
    _write(
        root / "docs" / f"{name}.json",
        json.dumps(
            {
                "title": title,
                "tags": tags,
                "sections": [
                    {
                        "id": "part",
                        "heading": "Part",
                        "content": "Parent.",
                        "sections": [
                            {"id": "detail", "heading": "Detail", "content": "Foo.", "links": [{"node_id": CODE_ID}]}
                        ],
                    }
                ],
            }
        ),
    )


def _hand_edit(path: Path) -> None:
    time.sleep(0.02)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sections"][0]["sections"][0]["content"] = "Foo, edited by hand."
    path.write_text(json.dumps(data), encoding="utf-8")


@workflow(
    purpose="A frozen doc whose child section changes and is then rewritten with a doc tool contributes nothing to "
    "check, drift_query or the build counts: neither the parent section nor the doc's own node, and the parent is "
    "never stored LINKED_STALE; the same shape in a doc that is not frozen still counts, and include_frozen shows "
    "the frozen rows"
)
def test_frozen_doc_parent_and_envelope_stay_out_of_drift(tmp_path: Path) -> None:
    from axiom_graph.docjson.api import axiom_graph_update_section
    from axiom_graph.query.api import compute_drift_query
    from tests.fixtures.full_recompute import assert_matches_full_recompute

    dbp = _db_path(str(tmp_path))
    frozen_part, frozen_detail = f"{FROZEN_TWIN}::part", f"{FROZEN_TWIN}::part.detail"
    live_part, live_detail = f"{LIVE_TWIN}::part", f"{LIVE_TWIN}::part.detail"
    frozen_nodes = {FROZEN_TWIN, frozen_part, frozen_detail}

    口 = Step(
        step_num=1,
        name="Twin docs, one frozen",
        purpose="An adr-tagged doc and an untagged twin, each with a parent section whose child links foo",
    )
    _write(tmp_path / "axiom-graph.toml", _FROZEN_CONFIG)
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 0\n")
    _twin_doc(tmp_path, "adr-002", "ADR 002", ["adr"])
    _twin_doc(tmp_path, "guide", "Guide", [])
    build_index(dbp, tmp_path)

    口 = Step(
        step_num=2,
        name="Change foo and edit both children by hand",
        purpose="The live child goes LINKED_STALE through foo; both children are CONTENT_UPDATED; the frozen parent "
        "is not raised by its child's change",
    )
    time.sleep(0.02)
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 1\n")
    build_index(dbp, tmp_path)
    _hand_edit(tmp_path / "docs" / "adr-002.json")
    _hand_edit(tmp_path / "docs" / "guide.json")
    built = build_index(dbp, tmp_path)
    assert built.check is not None
    assert _link_status(tmp_path, frozen_part) == "VERIFIED"
    assert _link_status(tmp_path, live_part) == "LINKED_STALE"
    assert not frozen_nodes & set(built.check.statuses)
    summary = compute_check_summary(dbp, tmp_path)
    assert summary is not None and not frozen_nodes & set(summary.statuses)
    with_frozen = compute_check_summary(dbp, tmp_path, include_frozen=True)
    assert with_frozen is not None and FROZEN_TWIN in with_frozen.problem_statuses

    口 = Step(
        step_num=3,
        name="Rewrite both children with the doc tool",
        purpose="The tool write verifies each child's text; the frozen doc then holds no drift at all, the live twin "
        "keeps the LINKED_STALE foo caused on its child, parent and doc",
    )
    for detail in (frozen_detail, live_detail):
        out = axiom_graph_update_section(str(tmp_path), section_id=detail, content="Foo, rewritten.")
        assert not out.startswith("ERROR"), out
    summary = compute_check_summary(dbp, tmp_path)
    assert summary is not None
    assert summary.link_counts["LINKED_STALE"] == 3
    assert {live_detail, live_part, LIVE_TWIN} <= set(summary.problem_statuses)
    assert not frozen_nodes & set(summary.statuses)
    for node_id in frozen_nodes:
        assert _link_status(tmp_path, node_id) == "VERIFIED", node_id
    built = build_index(dbp, tmp_path)
    assert built.check is not None and built.check.link_counts["LINKED_STALE"] == 3

    flat = _listed_ids(compute_drift_query(dbp, tmp_path, filter="LINKED_STALE", format="ids"))
    grouped = _listed_ids(compute_drift_query(dbp, tmp_path, filter="LINKED_STALE", group_by="status", format="ids"))
    assert flat == grouped == {live_detail, live_part, LIVE_TWIN}
    counts = compute_drift_query(dbp, tmp_path, filter="LINKED_STALE", group_by="status", format="counts")
    assert sum(int(line.rsplit("  ", 1)[1]) for line in counts.splitlines()) == 3
    assert_matches_full_recompute(dbp, tmp_path)
    compute_check_summary(dbp, tmp_path, full=True)
    for node_id in frozen_nodes:
        assert _link_status(tmp_path, node_id) == "VERIFIED", node_id


NOTES_SECTION = "proj::docs/adr-001::notes"


def _seed_carried_linked_stale(root: Path, node_ids: list[str]) -> None:
    """Store LINKED_STALE with a BECAME_LINKED_STALE row on *node_ids*, as an index written by older rules holds it."""
    from axiom_graph import db

    with db._connect(_db_path(str(root))) as conn:
        for node_id in node_ids:
            conn.execute("UPDATE nodes SET link_status = 'LINKED_STALE' WHERE id = ?", (node_id,))
            conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, db._now_utc(), "BECAME_LINKED_STALE", "abc", '{"from_link": "VERIFIED"}', 0),
            )


@workflow(
    purpose="A frozen doc's section keeps a carried LINKED_STALE only while something still causes it: a section "
    "with no links drops its stored LINKED_STALE on the next check, while a sibling whose linked function changed "
    "after its verification keeps it and holds the doc envelope stale until mark_clean verifies it"
)
def test_frozen_section_carries_linked_stale_only_while_caused(tmp_path: Path) -> None:
    dbp = _db_path(str(tmp_path))

    口 = Step(
        step_num=1,
        name="Make the linked section LINKED_STALE, then freeze the doc",
        purpose="An adr-tagged doc holds a section linking foo and a section with no links; foo changes before the "
        "doc is frozen",
    )
    _write(tmp_path / "axiom-graph.toml", _CONFIG)
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 0\n")
    _write(
        tmp_path / "docs" / "adr-001.json",
        json.dumps(
            {
                "title": "ADR 001",
                "tags": ["adr"],
                "sections": [
                    {"id": "context", "heading": "Context", "content": "Foo.", "links": [{"node_id": CODE_ID}]},
                    {"id": "notes", "heading": "Notes", "content": "No links here."},
                ],
            }
        ),
    )
    build_index(dbp, tmp_path)
    time.sleep(0.02)
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 1\n")
    build_index(dbp, tmp_path)
    _write(tmp_path / "axiom-graph.toml", _FROZEN_CONFIG)
    _check(tmp_path)
    assert _link_status(tmp_path, VERIFIED_SECTION) == "LINKED_STALE"
    assert _link_status(tmp_path, NOTES_SECTION) == "VERIFIED"

    口 = Step(
        step_num=2,
        name="Store a cause-less LINKED_STALE on the unlinked section",
        purpose="The section with no links holds LINKED_STALE and a BECAME_LINKED_STALE row with no verification "
        "after it",
    )
    _seed_carried_linked_stale(tmp_path, [NOTES_SECTION])

    口 = Step(
        step_num=3,
        name="Check every node",
        purpose="The unlinked section drops to VERIFIED; the section foo still causes keeps LINKED_STALE and so "
        "does the doc envelope",
    )
    assert compute_check_summary(dbp, tmp_path, full=True) is not None
    assert _link_status(tmp_path, NOTES_SECTION) == "VERIFIED"
    assert _link_status(tmp_path, VERIFIED_SECTION) == "LINKED_STALE"
    assert _link_status(tmp_path, DOC_ID) == "LINKED_STALE"

    口 = Step(
        step_num=4,
        name="mark_clean the caused section",
        purpose="With no section left stale the envelope clears by inheritance",
    )
    mark_clean_nodes(dbp, tmp_path, [VERIFIED_SECTION], reason="re-read against foo", verified_by="human")
    _check(tmp_path)
    for node_id in (VERIFIED_SECTION, NOTES_SECTION, DOC_ID):
        assert _link_status(tmp_path, node_id) == "VERIFIED", node_id


@workflow(
    purpose="An index whose statuses were stored under earlier staleness rules re-checks every node on its first "
    "plain check, so a cause-less LINKED_STALE on a frozen doc's section and its envelope clears without a full "
    "check or any manual step"
)
def test_frozen_cause_less_linked_stale_heals_on_first_check_after_rules_change(tmp_path: Path) -> None:
    from axiom_graph import db
    from axiom_graph.index.staleness import STALENESS_RULES

    dbp = _db_path(str(tmp_path))

    口 = Step(
        step_num=1,
        name="Index a frozen doc",
        purpose="An adr-tagged doc with one unlinked section, built and checked under the frozen config",
    )
    _write(tmp_path / "axiom-graph.toml", _FROZEN_CONFIG)
    _write(
        tmp_path / "docs" / "adr-001.json",
        json.dumps(
            {
                "title": "ADR 001",
                "tags": ["adr"],
                "sections": [{"id": "notes", "heading": "Notes", "content": "No links here."}],
            }
        ),
    )
    build_index(dbp, tmp_path)
    _check(tmp_path)

    口 = Step(
        step_num=2,
        name="Store the index as earlier rules left it",
        purpose="The section and the envelope hold a cause-less LINKED_STALE that the journal no longer names, and "
        "the stored scheme stamp names earlier staleness rules",
    )
    _seed_carried_linked_stale(tmp_path, [NOTES_SECTION, DOC_ID])
    with db._connect(dbp) as conn:
        last_row = conn.execute("SELECT MAX(id) FROM node_history").fetchone()[0]
        db.set_index_meta_conn(conn, db.STALENESS_WATERMARK_META_KEY, str(last_row))
        stamp = db.get_index_meta_conn(conn, db.STALENESS_SCHEME_META_KEY)
        assert stamp is not None and f".r{STALENESS_RULES}." in stamp
        db.set_index_meta_conn(conn, db.STALENESS_SCHEME_META_KEY, stamp.replace(f".r{STALENESS_RULES}.", ".r0."))

    口 = Step(
        step_num=3,
        name="Run a plain check",
        purpose="The rules change makes the check re-check every node: the section and its envelope read VERIFIED",
    )
    _check(tmp_path)
    for node_id in (NOTES_SECTION, DOC_ID):
        assert _link_status(tmp_path, node_id) == "VERIFIED", node_id
