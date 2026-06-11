"""Public Python API for the docjson bounded context.

Per ADR-019, the docjson domain owns every behavioural primitive that
manipulates DocJSON files on disk and the corresponding rows in the
axiom-graph index.  This module is the single canonical home for those
operations; the MCP wire surface (``axiom_graph.docjson.mcp_tools``) is a
thin layer that forwards calls.

Public surface (9 doc operations):
    ``axiom_graph_write_doc``       -- create or update a DocJSON file
    ``axiom_graph_read_doc``        -- read a DocJSON doc as Markdown
    ``axiom_graph_update_section``  -- patch a single section's fields
    ``axiom_graph_add_section``     -- add a new section to a doc
    ``axiom_graph_delete_section``  -- delete a section (and children)
    ``axiom_graph_add_link``        -- add a link from a doc section to a node
    ``axiom_graph_delete_link``     -- remove a link from a doc section
    ``axiom_graph_delete_doc``      -- delete an entire doc (file + DB)
    ``axiom_graph_update_doc_meta`` -- update a doc's title or tags

Public surface (helpers and diff):
    ``parse_section_id``      -- split ``proj::docs.x::sec`` into components
    ``load_doc_json``         -- look up a doc node + load its file
    ``save_and_reindex``      -- persist DocJSON dict + re-index in DB
    ``get_doc_diff``          -- old vs new doc sections vs a baseline SHA

Layering invariants (per ADR-019; enforced by ``tools/check_layering.py``):
    Allowed imports: ``axiom_graph.config``, ``axiom_graph.index.*``,
    ``axiom_graph.docjson.parse``, ``axiom_graph.docjson.render_agent``,
    and stdlib.  Never ``axiom_graph.mcp.*``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

from axiom_annotations import task, Step

from axiom_graph.config import AxiomGraphConfig
from axiom_graph.docjson import parse as json_doc_scanner
from axiom_graph.docjson.render_agent import _render_doc_markdown, _render_doc_toc
from axiom_graph.index import db
from axiom_graph.index.paths import require_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# ---------------------------------------------------------------------------
# Section tree helpers (pure tree operations).
# ---------------------------------------------------------------------------


def _find_section_in_tree(
    sections: list[dict],
    dot_path: str,
) -> dict | None:
    """Find a section by dot-path ID in a nested sections tree.

    For a dot_path like ``database-layer.tables``, this splits on ``.``
    and walks into nested ``sections`` arrays level by level.  For a
    simple ID like ``api-layer`` it does a flat scan of the top level.

    Args:
        sections: The top-level sections list from a DocJSON document.
        dot_path: Dot-separated section ID path (e.g. ``"parent.child"``).

    Returns:
        The matching section dict, or None if not found.
    """
    parts = dot_path.split(".")
    current_list = sections
    for part in parts:
        match = next((s for s in current_list if s.get("id") == part), None)
        if match is None:
            return None
        if part == parts[-1]:
            return match
        current_list = match.get("sections") or []
    return None


def _get_section_depth_in_tree(
    sections: list[dict],
    dot_path: str,
) -> int:
    """Return the nesting depth of a section identified by dot-path.

    Depth 0 is top-level, 1 is a child of a top-level section, etc.

    Args:
        sections: The top-level sections list.
        dot_path: Dot-separated section ID path.

    Returns:
        The depth of the section (number of dots in the path).
    """
    return dot_path.count(".")


def _validate_max_depth(sections: list[dict], current_depth: int = 0, max_depth: int = 2) -> str | None:
    """Recursively validate that no section exceeds max nesting depth.

    Args:
        sections: List of section dicts to validate.
        current_depth: Current nesting depth (0 for top-level).
        max_depth: Maximum allowed depth (inclusive).

    Returns:
        Error message string if depth exceeded, None if valid.
    """
    for sec in sections:
        children = sec.get("sections") or []
        if children and current_depth >= max_depth:
            return (
                f"ERROR: Section '{sec.get('id', '?')}' at depth {current_depth} "
                f"has children, which would exceed maximum nesting depth of "
                f"{max_depth + 1} levels"
            )
        if children:
            err = _validate_max_depth(children, current_depth + 1, max_depth)
            if err:
                return err
    return None


# ---------------------------------------------------------------------------
# Section-ID parsing
# ---------------------------------------------------------------------------


def parse_section_id(section_id: str) -> tuple[str, str, str, str] | str:
    """Parse a full qualified section ID into its components.

    Extracts project_part, doc_path_slug, sec_raw_id, and doc_node_id
    from a section_id like ``myproject::docs.architecture::database-layer``.

    Args:
        section_id: Full qualified section ID.

    Returns:
        A tuple of (project_part, doc_path_slug, sec_raw_id, doc_node_id)
        on success, or an error string on failure.
    """
    if "::docs." not in section_id:
        return f"ERROR: section_id must contain '::docs.' -- got '{section_id}'"
    project_part, rest = section_id.split("::docs.", 1)
    if "::" not in rest:
        return f"ERROR: section_id has no section suffix after doc id -- got '{section_id}'"
    doc_path_slug, sec_raw_id = rest.split("::", 1)
    doc_node_id = f"{project_part}::docs.{doc_path_slug}"
    return (project_part, doc_path_slug, sec_raw_id, doc_node_id)


# ---------------------------------------------------------------------------
# Doc JSON I/O
# ---------------------------------------------------------------------------


def load_doc_json(db_path: Path, root: Path, doc_node_id: str) -> tuple[dict, Path, "db.AxiomNode"] | str:
    """Load a DocJSON file by looking up the doc node in the DB.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        doc_node_id: The doc node ID (e.g. ``proj::docs.arch``).

    Returns:
        A tuple of (data_dict, json_file_path, doc_node) on success,
        or an error string on failure.
    """
    doc_node = db.get_node(db_path, doc_node_id)
    if doc_node is None:
        return f"ERROR: doc node not found in index: {doc_node_id}"
    json_file = root / doc_node.location
    if not json_file.exists():
        return f"ERROR: JSON doc file not found: {json_file}"
    data = json.loads(json_file.read_text(encoding="utf-8"))
    return (data, json_file, doc_node)


def save_and_reindex(
    data: dict,
    json_file: Path,
    db_path: Path,
    root: Path,
    project_id: str,
    cleanup_doc_node_id: str | None = None,
    verified_by: str = "agent",
    doc_node_id: str | None = None,
) -> None:
    """Write JSON data to file and re-index.

    For every existing-node (doc-level composite or section atomic) whose
    ``code_hash`` or ``desc_hash`` actually changes as a result of the write,
    an ``AGENT_VERIFIED`` (or ``MANUAL_VERIFIED`` when ``verified_by`` is a
    human identifier) verification snapshot is recorded via
    :func:`axiom_graph.index.mark_clean.mark_node_clean`.  Newly-created and
    deleted nodes are NOT auto-marked — only nodes that pre-existed AND whose
    bytes the writer actually changed.  This is the writer-is-verifier
    semantic from cycle pev-2026-05-02 and ADR-019: an explicit save through
    a docjson write tool IS the verification.

    Args:
        data: The DocJSON dict to write.
        json_file: Path to the JSON file on disk.
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        project_id: Project ID prefix for the scanner.
        cleanup_doc_node_id: If provided, clean up old section rows
            before re-indexing (for renames/deletes).
        verified_by: Verifier identifier passed through to ``mark_node_clean``
            for any auto-mark candidates.  Defaults to ``"agent"``.  Callers
            invoking this on behalf of a human (e.g. CLI doc-write) should
            override to ``"human"``.
        doc_node_id: Optional doc node id (e.g. ``proj::docs.arch``) used to
            scope the pre-state hash snapshot to the doc being written.
            When omitted, the snapshot is derived from the scan output —
            sufficient for first-creation flows where there is nothing to
            auto-mark.
    """
    # ------------------------------------------------------------------
    # Pre-state: snapshot existing (code_hash, desc_hash) for the doc-level
    # composite node + every section atomic node belonging to this doc.
    # Newly-created nodes have no pre-state row and are therefore NOT
    # candidates for auto-mark; deleted nodes have no post-state row and are
    # also not candidates (handled implicitly because they never appear in
    # the scan output below).
    # ------------------------------------------------------------------
    pre_hashes: dict[str, tuple[str | None, str | None]] = {}
    if doc_node_id is not None:
        with db._connect(db_path) as _conn:
            rows = _conn.execute(
                "SELECT id, code_hash, desc_hash FROM nodes WHERE id = ? OR id LIKE ?",
                (doc_node_id, f"{doc_node_id}::%"),
            ).fetchall()
            for r in rows:
                pre_hashes[r["id"]] = (r["code_hash"], r["desc_hash"])

    json_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if cleanup_doc_node_id is not None:
        _cleanup_old_section_rows(db_path, cleanup_doc_node_id, json_file, root, project_id)

    from axiom_graph.index.builder import _matched_docs_dir  # noqa: PLC0415

    _matched_dd = _matched_docs_dir(json_file, root)
    nodes, edges, doc_recs, sec_recs = json_doc_scanner.scan_single_json_doc(
        json_file, root, project_id, docs_dir=_matched_dd
    )
    for node in nodes:
        db.upsert_node(db_path, node)
    for edge in edges:
        db.upsert_edge(db_path, edge)
    with db._connect(db_path) as conn:
        for rec in doc_recs:
            db.upsert_doc(conn, rec)
        for rec in sec_recs:
            db.upsert_doc_section(conn, rec)

    # ------------------------------------------------------------------
    # Auto-mark pass: for each freshly-scanned node, if it pre-existed AND
    # either of its hashes differs from the stored pre-state, the writer
    # has effectively verified the new bytes.  Emit AGENT_VERIFIED via
    # mark_node_clean.
    # ------------------------------------------------------------------
    if doc_node_id is not None and pre_hashes:
        from axiom_graph.index.mark_clean import mark_node_clean  # noqa: PLC0415

        for n in nodes:
            pre = pre_hashes.get(n.id)
            if pre is None:
                continue  # newly-created node; not a candidate
            old_code, old_desc = pre
            if old_code != n.code_hash or (old_desc or None) != (n.desc_hash or None):
                mark_node_clean(
                    db_path,
                    root,
                    n,
                    reason="auto: docjson write",
                    verified_by=verified_by,
                )


def _cleanup_old_section_rows(
    db_path: Path,
    doc_node_id: str,
    json_file: Path,
    root: Path,
    project_id: str,
) -> None:
    """Remove old doc_section and node rows for a doc before re-indexing.

    This ensures that renamed sections don't leave orphan rows in the DB.

    Args:
        db_path: Path to the axiom-graph SQLite DB.
        doc_node_id: The doc node ID (e.g. ``proj::docs.arch``).
        json_file: Absolute path to the JSON doc file.
        root: Project root directory.
        project_id: Project ID prefix.
    """
    with db._connect(db_path) as conn:
        # Collect section node IDs for this doc
        section_rows = conn.execute(
            "SELECT id FROM nodes WHERE id LIKE ? AND id != ?",
            (f"{doc_node_id}::%", doc_node_id),
        ).fetchall()
        section_ids = [r["id"] for r in section_rows]

        # Delete all section rows for this doc
        conn.execute(
            "DELETE FROM doc_sections WHERE doc_id = ?",
            (doc_node_id,),
        )

        if section_ids:
            ph = ",".join("?" * len(section_ids))
            # Clean functional index tables to avoid orphaned rows.
            # Note: node_history is intentionally NOT cleaned here -- it is an
            # audit trail and must be preserved even when sections are deleted
            # or re-indexed.
            conn.execute(f"DELETE FROM tags WHERE node_id IN ({ph})", section_ids)
            conn.execute(f"DELETE FROM node_fts WHERE id IN ({ph})", section_ids)
            conn.execute(f"DELETE FROM node_verification WHERE node_id IN ({ph})", section_ids)
            # Delete edges involving section nodes of this doc
            conn.execute(
                f"DELETE FROM edges WHERE from_id IN ({ph}) OR to_id IN ({ph})",
                section_ids + section_ids,
            )
            # Delete section nodes
            conn.execute(f"DELETE FROM nodes WHERE id IN ({ph})", section_ids)


# ---------------------------------------------------------------------------
# Doc operations (public surface)
# ---------------------------------------------------------------------------


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
    path = require_db(project_root)
    root = Path(project_root).resolve()

    if isinstance(doc_json, str):
        data = json.loads(doc_json)
    else:
        data = doc_json
    for key in ("title", "sections"):
        if key not in data:
            return f"ERROR: doc_json missing required key '{key}'"

    # Validate nesting depth before writing
    depth_err = _validate_max_depth(data.get("sections", []))
    if depth_err:
        return depth_err

    # Derive filename slug from id (if present) or title
    raw_slug = data.get("id", "").strip() if isinstance(data.get("id"), str) else ""

    # Validate id-provided slug before falling back to title.
    # The id is a path-slug filename hint; reject node-id form and any
    # filesystem-illegal characters with messages that point at the right shape.
    if raw_slug:
        if "::" in raw_slug:
            return (
                f"ERROR: 'id' looks like a node-id ({raw_slug!r}). The 'id' "
                f"field is a path-slug filename hint, not the canonical node "
                f"id. Pass e.g. 'pev/instances/{raw_slug.rsplit('.', 1)[-1]}' "
                f"instead — see axiom_graph_write_doc docstring for details."
            )
        # NTFS-reserved characters that aren't path separators. '/' is a valid
        # subdirectory separator (the docstring example uses it). '\\' would
        # collide with Windows path semantics in subtle ways, so disallow it.
        _reserved = set('<>:"\\|?*')
        bad = sorted({c for c in raw_slug if c in _reserved or ord(c) < 32})
        if bad:
            return (
                f"ERROR: 'id' contains characters that are invalid in "
                f"filenames: {''.join(bad)!r}. Use only letters, digits, "
                f"dashes, dots, and forward slashes (for subdirectories)."
            )

    if not raw_slug:
        raw_slug = re.sub(r"[^a-z0-9]+", "-", data["title"].lower()).strip("-")
    if not raw_slug:
        return "ERROR: Could not derive a filename from id or title"

    # Check all linked node_ids (recurse into nested sections)
    unknown_ids: list[str] = []
    link_count = 0

    def _check_links(sections: list[dict]) -> None:
        nonlocal link_count
        for sec in sections:
            for link in sec.get("links") or []:
                nid = link.get("node_id", "").strip()
                if nid:
                    link_count += 1
                    if db.get_node(path, nid) is None:
                        unknown_ids.append(nid)
            _check_links(sec.get("sections") or [])

    _check_links(data.get("sections", []))

    # Strip top-level "id" -- canonical identity is derived from file path
    data.pop("id", None)

    # Resolve docs_dir + out_file. Primary docs root = config.scan.docs_dirs[0]
    # (honors absolute paths).  out_file may already exist (write_doc supports
    # overwrite semantics) or may be a brand-new file.  Either way,
    # save_and_reindex handles both branches uniformly: pre-state hash
    # snapshot is empty for new files; auto-mark candidate set is empty for
    # first-creation, non-empty when overwriting an existing doc with
    # changed bytes.
    _cfg = AxiomGraphConfig.load(root)
    _primary = (_cfg.scan.docs_dirs or ["docs"])[0]
    _primary_path = Path(_primary)
    docs_dir = _primary_path if _primary_path.is_absolute() else (root / _primary_path)
    out_file = docs_dir / f"{raw_slug}.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    _project_id = _cfg.project_id or root.name

    # Derive the doc node id from the slug.  ``raw_slug`` may include
    # subdirectory separators (e.g. ``"adrs/016-foo"``), which the indexer
    # encodes as ``::docs.adrs.016-foo``.
    _doc_path = raw_slug.replace("/", ".")
    _doc_node_id = f"{_project_id}::docs.{_doc_path}"

    save_and_reindex(
        data,
        out_file,
        path,
        root,
        _project_id,
        doc_node_id=_doc_node_id,
    )

    # Record LINK_ADDED history for each documents edge.  We re-scan from
    # disk to recover the edges (save_and_reindex doesn't return them).
    from axiom_graph.index.builder import _matched_docs_dir  # noqa: PLC0415

    _matched_dd = _matched_docs_dir(out_file, root)
    _, edges, _, sec_recs = json_doc_scanner.scan_single_json_doc(out_file, root, _project_id, docs_dir=_matched_dd)
    for edge in edges:
        if edge.edge_type == "documents":
            db.insert_history_row(
                path,
                node_id=edge.from_id,
                change_type="LINK_ADDED",
                meta=json.dumps(
                    {
                        "edge_type": "documents",
                        "source": edge.from_id,
                        "target": edge.to_id,
                        "actor": "agent",
                    }
                ),
                preserved=False,
            )

    summary = (
        f"Wrote {out_file.relative_to(root).as_posix()}\n"
        f"  sections written : {len(sec_recs)}\n"
        f"  links registered : {link_count}"
    )
    if unknown_ids:
        summary += f"\n  WARN: {len(unknown_ids)} node_id(s) not found in index:"
        for uid in unknown_ids:
            summary += f"\n    ! {uid}"
    return summary


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
    logger.debug(
        "axiom_graph_read_doc: doc_id=%s, section=%s, batch=%s", doc_id, section, len(doc_ids) if doc_ids else "no"
    )

    # Batch mode
    if doc_ids is not None:
        if not doc_ids:
            return "ERROR: doc_ids list is empty"
        parts: list[str] = []
        for did in doc_ids:
            try:
                result = axiom_graph_read_doc(project_root, doc_id=did, section=section)
            except Exception as exc:
                result = f"ERROR ({did}): {exc}"
            parts.append(result)
        return "\n\n---\n\n".join(parts)

    path = require_db(project_root)

    if doc_id == "list":
        docs = db.list_docs(path)
        if not docs:
            return "(no docs indexed)"
        return "\n".join(f"{d['id']}  {d['title']}" for d in docs)

    # Verify doc exists
    doc_node = db.get_node(path, doc_id)
    if doc_node is None:
        return f"ERROR: doc '{doc_id}' not found. Pass doc_id=\"list\" to see available docs."

    sections = db.get_doc_sections(path, doc_id)

    if section is None:
        rendered = _render_doc_markdown(path, doc_id, doc_node.title, sections)
        if len(rendered) > 3000:
            return _render_doc_toc(doc_node.title, sections)
        return rendered

    # Filter by slug -- tail of section ID after last "::"
    matched = [s for s in sections if s.get("id", "").split("::")[-1] == section]
    if not matched:
        # Fallback: substring match on slug
        matched = [s for s in sections if section in s.get("id", "").split("::")[-1]]
    if not matched:
        available = ", ".join(s.get("id", "").split("::")[-1] for s in sections)
        return f"ERROR: no section matching '{section}'. Available slugs: {available}"
    if len(matched) > 1:
        ids = "\n".join(f"  {s['id']}" for s in matched)
        header = f"[{len(matched)} sections matched '{section}' -- returning all. Use full section_id in axiom_graph_update_section to be specific.]\n{ids}\n"
        return header + _render_doc_markdown(path, doc_id, doc_node.title, matched)
    return _render_doc_markdown(path, doc_id, doc_node.title, matched)


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
    logger.debug("axiom_graph_update_section: section_id=%s", section_id)

    path = require_db(project_root)
    root = Path(project_root).resolve()

    if content is None and heading is None and new_id is None and after is None and tags is None:
        return "ERROR: at least one of 'content', 'heading', 'new_id', 'after', or 'tags' must be provided"

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id)
    if isinstance(parsed, str):
        return parsed
    project_part, doc_path_slug, sec_raw_id, doc_node_id = parsed

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    # Use dot-path tree traversal instead of flat lookup
    target = _find_section_in_tree(sections, sec_raw_id)
    if target is None:
        return f"ERROR: section '{sec_raw_id}' not found in {json_file.name}"

    # --- Handle rename (new_id) ---
    changes: list[str] = []
    old_short_id = sec_raw_id.split(".")[-1]  # last segment of dot-path

    if new_id is not None:
        # Validate slug format
        if not _SLUG_RE.match(new_id):
            return f"ERROR: new_id '{new_id}' is not slug-safe (must be lowercase alphanumeric plus hyphens)"

        # Check for sibling collision: find the parent's children list
        dot_parts = sec_raw_id.split(".")
        if len(dot_parts) > 1:
            # Nested section -- find parent's children
            parent_dot_path = ".".join(dot_parts[:-1])
            parent_sec = _find_section_in_tree(sections, parent_dot_path)
            sibling_list = parent_sec.get("sections", []) if parent_sec else []
        else:
            # Top-level section
            sibling_list = sections

        existing_ids = {s.get("id") for s in sibling_list if s is not target}
        if new_id in existing_ids:
            return f"ERROR: sibling section '{new_id}' already exists"

        # Perform the rename on the target
        target["id"] = new_id
        changes.append(f"id ({old_short_id} → {new_id})")

    # --- Handle reorder (after) ---
    if after is not None:
        # Find the parent's children list for the target section
        dot_parts = sec_raw_id.split(".")
        if len(dot_parts) > 1:
            parent_dot_path = ".".join(dot_parts[:-1])
            parent_sec = _find_section_in_tree(sections, parent_dot_path)
            parent_children = parent_sec.get("sections", []) if parent_sec else []
        else:
            parent_children = sections

        # Validate: after sibling must exist in the same parent
        after_idx = next(
            (i for i, s in enumerate(parent_children) if s.get("id") == after),
            None,
        )
        if after_idx is None:
            return f"ERROR: sibling '{after}' not found among siblings of '{sec_raw_id}' for 'after' reorder"

        # Find the target's current index in the same list
        target_idx = next(
            (i for i, s in enumerate(parent_children) if s is target),
            None,
        )
        if target_idx is not None and target_idx != after_idx:
            # Remove target from current position
            parent_children.pop(target_idx)
            # Re-find after_idx (may have shifted after removal)
            after_idx = next(i for i, s in enumerate(parent_children) if s.get("id") == after)
            # Insert after the sibling
            parent_children.insert(after_idx + 1, target)
        # If target_idx == after_idx, it's after=self -- no-op
        changes.append("reorder")

    # --- Handle tags ---
    if tags is not None:
        target["tags"] = tags
        changes.append("tags")

    # Apply content/heading updates
    if content is not None:
        target["content"] = content
        changes.append("content")
    if heading is not None:
        target["heading"] = heading
        changes.append("heading")

    # Save and re-index using shared helper
    cleanup_id = doc_node_id if new_id is not None else None
    save_and_reindex(
        data,
        json_file,
        path,
        root,
        project_part,
        cleanup_doc_node_id=cleanup_id,
        doc_node_id=doc_node_id,
    )

    return f"Updated {', '.join(changes)} for section: {section_id}"


def axiom_graph_patch_section(
    project_root: str,
    section_id: str,
    new_string: str,
    anchor: str | None = None,
    old_string: str | None = None,
) -> str:
    """Partially edit a section's content without re-transmitting the whole body.

    A lightweight companion to :func:`axiom_graph_update_section`.  Where
    ``update_section`` always whole-replaces the section content,
    ``patch_section`` mutates only a slice of it.  The final on-disk content,
    ``desc_hash``, staleness, and re-indexing are identical to the equivalent
    whole-replace -- this is purely an input-ergonomics optimisation, not a
    schema or graph-semantics change.

    Three mutually exclusive modes, selected by the ``anchor`` / ``old_string``
    parameters (exactly one must be supplied):

    - **append** (``anchor="$"``) -- concatenate ``new_string`` at the section
      end.  No need to know the existing content.
    - **prepend** (``anchor="^"``) -- concatenate ``new_string`` at the section
      start.  No need to know the existing content.
    - **replace** (``old_string=...``) -- ``Edit``-style unique-substring
      replacement of ``old_string`` with ``new_string``.  Errors (leaving the
      section unchanged) if ``old_string`` is missing or matches more than once.

    The ``^`` / ``$`` mnemonics line up with regex anchors but live
    **out-of-band** as a parameter, never inside ``new_string``.  A section body
    full of ``$VAR``, ``$x^2$``, or ``Ctrl-^`` therefore round-trips untouched --
    ``new_string`` is never scanned for sentinels.

    Newline policy (append / prepend): exactly one ``\\n`` separator is inserted
    at the join, unless the leading side already ends with ``\\n`` (so no double
    newline).  Appending / prepending into an empty section just sets the
    content.  Callers wanting a blank-line (paragraph) separator add their own
    extra ``\\n`` to ``new_string``.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer``.  Dot-path
            notation for nested sections is supported, exactly as in
            ``update_section``.
        new_string: The content to add (append / prepend modes) or the
            replacement string (replace mode).  Inserted verbatim -- never
            parsed for anchor sentinels.  Named to mirror ``Edit``'s
            ``old_string`` / ``new_string`` pair.
        anchor: ``"$"`` to append at the end, ``"^"`` to prepend at the start.
            Mutually exclusive with ``old_string``.
        old_string: Replace-mode target.  Must match exactly once within the
            section's current content; missing or non-unique is a hard error
            and the section is left unchanged.  Mutually exclusive with
            ``anchor``.

    Returns:
        A confirmation string naming the mode and section, or an ``ERROR:``
        string on validation / match failure (the section is untouched on
        error).
    """
    logger.debug("axiom_graph_patch_section: section_id=%s anchor=%s", section_id, anchor)

    path = require_db(project_root)
    root = Path(project_root).resolve()

    # --- Validate mode: exactly one of {anchor, old_string} ---
    if anchor is not None and old_string is not None:
        return "ERROR: provide exactly one of 'anchor' or 'old_string', not both"
    if anchor is None and old_string is None:
        return "ERROR: provide exactly one of 'anchor' ('$' append / '^' prepend) or 'old_string' (replace)"
    if anchor is not None and anchor not in ("$", "^"):
        return f"ERROR: anchor must be '$' (append/end) or '^' (prepend/start) -- got '{anchor}'"
    if old_string is not None and old_string == "":
        return "ERROR: old_string must not be empty"

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id)
    if isinstance(parsed, str):
        return parsed
    project_part, doc_path_slug, sec_raw_id, doc_node_id = parsed

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    # Use dot-path tree traversal (same as update_section)
    target = _find_section_in_tree(sections, sec_raw_id)
    if target is None:
        return f"ERROR: section '{sec_raw_id}' not found in {json_file.name}"

    existing = target.get("content", "") or ""

    # --- Compute new content per mode (the only logic beyond update_section) ---
    if anchor == "$":
        if existing == "":
            new_content = new_string
        elif existing.endswith("\n"):
            new_content = existing + new_string
        else:
            new_content = existing + "\n" + new_string
        mode_desc = "appended to"
    elif anchor == "^":
        if existing == "":
            new_content = new_string
        elif new_string.endswith("\n"):
            new_content = new_string + existing
        else:
            new_content = new_string + "\n" + existing
        mode_desc = "prepended to"
    else:
        # replace mode -- Edit's unique-match-or-error contract, scoped to one section
        match_count = existing.count(old_string)
        if match_count == 0:
            return f"ERROR: old_string not found in section '{sec_raw_id}' -- section unchanged"
        if match_count > 1:
            return (
                f"ERROR: old_string is not unique in section '{sec_raw_id}' "
                f"({match_count} matches) -- section unchanged. "
                f"Provide a longer, unique old_string."
            )
        new_content = existing.replace(old_string, new_string)
        mode_desc = "replaced in"

    target["content"] = new_content

    # Save and re-index using shared helper (no rename, so no cleanup)
    save_and_reindex(
        data,
        json_file,
        path,
        root,
        project_part,
        cleanup_doc_node_id=None,
        doc_node_id=doc_node_id,
    )

    return f"Patched ({mode_desc}) section: {section_id}"


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
    path = require_db(project_root)
    root = Path(project_root).resolve()

    # Validate slug format
    if not _SLUG_RE.match(section_id):
        return (
            f"ERROR: section_id '{section_id}' is not slug-safe (must be lowercase alphanumeric plus hyphens, no dots)"
        )

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    # Determine where to insert
    if parent_id is not None:
        parent_sec = _find_section_in_tree(sections, parent_id)
        if parent_sec is None:
            return f"ERROR: parent section '{parent_id}' not found"

        # Check depth limit: parent's depth + 1 must be <= _MAX_DEPTH (2)
        parent_depth = parent_id.count(".") + 1  # top-level parent is depth 0, its child is depth 1
        if parent_depth > 2:
            return f"ERROR: adding a child under '{parent_id}' would exceed maximum nesting depth of 3 levels"

        target_list = parent_sec.setdefault("sections", [])
    else:
        parent_depth = -1  # top-level: new section will be at depth 0
        target_list = sections

    # Check depth limit for the new section
    new_depth = parent_depth + 1 if parent_id else 0
    from axiom_graph.docjson.parse import _MAX_DEPTH

    if new_depth > _MAX_DEPTH:
        return (
            f"ERROR: adding section at depth {new_depth} would exceed maximum nesting depth of {_MAX_DEPTH + 1} levels"
        )

    # Check for ID collision with siblings
    existing_ids = {s.get("id") for s in target_list}
    if section_id in existing_ids:
        return f"ERROR: sibling section '{section_id}' already exists"

    # Build the new section dict
    new_section: dict = {"id": section_id, "heading": heading}
    if content is not None:
        new_section["content"] = content

    # Insert at the right position
    if after is not None:
        after_idx = next(
            (i for i, s in enumerate(target_list) if s.get("id") == after),
            None,
        )
        if after_idx is None:
            return f"ERROR: sibling '{after}' not found for 'after' positioning"
        target_list.insert(after_idx + 1, new_section)
    else:
        target_list.append(new_section)

    # Save and re-index using shared helper
    project_id = doc_id.split("::")[0]
    save_and_reindex(data, json_file, path, root, project_id, doc_node_id=doc_id)

    return f"Added section '{section_id}' to {doc_id}"


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
    path = require_db(project_root)
    root = Path(project_root).resolve()

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id)
    if isinstance(parsed, str):
        return parsed
    project_part, doc_path_slug, sec_raw_id, doc_node_id = parsed

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    # Find and remove the section from the tree
    dot_parts = sec_raw_id.split(".")
    if len(dot_parts) > 1:
        # Nested: find parent list
        parent_dot_path = ".".join(dot_parts[:-1])
        parent_sec = _find_section_in_tree(sections, parent_dot_path)
        if parent_sec is None:
            return f"ERROR: parent section '{parent_dot_path}' not found"
        target_list = parent_sec.get("sections", [])
    else:
        target_list = sections

    target_short_id = dot_parts[-1]
    target_idx = next(
        (i for i, s in enumerate(target_list) if s.get("id") == target_short_id),
        None,
    )
    if target_idx is None:
        return f"ERROR: section '{sec_raw_id}' not found in {json_file.name}"

    target_list.pop(target_idx)

    # Save and re-index with cleanup
    save_and_reindex(
        data,
        json_file,
        path,
        root,
        project_part,
        cleanup_doc_node_id=doc_node_id,
        doc_node_id=doc_node_id,
    )

    return f"Deleted section '{sec_raw_id}' from {doc_node_id}"


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
    # Resolve targets
    targets: list[str] = []
    if node_ids:
        targets = [nid.strip() for nid in node_ids if nid.strip()]
    elif node_id:
        targets = [node_id.strip()]
    if not targets:
        return "ERROR: provide node_id or non-empty node_ids"

    path = require_db(project_root)
    root = Path(project_root).resolve()

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id)
    if isinstance(parsed, str):
        return parsed
    project_part, doc_path_slug, sec_raw_id, doc_node_id = parsed

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    # Use dot-path tree traversal instead of flat lookup
    target = _find_section_in_tree(sections, sec_raw_id)
    if target is None:
        return f"ERROR: section '{sec_raw_id}' not found in {json_file.name}"

    # Append links, skipping duplicates
    links: list[dict] = target.setdefault("links", [])
    existing = {lk.get("node_id") for lk in links}
    added: list[str] = []
    skipped: list[str] = []
    for nid in targets:
        if nid in existing:
            skipped.append(nid)
        else:
            links.append({"node_id": nid})
            existing.add(nid)
            added.append(nid)

    if not added:
        return f"All {len(skipped)} link(s) already exist on {section_id}"

    # Save and re-index ONCE for all links
    save_and_reindex(data, json_file, path, root, project_part, doc_node_id=doc_node_id)

    # Record LINK_ADDED history for each new link
    for nid in added:
        db.insert_history_row(
            path,
            node_id=section_id,
            change_type="LINK_ADDED",
            meta=json.dumps(
                {
                    "edge_type": "documents",
                    "source": section_id,
                    "target": nid,
                    "actor": "agent",
                }
            ),
            preserved=False,
        )

    # Warn about targets not in index
    warnings: list[str] = []
    for nid in added:
        if db.get_node(path, nid) is None:
            warnings.append(f"  WARN: node_id not found in index: {nid}")

    lines = [f"Added {len(added)} link(s) to {section_id}"]
    if skipped:
        lines.append(f"  Skipped {len(skipped)} duplicate(s)")
    lines.extend(warnings)
    return "\n".join(lines)


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
    # Resolve targets
    targets: list[str] = []
    if node_ids:
        targets = [nid.strip() for nid in node_ids if nid.strip()]
    elif node_id:
        targets = [node_id.strip()]
    if not targets:
        return "ERROR: provide node_id or non-empty node_ids"

    path = require_db(project_root)
    root = Path(project_root).resolve()

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id)
    if isinstance(parsed, str):
        return parsed
    project_part, doc_path_slug, sec_raw_id, doc_node_id = parsed

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    target_sec = _find_section_in_tree(sections, sec_raw_id)
    if target_sec is None:
        return f"ERROR: section '{sec_raw_id}' not found in {json_file.name}"

    links: list[dict] = target_sec.get("links", [])
    to_remove = set(targets)
    new_links = [lk for lk in links if lk.get("node_id") not in to_remove]
    removed = [nid for nid in targets if nid in {lk.get("node_id") for lk in links}]
    not_found = [nid for nid in targets if nid not in {lk.get("node_id") for lk in links}]

    if not removed:
        return f"No matching links found on {section_id}"

    target_sec["links"] = new_links

    # Save and re-index ONCE with cleanup
    save_and_reindex(
        data,
        json_file,
        path,
        root,
        project_part,
        cleanup_doc_node_id=doc_node_id,
        doc_node_id=doc_node_id,
    )

    # Record LINK_REMOVED history for each removed link
    for nid in removed:
        db.insert_history_row(
            path,
            node_id=section_id,
            change_type="LINK_REMOVED",
            meta=json.dumps(
                {
                    "edge_type": "documents",
                    "source": section_id,
                    "target": nid,
                    "actor": "agent",
                }
            ),
            preserved=False,
        )

    lines = [f"Removed {len(removed)} link(s) from {section_id}"]
    if not_found:
        lines.append(f"  Not found: {', '.join(not_found)}")
    return "\n".join(lines)


def axiom_graph_delete_doc(project_root: str, doc_id: str) -> str:
    """Delete an entire DocJSON document, its JSON file, and all DB artifacts.

    This is a destructive operation. The JSON file is deleted from disk, and
    all DB rows (nodes, edges, sections, tags, FTS, history) are removed.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
    """
    path = require_db(project_root)
    root = Path(project_root).resolve()

    doc_node = db.get_node(path, doc_id)
    if doc_node is None:
        return f"ERROR: doc node not found in index: {doc_id}"
    json_file = root / doc_node.location
    if json_file.exists():
        json_file.unlink()

    with db._connect(path) as conn:
        db.delete_doc_by_id(conn, doc_id)

    return f"Deleted doc '{doc_id}' and file {doc_node.location}"


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
    path = require_db(project_root)
    root = Path(project_root).resolve()

    if title is None and tags is None:
        return "ERROR: at least one of 'title' or 'tags' must be provided"

    if title is not None and not title.strip():
        return "ERROR: title must be non-empty"

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    changes: list[str] = []
    if title is not None:
        data["title"] = title
        changes.append("title")
    if tags is not None:
        data["tags"] = tags
        changes.append("tags")

    # Save and re-index using shared helper
    project_id = doc_id.split("::")[0]
    save_and_reindex(data, json_file, path, root, project_id, doc_node_id=doc_id)

    return f"Updated {', '.join(changes)} for doc: {doc_id}"


# ---------------------------------------------------------------------------
# Doc diff (moved from axiom_graph/diff.py per ADR-019)
# ---------------------------------------------------------------------------


def _extract_sections(data: dict) -> list[dict]:
    """Extract section summaries from a parsed DocJSON structure.

    Each returned dict has ``id``, ``heading``, and ``content`` keys.
    """
    sections = data.get("sections") or []
    return [
        {
            "id": sec.get("id", ""),
            "heading": sec.get("heading", ""),
            "content": sec.get("content", ""),
        }
        for sec in sections
    ]


@task(
    purpose="Return old vs new sections for a doc, diffing across submodule commits",
    inputs="db_path, project_root, doc_id, optional baseline_sha (main repo commit)",
    outputs="dict with old_sections, new_sections, baseline_sha, submodule_sha",
)
def get_doc_diff(
    db_path: Path,
    project_root: Path,
    doc_id: str,
    baseline_sha: str | None = None,
) -> dict:
    """Return old vs new sections for a doc, diffing across submodule commits.

    The baseline SHA refers to a commit in the **main** repository.  The
    submodule commit is resolved via ``git ls-tree`` so the caller never
    needs to know the docs submodule's internal SHA.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Root directory of the main repository.
        doc_id: The doc ID as stored in the ``docs`` table.
        baseline_sha: A commit SHA in the main repo.  When ``None``, the
            function uses ``HEAD~1`` as a rough default.

    Returns:
        On success: ``{"old_sections": [...], "new_sections": [...],
        "baseline_sha": ..., "submodule_sha": ...}``.
        On failure: ``{"error": "...", "reason": "..."}``.
    """
    口 = Step(
        step_num=1,
        name="Look up doc file path",
        purpose="Query the docs table for the doc's file path and compute the submodule-relative path",
    )
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT file_path FROM docs WHERE id = ?", (doc_id,)).fetchone()
        conn.close()
    except Exception as exc:
        logger.warning("doc diff DB query failed: %s", exc)
        return {"error": "db_error", "reason": f"Database query failed: {exc}"}

    if row is None:
        return {"error": "not_found", "reason": f"Doc not found: {doc_id}"}

    file_path: str = row["file_path"]

    # Determine which configured docs root this file lives under.  Iterate
    # the configured docs_dirs; pick the first entry that is a prefix of
    # file_path (POSIX-normalized).  Fall back to "docs" for back-compat.
    try:
        _cfg = AxiomGraphConfig.load(project_root)
        _docs_entries = _cfg.scan.docs_dirs or ["docs"]
    except Exception:
        _docs_entries = ["docs"]

    _fp_posix = file_path.replace("\\", "/")
    docs_root_rel: str | None = None
    for _entry in _docs_entries:
        _e_posix = _entry.replace("\\", "/").rstrip("/")
        if _e_posix and _fp_posix.startswith(_e_posix + "/"):
            docs_root_rel = _e_posix
            break

    if docs_root_rel is None:
        return {
            "error": "bad_path",
            "reason": (f"file_path {file_path!r} is not under any configured docs root ({_docs_entries!r})"),
        }

    relative_path = _fp_posix[len(docs_root_rel) + 1 :]

    if baseline_sha is None:
        baseline_sha = "HEAD~1"

    口 = Step(
        step_num=2,
        name="Resolve submodule commit at baseline",
        purpose="Use git ls-tree to find the docs submodule SHA at the baseline commit",
        critical="Assumes the matched docs root is a git submodule — fails for inline (non-submodule) docs",
    )
    try:
        result = subprocess.run(
            ["git", "ls-tree", baseline_sha, docs_root_rel],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        if result.returncode != 0:
            return {
                "error": "git_error",
                "reason": f"git ls-tree failed: {result.stderr.strip()}",
            }
        parts = result.stdout.strip().split()
        if len(parts) < 3:
            return {
                "error": "git_error",
                "reason": f"Unexpected ls-tree output: {result.stdout.strip()!r}",
            }
        submodule_sha = parts[2]
    except subprocess.TimeoutExpired:
        logger.warning("git ls-tree timed out for %s", baseline_sha)
        return {"error": "git_error", "reason": "git ls-tree timed out"}
    except Exception as exc:
        logger.warning("git ls-tree error: %s", exc)
        return {"error": "git_error", "reason": f"git ls-tree error: {exc}"}

    口 = Step(
        step_num=3,
        name="Retrieve old DocJSON via git show",
        purpose="Get the doc file content at the submodule baseline commit",
    )
    docs_dir = str(project_root / docs_root_rel)
    git_rel = relative_path.replace("\\", "/")
    try:
        result = subprocess.run(
            ["git", "show", f"{submodule_sha}:{git_rel}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=docs_dir,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "does not exist" in stderr or "exists on disk" in stderr:
                old_data: dict = {"sections": []}
            else:
                return {
                    "error": "git_error",
                    "reason": f"git show failed for old content: {stderr}",
                }
        else:
            try:
                old_data = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                return {
                    "error": "parse_error",
                    "reason": f"Old JSON is invalid: {exc}",
                }
    except subprocess.TimeoutExpired:
        logger.warning("git show timed out for %s:%s", submodule_sha, git_rel)
        return {"error": "git_error", "reason": "git show timed out"}
    except Exception as exc:
        logger.warning("git show error: %s", exc)
        return {"error": "git_error", "reason": f"git show error: {exc}"}

    口 = Step(
        step_num=4,
        name="Read current DocJSON and extract sections",
        purpose="Load current file from disk and extract section summaries from both old and new",
    )
    current_file = Path(project_root) / file_path
    if not current_file.exists():
        return {
            "error": "not_found",
            "reason": f"Current file not found: {file_path}",
        }
    try:
        new_data = json.loads(current_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "error": "parse_error",
            "reason": f"Current JSON is invalid: {exc}",
        }

    old_sections = _extract_sections(old_data)
    new_sections = _extract_sections(new_data)

    return {
        "old_sections": old_sections,
        "new_sections": new_sections,
        "baseline_sha": baseline_sha,
        "submodule_sha": submodule_sha,
    }
