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
    axiom_graph_clone_doc as _api_clone_doc,
    axiom_graph_delete_doc as _api_delete_doc,
    axiom_graph_accept_doc_edits as _api_accept_doc_edits,
    axiom_graph_delete_link as _api_delete_link,
    axiom_graph_delete_section as _api_delete_section,
    axiom_graph_patch_section as _api_patch_section,
    axiom_graph_read_doc as _api_read_doc,
    axiom_graph_update_doc_meta as _api_update_doc_meta,
    axiom_graph_update_section as _api_update_section,
    axiom_graph_write_doc as _api_write_doc,
)

logger = logging.getLogger(__name__)


def axiom_graph_write_doc(
    project_root: str,
    doc_json: str | dict | None = None,
    docs_root: str | None = None,
    doc_file: str | None = None,
    expected_hash: str | None = None,
) -> str:
    """Write a DocJSON documentation file and register it in the index.

    Accepts a JSON string or dict describing a documentation document.  The
    file is written under the project's primary docs directory — or under
    ``docs_root`` when a different configured root is requested — and
    immediately indexed.

    **Important:** the ``id`` key (if present) is treated as a *path-slug
    filename hint*, not as the canonical node id. The canonical node id is
    derived later by the indexer from the file's path. Common mistake:
    passing the indexed node id back as input.

        # ✅ correct — path-slug form, supports subdirs
        {"id": "pev/instances/pev-instance-2026-04-28-foo", ...}

        # ❌ wrong — node-id form (will be rejected; use path-slug instead)
        {"id": "axiom_graph::docs/pev/instances/pev-instance-2026-04-28-foo", ...}

    The ``id`` field is stripped from the JSON before writing, so the
    saved file does not retain it.

    Args:
        project_root: Absolute path to the indexed project.
        doc_json: JSON string or dict with keys: ``title``, ``sections``
            (required) and optionally ``tags``.  An ``id`` key, if present,
            is used as a filename hint (supports subdirectory paths like
            ``adrs/016-my-adr``) and stripped before writing.  Each section
            needs ``id``, ``heading`` and optionally ``content``, ``links``,
            ``tags``, and nested ``sections``.  ``links`` is a list of
            ``{"node_id": "<id>"}`` objects; a bare node-id string is read as
            one (``"links": ["proj::pkg.mod::fn"]``).  The id must be non-empty;
            other keys beside ``node_id`` are ignored and none replaces it
            (``target``, ``id``, ``type`` are not read; the link type is
            always ``documents``).  Any other entry is an error naming the
            section's dot-path and the entry, and nothing is written.
        docs_root: Which configured documentation root to write under.  Must
            match an entry of ``[axiom_graph.scan].docs_dirs``; an unknown
            value is an error listing the valid roots.  Defaults to the first
            entry — the primary root.
        doc_file: Path to a UTF-8 file holding the doc JSON, instead of an
            inline ``doc_json`` (exactly one of the two).  Must resolve under
            the project root or the system temp directory.
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
        Summary: the file written, the ``doc id``, a ``doc_hash`` line (the
        sha256 of the file's text, to pass as ``expected_hash`` on the next
        overwrite), sections written, links registered, and any unknown
        node_ids.  Or an ``ERROR: ...`` string when validation fails.
    """
    return _api_write_doc(project_root, doc_json, docs_root, doc_file, expected_hash)


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
    """Copy an indexed doc to a new one, replacing or dropping named sections.

    The copy keeps the source's sections (ids, headings, content, links,
    tags, nesting) and every extra top-level key such as ``meta``; only the
    tool-write stamps are regenerated, so every section comes out VERIFIED.
    The source is never modified.  The copy is written and indexed through
    ``write_doc``'s save path.

    Every ``set_sections`` / ``omit_sections`` id is checked first: an id the
    source does not contain (or names twice), an id in both, or a
    ``set_sections`` id inside an omitted section is one ``ERROR`` naming
    every such id, and nothing is written.  A destination that already
    exists, or is the source, is refused -- ``clone_doc`` never overwrites.

    Args:
        project_root: Absolute path to the indexed project.
        source_doc_id: Doc id of the document to copy.
        new_id: Path-slug filename hint for the copy (``write_doc``'s ``id``
            contract, e.g. ``"pev/cycles/my-cycle"``; never a node id).
        title: Title of the copy; defaults to the source's.
        tags: Doc tags of the copy, replacing the source's wholesale;
            defaults to the source's.
        set_sections: ``{section dot-path: content}`` -- replaces only the
            content of each named section (nested ids dotted, e.g.
            ``builder.friction``); heading, links and children are kept.
        omit_sections: Section dot-paths left out of the copy, with their
            children.
        docs_root: Configured docs root to write under, as in ``write_doc``.

    Returns:
        ``write_doc``'s summary, including the ``doc id`` line the copy was
        indexed under and one ``content_hash`` line per section; or
        ``ERROR: ...``.
    """
    return _api_clone_doc(
        project_root,
        source_doc_id,
        new_id,
        title=title,
        tags=tags,
        set_sections=set_sections,
        omit_sections=omit_sections,
        docs_root=docs_root,
    )


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
    return _api_read_doc(
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
            content and hash -- merge and retry.
        content_file: Path to a UTF-8 file whose text becomes the content,
            instead of an inline ``content`` (not both).  Must resolve under
            the project root or the system temp directory.
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

    A pasted ``read_doc`` linked-nodes footer is removed before storing.

    Returns:
        ``Updated ...`` plus a ``content_hash: <sha256>`` line, then
        ``  still LINKED_STALE via: <ids>`` when the section is still stale
        through its links, or ``ERROR: ...``.
    """
    return _api_update_section(
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
            nested sections), e.g. ``myproject::docs/architecture::api.errors``.
        new_string: Content to add (append / prepend) or replacement string
            (replace).  Inserted verbatim -- never scanned for anchor sentinels.
            Mirrors ``Edit``'s ``old_string`` / ``new_string`` pair.  Required
            unless ``content_file`` is given.
        anchor: ``"$"`` to append, ``"^"`` to prepend.  Mutually exclusive with
            ``old_string``.
        old_string: Replace-mode target; must match exactly once within the
            section.  Mutually exclusive with ``anchor``.
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

    A pasted ``read_doc`` linked-nodes footer in the incoming text is removed
    before it is applied.

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
    return _api_patch_section(
        project_root, section_id, new_string, anchor, old_string, content_file, addresses=addresses, edits=edits
    )


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
    one re-index.  Items may nest under or follow sections added earlier in
    the same list.  One invalid item fails the whole call; nothing is written.
    New sections come out with own status VERIFIED; link status unchanged.  A new section whose dot-path another
    section already spells (``b`` under ``a`` beside a flat ``a.b``) is
    refused.

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
    return _api_add_section(
        project_root, doc_id, section_id, heading, content, parent_id, after, sections, content_file
    )


def axiom_graph_delete_section(project_root: str, section_id: str) -> str:
    """Delete a section (and all nested children) from a DocJSON document.

    This is a destructive operation. The section is removed from the JSON
    file on disk, and all corresponding DB rows (nodes, edges, doc_sections)
    are cleaned up.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs/architecture::database-layer`` or
            ``myproject::docs/architecture::database-layer.tables``.
    """
    return _api_delete_section(project_root, section_id)


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
    in a single pass with one re-index — much faster than calling this tool
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
    return _api_add_link(project_root, section_id, node_id, node_ids, links)


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
    return _api_delete_link(project_root, section_id, node_id, node_ids)


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
    return _api_accept_doc_edits(project_root, section_ids, all_flagged, dry_run)


def axiom_graph_delete_doc(project_root: str, doc_id: str) -> str:
    """Delete an entire DocJSON document, its JSON file, and all DB artifacts.

    This is a destructive operation. The JSON file is deleted from disk, and
    all DB rows (nodes, edges, sections, tags, FTS, history) are removed.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs/architecture``.
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
        doc_id: Full doc node ID, e.g. ``myproject::docs/architecture``.
        title: New title for the document. Must be non-empty if provided.
        tags: New list of tags for the document. Pass an empty list to clear.
    """
    return _api_update_doc_meta(project_root, doc_id, title, tags)
