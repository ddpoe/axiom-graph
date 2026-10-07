"""Static checks that the PEV plugin names real axiom-graph tools and allows the ones it calls first."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev"

pytestmark = pytest.mark.skipif(not PLUGIN_ROOT.is_dir(), reason="pev_nexus_agents/pev/ is not in this tree")

TOOL_PREFIX = "axiom_graph_"
# A lookbehind rather than \b, so the name inside ``mcp__axiom-graph__axiom_graph_X``
# (an agent's ``tools:`` line) is collected too.
TOOL_NAME = re.compile(r"(?<![A-Za-z0-9])axiom_graph_\w+")
FIRST_CALL_TOOLS = ("axiom_graph_guide", "axiom_graph_info")

# Tokens that match the tool-name pattern but are not MCP tools.
NOT_TOOLS = frozenset(
    {
        "axiom_graph_scope",  # a result key in the spike's JSON report
    }
)

SCANNED_SUFFIXES = {".md", ".sh", ".py", ".json", ".docjson", ".ts", ".toml"}


def _registered_tools() -> set[str]:
    """Return the names of the tools registered on the axiom-graph MCP server.

    Returns:
        Tool names as registered, e.g. ``axiom_graph_search``.
    """
    from axiom_graph.mcp.server import mcp

    return {tool.name for tool in mcp._tool_manager.list_tools()}


def _plugin_tool_names() -> dict[str, list[str]]:
    """Collect every ``axiom_graph_*`` token in the plugin's text files.

    Returns:
        Map of token to the plugin-relative paths it appears in.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(PLUGIN_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        rel = path.relative_to(PLUGIN_ROOT).as_posix()
        if rel == "CHANGELOG.md" or rel.startswith("docs/"):
            # Release history may name tools as they were; docs/ is rendered output.
            continue
        text = path.read_text(encoding="utf-8")
        for name in set(TOOL_NAME.findall(text)):
            found.setdefault(name, []).append(rel)
    return found


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split a markdown file into its YAML frontmatter and body.

    Args:
        text: The file's contents.

    Returns:
        ``(frontmatter, body)``; the frontmatter is empty when the file has none.
    """
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    return text[3:end], text[end + 4 :]


def _frontmatter_list(frontmatter: str, key: str) -> list[str]:
    """Read a YAML block list (``key:`` then ``  - item`` lines) from frontmatter.

    Args:
        frontmatter: The frontmatter text.
        key: The list's key, e.g. ``tools``.

    Returns:
        The list items, comments skipped; empty when the key is absent.
    """
    items: list[str] = []
    in_list = False
    for line in frontmatter.splitlines():
        if re.match(rf"^{key}:\s*$", line):
            in_list = True
            continue
        if not in_list:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t", "-")):
            break
        if stripped.startswith("- "):
            items.append(stripped[2:].strip().strip("\"'"))
    return items


def _first_call_tools(text: str) -> set[str]:
    """Return the tools a text tells its reader to call first.

    A sentence counts when it says "call" and "first" and names
    ``axiom_graph_guide`` or ``axiom_graph_info``.

    Args:
        text: Agent body or skill text.

    Returns:
        The subset of ``FIRST_CALL_TOOLS`` the text names in such a sentence.
    """
    named: set[str] = set()
    for line in text.splitlines():
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if re.search(r"\b[Cc]all\b", sentence) and re.search(r"\bfirst\b", sentence):
                named.update(t for t in FIRST_CALL_TOOLS if re.search(rf"(?<![A-Za-z0-9]){t}\b", sentence))
    return named


def test_agents_allow_the_tools_they_are_told_to_call_first():
    agents = sorted((PLUGIN_ROOT / "agents").glob("*.md"))
    assert agents, "no agent definitions found"
    missing: list[str] = []
    for agent in agents:
        frontmatter, body = _split_frontmatter(agent.read_text(encoding="utf-8"))
        texts = [body]
        for skill in _frontmatter_list(frontmatter, "skills"):
            skill_file = PLUGIN_ROOT / "skills" / skill.split(":")[-1] / "SKILL.md"
            if skill_file.is_file():
                texts.append(skill_file.read_text(encoding="utf-8"))
        told = set().union(*(_first_call_tools(t) for t in texts))
        allowed = {t.removeprefix("mcp__axiom-graph__") for t in _frontmatter_list(frontmatter, "tools")}
        missing.extend(f"{agent.name}: {tool}" for tool in sorted(told - allowed))
    assert not missing, f"agents told to call a tool first that their tools: list lacks: {missing}"


def test_plugin_names_only_registered_axiom_graph_tools():
    registered = _registered_tools()
    names = _plugin_tool_names()
    unknown = {
        name: paths
        for name, paths in names.items()
        if name not in registered and name not in NOT_TOOLS and name != "axiom_graph_info"
    }
    assert not unknown, f"plugin names axiom-graph tools the server does not register: {unknown}"


@pytest.mark.xfail(
    condition="axiom_graph_info" not in _registered_tools(),
    strict=True,
    reason="axiom_graph_info lands with the parallel info cycle",
)
def test_plugin_info_tool_is_registered():
    names = _plugin_tool_names()
    assert "axiom_graph_info" in names, "the plugin does not name axiom_graph_info yet"
    assert "axiom_graph_info" in _registered_tools()
