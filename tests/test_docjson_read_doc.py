"""Behavioural tests for ``axiom_graph_read_doc``: budget, subtrees, multi-section reads, footers, list paging."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from axiom_annotations import Step, workflow

from axiom_graph.docjson.api import axiom_graph_read_doc, axiom_graph_update_doc_meta, strip_linked_nodes_footer
from axiom_graph.docjson.render_agent import _render_doc_markdown
from axiom_graph.index import builder, db
from axiom_graph.index.paths import db_path
from axiom_graph.lifecycle.api import build_index

from tests.fixtures import doc_trees


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with one code module, ready for docs to be written and built."""
    doc_trees.write_toml(tmp_path, ["docs"], project_id="proj")
    (tmp_path / "docs").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text('def func():\n    """Do the thing."""\n    return 1\n', encoding="utf-8")
    return tmp_path


def _flat_doc(project: Path, name: str, count: int, size: int) -> str:
    """Write a doc of *count* sections of *size* chars each; return its doc id."""
    doc_trees.write_doc(
        project,
        f"docs/{name}.json",
        title=name.title(),
        sections=[{"id": f"s{i}", "heading": f"S{i}", "content": f"[body-{i}]" + "x" * size} for i in range(count)],
    )
    return f"proj::docs/{name}"


def _omitted_ids(result: str) -> list[str]:
    """Pull the ``section_ids=[...]`` list out of an omitted-sections hint."""
    m = re.search(r"Read the rest with section_ids=(\[.*\])\]", result)
    assert m, result
    return json.loads(m.group(1))


def _strip_doc_head(result: str) -> str:
    """Drop the ``# title`` line and the generated doc meta line from a single-doc read."""
    head = re.match(r"# [^\n]*\n\n<!-- doc: [^\n]* -->\n\n", result)
    assert head, result[:200]
    return result[head.end() :]


@workflow(purpose="section_ids reads sections from several docs in one call, in the order given")
def test_section_ids_span_docs_in_order(project: Path) -> None:
    """Each section comes under its own doc's title, in request order."""
    a = _flat_doc(project, "alpha", 2, 10)
    b = _flat_doc(project, "beta", 2, 10)
    builder.build(project)

    result = axiom_graph_read_doc(str(project), section_ids=[f"{b}::s1", f"{a}::s0"])

    assert result.index("# Beta") < result.index("[body-1]") < result.index("# Alpha") < result.index("[body-0]")
    assert "proj::docs/beta::s0 -->" not in result
    assert "[read_doc:" not in result


@workflow(purpose="Reading a parent section returns the section and every section nested under it")
def test_parent_section_read_returns_its_subtree(project: Path) -> None:
    """A slug read and a section_ids read both include all descendants, and nothing else."""
    doc_trees.write_doc(
        project,
        "docs/tree.json",
        title="Tree",
        sections=[
            {
                "id": "parent",
                "heading": "Parent",
                "content": "Intro.",
                "sections": [
                    {"id": "a", "heading": "A", "content": "Child A."},
                    {
                        "id": "b",
                        "heading": "B",
                        "content": "Child B.",
                        "sections": [{"id": "deep", "heading": "Deep", "content": "Grandchild."}],
                    },
                    {"id": "c", "heading": "C", "content": "Child C."},
                ],
            },
            {"id": "other", "heading": "Other", "content": "Sibling."},
        ],
    )
    builder.build(project)

    by_slug = axiom_graph_read_doc(str(project), "proj::docs/tree", section="parent")
    by_id = axiom_graph_read_doc(str(project), section_ids=["proj::docs/tree::parent"])

    for result in (by_slug, by_id):
        for text in ("Intro.", "Child A.", "Child B.", "Grandchild.", "Child C."):
            assert text in result
        assert "Sibling." not in result
    assert by_slug == by_id


@workflow(purpose="A doc over the budget stops at a section boundary and names the omitted sections to read next")
def test_over_budget_doc_lists_omitted_ids_that_read_the_rest(project: Path) -> None:
    """Following the omitted-ids hint returns exactly the sections the first read left out."""
    口 = Step(step_num=1, name="Write a doc larger than the budget", purpose="Ten 500-char sections")
    doc_id = _flat_doc(project, "big", 10, 500)
    builder.build(project)

    口 = Step(step_num=2, name="Read it with a small budget", purpose="Output must stop at a section boundary")
    first = axiom_graph_read_doc(str(project), doc_id, max_chars=2000)
    body = first.split("\n\n[read_doc:")[0]
    assert len(body) <= 2000
    shown = [i for i in range(10) if f"[body-{i}]" in first]
    assert shown == list(range(len(shown))) and 0 < len(shown) < 10
    assert all(f"[body-{i}]" + "x" * 500 in first for i in shown)

    口 = Step(step_num=3, name="Read the omitted ids", purpose="The hint's section_ids return the rest")
    omitted = _omitted_ids(first)
    assert omitted == [f"{doc_id}::s{i}" for i in range(len(shown), 10)]
    rest = axiom_graph_read_doc(str(project), section_ids=omitted, max_chars=None)
    assert [i for i in range(10) if f"[body-{i}]" in rest] == list(range(len(shown), 10))


@workflow(purpose="A single section larger than the budget is cut and resumes from the offset its hint gives")
def test_oversized_section_truncates_and_resumes_at_offset(project: Path) -> None:
    """Concatenating the pages reproduces the unbudgeted section exactly."""
    doc_trees.write_doc(
        project,
        "docs/huge.json",
        title="Huge",
        sections=[{"id": "only", "heading": "Only", "content": "".join(f"{i:05d}\n" for i in range(1000))}],
    )
    builder.build(project)
    sid = "proj::docs/huge::only"
    full = axiom_graph_read_doc(str(project), section_ids=[sid], max_chars=None)

    pages: list[str] = []
    offset = 0
    for _ in range(10):
        result = axiom_graph_read_doc(str(project), section_ids=[sid], max_chars=2500, offset=offset)
        m = re.search(r'Continue with section_ids=\["([^"]+)"\], offset=(\d+)', result)
        if m is None:
            pages.append(_strip_doc_head(result))
            break
        assert m.group(1) == sid
        pages.append(_strip_doc_head(result.split("\n\n[read_doc:")[0]))
        offset = int(m.group(2))
    else:
        pytest.fail("section never finished paging")

    assert len(pages) > 1
    assert "".join(pages) == _strip_doc_head(full)


@workflow(purpose="A whole-doc read under the default budget renders the full doc rather than a section table")
def test_whole_doc_under_default_budget_renders_in_full(project: Path) -> None:
    """A 10k-character doc comes back whole, byte-equal to the unbudgeted render."""
    doc_id = _flat_doc(project, "medium", 10, 1000)
    builder.build(project)

    result = axiom_graph_read_doc(str(project), doc_id)

    assert all(f"[body-{i}]" + "x" * 1000 in result for i in range(10))
    assert "[read_doc:" not in result and "| Section |" not in result
    path = db_path(project)
    rendered = _render_doc_markdown(path, doc_id, "Medium", db.get_doc_sections(path, doc_id))
    assert _strip_doc_head(result) == rendered.removeprefix("# Medium\n\n")


@workflow(purpose="The generated linked-nodes footer is wrapped in the marker pair the write path strips")
def test_linked_nodes_footer_is_wrapped_in_markers(project: Path) -> None:
    """The footer sits between the exact markers, and stripping it leaves the section content."""
    doc_trees.write_doc(
        project,
        "docs/linked.json",
        title="Linked",
        sections=[
            {
                "id": "refs",
                "heading": "Refs",
                "content": "Describes func.",
                "links": [{"node_id": "proj::pkg.mod::func"}],
            }
        ],
    )
    builder.build(project)

    result = axiom_graph_read_doc(str(project), "proj::docs/linked")

    block = re.search(
        r"^<!-- axiom:linked-nodes -->\n\*\*Linked nodes:\*\*\n- `proj::pkg\.mod::func`.*\n<!-- /axiom:linked-nodes -->$",
        result,
        re.MULTILINE,
    )
    assert block, result
    pasted = result.split("<!-- id: proj::docs/linked::refs -->\n\n", 1)[1]
    assert strip_linked_nodes_footer(pasted) == ("Describes func.", True)


@workflow(purpose="doc_id='list' pages with max_results / offset and filters by doc-id prefix")
def test_list_pages_and_filters_by_prefix(project: Path) -> None:
    """A page reports the remaining count and the next offset; prefix narrows the list."""
    doc_trees.many_docs(project, 12, project_id="proj")
    doc_trees.write_doc(project, "docs/other/extra.json", title="Extra")
    builder.build(project)

    page = axiom_graph_read_doc(str(project), "list", max_results=5)
    assert len([ln for ln in page.splitlines() if ln.startswith("proj::")]) == 5
    assert "5 of 13 docs shown; 8 more -- next: offset=5" in page

    last = axiom_graph_read_doc(str(project), "list", max_results=5, offset=10)
    assert len([ln for ln in last.splitlines() if ln.startswith("proj::")]) == 3
    assert "next:" not in last

    only = axiom_graph_read_doc(str(project), "list", prefix="docs/other/")
    assert only.splitlines() == [ln for ln in only.splitlines() if ln.startswith("proj::docs/other/extra")]


def _tree_doc(project: Path) -> str:
    """Write a doc with a three-level section tree and a sibling; return its doc id."""
    doc_trees.write_doc(
        project,
        "docs/outline.json",
        title="Outline",
        sections=[
            {
                "id": "parent",
                "heading": "Parent",
                "content": "[parent-body]",
                "sections": [
                    {"id": "a", "heading": "A", "content": "[a-body]"},
                    {
                        "id": "b",
                        "heading": "B",
                        "content": "[b-body]",
                        "sections": [{"id": "deep", "heading": "Deep", "content": "[deep-body]"}],
                    },
                ],
            },
            {
                "id": "refs",
                "heading": "Refs",
                "content": "[refs-body] Describes func.",
                "links": [{"node_id": "proj::pkg.mod::func"}],
            },
        ],
    )
    return "proj::docs/outline"


def _outline_lines(result: str) -> dict[str, str]:
    """Map each outlined section id to its outline line."""
    return {m.group(1): ln for ln in result.splitlines() if (m := re.match(r"\s*- (\S+)  ", ln))}


@workflow(purpose="outline=True lists a doc's whole section tree, indented by depth, without any section bodies")
def test_outline_lists_nested_tree_without_bodies(project: Path) -> None:
    """Every section appears once with its full id and heading, children indented under parents."""
    doc_id = _tree_doc(project)
    builder.build(project)

    result = axiom_graph_read_doc(str(project), doc_id, outline=True)

    assert result.splitlines()[0].startswith(f"{doc_id}  Outline  (5 sections, ")
    assert "-body]" not in result
    lines = _outline_lines(result)
    assert list(lines) == [f"{doc_id}::{s}" for s in ("parent", "parent.a", "parent.b", "parent.b.deep", "refs")]
    assert lines[f"{doc_id}::parent"].startswith(f"- {doc_id}::parent  Parent  (")
    assert "2 subsections" in lines[f"{doc_id}::parent"]
    assert lines[f"{doc_id}::parent.a"].startswith(f"  - {doc_id}::parent.a  A  (")
    assert lines[f"{doc_id}::parent.b.deep"].startswith(f"    - {doc_id}::parent.b.deep  Deep  (")
    assert "subsection" not in lines[f"{doc_id}::parent.a"]


@workflow(purpose="An outlined section's size is what reading that section spends against max_chars")
def test_outline_size_matches_section_read(project: Path) -> None:
    """For every section, header + size equals the length of a section_ids read of it."""
    doc_id = _tree_doc(project)
    builder.build(project)

    lines = _outline_lines(axiom_graph_read_doc(str(project), doc_id, outline=True))

    for sid, line in lines.items():
        size = int(re.search(r"\(([\d,]+) chars", line).group(1).replace(",", ""))
        read = axiom_graph_read_doc(str(project), section_ids=[sid], max_chars=None)
        assert len(_strip_doc_head(read)) == size, sid
    whole = axiom_graph_read_doc(str(project), doc_id, max_chars=None)
    header = axiom_graph_read_doc(str(project), doc_id, outline=True).splitlines()[0]
    assert f"{len(whole):,} chars)" in header


@workflow(purpose="outline=True with a section slug outlines only that section's subtree")
def test_outline_of_a_section_covers_only_its_subtree(project: Path) -> None:
    """The subtree root sits at the left margin; siblings outside it are absent."""
    doc_id = _tree_doc(project)
    builder.build(project)

    result = axiom_graph_read_doc(str(project), doc_id, section="b", outline=True)

    lines = _outline_lines(result)
    assert list(lines) == [f"{doc_id}::parent.b", f"{doc_id}::parent.b.deep"]
    assert lines[f"{doc_id}::parent.b"].startswith("- ")
    assert lines[f"{doc_id}::parent.b.deep"].startswith("  - ")


@workflow(purpose="An outline flags sections whose node is not VERIFIED and leaves verified ones bare")
def test_outline_flags_only_non_verified_sections(project: Path) -> None:
    """Editing a linked function marks its documenting section LINKED_STALE in the outline."""
    doc_id = _tree_doc(project)
    build_index(db_path(project), project, project_id="proj", discovery_only=False)
    (project / "pkg" / "mod.py").write_text('def func():\n    """Do the thing."""\n    return 2\n', encoding="utf-8")
    build_index(db_path(project), project, project_id="proj", discovery_only=False)

    lines = _outline_lines(axiom_graph_read_doc(str(project), doc_id, outline=True))

    assert lines[f"{doc_id}::refs"].endswith("  [LINKED_STALE]")
    assert "[" not in lines[f"{doc_id}::parent.a"]


@workflow(purpose="An id taken from an outline reads that section when passed back as section_ids")
def test_outline_ids_round_trip_into_section_ids(project: Path) -> None:
    """Each outlined id reads its own section body; an over-budget outline names the rest to outline next."""
    doc_id = _tree_doc(project)
    builder.build(project)

    lines = _outline_lines(axiom_graph_read_doc(str(project), doc_id, outline=True))
    read = axiom_graph_read_doc(str(project), section_ids=[f"{doc_id}::parent.b.deep"])
    assert f"{doc_id}::parent.b.deep" in lines and "[deep-body]" in read and "[a-body]" not in read

    cut = axiom_graph_read_doc(str(project), doc_id, outline=True, max_chars=200)
    m = re.search(r"Outline the rest with section_ids=(\[.*\]), outline=True\]", cut)
    assert m, cut
    rest = axiom_graph_read_doc(str(project), section_ids=json.loads(m.group(1)), outline=True)
    assert set(_outline_lines(cut)) | set(_outline_lines(rest)) == set(lines)


@workflow(purpose="outline=True is rejected with doc_id='list' and with a resume offset")
def test_outline_conflicts_return_errors(project: Path) -> None:
    """Both conflicting combinations return an ERROR string instead of output."""
    doc_id = _tree_doc(project)
    builder.build(project)

    assert axiom_graph_read_doc(str(project), "list", outline=True).startswith("ERROR:")
    assert axiom_graph_read_doc(str(project), doc_id, outline=True, offset=5).startswith("ERROR:")


@workflow(purpose="A read names each doc's id, tags and file on a generated line under its title")
def test_read_shows_doc_id_tags_and_file(project: Path) -> None:
    """Tags set with update_doc_meta read back from read_doc, above every section."""
    doc_id = _tree_doc(project)
    builder.build(project)
    assert not axiom_graph_update_doc_meta(str(project), doc_id, tags=["pev-request", "completed"]).startswith("ERROR")

    result = axiom_graph_read_doc(str(project), doc_id)

    lines = result.splitlines()
    assert lines[0] == "# Outline"
    assert lines[2] == f"<!-- doc: {doc_id}  tags: pev-request, completed  file: docs/outline.json -->"
    assert result.index("<!-- doc:") < result.index("## Parent")


@workflow(purpose="An outline's doc header carries the doc's tags")
def test_outline_header_shows_tags(project: Path) -> None:
    """The tags follow the section count and size on the outline's doc line."""
    doc_id = _tree_doc(project)
    builder.build(project)
    axiom_graph_update_doc_meta(str(project), doc_id, tags=["consumer"])

    header = axiom_graph_read_doc(str(project), doc_id, outline=True).splitlines()[0]

    assert header.startswith(f"{doc_id}  Outline  (5 sections, ")
    assert header.endswith("  [tags: consumer]")


@workflow(purpose="A doc without tags says so in a read and adds nothing to its outline header")
def test_untagged_doc_reads_tags_none(project: Path) -> None:
    """The meta line reads tags: (none); the outline header has no tag note."""
    doc_id = _tree_doc(project)
    builder.build(project)

    read = axiom_graph_read_doc(str(project), doc_id)
    header = axiom_graph_read_doc(str(project), doc_id, outline=True).splitlines()[0]

    assert f"<!-- doc: {doc_id}  tags: (none)  file: docs/outline.json -->" in read
    assert "[tags:" not in header
