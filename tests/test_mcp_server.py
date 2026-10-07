"""Tests for the registered MCP tools: the wire format (every tool result is plain text) and the arguments they take."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from axiom_annotations import workflow
from mcp.types import TextContent

from axiom_graph.index import builder
from axiom_graph.mcp.server import mcp

QUOTED_LINE = 'Set "doc_quality" to `strict`.'
SECOND_LINE = "Row | with a pipe."


def _call(name: str, arguments: dict) -> list:
    return list(asyncio.run(mcp.call_tool(name, arguments)))


def _project_with_quoted_doc(root: Path) -> Path:
    docs = root / "docs"
    docs.mkdir()
    doc = docs / "guide.json"
    doc.write_text(
        json.dumps(
            {
                "title": "Guide",
                "sections": [{"id": "config", "heading": "Config", "content": f"{QUOTED_LINE}\n{SECOND_LINE}"}],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    builder.build(root, project_id="proj", discovery_only=False)
    return doc


@workflow(purpose="No registered MCP tool declares an output schema, so no result is duplicated as structuredContent")
def test_no_tool_declares_an_output_schema():
    tools = mcp._tool_manager.list_tools()
    assert tools
    with_schema = [t.name for t in tools if t.fn_metadata.output_schema is not None]
    assert with_schema == []


@workflow(purpose="A read_doc call over MCP returns one text block with literal quotes and real newlines")
def test_read_doc_result_is_plain_text(mini_project: Path):
    _project_with_quoted_doc(mini_project)

    blocks = _call("axiom_graph_read_doc", {"project_root": str(mini_project), "doc_id": "proj::docs/guide"})

    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)
    assert f"{QUOTED_LINE}\n{SECOND_LINE}" in blocks[0].text
    assert '\\"' not in blocks[0].text


@workflow(purpose="Text copied from a read_doc result matches as a patch_section old_string")
def test_read_doc_text_round_trips_into_patch_section(mini_project: Path):
    doc = _project_with_quoted_doc(mini_project)
    text = _call("axiom_graph_read_doc", {"project_root": str(mini_project), "doc_id": "proj::docs/guide"})[0].text
    start = text.index("Set ")
    copied = text[start : text.index("pipe.", start) + len("pipe.")]

    blocks = _call(
        "axiom_graph_patch_section",
        {
            "project_root": str(mini_project),
            "section_id": "proj::docs/guide::config",
            "old_string": copied,
            "new_string": "Replaced.",
        },
    )

    assert not blocks[0].text.startswith("ERROR"), blocks[0].text
    stored = json.loads(doc.read_text(encoding="utf-8"))["sections"][0]["content"]
    assert stored == "Replaced."


@workflow(
    purpose="The registered axiom_graph_check tool takes full: its schema lists the argument, full=true runs a full "
    "recompute, and a call without it does not"
)
def test_registered_check_forwards_full(mini_project: Path, caplog) -> None:
    (mini_project / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    assert "full" in mcp._tool_manager.get_tool("axiom_graph_check").parameters["properties"]
    caplog.set_level(logging.INFO, logger="axiom_graph.index.refresh")

    _call("axiom_graph_check", {"project_root": str(mini_project)})
    assert "staleness refresh: full (requested)" not in caplog.messages

    caplog.clear()
    blocks = _call("axiom_graph_check", {"project_root": str(mini_project), "full": True})
    assert not blocks[0].text.startswith("ERROR"), blocks[0].text
    assert "staleness refresh: full (requested)" in caplog.messages
