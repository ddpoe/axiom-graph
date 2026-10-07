"""Behavioural tests for how the doc write tools resolve a section id.

A section's id is the dot-path of its own id and its ancestors' ids.  An id
may itself contain a literal ``.`` (``category.adr``), so the write tools
resolve a dot-path against the real ids in the doc rather than splitting it
on every dot: a flat dotted id, a nested ``parent.child`` and a dotted id
under a parent (``schema.category.adr``) all resolve, and a dot-path that
names two different sections is refused.  Writes enter through
``axiom_graph.docjson.api``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

from axiom_annotations import workflow

from axiom_graph.docjson.api import (
    AmbiguousSectionIdError,
    _find_section_in_tree,
    axiom_graph_accept_doc_edits,
    axiom_graph_add_link,
    axiom_graph_add_section,
    axiom_graph_delete_link,
    axiom_graph_delete_section,
    axiom_graph_patch_section,
    axiom_graph_update_section,
    axiom_graph_write_doc,
)
from axiom_graph.index import db, doc_stamps
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, mark_clean_nodes

DOC_ID = "proj::docs/spec"
FOO = "proj::src.mod::foo"
BAR = "proj::src.mod::bar"


def _sec(sec_id: str, content: str, children: list[dict] | None = None) -> dict:
    sec: dict = {"id": sec_id, "heading": sec_id.title(), "content": content}
    if children is not None:
        sec["sections"] = children
    return sec


def _linked(sec_id: str, content: str) -> dict:
    return {**_sec(sec_id, content), "links": [{"node_id": FOO}]}


# ``category`` is a real section whose id is a prefix of the flat
# ``category.adr`` / ``category.prd`` ids, so resolving those must not
# mistake them for children of ``category``.
DOC = {
    "id": "spec",
    "title": "Spec",
    "sections": [
        _sec("intro", "Intro."),
        _sec("category", "Prefix sibling.", [_sec("design", "Category design.")]),
        _linked("category.adr", "Flat dotted."),
        _sec("category.prd", "Flat dotted sibling."),
        _sec("schema", "Schema.", [_linked("category.adr", "Dotted under parent."), _sec("category.prd", "Other.")]),
        _sec("parent", "Parent.", [_linked("child", "Nested child."), _sec("sibling", "Nested sibling.")]),
    ],
}


class Target(NamedTuple):
    """A section to address: its dot-path, its own id, its parent's dot-path ('' at top level), a sibling id."""

    dot: str
    own: str
    parent: str
    sibling: str


SHAPES = {
    "flat-dotted": Target("category.adr", "category.adr", "", "category.prd"),
    "nested": Target("parent.child", "child", "parent", "sibling"),
    "dotted-under-parent": Target("schema.category.adr", "category.adr", "schema", "category.prd"),
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with two code nodes and the ``DOC`` doc written by the doc tools."""
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(
        "def foo():\n    return 0\n\n\ndef bar():\n    return 1\n", encoding="utf-8"
    )
    build_index(_db_path(str(tmp_path)), tmp_path)
    out = axiom_graph_write_doc(str(tmp_path), json.loads(json.dumps(DOC)))
    assert "Wrote" in out, out
    return tmp_path


def _doc_file(project: Path) -> Path:
    return project / "docs" / "spec.docjson"


def _load(project: Path) -> dict:
    return json.loads(_doc_file(project).read_text(encoding="utf-8"))


def _flat(project: Path) -> dict[str, dict]:
    return doc_stamps.load_section_dicts(_doc_file(project))


def _view(sec: dict) -> tuple:
    return sec.get("heading"), sec.get("content"), [lk.get("node_id") for lk in sec.get("links") or []]


def _sibling_ids(project: Path, t: Target) -> list[str]:
    data = _load(project)
    siblings = (
        doc_stamps.flatten_section_dicts(data["sections"])[t.parent]["sections"] if t.parent else data["sections"]
    )
    return [s["id"] for s in siblings]


def _hand_edit(project: Path, dot: str, content: str) -> None:
    """Change one section's content directly in the file, as an editor would."""
    data = _load(project)
    doc_stamps.flatten_section_dicts(data["sections"])[dot]["content"] = content
    _doc_file(project).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _node(project: Path, dot: str):
    return db.get_node(_db_path(str(project)), f"{DOC_ID}::{dot}")


# ---------------------------------------------------------------------------
# Writers: each edits the target and returns (result, dot-paths it may change)
# ---------------------------------------------------------------------------


def _renamed(t: Target) -> str:
    return f"{t.parent}.renamed" if t.parent else "renamed"


def _update(project: Path, t: Target) -> tuple[str, set[str]]:
    return axiom_graph_update_section(str(project), f"{DOC_ID}::{t.dot}", content="Updated."), {t.dot}


def _patch(project: Path, t: Target) -> tuple[str, set[str]]:
    return axiom_graph_patch_section(str(project), f"{DOC_ID}::{t.dot}", new_string="Appended.", anchor="$"), {t.dot}


def _rename(project: Path, t: Target) -> tuple[str, set[str]]:
    return axiom_graph_update_section(str(project), f"{DOC_ID}::{t.dot}", new_id="renamed"), {t.dot, _renamed(t)}


def _reorder(project: Path, t: Target) -> tuple[str, set[str]]:
    return axiom_graph_update_section(str(project), f"{DOC_ID}::{t.dot}", after=t.sibling), set()


def _delete(project: Path, t: Target) -> tuple[str, set[str]]:
    return axiom_graph_delete_section(str(project), f"{DOC_ID}::{t.dot}"), {t.dot}


def _add_child(project: Path, t: Target) -> tuple[str, set[str]]:
    out = axiom_graph_add_section(str(project), DOC_ID, "added", "Added", content="Added.", parent_id=t.dot)
    return out, {t.dot, f"{t.dot}.added"}


def _add_link(project: Path, t: Target) -> tuple[str, set[str]]:
    return axiom_graph_add_link(str(project), f"{DOC_ID}::{t.dot}", node_id=BAR), {t.dot}


def _delete_link(project: Path, t: Target) -> tuple[str, set[str]]:
    return axiom_graph_delete_link(str(project), f"{DOC_ID}::{t.dot}", node_id=FOO), {t.dot}


def _accept(project: Path, t: Target) -> tuple[str, set[str]]:
    _hand_edit(project, t.dot, "Edited by hand.")
    return axiom_graph_accept_doc_edits(str(project), section_ids=[f"{DOC_ID}::{t.dot}"]), {t.dot}


def _check_update(project: Path, t: Target, out: str) -> None:
    assert _flat(project)[t.dot]["content"] == "Updated."
    assert _node(project, t.dot).level_2 == "Updated."


def _check_patch(project: Path, t: Target, out: str) -> None:
    assert _flat(project)[t.dot]["content"].endswith("\nAppended.")


def _check_rename(project: Path, t: Target, out: str) -> None:
    flat = _flat(project)
    assert t.dot not in flat
    assert flat[_renamed(t)]["heading"] == t.own.title()
    assert _node(project, _renamed(t)) is not None


def _check_reorder(project: Path, t: Target, out: str) -> None:
    ids = _sibling_ids(project, t)
    assert ids.index(t.own) == ids.index(t.sibling) + 1


def _check_delete(project: Path, t: Target, out: str) -> None:
    assert t.dot not in _flat(project)
    assert _node(project, t.dot) is None


def _check_add_child(project: Path, t: Target, out: str) -> None:
    assert _flat(project)[f"{t.dot}.added"]["content"] == "Added."


def _check_add_link(project: Path, t: Target, out: str) -> None:
    assert [lk["node_id"] for lk in _flat(project)[t.dot]["links"]] == [FOO, BAR]
    res = mark_clean_nodes(_db_path(str(project)), project, [f"{DOC_ID}::{t.dot}"], "checked", verified_by="agent")
    assert not res.not_found


def _check_delete_link(project: Path, t: Target, out: str) -> None:
    assert not _flat(project)[t.dot].get("links")


def _check_accept(project: Path, t: Target, out: str) -> None:
    assert "Accepted 1 section(s)" in out
    sec = _flat(project)[t.dot]
    assert sec["content"] == "Edited by hand."
    assert doc_stamps.stamp_state(sec) == doc_stamps.STAMP_VALID


WRITERS: dict[str, tuple[Callable, Callable]] = {
    "update_section": (_update, _check_update),
    "patch_section": (_patch, _check_patch),
    "update_section-rename": (_rename, _check_rename),
    "update_section-reorder": (_reorder, _check_reorder),
    "delete_section": (_delete, _check_delete),
    "add_section-parent": (_add_child, _check_add_child),
    "add_link": (_add_link, _check_add_link),
    "delete_link": (_delete_link, _check_delete_link),
    "accept_doc_edits": (_accept, _check_accept),
}

# add_section nests a child under the target; only a top-level target leaves
# room under the nesting-depth limit that tool enforces.
CASES = [
    (shape, writer)
    for shape, t in SHAPES.items()
    for writer in WRITERS
    if not (writer == "add_section-parent" and t.parent)
]


@workflow(
    purpose="Every doc write tool addresses a section by its dot-path whether the section's own id contains a dot, "
    "it is nested under a parent, or both, and changes only that section"
)
@pytest.mark.parametrize(("shape", "writer"), CASES)
def test_write_tools_resolve_every_section_id_shape(project: Path, shape: str, writer: str) -> None:
    t = SHAPES[shape]
    write, check = WRITERS[writer]
    before = _flat(project)

    out, touched = write(project, t)

    assert "ERROR" not in out, out
    check(project, t, out)
    after = _flat(project)
    untouched = {d for d in before if d not in touched and not d.startswith(f"{t.dot}.")}
    assert {d: _view(after[d]) for d in untouched} == {d: _view(before[d]) for d in untouched}


def _make_ambiguous(project: Path) -> bytes:
    """Give the doc both a flat ``a.b`` and a nested ``a`` > ``b``; return the file bytes."""
    out = axiom_graph_add_section(
        str(project),
        DOC_ID,
        sections=[
            {"section_id": "a", "heading": "A", "content": "Parent."},
            {"section_id": "b", "heading": "B", "content": "Nested.", "parent_id": "a"},
        ],
    )
    assert "ERROR" not in out, out
    data = _load(project)
    data["sections"].append(_linked("a.b", "Flat."))
    _doc_file(project).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return _doc_file(project).read_bytes()


@workflow(
    purpose="A dot-path that names both a flat dotted section and a nested one is refused with an error naming "
    "both, and the write tools change nothing"
)
@pytest.mark.parametrize("writer", list(WRITERS))
def test_ambiguous_section_id_is_refused_by_every_write_tool(project: Path, writer: str) -> None:
    raw = _make_ambiguous(project)
    write, _check = WRITERS[writer]
    if writer == "accept_doc_edits":
        out = axiom_graph_accept_doc_edits(str(project), section_ids=[f"{DOC_ID}::a.b"])
    else:
        out, _touched = write(project, Target("a.b", "b", "a", "intro"))

    assert "ERROR" in out and "ambiguous" in out, out
    assert "'a.b'" in out and "'a' > 'b'" in out, out
    assert _doc_file(project).read_bytes() == raw


@pytest.mark.parametrize(
    ("sections", "dot", "expected"),
    [
        ([_sec("category.adr", "flat")], "category.adr", "flat"),
        ([_sec("category", "p", [_sec("adr", "nested")])], "category.adr", "nested"),
        ([_sec("schema", "p", [_sec("category.adr", "under")])], "schema.category.adr", "under"),
        ([_sec("a", "outer", [_sec("a", "inner")])], "a.a", "inner"),
        ([_sec("category", "p", [_sec("x", "x")]), _sec("category.adr", "flat")], "category.adr", "flat"),
        ([_sec("category", "p")], "category.adr", None),
        ([_sec("a.b", "flat")], "a", None),
    ],
)
def test_find_section_in_tree_matches_real_ids(sections: list[dict], dot: str, expected: str | None) -> None:
    """A dot-path resolves against the ids actually present at each level."""
    found = _find_section_in_tree(sections, dot)
    assert (found or {}).get("content") == expected


def test_find_section_in_tree_raises_on_two_matches() -> None:
    """A dot-path naming a flat dotted section and a nested one raises, naming both."""
    sections = [_sec("a", "outer", [_sec("b", "nested")]), _sec("a.b", "flat")]
    with pytest.raises(AmbiguousSectionIdError) as err:
        _find_section_in_tree(sections, "a.b")
    assert "'a.b'" in str(err.value) and "'a' > 'b'" in str(err.value)


# ---------------------------------------------------------------------------
# Writes that would give two sections one dot-path
# ---------------------------------------------------------------------------

OTHER_ID = "proj::docs/other"


def _other_file(project: Path) -> Path:
    return project / "docs" / "other.docjson"


def _write_other(project: Path, sections: list[dict]) -> str:
    return axiom_graph_write_doc(str(project), {"id": "other", "title": "Other", "sections": sections})


def _no_setup(project: Path) -> None:
    return None


def _setup_flat_beside_parent(project: Path) -> None:
    out = _write_other(project, [_sec("a.b", "Flat."), _sec("z", "Z.", [_sec("b", "Nested.")])])
    assert "ERROR" not in out, out


class Collision(NamedTuple):
    """A write that would give two sections one dot-path, the doc it targets, and what its error must name."""

    setup: Callable[[Path], None]
    write: Callable[[Path], str]
    doc_id: str
    dot: str
    shapes: tuple[str, ...]


COLLISIONS = {
    "write_doc-flat-beside-nested": Collision(
        _no_setup,
        lambda p: _write_other(p, [_sec("a", "Parent.", [_sec("b", "Nested.")]), _sec("a.b", "Flat.")]),
        OTHER_ID,
        "a.b",
        ("'a' > 'b'", "'a.b'"),
    ),
    "write_doc-duplicate-siblings": Collision(
        _no_setup,
        lambda p: _write_other(p, [_sec("a", "First."), _sec("x", "X."), _sec("a", "Second.")]),
        OTHER_ID,
        "a",
        ("'a' (position 1: \"A\") and 'a' (position 3: \"A\")",),
    ),
    "add_section": Collision(
        _no_setup,
        lambda p: axiom_graph_add_section(str(p), DOC_ID, "adr", "Adr", content="Nested.", parent_id="category"),
        DOC_ID,
        "category.adr",
        ("'category.adr'", "'category' > 'adr'"),
    ),
    "add_section-batch": Collision(
        _no_setup,
        lambda p: axiom_graph_add_section(
            str(p),
            DOC_ID,
            sections=[
                {"section_id": "fine", "heading": "Fine", "content": "No clash."},
                {"section_id": "adr", "heading": "Adr", "content": "Nested.", "parent_id": "category"},
            ],
        ),
        DOC_ID,
        "category.adr",
        ("'category.adr'", "'category' > 'adr'"),
    ),
    "update_section-rename": Collision(
        _no_setup,
        lambda p: axiom_graph_update_section(str(p), f"{DOC_ID}::category.design", new_id="adr"),
        DOC_ID,
        "category.adr",
        ("'category.adr'", "'category' > 'adr'"),
    ),
    "update_section-rename-parent": Collision(
        _setup_flat_beside_parent,
        lambda p: axiom_graph_update_section(str(p), f"{OTHER_ID}::z", new_id="a"),
        OTHER_ID,
        "a.b",
        ("'a.b'", "'a' > 'b'"),
    ),
}


def _doc_file_for(project: Path, doc_id: str) -> Path:
    return project / "docs" / f"{doc_id.rsplit('/', 1)[-1]}.docjson"


def _section_nodes(project: Path, doc_id: str) -> dict[str, str | None]:
    return {n.id: n.level_2 for n in db.all_nodes(_db_path(str(project))) if n.id.startswith(f"{doc_id}::")}


@workflow(
    purpose="A doc write that would give two sections the same dot-path is refused with an error naming the "
    "dot-path and both sections, and nothing is written to the file or the index"
)
@pytest.mark.parametrize("case", list(COLLISIONS))
def test_write_that_would_collide_dot_paths_is_refused(project: Path, case: str) -> None:
    c = COLLISIONS[case]
    c.setup(project)
    file = _doc_file_for(project, c.doc_id)
    raw = file.read_bytes() if file.exists() else None
    nodes = _section_nodes(project, c.doc_id)

    out = c.write(project)

    assert out.startswith("ERROR"), out
    assert f"'{c.dot}'" in out and "nothing was written" in out, out
    for shape in c.shapes:
        assert shape in out, out
    assert (file.read_bytes() if file.exists() else None) == raw
    assert _section_nodes(project, c.doc_id) == nodes


def test_write_to_a_doc_with_an_existing_collision_is_not_blocked(project: Path) -> None:
    """A write that adds no new collision lands in a doc that already has one; the first section keeps the id."""
    _make_ambiguous(project)

    out = axiom_graph_add_section(str(project), DOC_ID, "later", "Later", content="Unrelated.")

    assert "ERROR" not in out, out
    assert _flat(project)["later"]["content"] == "Unrelated."
    assert _node(project, "a.b").level_2 == "Nested."


def test_rename_that_removes_a_collision_is_allowed(project: Path) -> None:
    """Renaming the parent of the nested spelling gives each section its own dot-path again."""
    _make_ambiguous(project)

    out = axiom_graph_update_section(str(project), f"{DOC_ID}::a", new_id="c")

    assert "ERROR" not in out, out
    flat = _flat(project)
    assert (flat["c.b"]["content"], flat["a.b"]["content"]) == ("Nested.", "Flat.")
    assert (_node(project, "c.b").level_2, _node(project, "a.b").level_2) == ("Nested.", "Flat.")
