"""Tests for the MCP server's agent-facing usage text: the instructions block and the guide tool."""

from __future__ import annotations

import asyncio
import re

from axiom_annotations import workflow

from axiom_graph.mcp.instructions import (
    FAMILIES,
    INSTRUCTIONS_LIMIT,
    PATTERNS,
    TOOL_PREFIX,
    build_guide,
    build_instructions,
)
from axiom_graph.mcp.server import mcp
from axiom_graph.project.api import DEFAULT_POLICY_SECTIONS


def _registered() -> dict[str, str]:
    return {t.name[len(TOOL_PREFIX) :]: t.description or "" for t in mcp._tool_manager.list_tools()}


@workflow(purpose="The server sends the instructions block at connect time, within the length clients show untruncated")
def test_server_sends_instructions_within_limit():
    text = build_instructions()

    assert mcp.instructions == text
    assert len(text) <= INSTRUCTIONS_LIMIT, len(text)
    assert PATTERNS in text
    assert "axiom_graph_guide" in text
    assert "info" in FAMILIES["index"]
    ids_line = next(line for line in PATTERNS.splitlines() if line.strip().startswith("ids "))
    assert "info(project_root)" in ids_line


def test_families_name_every_registered_tool_exactly_once():
    listed = [name for names in FAMILIES.values() for name in names]

    assert len(listed) == len(set(listed)), "a tool is listed under two families"
    assert set(listed) == set(_registered())


def test_instructions_name_every_tool_in_its_family_row():
    text = build_instructions()
    words = set(text.split())

    for names in FAMILIES.values():
        for name in names:
            assert name in words, name


@workflow(purpose="Every tool the resident block and the shipped default policy name is a registered tool")
def test_block_and_default_policy_name_only_registered_tools():
    registered = set(_registered())
    family_words = {word for names in FAMILIES.values() for word in names}
    called = set(re.findall(r"\b([a-z_]+)\(", PATTERNS))
    policy_text = " ".join(content for _sid, _heading, content in DEFAULT_POLICY_SECTIONS)
    prefixed = set(re.findall(rf"\b{TOOL_PREFIX}([a-z_]+)", policy_text))
    policy_called = set(re.findall(r"\b([a-z_]+)\(", policy_text))

    assert called and prefixed
    for name in family_words | called | prefixed | policy_called:
        assert name in registered, name


def test_every_tool_first_line_is_a_short_gloss():
    for name, description in _registered().items():
        first_paragraph = description.strip().split("\n\n")[0]
        assert first_paragraph, f"{name} has no docstring"
        assert "\n" not in first_paragraph, f"{name}: first sentence wraps onto a second line"
        assert len(first_paragraph) <= 80, f"{name}: {len(first_paragraph)} chars"


@workflow(purpose="The guide tool returns the instructions block followed by one line per registered tool")
def test_guide_tool_returns_instructions_plus_a_line_per_tool():
    blocks = list(asyncio.run(mcp.call_tool("axiom_graph_guide", {})))
    text = blocks[0].text

    assert text.startswith(build_instructions())
    table = text[len(build_instructions()) :]
    for name, description in _registered().items():
        gloss = description.strip().splitlines()[0].strip()
        assert f"{name}  " in table or f" {name} " in table, name
        assert gloss in table, name


def test_guide_lists_an_unmapped_tool_under_other():
    text = build_guide([("axiom_graph_search", "Search."), ("axiom_graph_brand_new", "Does a new thing.")])

    other = text[text.index("  other") :]
    assert "brand_new" in other
    assert "Does a new thing." in other
