"""A DocJSON document's tags must reach the index whatever the file's shape.

A document's tags live in two places: the ``docs.tags`` JSON column and
``tags`` rows on the document's envelope node.  Everything that reads tags
back -- ``list_tags``, the ``tag`` filter on search, ``query_nodes(tag=...)``,
``get_node().tags``, the visualiser filters -- reads the rows, so the rows are
what these tests assert.

Every fixture here is seeded through ``build`` or a docjson api function so the
write path under test is the one production uses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.docjson.api import axiom_graph_update_doc_meta
from axiom_graph.index import builder, db
from axiom_graph.index.paths import db_path as _db_path

#: Long enough that a trailing ``tags`` key lands well past the envelope's
#: stored text prefix.
_PREFIX_CHARS = 4000


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal project root with an initialised index and a docs/ dir."""
    (tmp_path / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n',
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    builder.build(tmp_path)
    return tmp_path


def _long_doc(tags: list[str] | None = None) -> dict:
    """A DocJSON dict whose ``tags`` key follows a body longer than the prefix.

    Args:
        tags: Doc-level tags.  Omitted entirely when ``None``, which is the
            shape of a document that has never been tagged.

    Returns:
        The DocJSON dict, with ``tags`` inserted after ``sections`` so the
        serialised key order matches what a later doc-metadata edit produces.
    """
    body = "The indexer walks every section of this document in order. " * 120
    data: dict = {
        "title": "Long Guide",
        "sections": [
            {"id": "overview", "heading": "Overview", "content": body},
            {"id": "details", "heading": "Details", "content": "Closing note."},
        ],
    }
    if tags is not None:
        data["tags"] = tags
    return data


def _seed(project: Path, slug: str, data: dict) -> str:
    """Write a DocJSON file and index it through a normal build.

    Args:
        project: Project root.
        slug: Document slug under ``docs/``.
        data: DocJSON dict to serialise.

    Returns:
        The document's envelope node id.
    """
    path = project / "docs" / f"{slug}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    builder.build(project)
    return f"proj::docs.{slug}"


def _index_tags(project: Path, doc_id: str) -> set[str]:
    """Return the tag rows the index holds for *doc_id*."""
    node = db.get_node(_db_path(project), doc_id)
    assert node is not None, f"{doc_id} must be indexed"
    return set(node.tags or [])


def _tags_key_offset(project: Path, slug: str) -> int:
    """Return the character offset of the ``tags`` key in the file on disk."""
    text = (project / "docs" / f"{slug}.json").read_text(encoding="utf-8")
    offset = text.find('"tags"')
    assert offset >= 0, "the document on disk must carry a tags key"
    return offset


@workflow(
    purpose="Retagging a long document whose tags key sits past the envelope's stored text prefix updates the index, not just the file",
)
def test_retagging_a_long_document_reaches_the_index(project: Path) -> None:
    """A tag-only edit to a long document is visible to tag queries."""
    口 = Step(
        step_num=1,
        name="Index a long tagged document",
        purpose="Seed through a normal build so the envelope carries its initial tag rows",
    )
    doc_id = _seed(project, "long-tagged", _long_doc(tags=["draft"]))
    assert _index_tags(project, doc_id) == {"draft"}

    口 = Step(
        step_num=2,
        name="Confirm the tags key is past the stored prefix",
        purpose="Without this the document is short enough that a tag edit changes the stored text and the gate is never exercised",
    )
    assert _tags_key_offset(project, "long-tagged") > _PREFIX_CHARS

    口 = Step(
        step_num=3,
        name="Set new tags through the doc-metadata path",
        purpose="The ordinary in-place write every tagging tool uses",
    )
    result = axiom_graph_update_doc_meta(str(project), doc_id, tags=["reviewed", "architecture"])
    assert "Updated" in result, result

    口 = Step(
        step_num=4,
        name="Query the index by tag",
        purpose="Tag rows are what search, filters and the visualiser read",
    )
    assert _index_tags(project, doc_id) == {"reviewed", "architecture"}
    matched = {n.id for n in db.query_nodes(_db_path(project), tag="reviewed")}
    assert doc_id in matched, "a tag query must find the document that was just tagged"


@workflow(
    purpose="Tagging a long document for the first time creates tag rows rather than leaving the document with none",
)
def test_first_tags_on_a_long_document_reach_the_index(project: Path) -> None:
    """A never-tagged long document gains tag rows when it is first tagged."""
    口 = Step(
        step_num=1,
        name="Index a long untagged document",
        purpose="Seed through a normal build; the document carries no tags key at all",
    )
    doc_id = _seed(project, "long-untagged", _long_doc())
    assert _index_tags(project, doc_id) == set()

    口 = Step(
        step_num=2,
        name="Tag it for the first time",
        purpose="The doc-metadata path appends the tags key after sections, which on a long document puts it past the stored prefix",
    )
    result = axiom_graph_update_doc_meta(str(project), doc_id, tags=["architecture"])
    assert "Updated" in result, result
    assert _tags_key_offset(project, "long-untagged") > _PREFIX_CHARS

    口 = Step(
        step_num=3,
        name="Query the index by tag",
        purpose="The document must be reachable by the tag it was just given",
    )
    assert _index_tags(project, doc_id) == {"architecture"}
    matched = {n.id for n in db.query_nodes(_db_path(project), tag="architecture")}
    assert doc_id in matched


@workflow(
    purpose="Tags edited directly on disk reach the index on the next build, not only through the write tools",
)
def test_tags_edited_on_disk_reach_the_index_on_build(project: Path) -> None:
    """The build path syncs tags for a document edited outside the tools."""
    口 = Step(
        step_num=1,
        name="Index a long tagged document",
        purpose="Establish the starting tag rows through a normal build",
    )
    doc_id = _seed(project, "disk-edited", _long_doc(tags=["draft"]))
    assert _index_tags(project, doc_id) == {"draft"}

    口 = Step(
        step_num=2,
        name="Edit the tags on disk and bump the mtime",
        purpose="An editor, a script or a merge changes the file without going through a write tool",
    )
    path = project / "docs" / "disk-edited.json"
    path.write_text(
        json.dumps(_long_doc(tags=["shipped"]), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
    assert _tags_key_offset(project, "disk-edited") > _PREFIX_CHARS

    口 = Step(
        step_num=3,
        name="Run a normal build",
        purpose="The file is rescanned because its mtime moved; its tag rows must follow",
    )
    builder.build(project)

    口 = Step(
        step_num=4,
        name="Index agrees with the file",
        purpose="The build path must leave the index matching what is on disk",
    )
    assert _index_tags(project, doc_id) == {"shipped"}


@workflow(
    purpose="Removing a tag from a long document removes its row from the index — tags are compared as a set, not merged"
)
def test_removing_a_tag_removes_it_from_the_index(project: Path) -> None:
    """Dropping one of several tags drops its row rather than leaving it behind."""
    doc_id = _seed(project, "long-multi", _long_doc(tags=["draft", "architecture"]))
    assert _index_tags(project, doc_id) == {"draft", "architecture"}
    assert _tags_key_offset(project, "long-multi") > _PREFIX_CHARS

    result = axiom_graph_update_doc_meta(str(project), doc_id, tags=["architecture"])
    assert "Updated" in result, result

    assert _index_tags(project, doc_id) == {"architecture"}
    matched = {n.id for n in db.query_nodes(_db_path(project), tag="draft")}
    assert doc_id not in matched, "a removed tag must stop matching the document"
