"""Agent-facing usage text for the axiom-graph MCP server.

One source, two renderings:

- :func:`build_instructions` -- the resident block passed to
  ``FastMCP(instructions=...)``.  Clients that honour server instructions put
  it in the agent's context from the first turn.  Claude Code cuts server
  instructions at 2,048 characters, so the block names each tool without a
  gloss.
- :func:`build_guide` -- the same text followed by one line per tool, taken
  from each tool's first docstring line.  ``axiom_graph_guide`` returns it,
  for subagents and clients that never receive server instructions.

Every registered tool must appear in exactly one of :data:`FAMILIES`; the test
suite checks the map against the live registry.  A tool missing from the map
still shows up in the guide, under ``other``.
"""

from __future__ import annotations

from collections.abc import Iterable

INSTRUCTIONS_LIMIT = 2048
"""Longest instructions text Claude Code shows without truncating it."""

TOOL_PREFIX = "axiom_graph_"

FAMILIES: dict[str, tuple[str, ...]] = {
    "navigate": (
        "search",
        "source",
        "graph",
        "list",
        "list_tags",
        "list_undocumented",
        "render",
        "sql",
        "workflow_list",
        "workflow_detail",
        "workflow_export",
    ),
    "docs": (
        "read_doc",
        "write_doc",
        "clone_doc",
        "update_section",
        "patch_section",
        "add_section",
        "delete_section",
        "delete_doc",
        "update_doc_meta",
        "add_link",
        "delete_link",
        "accept_doc_edits",
        "render_site",
    ),
    "staleness": (
        "check",
        "drift_query",
        "mark_clean",
        "reverify",
        "purge_node",
        "diff",
        "history",
        "report",
        "list_reference_points",
        "apply_rename",
        "revert_rename",
    ),
    "index": ("build", "checkout", "carry_forward", "guide", "info"),
}
"""Tool families, by short name (the tool name without :data:`TOOL_PREFIX`)."""

INTRO = """\
axiom-graph indexes this project's code, its docs, and the links between them, \
and tracks which docs have drifted from the code they describe. Prefer these \
tools to Read, Grep and Bash: they show which tests and docs cover the code \
you change, whether a doc is stale, and doc writes made through them stay \
indexed. Function calls are not indexed.

axiom_graph_guide returns this text plus one line per tool. Subagents do not \
receive these instructions and should call it first. All tools are prefixed \
axiom_graph_. If your client defers tool schemas, load only the 2-3 you need."""

PATTERNS = """\
patterns
  read a doc     read_doc(outline=true), then section_ids=[...]; avoid whole-doc reads
  find a mention search('"phrase"', scope="docs") -> section ids for the write tools
  ids            doc <id>::<docs root>/<path, no ext>; section adds ::<slug>; info(project_root) has id, roots, ext
  batch          list args take many at once (node_ids, section_ids, doc_ids): one call
  edit a doc     patch_section to append or edit in place; content_file for large text.
                 Write tools validate and re-index: never hand-edit docs, never re-parse.
                 A raw edit that already landed: accept_doc_edits keeps it.
  staleness      check for counts, drift_query for which and why; clear with batched
                 mark_clean / reverify, never SQL; an edit alone never clears
                 LINKED_STALE: name what it reconciles in addresses=[...]
  after upgrade  restart the MCP server; tools run the code it loaded"""

_FAMILY_WIDTH = 11
_NAME_LINE_WIDTH = 80


def _short(name: str) -> str:
    """Return a tool name without the ``axiom_graph_`` prefix."""
    return name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name


def _gloss(description: str) -> str:
    """Return the first line of a tool description, stripped."""
    lines = description.strip().splitlines()
    return lines[0].strip() if lines else ""


def _name_rows(family: str, names: Iterable[str]) -> list[str]:
    """Lay out a family's tool names as wrapped rows under one label."""
    rows: list[str] = []
    line = family.ljust(_FAMILY_WIDTH)
    for name in names:
        sep = "" if line.endswith(" ") else " "
        if sep and len(line) + 1 + len(name) > _NAME_LINE_WIDTH:
            rows.append(line)
            line, sep = " " * _FAMILY_WIDTH, ""
        line += sep + name
    rows.append(line)
    return rows


def build_instructions() -> str:
    """Build the resident instructions block sent at connect time.

    Returns:
        The persuading paragraph, the tool families with bare tool names, and
        the usage patterns, joined by blank lines.
    """
    family_rows: list[str] = []
    for family, names in FAMILIES.items():
        family_rows.extend(_name_rows(family, names))
    return "\n\n".join([INTRO, "\n".join(family_rows), PATTERNS])


def build_guide(tools: Iterable[tuple[str, str]]) -> str:
    """Build the on-demand guide: the resident block plus one line per tool.

    Args:
        tools: ``(name, description)`` pairs for every registered tool, with
            names as registered (``axiom_graph_`` prefix included).

    Returns:
        :func:`build_instructions` text, then a ``tools`` table listing each
        tool under its family with the first line of its description.  Tools
        missing from :data:`FAMILIES` are listed under ``other``.
    """
    glosses = {_short(name): _gloss(description) for name, description in tools}
    grouped: dict[str, list[str]] = {}
    for family, names in FAMILIES.items():
        grouped[family] = [n for n in names if n in glosses]
    mapped = {n for names in FAMILIES.values() for n in names}
    unmapped = sorted(n for n in glosses if n not in mapped)
    if unmapped:
        grouped["other"] = unmapped

    width = max((len(n) for n in glosses), default=0) + 2
    table = ["tools"]
    for family, names in grouped.items():
        if not names:
            continue
        table.append(f"  {family}")
        table.extend(f"    {n.ljust(width)}{glosses[n]}" for n in names)
    return "\n\n".join([build_instructions(), "\n".join(table)])
