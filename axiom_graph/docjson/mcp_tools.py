"""DocJSON MCP wire surface.

Thin wrappers re-exporting the docjson behavioural API for the MCP tool
registry in ``axiom_graph.mcp.server``.  Each wrapper preserves the
public docstring and signature and forwards to
``axiom_graph.docjson.api``.  The ``_timed_tool`` decorator is applied at
registration time in ``mcp.server`` (matching ``workflows.mcp_tools``
precedent) so it composes cleanly with the symmetric four-domain
registration block.

Per ADR-019, this module's allowed imports are: ``axiom_graph.docjson.api``
and the standard library.  Nothing else.
"""

from __future__ import annotations

import logging

from axiom_graph.docjson.api import (
    axiom_graph_add_link as _api_add_link,
    axiom_graph_add_section as _api_add_section,
    axiom_graph_delete_doc as _api_delete_doc,
    axiom_graph_delete_link as _api_delete_link,
    axiom_graph_delete_section as _api_delete_section,
    axiom_graph_patch_section as _api_patch_section,
    axiom_graph_read_doc as _api_read_doc,
    axiom_graph_update_doc_meta as _api_update_doc_meta,
    axiom_graph_update_section as _api_update_section,
    axiom_graph_write_doc as _api_write_doc,
)

logger = logging.getLogger(__name__)


def axiom_graph_write_doc(project_root: str, doc_json: str | dict) -> str:
    """Write a DocJSON documentation file and register it in the index.

    Accepts a JSON string or dict describing a documentation document.  The
    file is written under the project's primary docs directory and
    immediately indexed.

    **Important:** the ``id`` key (if present) is treated as a *path-slug
    filename hint*, not as the canonical node id. The canonical node id is
    derived later by the indexer from the file's path. Common mistake:
    passing the indexed node id back as input.

        # ✅ correct — path-slug form, supports subdirs
        {"id": "pev/instances/pev-instance-2026-04-28-foo", ...}

        # ❌ wrong — node-id form (will be rejected; use path-slug instead)
        {"id": "axiom_graph::docs.pev.instances.pev-instance-2026-04-28-foo", ...}

    The ``id`` field is stripped from the JSON before writing, so the
    saved file does not retain it.

    Args:
        project_root: Absolute path to the indexed project.
        doc_json: JSON string or dict with keys: ``title``, ``sections``
            (required) and optionally ``tags``.  An ``id`` key, if present,
            is used as a filename hint (supports subdirectory paths like
            ``adrs/016-my-adr``) and stripped before writing.  Each section
            needs ``id``, ``heading`` and optionally ``content``, ``links``
            (list of ``{node_id: ...}``), ``tags``, and nested ``sections``.

    Returns:
        Summary: sections written, links registered, and any unknown node_ids.
        Or an ``ERROR: ...`` string when validation fails.
    """
    return _api_write_doc(project_root, doc_json)


def axiom_graph_read_doc(
    project_root: str,
    doc_id: str,
    section: str | None = None,
    doc_ids: list[str] | None = None,
) -> str:
    """Read a DocJSON document as Markdown.

    Each section heading is annotated with its full section ID in an HTML
    comment (e.g. ``<!-- id: myproject::docs.architecture::overview -->``),
    so you can pass that ID directly to ``axiom_graph_update_section`` or
    ``axiom_graph_add_link`` without a separate lookup step.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
            Pass ``"list"`` to see all available doc IDs.
        section: Optional short slug to read a single section, e.g.
            ``"problem"`` or ``"architecture"``.  The slug is matched against
            the tail of each section ID (the part after the last ``::``).
            If multiple sections match, all are returned with a disambiguation
            header listing the full section IDs so you can re-call with the
            exact slug or switch to the full section ID in
            ``axiom_graph_update_section``.  Omit to read the full document.
        doc_ids: Optional list of doc IDs for batch operation.  When provided,
            ``doc_id`` is ignored and results are returned for all listed IDs
            with per-ID delimiters.
    """
    return _api_read_doc(project_root, doc_id, section, doc_ids)


def axiom_graph_update_section(
    project_root: str,
    section_id: str,
    content: str | None = None,
    heading: str | None = None,
    new_id: str | None = None,
    after: str | None = None,
    tags: list[str] | None = None,
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
    existing sibling.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer`` or
            ``myproject::docs.architecture::database-layer.tables``.
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
    """
    return _api_update_section(project_root, section_id, content, heading, new_id, after, tags)


def axiom_graph_patch_section(
    project_root: str,
    section_id: str,
    new_string: str,
    anchor: str | None = None,
    old_string: str | None = None,
) -> str:
    """Partially edit a section's content (append / prepend / unique-match replace).

    A lightweight companion to ``update_section``: instead of whole-replacing
    the section content, mutate only a slice of it.  Final on-disk content and
    re-indexing are identical to the equivalent whole-replace -- this is purely
    an input-ergonomics optimisation.

    Exactly one of ``anchor`` / ``old_string`` must be supplied:

    - **append** (``anchor="$"``) -- concatenate ``new_string`` at the section end.
    - **prepend** (``anchor="^"``) -- concatenate ``new_string`` at the section start.
    - **replace** (``old_string=...``) -- ``Edit``-style unique-substring
      replacement of ``old_string`` with ``new_string``; missing or non-unique
      match is a hard error and the section is left unchanged.

    The ``^`` / ``$`` mnemonics are out-of-band parameters, never embedded in
    ``new_string`` -- a body containing ``$VAR``, ``$x^2$``, or ``Ctrl-^``
    round-trips untouched.  Append / prepend insert exactly one ``\\n``
    separator at the join (skipped when the leading side already ends with
    ``\\n``); into an empty section they just set the content.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID (dot-path notation supported for
            nested sections), e.g. ``myproject::docs.architecture::api.errors``.
        new_string: Content to add (append / prepend) or replacement string
            (replace).  Inserted verbatim -- never scanned for anchor sentinels.
            Mirrors ``Edit``'s ``old_string`` / ``new_string`` pair.
        anchor: ``"$"`` to append, ``"^"`` to prepend.  Mutually exclusive with
            ``old_string``.
        old_string: Replace-mode target; must match exactly once within the
            section.  Mutually exclusive with ``anchor``.
    """
    return _api_patch_section(project_root, section_id, new_string, anchor, old_string)


def axiom_graph_add_section(
    project_root: str,
    doc_id: str,
    section_id: str,
    heading: str,
    content: str | None = None,
    parent_id: str | None = None,
    after: str | None = None,
) -> str:
    """Add a new section to an existing DocJSON document.

    Appends a section to the end of the document (or after a specified
    sibling).  When ``parent_id`` is given, the section is added as a child
    of that parent section instead of at the top level.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
        section_id: Short slug for the new section (e.g. ``"new-section"``).
            Must be slug-safe (lowercase alphanumeric plus hyphens, no dots).
        heading: Heading text for the new section.
        content: Optional markdown content for the new section.
        parent_id: Optional dot-path of the parent section to nest under.
            If omitted, the section is added at the top level.
        after: Optional short sibling ID to insert after.  If omitted, the
            section is appended at the end.
    """
    return _api_add_section(project_root, doc_id, section_id, heading, content, parent_id, after)


def axiom_graph_delete_section(project_root: str, section_id: str) -> str:
    """Delete a section (and all nested children) from a DocJSON document.

    This is a destructive operation. The section is removed from the JSON
    file on disk, and all corresponding DB rows (nodes, edges, doc_sections)
    are cleaned up.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer`` or
            ``myproject::docs.architecture::database-layer.tables``.
    """
    return _api_delete_section(project_root, section_id)


def axiom_graph_add_link(
    project_root: str,
    section_id: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
) -> str:
    """Add link(s) from a doc section to code node(s).

    Loads the section's JSON file, appends the link(s), writes the file back,
    and re-indexes once.  When ``node_ids`` is provided, all links are added
    in a single pass with one re-index — much faster than calling this tool
    N times.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer``.
        node_id: The code node ID to link to (single-link mode).
        node_ids: List of code node IDs to link to (batch mode).
            When provided, ``node_id`` is ignored.
    """
    return _api_add_link(project_root, section_id, node_id, node_ids)


def axiom_graph_delete_link(
    project_root: str,
    section_id: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
) -> str:
    """Remove link(s) from a doc section to code node(s).

    This is a destructive operation. The ``documents`` edge(s) are removed.
    Other links and content in the section are untouched.  When ``node_ids``
    is provided, all matching links are removed in a single pass with one
    re-index.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer``.
        node_id: The code node ID to unlink (single-link mode).
        node_ids: List of code node IDs to unlink (batch mode).
            When provided, ``node_id`` is ignored.
    """
    return _api_delete_link(project_root, section_id, node_id, node_ids)


def axiom_graph_delete_doc(project_root: str, doc_id: str) -> str:
    """Delete an entire DocJSON document, its JSON file, and all DB artifacts.

    This is a destructive operation. The JSON file is deleted from disk, and
    all DB rows (nodes, edges, sections, tags, FTS, history) are removed.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
    """
    return _api_delete_doc(project_root, doc_id)


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
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
        title: New title for the document. Must be non-empty if provided.
        tags: New list of tags for the document. Pass an empty list to clear.
    """
    return _api_update_doc_meta(project_root, doc_id, title, tags)
