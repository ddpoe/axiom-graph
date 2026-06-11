"""Behavioural tests for ``axiom_graph_patch_section``.

Partial-edit companion to ``update_section``: append (``anchor="$"``),
prepend (``anchor="^"``), and ``Edit``-style unique-match replace
(``old_string``).  These drive ``axiom_graph.docjson.api`` directly (with one
test crossing the ``mcp_tools`` wrapper boundary), mirroring the Tier-1
plain-pytest style of ``test_docjson_api_behavioural.py``.

Tied to the patch-section-tool pev-request: purely additive input ergonomics,
no schema or graph-semantics change — final content and indexing are identical
to the equivalent whole-replace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_graph.docjson.api import (
    axiom_graph_patch_section,
    axiom_graph_update_section,
    axiom_graph_write_doc,
)
from axiom_graph.docjson.mcp_tools import axiom_graph_patch_section as mcp_patch_section
from axiom_graph.index import builder, db
from axiom_graph.index.paths import db_path as _db_path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal project root with axiom-graph.toml and an initialised index."""
    (tmp_path / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n',
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    builder.build(tmp_path)
    return tmp_path


def _write_doc(project: Path, doc_id: str, sections: list[dict]) -> str:
    """Write a DocJSON doc with the given sections; return its node id."""
    doc = {"id": doc_id, "title": doc_id.title(), "sections": sections}
    res = axiom_graph_write_doc(str(project), doc)
    assert "Wrote" in res, res
    return f"proj::docs.{doc_id}"


def _raw_content(project: Path, doc_node_id: str, short_id: str) -> str:
    """Read a top-level section's raw ``content`` field straight from the JSON file."""
    db_p = _db_path(str(project))
    node = db.get_node(db_p, doc_node_id)
    data = json.loads((project / node.location).read_text(encoding="utf-8"))
    sec = next(s for s in data["sections"] if s["id"] == short_id)
    return sec.get("content", "")


def test_patch_section_append(project: Path) -> None:
    """anchor='$' concatenates at the end; the section re-indexes to the new content."""
    doc_id = _write_doc(project, "led", [{"id": "log", "heading": "Log", "content": "- first"}])
    section_id = f"{doc_id}::log"
    db_p = _db_path(str(project))
    pre_hash = db.get_node(db_p, section_id).desc_hash

    res = axiom_graph_patch_section(str(project), section_id, "- second", anchor="$")

    assert "appended to" in res, res
    assert _raw_content(project, doc_id, "log") == "- first\n- second"
    # desc_hash moved → the new content was re-indexed.
    assert db.get_node(db_p, section_id).desc_hash != pre_hash


def test_patch_section_prepend(project: Path) -> None:
    """anchor='^' concatenates at the start."""
    doc_id = _write_doc(project, "led", [{"id": "log", "heading": "Log", "content": "- second"}])
    res = axiom_graph_patch_section(str(project), f"{doc_id}::log", "- first", anchor="^")
    assert "prepended to" in res, res
    assert _raw_content(project, doc_id, "log") == "- first\n- second"


def test_patch_section_replace_unique(project: Path) -> None:
    """old_string matched exactly once is replaced; the rest of the section is untouched."""
    doc_id = _write_doc(project, "doc", [{"id": "s", "heading": "S", "content": "alpha BETA gamma"}])
    res = axiom_graph_patch_section(str(project), f"{doc_id}::s", "delta", old_string="BETA")
    assert "replaced in" in res, res
    assert _raw_content(project, doc_id, "s") == "alpha delta gamma"


def test_patch_section_replace_not_found_leaves_unchanged(project: Path) -> None:
    """A missing old_string is a hard error and the section is left unchanged."""
    doc_id = _write_doc(project, "doc", [{"id": "s", "heading": "S", "content": "hello world"}])
    res = axiom_graph_patch_section(str(project), f"{doc_id}::s", "X", old_string="absent")
    assert res.startswith("ERROR") and "not found" in res, res
    assert _raw_content(project, doc_id, "s") == "hello world"


def test_patch_section_replace_non_unique_leaves_unchanged(project: Path) -> None:
    """A non-unique old_string is a hard error and the section is left unchanged."""
    doc_id = _write_doc(project, "doc", [{"id": "s", "heading": "S", "content": "x and x"}])
    res = axiom_graph_patch_section(str(project), f"{doc_id}::s", "Y", old_string="x")
    assert res.startswith("ERROR") and "not unique" in res, res
    assert _raw_content(project, doc_id, "s") == "x and x"


@pytest.mark.parametrize(
    "kwargs, expect",
    [
        (dict(anchor="$", old_string="x"), "not both"),  # both supplied
        (dict(), "exactly one"),  # neither supplied
        (dict(anchor="@"), "anchor must be"),  # bad anchor value
        (dict(old_string=""), "must not be empty"),  # empty old_string
    ],
)
def test_patch_section_validation_errors(project: Path, kwargs: dict, expect: str) -> None:
    """Mode validation: exactly one of {anchor, old_string}; anchor in {$,^}; old_string non-empty."""
    doc_id = _write_doc(project, "doc", [{"id": "s", "heading": "S", "content": "body"}])
    res = axiom_graph_patch_section(str(project), f"{doc_id}::s", "ignored", **kwargs)
    assert res.startswith("ERROR") and expect in res, res
    assert _raw_content(project, doc_id, "s") == "body"


def test_patch_section_anchors_are_out_of_band(project: Path) -> None:
    """A body full of $ and ^ round-trips through every mode (anchors live out-of-band)."""
    body = "shell $VAR, math $x^2$, key Ctrl-^"
    doc_id = _write_doc(
        project,
        "doc",
        [
            {"id": "a", "heading": "A", "content": body},
            {"id": "p", "heading": "P", "content": body},
            {"id": "r", "heading": "R", "content": body},
        ],
    )
    # append text that itself contains anchor characters
    axiom_graph_patch_section(str(project), f"{doc_id}::a", "tail $y^3$", anchor="$")
    assert _raw_content(project, doc_id, "a") == body + "\ntail $y^3$"
    # prepend
    axiom_graph_patch_section(str(project), f"{doc_id}::p", "head $z$", anchor="^")
    assert _raw_content(project, doc_id, "p") == "head $z$\n" + body
    # replace a chunk that contains both $ and ^
    axiom_graph_patch_section(str(project), f"{doc_id}::r", "REPLACED", old_string="$x^2$")
    assert _raw_content(project, doc_id, "r") == "shell $VAR, math REPLACED, key Ctrl-^"


def test_patch_section_newline_policy_and_empty_section(project: Path) -> None:
    """One \\n separator on append; no double newline; empty section just sets content."""
    doc_id = _write_doc(
        project,
        "doc",
        [
            {"id": "empty", "heading": "Empty"},  # no content field
            {"id": "nl", "heading": "NL", "content": "line1\n"},  # already ends with \n
        ],
    )
    # empty section → append just sets content (no leading separator)
    axiom_graph_patch_section(str(project), f"{doc_id}::empty", "first", anchor="$")
    assert _raw_content(project, doc_id, "empty") == "first"
    # existing already ends with \n → no double newline inserted
    axiom_graph_patch_section(str(project), f"{doc_id}::nl", "line2", anchor="$")
    assert _raw_content(project, doc_id, "nl") == "line1\nline2"


def test_patch_section_append_idempotent_with_update_section(project: Path) -> None:
    """An append yields byte-identical content + identical indexing to a whole-replace."""
    doc_id = _write_doc(
        project,
        "doc",
        [
            {"id": "a", "heading": "A", "content": "Start."},
            {"id": "b", "heading": "B", "content": "Start."},
        ],
    )
    db_p = _db_path(str(project))
    # patch-append on 'a' vs equivalent whole-replace on 'b'
    axiom_graph_patch_section(str(project), f"{doc_id}::a", "More.", anchor="$")
    axiom_graph_update_section(str(project), f"{doc_id}::b", content="Start.\nMore.")

    assert _raw_content(project, doc_id, "a") == _raw_content(project, doc_id, "b") == "Start.\nMore."
    # content-addressed hashes match → indexing is identical to whole-replace
    na = db.get_node(db_p, f"{doc_id}::a")
    nb = db.get_node(db_p, f"{doc_id}::b")
    assert na.desc_hash == nb.desc_hash
    assert na.code_hash == nb.code_hash


def test_patch_section_via_mcp_wrapper(project: Path) -> None:
    """The MCP wrapper drives a mode and surfaces errors as ERROR strings (not exceptions)."""
    doc_id = _write_doc(project, "doc", [{"id": "s", "heading": "S", "content": "- a"}])
    section_id = f"{doc_id}::s"
    res = mcp_patch_section(str(project), section_id, "- b", anchor="$")
    assert "appended to" in res, res
    assert _raw_content(project, doc_id, "s") == "- a\n- b"
    err = mcp_patch_section(str(project), section_id, "x", old_string="absent")
    assert err.startswith("ERROR"), err
