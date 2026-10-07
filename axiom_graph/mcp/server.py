"""axiom-graph MCP server -- FastMCP wrapper around the axiom-graph index.

Tool implementations live in domain-specific sub-modules, grouped by concern:
    axiom_graph.query.mcp_tools       -- read-only index query tools
    axiom_graph.docjson.mcp_tools     -- document manipulation tools
    axiom_graph.lifecycle.mcp_tools   -- build, staleness, verification, history tools
    axiom_graph.workflows.mcp_tools   -- workflow/task envelope inspection tools

This file contains only:
    - The FastMCP app instance, with the connect-time instructions block
      built by ``axiom_graph.mcp.instructions``
    - Tool registration (``@_tool()`` wrappers; results are plain text)
    - ``axiom_graph_guide``, which returns that block plus one line per tool
    - The ``run()`` entry point

Each registration is a thin ``functools.wraps``-style passthrough to the
matching ``<domain>.mcp_tools`` function, decorated with ``_timed_tool``.
The MCP-client-visible docstrings live on the registered wrappers
themselves (FastMCP reads them at registration time).  Wrappers without
``@_copy_doc`` carry their own copy of the implementation docstring; keep
the two in step, the first line above all, since the guide quotes it.

Entry point registered in pyproject.toml as:
    axiom-graph-mcp = "axiom_graph.mcp_server:run"
"""

from __future__ import annotations

import functools
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from axiom_graph.mcp.instructions import build_guide, build_instructions


def _copy_doc(impl):
    """Decorator that copies ``__doc__`` from ``impl`` onto the wrapper.

    Unlike :func:`functools.wraps`, this does NOT set ``__wrapped__`` --
    so :mod:`inspect.signature` continues to introspect the wrapper's
    signature, not the impl's.  FastMCP reads the registered wrapper's
    signature to build each tool's parameter schema, so the wrapper stays
    the authority on what the tool accepts while the impl supplies the
    documentation.
    """

    def decorator(wrapper):
        wrapper.__doc__ = impl.__doc__
        wrapper.__module__ = impl.__module__
        return wrapper

    return decorator


logger = logging.getLogger(__name__)

mcp = FastMCP("axiom-graph", instructions=build_instructions())

# Every tool returns plain text.  FastMCP's default (``structured_output=None``)
# infers a ``{"result": str}`` output schema for a ``str`` return and sends the
# result a second time as ``structuredContent``.  Clients that display that copy
# show it JSON-escaped, so text an agent copies from one result no longer
# matches as an exact-match argument to another tool.  Register every tool
# through ``_tool`` -- never bare ``mcp.tool()``.
_tool = functools.partial(mcp.tool, structured_output=False)


# ---------------------------------------------------------------------------
# Import tool implementations from sub-modules
# ---------------------------------------------------------------------------

from axiom_graph.mcp._helpers import _timed_tool  # noqa: E402

# -- Query tools --
from axiom_graph.query.mcp_tools import (  # noqa: E402
    axiom_graph_sql as _impl_sql,
    axiom_graph_render as _impl_render,
    axiom_graph_list as _impl_list,
    axiom_graph_graph as _impl_graph,
    axiom_graph_search as _impl_search,
    axiom_graph_source as _impl_source,
    axiom_graph_list_tags as _impl_list_tags,
    axiom_graph_list_undocumented as _impl_list_undocumented,
    axiom_graph_drift_query as _impl_drift_query,
)

# -- Doc tools --
from axiom_graph.docjson.mcp_tools import (  # noqa: E402
    axiom_graph_write_doc as _impl_write_doc,
    axiom_graph_clone_doc as _impl_clone_doc,
    axiom_graph_read_doc as _impl_read_doc,
    axiom_graph_update_section as _impl_update_section,
    axiom_graph_patch_section as _impl_patch_section,
    axiom_graph_add_section as _impl_add_section,
    axiom_graph_delete_section as _impl_delete_section,
    axiom_graph_add_link as _impl_add_link,
    axiom_graph_delete_link as _impl_delete_link,
    axiom_graph_accept_doc_edits as _impl_accept_doc_edits,
    axiom_graph_delete_doc as _impl_delete_doc,
    axiom_graph_update_doc_meta as _impl_update_doc_meta,
)

# -- Lifecycle tools --
from axiom_graph.lifecycle.mcp_tools import (  # noqa: E402
    axiom_graph_build as _impl_build,
    axiom_graph_checkout as _impl_checkout,
    axiom_graph_carry_forward as _impl_carry_forward,
    axiom_graph_check as _impl_check,
    axiom_graph_history as _impl_history,
    axiom_graph_list_reference_points as _impl_list_reference_points,
    axiom_graph_report as _impl_report,
    axiom_graph_diff as _impl_diff,
    axiom_graph_mark_clean as _impl_mark_clean,
    axiom_graph_reverify as _impl_reverify,
    axiom_graph_purge_node as _impl_purge_node,
    axiom_graph_apply_rename as _impl_apply_rename,
    axiom_graph_revert_rename as _impl_revert_rename,
    axiom_graph_render_site as _impl_render_site,
)

# -- Workflow tools --
from axiom_graph.workflows.mcp_tools import (  # noqa: E402
    axiom_graph_workflow_list as _impl_workflow_list,
    axiom_graph_workflow_detail as _impl_workflow_detail,
    axiom_graph_workflow_export as _impl_workflow_export,
)

# -- Project tools --
from axiom_graph.project.mcp_tools import (  # noqa: E402
    axiom_graph_info as _impl_info,
)


# ---------------------------------------------------------------------------
# Register tools with FastMCP
#
# Each tool is wrapped with @_tool() and @_timed_tool. The implementation
# lives in the sub-module; the wrapper here just delegates. @_copy_doc copies
# the implementation's docstring onto the wrapper, and the wrapper's own
# signature defines the tool's parameter schema.
# ---------------------------------------------------------------------------

# -- Query tools --


@_tool()
@_timed_tool
@_copy_doc(_impl_sql)
def axiom_graph_sql(project_root: str, query: str, max_results: int = 50, max_rows: int | None = None) -> str:
    effective = max_rows if max_rows is not None else max_results
    return _impl_sql(project_root, query, effective)


@_tool()
@_timed_tool
@_copy_doc(_impl_render)
def axiom_graph_render(
    project_root: str,
    level: int,
    node_id: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    return _impl_render(project_root, level, node_id, max_results, offset)


@_tool()
@_timed_tool
@_copy_doc(_impl_list)
def axiom_graph_list(
    project_root: str,
    node_type: str | None = None,
    tag: str | None = None,
    parent_id: str | None = None,
    location: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    return _impl_list(project_root, node_type, tag, parent_id, location, max_results, offset)


@_tool()
@_timed_tool
@_copy_doc(_impl_graph)
def axiom_graph_graph(
    project_root: str,
    node_id: str = "",
    direction: str = "out",
    depth: int = 1,
    max_results: int = 40,
    node_ids: list[str] | None = None,
    offset: int = 0,
) -> str:
    return _impl_graph(project_root, node_id, direction, depth, max_results, node_ids, offset=offset)


@_tool()
@_timed_tool
@_copy_doc(_impl_search)
def axiom_graph_search(
    project_root: str,
    query: str,
    level: int | None = None,
    max_results: int = 20,
    node_type: str | None = None,
    scope: str = "all",
    tag: str | None = None,
    offset: int = 0,
) -> str:
    return _impl_search(
        project_root,
        query,
        level,
        max_results,
        node_type,
        scope,
        tag,
        offset=offset,
    )


@_tool()
@_timed_tool
@_copy_doc(_impl_source)
def axiom_graph_source(
    project_root: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
    max_chars: int = 40_000,
) -> str:
    return _impl_source(project_root, node_id, node_ids, max_chars)


@_tool()
@_timed_tool
@_copy_doc(_impl_list_tags)
def axiom_graph_list_tags(project_root: str) -> str:
    return _impl_list_tags(project_root)


@_tool()
@_timed_tool
@_copy_doc(_impl_list_undocumented)
def axiom_graph_list_undocumented(
    project_root: str,
    node_type: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    return _impl_list_undocumented(project_root, node_type, max_results, offset)


# -- Doc tools --


@_tool()
@_timed_tool
def axiom_graph_write_doc(
    project_root: str,
    doc_json: str | dict | None = None,
    docs_root: str | None = None,
    doc_file: str | None = None,
    expected_hash: str | None = None,
) -> str:
    """Write a DocJSON documentation file and register it in the index.

    Accepts a JSON string or dict describing a documentation document.  The
    file is written under the project's primary docs directory (the first
    entry of ``[axiom_graph.scan].docs_dirs`` in ``axiom-graph.toml``,
    falling back to ``docs/``) and immediately indexed.  Pass ``docs_root``
    to author into a different configured root instead.

    **The 'id' field is a path-slug filename hint, NOT the canonical node id.**
    The canonical node id is derived from the file's path within its docs
    root, and the result reports it on its ``doc id`` line — take it from
    there rather than rebuilding it. Common mistake: passing the indexed
    node id back as input.

        # ✅ correct — path-slug, supports subdirs
        {"id": "pev/instances/my-instance", ...}

        # ❌ wrong — node-id form (rejected with an error)
        {"id": "axiom_graph::docs/pev/instances/my-instance", ...}

    The 'id' field is stripped from the JSON before writing.

    Args:
        project_root: Absolute path to the indexed project.
        doc_json: JSON string or dict with keys: ``title``, ``sections``
            (required) and optionally ``tags``.  Each section needs ``id``
            and ``heading`` and optionally ``content``, ``links``, ``tags``
            and nested ``sections``.  ``links`` is a list of
            ``{"node_id": "<id>"}`` objects; a bare node-id string is read as
            one (``"links": ["proj::pkg.mod::fn"]``).  The id must be non-empty;
            other keys beside ``node_id`` are ignored and none replaces it
            (``target``, ``id``, ``type`` are not read; the link type is
            always ``documents``).  Any other entry is an error naming the
            section's dot-path and the entry, and nothing is written.
        docs_root: Which configured documentation root to write under — must
            match an entry of ``[axiom_graph.scan].docs_dirs`` (e.g.
            ``".pev"``).  Defaults to the primary root.  The root is part
            of the doc id: ``.pev/test-policy.docjson`` is
            ``{project_id}::.pev/test-policy``.
        doc_file: Path to a UTF-8 file holding the doc JSON -- use it for
            large docs instead of an inline ``doc_json`` (exactly one of the
            two).  Must resolve under the project root or the system temp
            directory.
        expected_hash: Optional guard for an overwrite: the ``doc_hash`` you
            last saw for this doc (every write_doc / clone_doc result reports
            it).  A mismatch, or no existing doc, returns ERROR with the
            current doc_hash and writes nothing.  Omit to create or overwrite
            unconditionally.

    Every section the write creates or changes comes out with own status
    VERIFIED; link status unchanged (a section stale through its links stays
    LINKED_STALE until ``update_section(addresses=...)``, ``mark_clean`` or
    ``reverify``).  Pasted ``read_doc`` linked-nodes footers are removed
    from section content before storing.  A doc in which two sections spell
    one dot-path (a flat ``a.b`` beside a ``b`` nested under ``a``) is
    refused, naming both; nothing is written.

    Returns:
        Summary: the file written, the doc id it was indexed under, sections
        written, links registered, and any unknown node_ids.
        Or an ``ERROR: ...`` string when validation fails.
    """
    return _impl_write_doc(project_root, doc_json, docs_root, doc_file, expected_hash)


@_tool()
@_timed_tool
@_copy_doc(_impl_clone_doc)
def axiom_graph_clone_doc(
    project_root: str,
    source_doc_id: str,
    new_id: str,
    title: str | None = None,
    tags: list[str] | None = None,
    set_sections: dict[str, str] | None = None,
    omit_sections: list[str] | None = None,
    docs_root: str | None = None,
) -> str:
    return _impl_clone_doc(
        project_root,
        source_doc_id,
        new_id,
        title=title,
        tags=tags,
        set_sections=set_sections,
        omit_sections=omit_sections,
        docs_root=docs_root,
    )


@_tool()
@_timed_tool
def axiom_graph_read_doc(
    project_root: str,
    doc_id: str = "",
    section: str | None = None,
    doc_ids: list[str] | None = None,
    section_ids: list[str] | None = None,
    max_chars: int | None = 40_000,
    offset: int = 0,
    max_results: int | None = None,
    prefix: str | None = None,
    outline: bool = False,
) -> str:
    """Read DocJSON documents as Markdown, within a character budget.

    Each section heading is annotated with its full section ID in an HTML
    comment (e.g. ``<!-- id: myproject::docs/architecture::overview -->``),
    so you can pass that ID directly to ``axiom_graph_update_section`` or
    ``axiom_graph_add_link`` without a separate lookup step.  A section's
    links follow it as a generated list wrapped in
    ``<!-- axiom:linked-nodes -->`` ... ``<!-- /axiom:linked-nodes -->``.
    Each doc's ``# title`` is followed by a generated line naming the doc's
    id, tags and file: ``<!-- doc: {id}  tags: a, b  file: docs/x.docjson -->``.

    Reading a section returns its whole subtree (the section and every
    section nested under it).  Output stops at a section boundary once
    *max_chars* would be exceeded and ends with the omitted section ids to
    pass as ``section_ids`` next; a single section larger than the budget
    is cut and ends with the ``offset`` to resume from.

    With ``outline=True`` the same targets come back as a section tree
    instead of bodies: a header line per doc (id, title, section count,
    total size, and its tags when it has any), then one line per section,
    indented by depth, with its
    full id, heading, the rendered size of its subtree (what a read of it
    spends against ``max_chars``), its subsection count when non-zero, and
    ``[STATUS]`` when its node is not VERIFIED.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs/architecture``.
            Pass ``"list"`` to list the indexed docs (paged with
            ``max_results`` / ``offset``, filtered with ``prefix``).
        section: Optional short slug to read one section and its children,
            e.g. ``"problem"`` or ``"architecture"``.  The slug is matched
            against the dot-path after the last ``::``, exactly first, then
            as a substring.  If several sections match, all are returned with
            a header listing their full IDs.  Omit to read the full document.
        doc_ids: Optional list of doc IDs read in one call, separated by
            ``---``.  When provided, ``doc_id`` is ignored; ``section``
            applies to each doc.
        section_ids: Optional list of fully-qualified section IDs, possibly
            from different docs, read in the order given, each with its
            subtree.  Takes precedence over ``doc_id`` and ``doc_ids``.
        max_chars: Character budget for the whole call (default 40 000);
            ``None`` disables it.  The trailing hint is not counted.
        offset: For a read of exactly one section, the number of characters
            of its rendered text to skip -- the value a truncation hint
            gives.  For ``doc_id="list"``, the number of docs to skip.
        max_results: For ``doc_id="list"``, the most docs to return.
        prefix: For ``doc_id="list"``, keep only doc IDs starting with it,
            with or without the ``project::`` part.
        outline: List the section tree (ids, headings, sizes, non-VERIFIED
            statuses) instead of the bodies.  Not valid with
            ``doc_id="list"`` or a non-zero ``offset``.
    """
    return _impl_read_doc(
        project_root,
        doc_id,
        section,
        doc_ids,
        section_ids=section_ids,
        max_chars=max_chars,
        offset=offset,
        max_results=max_results,
        prefix=prefix,
        outline=outline,
    )


@_tool()
@_timed_tool
def axiom_graph_update_section(
    project_root: str,
    section_id: str = "",
    content: str | None = None,
    heading: str | None = None,
    new_id: str | None = None,
    after: str | None = None,
    tags: list[str] | None = None,
    expected_hash: str | None = None,
    content_file: str | None = None,
    addresses: list[str] | None = None,
    edits: list[dict] | None = None,
) -> str:
    """Update a single section's content, heading, or ID in a DocJSON file.

    Loads the section's JSON file, replaces the specified fields, writes the
    file back, and re-indexes.  The caller provides plain markdown content --
    no need to construct full DocJSON.

    Supports dot-path section IDs for nested sections (e.g.
    ``database-layer.tables``).

    When ``new_id`` is provided, the section is renamed.  Child section IDs
    are cascaded (dot-path prefix replacement).  The new ID must be slug-safe
    (lowercase alphanumeric plus hyphens) and must not collide with an
    existing sibling, nor give the section or a child a dot-path another
    section already spells (e.g. a flat ``a.b``).

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs/architecture::database-layer`` or
            ``myproject::docs/architecture::database-layer.tables``.
        content: New markdown content for the section.  If omitted, content
            is left unchanged.
        heading: New heading for the section.  If omitted, heading is left
            unchanged.
        new_id: New short slug for the section ID (e.g. ``"rest-api"``).
            Must be slug-safe.  Children are cascaded.
        after: Optional short sibling ID to reorder after.  Moves the target
            section to the position immediately after the named sibling within
            the same parent.  Within-parent only -- referencing a sibling
            under a different parent returns an error.
        tags: Optional list of tag strings to set on the section.  Pass an
            empty list to clear tags.  Tags are written to the section object
            in the JSON file and picked up by re-indexing.
        expected_hash: Optional ``content_hash`` of the section as you last
            saw it (every write result reports one).  If the section changed
            since, nothing is written and the error returns the current
            content and hash -- merge and retry.  Use it when other agents
            may be editing the same section.
        content_file: Path to a UTF-8 file whose text becomes the content --
            use it for long or quote-heavy bodies instead of an inline
            ``content`` (not both).  Must resolve under the project root or
            the system temp directory.
        addresses: The offenders this edit reconciles: ids the section is
            LINKED_STALE through, as ``drift_query`` lists them (``via=`` /
            ``root=``).  Each named code or doc target's receipt is refreshed
            at its indexed hash, so the section clears once every offender is
            named; a section reached through a stale doc section clears when
            that section does.  Naming anything that is not a current
            offender is an error and nothing is written.  Without it the edit
            verifies the text only: own status VERIFIED, link status
            unchanged -- an edit never clears LINKED_STALE by itself.
        edits: Batch mode -- a list of items with this tool's single-call
            keys (``section_id`` required, plus any of ``content`` /
            ``content_file``, ``heading``, ``new_id``, ``after``, ``tags``,
            ``expected_hash``, ``addresses``), across any number of docs.
            Every item is checked first, each against its doc as the earlier
            items left it; then each touched file is written and re-indexed
            once.  Any invalid item is an error naming it, and nothing is
            written.  Leave the single-call parameters unset.  The result has
            one ``content_hash`` line per item.

    A pasted ``read_doc`` linked-nodes footer is removed before storing (links
    live in the section's links array).

    Returns:
        ``Updated ...`` plus a ``content_hash: <sha256>`` line, then
        ``  still LINKED_STALE via: <ids>`` when the section is still stale
        through its links, or ``ERROR: ...``.
    """
    return _impl_update_section(
        project_root,
        section_id,
        content,
        heading,
        new_id,
        after,
        tags,
        expected_hash,
        content_file,
        addresses=addresses,
        edits=edits,
    )


@_tool()
@_timed_tool
def axiom_graph_patch_section(
    project_root: str,
    section_id: str = "",
    new_string: str | None = None,
    anchor: str | None = None,
    old_string: str | None = None,
    content_file: str | None = None,
    addresses: list[str] | None = None,
    edits: list[dict] | None = None,
) -> str:
    """Partially edit a section's content (append / prepend / unique-match replace).

    A lightweight companion to ``axiom_graph_update_section``: instead of
    whole-replacing the section content, mutate only a slice of it.  The final
    on-disk content, ``desc_hash``, staleness, and re-indexing are identical to
    the equivalent whole-replace -- this is purely an input-ergonomics
    optimisation (cheaper edits, no read-modify-write clobber risk for
    append-mostly sections like ledgers and friction logs).

    Exactly one of ``anchor`` / ``old_string`` must be supplied:

    - **append** (``anchor="$"``) -- concatenate ``new_string`` at the section
      end.  No need to know the existing content.
    - **prepend** (``anchor="^"``) -- concatenate ``new_string`` at the section
      start.  No need to know the existing content.
    - **replace** (``old_string=...``) -- ``Edit``-style unique-substring
      replacement of ``old_string`` with ``new_string``.  Missing or non-unique
      match is a hard error and the section is left unchanged.

    The ``^`` / ``$`` mnemonics line up with regex anchors but are out-of-band
    parameters, never embedded in ``new_string`` -- a section body containing
    ``$VAR``, ``$x^2$``, or ``Ctrl-^`` round-trips untouched.  Append / prepend
    insert exactly one ``\\n`` separator at the join (skipped when the leading
    side already ends with ``\\n``); into an empty section they just set the
    content.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID (dot-path notation supported for
            nested sections), e.g. ``myproject::docs/architecture::api.errors``.
        new_string: Content to add (append / prepend) or replacement string
            (replace).  Inserted verbatim -- never scanned for anchor sentinels.
            Mirrors ``Edit``'s ``old_string`` / ``new_string`` pair.  Required
            unless ``content_file`` is given.
        anchor: ``"$"`` to append at the end, ``"^"`` to prepend at the start.
            Mutually exclusive with ``old_string``.
        old_string: Replace-mode target; must match exactly once within the
            section's current content.  Mutually exclusive with ``anchor``.
        content_file: Path to a UTF-8 file whose text is used as
            ``new_string`` (omit ``new_string``).  Must resolve under the
            project root or the system temp directory.
        addresses: The offenders this edit reconciles: ids the section is
            LINKED_STALE through, as ``drift_query`` lists them (``via=`` /
            ``root=``).  Each named code or doc target's receipt is refreshed
            at its indexed hash, so the section clears once every offender is
            named; a section reached through a stale doc section clears when
            that section does.  Naming anything that is not a current
            offender is an error and nothing is written.  Without it the edit
            verifies the text only: own status VERIFIED, link status
            unchanged -- an edit never clears LINKED_STALE by itself.
        edits: Batch mode -- a list of items with this tool's single-call
            keys (``section_id`` required, plus ``new_string`` /
            ``content_file``, ``anchor`` or ``old_string``, ``addresses``),
            across any number of docs.  Every item is checked first, each
            against its doc as the earlier items left it; then each touched
            file is written and re-indexed once.  Any invalid item (unknown
            section, non-unique ``old_string``, a name that is not an
            offender) is an error naming it, and nothing is written.  Leave
            the single-call parameters unset.  The result has one
            ``content_hash`` line per item.

    Concurrent patches to one doc are serialised, so parallel appends all
    land.  A pasted ``read_doc`` linked-nodes footer in the incoming text is
    removed before it is applied.

    Returns:
        ``Patched (<mode>) section: <id>``, then the new ``content_hash``,
        the section's new length (``length: <chars> chars, <lines> lines``)
        and a fenced ``edited region (lines A-B of N):`` block holding the
        inserted or replaced text plus up to 2 lines of context either side
        (a region over 40 lines keeps its first and last 10) -- text copied
        from it matches as a next ``old_string``, so there is no need to
        re-read the section.  ``  still LINKED_STALE via: <ids>`` follows the
        length line when the section is still stale through its links.
        Or ``ERROR: ...``.
    """
    return _impl_patch_section(
        project_root, section_id, new_string, anchor, old_string, content_file, addresses=addresses, edits=edits
    )


@_tool()
@_timed_tool
def axiom_graph_add_section(
    project_root: str,
    doc_id: str,
    section_id: str = "",
    heading: str = "",
    content: str | None = None,
    parent_id: str | None = None,
    after: str | None = None,
    sections: list[dict] | None = None,
    content_file: str | None = None,
) -> str:
    """Add one or more new sections to an existing DocJSON document.

    Appends a section to the end of the document (or after a specified
    sibling).  When ``parent_id`` is given, the section is added as a child
    of that parent section instead of at the top level.

    Batch mode (``sections=[...]``) adds several sections with one write and
    one re-index -- prefer it to N calls.  Items may nest under or follow
    sections added earlier in the same list.  One invalid item fails the
    whole call; nothing is written.  New sections come out with own status VERIFIED; link status unchanged.  A new
    section whose dot-path another section already spells (``b`` under
    ``a`` beside a flat ``a.b``) is refused.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs/architecture``.
        section_id: Short slug for the new section (e.g. ``"new-section"``).
            Must be slug-safe (lowercase alphanumeric plus hyphens, no dots).
        heading: Heading text for the new section.
        content: Optional markdown content for the new section.
        parent_id: Optional dot-path of the parent section to nest under.
            If omitted, the section is added at the top level.
        after: Optional short sibling ID to insert after.  If omitted, the
            section is appended at the end.
        sections: Batch mode -- list of items with the single-call keys
            (``section_id``, ``heading``, optional ``content`` /
            ``content_file``, ``parent_id``, ``after``).  Leave the
            single-section parameters unset when using it.
        content_file: Path to a UTF-8 file whose text becomes the content,
            instead of an inline ``content`` (not both).  Must resolve under
            the project root or the system temp directory.

    Returns:
        ``Added ...`` plus one ``<dot-path>  content_hash: <sha256>`` line per
        new section, or ``ERROR: ...``.
    """
    return _impl_add_section(
        project_root, doc_id, section_id, heading, content, parent_id, after, sections, content_file
    )


@_tool()
@_timed_tool
def axiom_graph_delete_section(project_root: str, section_id: str) -> str:
    """Delete a section (and all nested children) from a DocJSON document.

    This is a destructive operation. The section is removed from the JSON
    file on disk, and all corresponding DB rows (section nodes, edges)
    are cleaned up.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs/architecture::database-layer`` or
            ``myproject::docs/architecture::database-layer.tables``.
    """
    return _impl_delete_section(project_root, section_id)


@_tool()
@_timed_tool
def axiom_graph_add_link(
    project_root: str,
    section_id: str = "",
    node_id: str = "",
    node_ids: list[str] | None = None,
    links: list[dict] | None = None,
) -> str:
    """Link doc sections to the code nodes they describe, so drift flags them.

    Loads the section's JSON file, appends the link(s), writes the file back,
    and re-indexes once.  When ``node_ids`` is provided, all links are added
    in a single pass with one re-index -- much faster than calling this tool
    N times.  ``links`` does the same across several sections of one doc.

    Adding a link does not verify the section.  A section whose ``links``
    already holds an entry without a usable node id (a hand edit such as
    ``{"target": ...}``) is refused with an ``ERROR`` naming the entry,
    and nothing is written; correct the entry first.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs/architecture::database-layer``.
        node_id: The code node ID to link to (single-link mode).
        node_ids: List of code node IDs to link to (batch mode).
            When provided, ``node_id`` is ignored.
        links: Cross-section batch -- list of ``{"section_id", "node_id"}``
            items, all sections of one doc; one write and one re-index.
            Items spanning docs, or naming a missing section, fail the whole
            call and nothing is written.  Leave ``section_id`` / ``node_id``
            / ``node_ids`` unset when using it.
    """
    return _impl_add_link(project_root, section_id, node_id, node_ids, links)


@_tool()
@_timed_tool
def axiom_graph_delete_link(
    project_root: str,
    section_id: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
) -> str:
    """Remove links from a doc section to code nodes.

    This is a destructive operation. The ``documents`` edge(s) are removed.
    Other links and content in the section are untouched.  When ``node_ids``
    is provided, all matching links are removed in a single pass with one
    re-index.  A section whose ``links`` holds an entry without a usable
    node id is refused, as in ``axiom_graph_add_link``.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs/architecture::database-layer``.
        node_id: The code node ID to unlink (single-link mode).
        node_ids: List of code node IDs to unlink (batch mode).
            When provided, ``node_id`` is ignored.
    """
    return _impl_delete_link(project_root, section_id, node_id, node_ids)


@_tool()
@_timed_tool
def axiom_graph_accept_doc_edits(
    project_root: str,
    section_ids: list[str] | None = None,
    all_flagged: bool = False,
    dry_run: bool = False,
) -> str:
    """Accept hand-edited DocJSON sections: stamp them and verify their text.

    A section edited by hand (a raw DocJSON edit) is indexed but never
    auto-verified, and builds and writes report it once.  Accepting it
    writes a fresh tool-write stamp into the file (under the doc's write
    lock) and verifies the section's text as it stands (own status
    VERIFIED; link status unchanged), with op ``accept_raw_docjson_edit``.  The section's content and hashes
    are unchanged.  The other fix is to re-apply the change with
    ``update_section`` / ``patch_section`` / ``add_section``.

    Args:
        project_root: Absolute path to the indexed project.
        section_ids: Full section ids to accept.  A section whose stamp is
            already valid is skipped.
        all_flagged: Accept every section currently flagged as a raw
            DocJSON edit.  Not a backfill: unflagged unstamped sections are
            left alone.
        dry_run: List the flagged sections and change nothing.

    Returns:
        A summary of what was listed or accepted, or ``ERROR: ...``.
    """
    return _impl_accept_doc_edits(project_root, section_ids, all_flagged, dry_run)


@_tool()
@_timed_tool
def axiom_graph_delete_doc(project_root: str, doc_id: str) -> str:
    """Delete an entire DocJSON document, its JSON file, and all DB artifacts.

    This is a destructive operation. The JSON file is deleted from disk, and
    all DB rows (nodes, edges, sections, tags, FTS, history) are removed.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs/architecture``.
    """
    return _impl_delete_doc(project_root, doc_id)


@_tool()
@_timed_tool
def axiom_graph_update_doc_meta(
    project_root: str,
    doc_id: str,
    title: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Update a document's title or tags without rewriting the entire document.

    Patches the top-level fields in the JSON file and re-indexes. Sections
    and their content are untouched.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs/architecture``.
        title: New title for the document. Must be non-empty if provided.
        tags: New list of tags for the document. Pass an empty list to clear.
    """
    return _impl_update_doc_meta(project_root, doc_id, title, tags)


# -- Lifecycle tools --


@_tool()
@_timed_tool
def axiom_graph_build(project_root: str, verbose: bool = False) -> str:
    """Re-index the project: add new nodes and refresh edges after code changes.

    Only nodes that have never been indexed are inserted, preserving
    ``CONTENT_UPDATED`` / ``NOT_FOUND`` signals.  Edges are updated
    in all cases.

    Refuses (returns ``ERROR: ...``) when the resolved project id differs from the index's stored id.
    To purge individual NOT_FOUND nodes, use ``axiom_graph_purge_node``.
    For bulk purge of all NOT_FOUND nodes, use the CLI
    ``axiom-graph purge --all-not-found``.

    Args:
        project_root: Absolute path to the project to index.
        verbose: When ``True``, include all warning details.  Default
            ``False`` shows only the warning count.
    """
    return _impl_build(project_root, verbose)


@_tool()
@_timed_tool
def axiom_graph_checkout(project_root: str, worktree_path: str) -> str:
    """Copy the index into a git worktree as a consistent snapshot.

    Produces an atomic, consistent snapshot of the source index --
    safe regardless of WAL state or concurrent writes.

    Args:
        project_root: Absolute path to the source project (must have
            .axiom_graph/graph.db).
        worktree_path: Absolute path to the target directory.
    """
    return _impl_checkout(project_root, worktree_path)


@_tool()
@_timed_tool
@_copy_doc(_impl_carry_forward)
def axiom_graph_carry_forward(
    project_root: str,
    worktree_path: str,
    dry_run: bool = False,
    list_nodes: bool = False,
) -> str:
    return _impl_carry_forward(project_root, worktree_path, dry_run, list_nodes)


@_tool()
@_timed_tool
def axiom_graph_check(project_root: str, include_frozen: bool = False, full: bool = False) -> str:
    """Summarise staleness in one line: counts per status, no node list.

    Returns a single summary line covering both dimensions of node
    health, with optional ``(all nodes VERIFIED)`` / DOC_SECTION_LONG
    counts when applicable.  Files that hold functions or sections the
    index lacks, or that lost ones the index holds, add one line each,
    e.g. ``utils.py has 2 new functions — run build`` or ``utils.py has
    1 indexed function no longer found — run build``.

    For per-node detail, paginated lists, filtered slices, or grouped
    aggregates use ``axiom_graph_drift_query``.

    Args:
        project_root: Absolute path to the indexed project.
        include_frozen: When ``False`` (the default), the rows of a doc
            tagged in ``config.staleness.frozen_tags``, its sections and
            the doc's own node alike, are excluded from the summary
            counts, except a row whose link status is BROKEN_LINK: it
            is counted under BROKEN_LINK
            (only), as ``axiom_graph_drift_query`` lists it.  When
            ``True`` they are included.  No-op when ``frozen_tags`` is
            empty.
        full: When ``False`` (the default), only what changed since the
            last check is recomputed.  When ``True`` every node is
            recomputed from a re-hash of every file: the same statuses,
            only slower.

    Note:
        ``verbose`` and ``filter`` parameters were removed in the
        2026-05 drift_query cycle.  Calling with those keyword arguments
        raises ``TypeError`` -- migrate to ``axiom_graph_drift_query``.
    """
    return _impl_check(project_root, include_frozen=include_frozen, full=full)


@_tool()
@_timed_tool
@_copy_doc(_impl_drift_query)
def axiom_graph_drift_query(
    project_root: str,
    filter: str | None = None,
    location_glob: str | None = None,
    group_by: str | None = None,
    format: str | None = None,
    page: int = 0,
    limit: int = 100,
    include_frozen: bool = False,
) -> str:
    return _impl_drift_query(
        project_root,
        filter=filter,
        location_glob=location_glob,
        group_by=group_by,
        format=format,
        page=page,
        limit=limit,
        include_frozen=include_frozen,
    )


@_tool()
@_timed_tool
def axiom_graph_history(
    project_root: str,
    node_id: str = "",
    max_results: int = 10,
    offset: int = 0,
    node_ids: list[str] | None = None,
    limit: int | None = None,
) -> str:
    """Show the change history for a single node.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: The node to inspect.
        max_results: Number of history entries to return (default 10, max 100).
        offset: Number of entries to skip (default 0).
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored.
        limit: Deprecated alias for max_results.
    """
    return _impl_history(project_root, node_id, max_results, offset, node_ids, limit)


@_tool()
@_timed_tool
def axiom_graph_list_reference_points(project_root: str) -> str:
    """List the checkpoints and SHAs that report(since_sha=...) can start from.

    Call this **before** ``axiom_graph_report`` to discover valid SHA values.

    Args:
        project_root: Absolute path to the indexed project.
    """
    return _impl_list_reference_points(project_root)


@_tool()
@_timed_tool
def axiom_graph_report(
    project_root: str,
    since_sha: str | None = None,
    since_timestamp: str | None = None,
    verbose: bool | None = None,
    change_type_pattern: str | None = None,
    node_pattern: str | None = None,
    node_type: str | None = None,
    detail: str | None = None,
    exclude_node_pattern: str | list[str] | None = None,
    max_chars: int | None = 40_000,
) -> str:
    """Impact report: what changed since a checkpoint, SHA, or datetime.

    Summarises content changes, staleness transitions, link modifications,
    and verification activity recorded in ``node_history``.  Every response
    starts with a ``reference:`` line saying what the report was measured
    against and how it resolved (checkpoint, build row, git commit time,
    timestamp, default fallback, or whole history when no reference exists).

    ``since_sha`` resolves against the index first (prefix match in both
    directions, so a full SHA matches a 12-char checkpoint), then against
    git's commit time.  A SHA neither knows -- or an ambiguous / <4-char
    one -- returns ``ERROR: ...`` instead of a report.

    Args:
        project_root: Absolute path to the indexed project.
        since_sha: Git SHA prefix (4+ characters).
        since_timestamp: ISO-8601 datetime cutoff.
        verbose: Deprecated alias -- ``True`` means ``detail="full"``.
            Ignored when ``detail`` is given.
        change_type_pattern: Glob pattern to filter change types.
        node_pattern: Glob pattern to filter node IDs.
        node_type: Filter to nodes of this type.
        detail: ``"summary"`` (default), ``"condensed"`` (aggregated by
            container; hand-made changes verbatim) or ``"full"``.
        exclude_node_pattern: Glob or list of globs for node IDs to leave
            out (e.g. the cycle manifest: ``"{doc_id}*"``).
        max_chars: Response character cap (default 40 000; ``None``
            disables).  Over-long output is cut at a line boundary with a
            footer.
    """
    return _impl_report(
        project_root,
        since_sha,
        since_timestamp,
        verbose,
        change_type_pattern,
        node_pattern,
        node_type,
        detail,
        exclude_node_pattern,
        max_chars,
    )


@_tool()
@_timed_tool
def axiom_graph_diff(
    project_root: str,
    node_id: str = "",
    baseline_sha: str | None = None,
    node_ids: list[str] | None = None,
    summary_only: bool = False,
) -> str:
    """Show what changed in a node since a baseline commit.

    When ``summary_only`` is ``False`` (default), returns JSON with keys:
    ``node_id``, ``baseline_sha``, ``baseline_date``, ``baseline_reason``,
    ``path``, ``baseline_path``, ``old_content``, ``new_content``, ``summary``.

    With no ``baseline_sha``, a node that went stale and has not been
    verified since diffs against the last commit recorded before it went
    stale (none recorded: ``{"error": "no_baseline"}``); any other node
    against its last verification or checkpoint commit, else the commit it
    was first indexed at.  ``baseline_reason`` names the rule used.

    Follows renames: a file moved since the baseline (committed or staged)
    diffs against its old path, ``baseline_path`` (``null`` for a new
    file).  If git cannot tell whether it was renamed, the result is
    ``{"error": "baseline_path_unresolved"}``, not an all-new diff.

    Finds the node by identity in each side (code re-scanned, renamed
    nodes via rename history; a DocJSON section as its own heading and
    content), not at its indexed line range.  A node new since the
    baseline has an empty ``old_content``.  If its position cannot be
    determined (e.g. the baseline does not parse) the result is
    ``{"error": "node_position_unresolved", "reason": ...}``.

    When ``summary_only`` is ``True``, omits ``old_content`` and
    ``new_content``, adds ``lines_added`` and ``lines_removed`` integers.
    Use for triage before deep-diving into individual nodes.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: The node to diff.
        baseline_sha: Optional git SHA to diff against.
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored.
        summary_only: Return only metadata and line-count stats.
    """
    return _impl_diff(project_root, node_id, baseline_sha, node_ids, summary_only)


@_tool()
@_timed_tool
def axiom_graph_mark_clean(
    project_root: str,
    reason: str,
    node_id: str = "",
    verified_by: str = "agent",
    node_ids: list[str] | None = None,
) -> str:
    """Mark nodes verified after reviewing them, clearing their drift.

    Records an AGENT_VERIFIED history row per node; on the next check, any node
    whose content still matches is promoted from CONTENT_UPDATED, DESC_UPDATED,
    or RENAMED back to VERIFIED.  It records a verification for the node it
    names: LINKED_STALE on that node clears when each linked node is at the
    version the verification recorded.  A link it recorded no version for
    (an older verification from before versions were recorded, or a link
    added since) falls back to the time rule: it clears when the
    verification is newer than the linked node's last change.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: Single node to mark clean (used when node_ids is omitted).
        reason: Brief explanation of why the documentation is still accurate.
        verified_by: Identifier for the verifier. Defaults to ``'agent'``.
        node_ids: Optional list of node IDs for batch operation.
    """
    return _impl_mark_clean(project_root, node_id, reason, verified_by, node_ids)


@_tool()
@_timed_tool
def axiom_graph_reverify(
    project_root: str,
    reason: str,
    node_id: str = "",
    verified_by: str = "agent",
    node_ids: list[str] | None = None,
    verbose: bool = False,
) -> str:
    """Verify a node and clear the LINKED_STALE it caused, in one operation.

    Assertion semantics: "I verified this node; my change to it does not
    invalidate its dependents."  Refreshes statuses first (as ``check``
    does), expands composite sources to their subtree, resolves transitive
    doc-to-doc chains back to their root offender, clears the attributed
    dependents (with ``[reverify:<source>]`` provenance), and finishes with
    a staleness recompute.  A cleared dependent with its own unreviewed
    change keeps it: only its links are verified, and it is counted as an
    "own change kept".

    Skip rule: dependents that are also stale via *other* root offenders
    (including an edit not yet built) are left LINKED_STALE and reported as
    skipped.

    The report is compact by default: it leads with how many LINKED_STALE
    nodes were cleared, gives counts, the before/after LINKED_STALE counts
    as ``check`` counts them (frozen docs excluded), and the skipped
    dependents with what still holds them.  ``verbose=True`` adds the
    source, cleared and own-change-kept lists.

    Batch: pass ``node_ids`` to reverify several sources at once.  Their
    subtrees form one source set, so a dependent held only by batch sources
    clears in this call whatever the order; one report covers the batch,
    with skipped offenders outside the batch and ``Not found`` IDs.

    Args:
        project_root: Absolute path to the indexed project.
        reason: Brief explanation of why dependents remain accurate.
        node_id: The node you verified (used when node_ids is omitted).
        verified_by: Identifier for the verifier. Defaults to ``'agent'``.
        node_ids: Optional list of sources for batch operation. When
            provided, ``node_id`` is ignored.
        verbose: List every source, cleared node, own-change-kept node
            and skipped dependent.  Defaults to the compact report.
    """
    return _impl_reverify(project_root, node_id, reason, verified_by, node_ids, verbose)


@_tool()
@_timed_tool
def axiom_graph_purge_node(
    project_root: str,
    reason: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
) -> str:
    """Remove NOT_FOUND nodes (deleted code or docs) from the index.

    Only nodes with ``own_status = 'NOT_FOUND'`` can be purged.  A module,
    DocJSON doc or config node whose file is still on disk is refused: its
    NOT_FOUND is inherited from the NOT_FOUND nodes in that file, which the
    error lists to purge if they were really removed.  Any node of a Python,
    DocJSON or JS/TS file that is on disk but does not parse (for JS/TS, a
    file tree-sitter parses with errors), a function or section
    as well as the module or doc, is refused with an error saying the file
    does not parse; it lists nothing (fix the file and re-run check, do not
    purge its nodes).  The DELETED history
    row records actor ``agent``.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: Full node ID to purge.
        reason: Human-readable reason for the purge.
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored.
    """
    return _impl_purge_node(project_root, node_id, reason, node_ids)


@_tool()
@_timed_tool
def axiom_graph_apply_rename(
    project_root: str,
    old_id: str,
    new_id: str,
) -> str:
    """Record a rename the automatic matcher missed, keeping the node's history.

    Escape hatch for a real rename that fell below the similarity threshold.
    Restricted to the ``(NOT_FOUND old, newly-created new)`` safety contract.
    On success it migrates history/verification/edges to ``new_id`` and marks
    it ``RENAMED``.

    Args:
        project_root: Absolute path to the indexed project.
        old_id: The ``NOT_FOUND`` node being renamed from.
        new_id: The newly-created live node being renamed to.
    """
    return _impl_apply_rename(project_root, old_id, new_id)


@_tool()
@_timed_tool
def axiom_graph_revert_rename(
    project_root: str,
    new_id: str,
) -> str:
    """Undo an applied rename, restoring the node's prior identity.

    Re-runs the recorded migration in reverse: history/verification/edges move
    back to the original ID, which is restored as the live identity while
    ``new_id`` is detached as a fresh node.

    Args:
        project_root: Absolute path to the indexed project.
        new_id: The current (renamed-to) identity to revert.
    """
    return _impl_revert_rename(project_root, new_id)


@_tool()
@_timed_tool
def axiom_graph_render_site(
    project_root: str,
    build: bool = False,
    nav_path: str | None = None,
    output_dir: str | None = None,
    targets: list[str] | None = None,
) -> str:
    """Render the configured consumer doc targets (Sphinx pages, README) from docs.

    With no ``nav_path``/``output_dir``, renders every configured render target
    (``[[axiom_graph.site.targets]]``) -- or the subset named in *targets* --
    in its declared flavor (plain GFM or Sphinx/MyST).  When no targets are
    configured an implicit ``guide`` (sphinx -> ``userdocs/guide``) target is
    synthesised.  Renders each doc to clean Markdown (no agent annotations,
    internal doc-id links stripped) with a provenance stamp.

    ``nav_path``/``output_dir`` are single-target ad-hoc overrides that bypass
    the target list and render one nav-driven Sphinx subtree.

    Args:
        project_root: Absolute path to the indexed project.
        build: If True, also run ``sphinx-build`` after generating files
            (sphinx-format targets only).
        nav_path: Path to site-nav.yml ad-hoc override.  Defaults to
            ``{project_root}/site-nav.yml``.
        output_dir: Directory for the generated MyST pages ad-hoc override.
            Defaults to ``{project_root}/userdocs/guide``.
        targets: Optional list of target names to render; others are skipped.
    """
    return _impl_render_site(project_root, build, nav_path, output_dir, targets)


# -- Workflow tools --


@_tool()
@_timed_tool
def axiom_graph_workflow_list(
    project_root: str,
    module: str | None = None,
    role: str | None = None,
    scope: str = "production",
    has_steps: bool = False,
    max_results: int = 30,
    offset: int = 0,
) -> str:
    """List workflows, tasks and state machines, with their purpose and location.

    Returns one line per function: name, decorator role, purpose summary,
    file location, and the corresponding axiom-graph node ID (when resolved).

    Args:
        project_root: Absolute path to the indexed project.
        module: File path substring filter.
        role: Filter by decorator type: ``"workflow"`` or ``"task"``.
        scope: ``"production"`` (default), ``"tests"``, or ``"all"``.
        has_steps: When True, only return functions with Step markers.
        max_results: Maximum rows returned (default 30).
        offset: Starting index for pagination (default 0).
    """
    return _impl_workflow_list(project_root, module, role, scope, has_steps, max_results, offset)


@_tool()
@_timed_tool
def axiom_graph_workflow_detail(
    project_root: str,
    workflow_id: str,
    verbose: bool = False,
    format: str = "text",
) -> str:
    """Show ordered steps for a single workflow or task function.

    ``workflow_id`` is the function name (e.g. ``"run_pipeline"``) or the
    axiom-graph node ID from ``axiom_graph_workflow_list``.

    Args:
        project_root: Absolute path to the indexed project.
        workflow_id: Function name or axiom-graph node ID.
        verbose: When ``True``, include purpose, inputs, outputs, and
            critical fields.
        format: ``"text"`` (the default) for the human-readable outline,
            or ``"json"`` for the envelope's full structured form -- the
            same per-workflow shape the export bundle carries.
    """
    return _impl_workflow_detail(project_root, workflow_id, verbose, format=format)


@_tool()
@_timed_tool
@_copy_doc(_impl_workflow_export)
def axiom_graph_workflow_export(
    project_root: str,
    workflow_ids: list[str] | None = None,
    files: list[str] | None = None,
    output_path: str | None = None,
    format: str = "html",
) -> str:
    return _impl_workflow_export(project_root, workflow_ids, files, output_path, format)


# -- Project facts --


@_tool()
@_timed_tool
@_copy_doc(_impl_info)
def axiom_graph_info(project_root: str) -> str:
    return _impl_info(project_root)


# -- Guide --


@_tool()
@_timed_tool
def axiom_graph_guide() -> str:
    """Tool-usage guide: families, patterns, one line per tool. Call it first.

    Returns the instructions this server sends at connect time, followed by
    one line per tool.  Subagents never receive server instructions, so this
    is how they get them.  Takes no arguments and reads no project.
    """
    tools = mcp._tool_manager.list_tools()
    return build_guide((t.name, t.description or "") for t in tools)


# ---------------------------------------------------------------------------
# Backward-compatible re-exports for helpers used in tests
# ---------------------------------------------------------------------------

from axiom_graph.mcp._helpers import (  # noqa: E402, F811
    _timed_tool,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Start the MCP server (stdio transport).

    Configures logging to stderr (MCP uses stdio for transport, so all
    logging MUST go to stderr to avoid corrupting the protocol stream).

    Env vars:
        AXIOM_GRAPH_LOG_LEVEL: Override the default INFO level (DEBUG, INFO,
            WARNING, ERROR).
        AXIOM_GRAPH_LOG_FILE: If set, also log to this file via a
            RotatingFileHandler (5 MB per file, 2 backups = 15 MB max).
    """
    level_name = os.environ.get("AXIOM_GRAPH_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format=fmt,
    )
    log_file = os.environ.get("AXIOM_GRAPH_LOG_FILE")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(file_handler)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run()
