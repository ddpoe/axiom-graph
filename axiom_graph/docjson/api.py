"""Public Python API for the docjson bounded context.

Per ADR-019, the docjson domain owns every behavioural primitive that
manipulates DocJSON files on disk and the corresponding rows in the
axiom-graph index.  This module is the single canonical home for those
operations; the MCP wire surface (``axiom_graph.docjson.mcp_tools``) is a
thin layer that forwards calls.

Public surface (doc operations):
    ``axiom_graph_write_doc``       -- create or update a DocJSON file
    ``axiom_graph_clone_doc``       -- copy a doc to a new one, with section overrides
    ``axiom_graph_read_doc``        -- read a DocJSON doc as Markdown
    ``axiom_graph_update_section``  -- patch a single section's fields
    ``axiom_graph_add_section``     -- add a new section to a doc
    ``axiom_graph_delete_section``  -- delete a section (and children)
    ``axiom_graph_add_link``        -- add a link from a doc section to a node
    ``axiom_graph_delete_link``     -- remove a link from a doc section
    ``axiom_graph_delete_doc``      -- delete an entire doc (file + DB)
    ``axiom_graph_update_doc_meta`` -- update a doc's title or tags
    ``axiom_graph_accept_doc_edits`` -- stamp and verify raw DocJSON edits

Public surface (helpers and diff):
    ``parse_section_id``      -- split ``proj::docs/x::sec`` into components
    ``load_doc_json``         -- look up a doc node + load its file
    ``save_and_reindex``      -- persist DocJSON dict + re-index in DB
    ``get_doc_diff``          -- old vs new doc sections vs a baseline SHA

Layering invariants (per ADR-019; enforced by ``tools/check_layering.py``):
    Allowed imports: ``axiom_graph.config``, ``axiom_graph.index.*``,
    ``axiom_graph.docjson.parse``, ``axiom_graph.docjson.render_agent``,
    and stdlib.  Never ``axiom_graph.mcp.*``.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from axiom_annotations import AutoStep, Step, task, workflow

from axiom_graph.config import AxiomGraphConfig, config_scope
from axiom_graph.docjson import parse as json_doc_scanner
from axiom_graph.scanners import node_hashing
from axiom_graph.docjson.render_agent import (
    LINKED_NODES_CLOSE,
    LINKED_NODES_OPEN,
    _render_doc_header,
    _render_doc_meta,
    _render_section_block,
    linked_nodes,
)
from axiom_graph.index import db, doc_ids, doc_stamps
from axiom_graph.index.doc_io import dumps_doc_json, lock_docs, save_doc_json
from axiom_graph.index.doc_lock import DocLockTimeout
from axiom_graph.index.git_utils import read_file_at_baseline
from axiom_graph.index.link_maintenance import LinkPatchResult, link_rewrite_warnings
from axiom_graph.index.paths import require_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# ---------------------------------------------------------------------------
# Section tree helpers (pure tree operations).
# ---------------------------------------------------------------------------


class AmbiguousSectionIdError(ValueError):
    """A section dot-path names more than one section of a doc."""


def _section_path_matches(sections: list[dict], dot_path: str) -> list[list[dict]]:
    """Return every chain of sections whose ids, joined with ``.``, spell *dot_path*.

    A section id may itself contain a ``.``, so the path is matched against
    the real ids at each level instead of being split on every dot: a
    section matches when its id is the whole remaining path, or when its id
    plus ``.`` is a prefix of it and the rest resolves among its children.

    Args:
        sections: A DocJSON ``sections`` list (possibly nested).
        dot_path: The dot-path to resolve, relative to *sections*.

    Returns:
        One chain per match, each running from a section of *sections* down
        to the matched section.  Empty when nothing matches.
    """
    matches: list[list[dict]] = []
    for sec in sections:
        sec_id = sec.get("id") if isinstance(sec, dict) else None
        if not isinstance(sec_id, str) or not sec_id:
            continue
        if sec_id == dot_path:
            matches.append([sec])
        elif dot_path.startswith(sec_id + "."):
            rest = dot_path[len(sec_id) + 1 :]
            matches.extend([sec, *chain] for chain in _section_path_matches(sec.get("sections") or [], rest))
    return matches


def _resolve_section_path(sections: list[dict], dot_path: str) -> list[dict] | None:
    """Resolve a dot-path to the chain of sections from the top level down to it.

    ``category.adr`` resolves to a top-level section of that id,
    ``parent.child`` to ``child`` nested under ``parent``, and
    ``schema.category.adr`` to ``category.adr`` nested under ``schema``.

    Args:
        sections: The top-level sections list from a DocJSON document.
        dot_path: The section's dot-path (the part of its id after the
            last ``::``).

    Returns:
        The chain of section dicts (``chain[-1]`` is the target and
        ``chain[-2]``, when present, its parent), or None if not found.

    Raises:
        AmbiguousSectionIdError: The dot-path names more than one section,
            e.g. a flat ``a.b`` and a ``b`` nested under ``a``.
    """
    matches = _section_path_matches(sections, dot_path)
    if len(matches) > 1:
        shapes = _describe_chains([[s.get("id") for s in chain] for chain in matches])
        raise AmbiguousSectionIdError(f"section id '{dot_path}' is ambiguous: it names {shapes}")
    return matches[0] if matches else None


def _describe_chains(chains: list[list]) -> str:
    """Render section id chains as ``'a' > 'b' and 'a.b'`` for an error message."""
    return " and ".join(" > ".join(f"'{sec_id}'" for sec_id in chain) for chain in chains)


def _sibling_position(sections: list[dict], chain: list[dict]) -> int:
    """Return the 1-based position of ``chain[-1]`` among its siblings."""
    siblings = (chain[-2].get("sections") or []) if len(chain) > 1 else sections
    return next(i for i, sec in enumerate(siblings, 1) if sec is chain[-1])


def _dot_path_counts(sections: list[dict]) -> dict[str, int]:
    """Return ``{dot_path: number of sections spelling it}`` for the colliding dot-paths of a doc."""
    return {dot: len(chains) for dot, chains in json_doc_scanner.section_dot_path_collisions(sections)}


def _dot_path_collision_error(sections: list[dict], before: dict[str, int] | None = None) -> str | None:
    """Return the error for a write that would give two sections one dot-path.

    Args:
        sections: The doc's top-level ``sections`` as the write would leave
            them.
        before: :func:`_dot_path_counts` of the doc before the write, so a
            collision already in the file does not block an unrelated write.
            ``None`` when the write supplies the whole doc.

    Returns:
        An ``ERROR: ...`` string naming the first dot-path the write gives to
        more sections than before, and every section that spells it; or
        ``None`` when the write adds no collision.
    """
    for dot, chains in json_doc_scanner.section_dot_path_collisions(sections):
        if before is not None and len(chains) <= before.get(dot, 1):
            continue
        id_chains = [[sec.get("id") for sec in chain] for chain in chains]
        shapes = [_describe_chains([ids]) for ids in id_chains]
        # Two siblings sharing an id render the same: add each one's 1-based
        # position among its siblings and its heading to tell them apart.
        named = [
            f'{shape} (position {_sibling_position(sections, chain)}: "{chain[-1].get("heading", "")}")'
            if shapes.count(shape) > 1
            else shape
            for shape, chain in zip(shapes, chains)
        ]
        return (
            f"ERROR: section id '{dot}' would name more than one section: {' and '.join(named)} -- "
            "each section needs its own dot-path; nothing was written"
        )
    return None


def _find_section_in_tree(
    sections: list[dict],
    dot_path: str,
) -> dict | None:
    """Find a section by dot-path ID in a nested sections tree.

    The dot-path is resolved against the real section ids (see
    :func:`_resolve_section_path`), so an id that itself contains a ``.``
    is found as well as a nested ``parent.child``.

    Args:
        sections: The top-level sections list from a DocJSON document.
        dot_path: Dot-separated section ID path (e.g. ``"parent.child"``).

    Returns:
        The matching section dict, or None if not found.

    Raises:
        AmbiguousSectionIdError: The dot-path names more than one section.
    """
    chain = _resolve_section_path(sections, dot_path)
    return chain[-1] if chain else None


def _locate_section(sections: list[dict], dot_path: str, not_found: str) -> list[dict] | str:
    """Resolve a write tool's target section, or return the tool's error.

    Args:
        sections: The top-level sections list from a DocJSON document.
        dot_path: The target's dot-path.
        not_found: The ``ERROR: ...`` string to return when nothing matches.

    Returns:
        The chain from :func:`_resolve_section_path`, or an ``ERROR: ...``
        string when the section is missing or the dot-path is ambiguous.
    """
    try:
        chain = _resolve_section_path(sections, dot_path)
    except AmbiguousSectionIdError as exc:
        return f"ERROR: {exc}"
    return chain if chain else not_found


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


def docs_roots_for(root: Path) -> list[str]:
    """Return the configured docs roots for *root*, as written in the config.

    Args:
        root: Project root directory.

    Returns:
        ``config.scan.docs_dirs`` entries, defaulting to ``["docs"]`` when the
        config cannot be loaded or names none.

    Note:
        The fallback is deliberately silent-but-logged rather than fatal: a
        section operation must still resolve when the config is unreadable.
        It is not harmless, though -- falling back to the primary root alone
        excludes every non-primary root, so a section under ``.pev`` stops
        resolving.  That is the failure this function warns about, so an
        operator sees the cause rather than an unexplained parse error.
    """
    try:
        cfg = AxiomGraphConfig.load(root)
    except Exception as exc:  # pragma: no cover - unreadable config
        logger.warning(
            "could not load axiom-graph config at %s (%s); falling back to the "
            "'docs' root alone — section ids under any other configured docs "
            "root will not resolve until the config is readable",
            root,
            exc,
        )
        return ["docs"]
    return list(cfg.scan.docs_dirs or ["docs"])


def parse_section_id(section_id: str, docs_roots: list[str]) -> tuple[str, str, str, str] | str:
    """Parse a full qualified section ID into its components.

    A section ID is ``{project}::{doc body}::{section dot-path}`` — exactly
    two ``::`` however deep the document sits.  The middle segment is
    recognised as a *document* by resolving it against the project's
    configured docs roots, not by looking for punctuation: a code node's ID
    has the same ``::`` arity and must still be refused.

    Args:
        section_id: Full qualified section ID.
        docs_roots: ``config.scan.docs_dirs`` for the project the ID belongs
            to.  Use :func:`docs_roots_for` to obtain them.

    Returns:
        A tuple of (project_part, doc_path_slug, sec_raw_id, doc_node_id)
        on success, or an error string on failure.  ``doc_path_slug`` is the
        document's path within its docs root.
    """
    parts = section_id.split("::")
    if len(parts) != 3:
        return (
            f"ERROR: section_id must be '{{project}}::{{doc}}::{{section}}' "
            f"({len(parts) - 1} '::' separator(s) found) -- got '{section_id}'"
        )
    project_part, doc_body, sec_raw_id = parts
    prefixes = doc_ids.doc_id_root_prefixes(docs_roots)
    matched = next((p for p in prefixes if doc_body.startswith(p)), None)
    if matched is None:
        return (
            f"ERROR: '{section_id}' does not name a document section -- "
            f"'{doc_body}' is under none of the configured docs roots "
            f"({', '.join(docs_roots) or 'none'})"
        )
    if not sec_raw_id:
        return f"ERROR: section_id has no section suffix after doc id -- got '{section_id}'"
    doc_path_slug = doc_body[len(matched) :]
    doc_node_id = f"{project_part}::{doc_body}"
    return (project_part, doc_path_slug, sec_raw_id, doc_node_id)


# ---------------------------------------------------------------------------
# Doc JSON I/O
# ---------------------------------------------------------------------------


def load_doc_json(db_path: Path, root: Path, doc_node_id: str) -> tuple[dict, Path, "db.AxiomNode"] | str:
    """Load a DocJSON file by looking up the doc node in the DB.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        doc_node_id: The doc node ID (e.g. ``proj::docs/arch``).

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


#: Edge types the DocJSON scanner derives from a doc file.  Only these are
#: reconciled against a fresh scan; any other edge type touching a doc node
#: belongs to some other producer and is never dropped by a doc write.
_DOC_SCANNED_EDGE_TYPES = ("composes", "documents")


def _section_contents(sections: list[dict], prefix: str | None = None) -> dict[str, str]:
    """Flatten a DocJSON sections tree into ``{dot_path: content}``.

    Args:
        sections: A DocJSON ``sections`` list (possibly nested).
        prefix: Dot-path of the parent (``None`` at the top level).

    Returns:
        Mapping of every section's dot-path to its content (``""`` when
        absent).  Malformed entries (non-dicts, missing ids) are skipped,
        and a dot-path two sections spell keeps the first in document order
        (see :func:`axiom_graph.index.doc_stamps.flatten_section_dicts`).
    """
    return {dot: sec.get("content") or "" for dot, sec in doc_stamps.flatten_section_dicts(sections, prefix).items()}


def _doc_section_ids_conn(conn: sqlite3.Connection, doc_node_id: str) -> set[str]:
    """Return the ids of every section row this doc owns, by exact prefix.

    ``LIKE`` treats ``_`` (common in ids such as ``pev_nexus_agents``) as a
    wildcard, so a ``LIKE '{doc}::%'`` set can include another doc's rows.
    Comparing a literal prefix with ``substr`` cannot.

    Args:
        conn: Open SQLite connection.
        doc_node_id: The doc envelope id.

    Returns:
        Section node ids that start with ``{doc_node_id}::``.
    """
    prefix = f"{doc_node_id}::"
    rows = conn.execute("SELECT id FROM nodes WHERE substr(id, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    return {r["id"] for r in rows}


def _move_section_identities(conn: sqlite3.Connection, moves: dict[str, str], file_path: str) -> None:
    """Move renamed section rows, history, verification and edges to new ids.

    Every new ``nodes`` row is materialised before anything moves onto it:
    ``node_verification.node_id`` is a foreign key onto ``nodes(id)`` with
    ``ON DELETE CASCADE``, so verification can only move onto an identity
    that already exists.  The old rows are retired last.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        moves: Old section node id -> new section node id, including every
            cascaded descendant of a renamed parent.
        file_path: Repo-relative doc path recorded in the ``node_renames``
            ledger.
    """
    now = db._now_utc()
    for old_id, new_id in moves.items():
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (old_id,)).fetchone()
        if row is None:
            continue
        columns = list(row.keys())
        values = [new_id if col == "id" else row[col] for col in columns]
        conn.execute(
            f"INSERT OR REPLACE INTO nodes ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
            values,
        )
        conn.execute(
            "INSERT OR IGNORE INTO tags (node_id, tag) SELECT ?, tag FROM tags WHERE node_id = ?", (new_id, old_id)
        )
        conn.execute("DELETE FROM node_fts WHERE id = ?", (new_id,))
        conn.execute(
            "INSERT INTO node_fts (id, level_1, level_2) SELECT ?, level_1, level_2 FROM node_fts WHERE id = ?",
            (new_id, old_id),
        )
    for old_id, new_id in moves.items():
        conn.execute(
            "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
            (old_id, new_id, now, file_path),
        )
        conn.execute("UPDATE node_history SET node_id = ? WHERE node_id = ?", (new_id, old_id))
        conn.execute("DELETE FROM node_verification WHERE node_id = ?", (new_id,))
        conn.execute("UPDATE OR IGNORE node_verification SET node_id = ? WHERE node_id = ?", (new_id, old_id))
        db._migrate_edges(conn, old_id, new_id)
        db.rekey_verification_targets_conn(conn, old_id, new_id)
    for old_id in moves:
        conn.execute("DELETE FROM tags WHERE node_id = ?", (old_id,))
        conn.execute("DELETE FROM node_fts WHERE id = ?", (old_id,))
        conn.execute("DELETE FROM node_verification WHERE node_id = ?", (old_id,))
        conn.execute("DELETE FROM nodes WHERE id = ?", (old_id,))


def _retire_vanished_sections(conn: sqlite3.Connection, doc_node_id: str, vanished: set[str]) -> None:
    """Delete the index rows of sections that no longer exist in the doc.

    ``node_history`` is kept (it is an audit trail).  Inbound ``documents``
    edges from *other* docs are kept too, so the next check flags their
    source BROKEN_LINK instead of the link silently vanishing.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        doc_node_id: The doc envelope id that owned the sections.
        vanished: Section node ids to retire.
    """
    if not vanished:
        return
    ids = sorted(vanished)
    ph = ",".join("?" * len(ids))
    prefix = f"{doc_node_id}::"
    conn.execute(f"DELETE FROM tags WHERE node_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM node_fts WHERE id IN ({ph})", ids)
    conn.execute(f"DELETE FROM node_verification WHERE node_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM edges WHERE from_id IN ({ph})", ids)
    conn.execute(
        f"""
        DELETE FROM edges
        WHERE to_id IN ({ph})
          AND NOT (edge_type = 'documents' AND from_id != ? AND substr(from_id, 1, ?) != ?)
        """,
        [*ids, doc_node_id, len(prefix), prefix],
    )
    conn.execute(f"DELETE FROM nodes WHERE id IN ({ph})", ids)


def _reconcile_outbound_edges(conn: sqlite3.Connection, scanned_ids: set[str], scanned_edge_ids: set[str]) -> None:
    """Drop scanner-derived edges from this doc's nodes that the fresh scan no longer produces.

    Only edges whose source is one of this doc's freshly scanned nodes are
    considered -- an edge owned by another doc is never touched.  A removed
    ``documents`` edge records ``LINK_REMOVED`` history.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        scanned_ids: Node ids the scan of this doc produced.
        scanned_edge_ids: Edge ids the scan of this doc produced.
    """
    if not scanned_ids:
        return
    ids = sorted(scanned_ids)
    ph = ",".join("?" * len(ids))
    types_ph = ",".join("?" * len(_DOC_SCANNED_EDGE_TYPES))
    rows = conn.execute(
        f"SELECT id, edge_type, from_id, to_id FROM edges WHERE from_id IN ({ph}) AND edge_type IN ({types_ph})",
        [*ids, *_DOC_SCANNED_EDGE_TYPES],
    ).fetchall()
    for r in rows:
        if r["id"] in scanned_edge_ids:
            continue
        if r["edge_type"] == "documents":
            db.delete_documents_edge_conn(conn, r["from_id"], r["to_id"], actor="agent")
        else:
            conn.execute("DELETE FROM edges WHERE id = ?", (r["id"],))


class AddressesError(ValueError):
    """An ``addresses=`` name is not a current offender of its section; the message is the tool's ``ERROR:``."""


@dataclass
class _PreWrite:
    """A doc's state just before a write: its text, its sections, and the open offenders of what the write verifies."""

    pre_text: str | None
    pre_sections: dict[str, str]
    pre_dicts: dict[str, dict]
    offenders: dict
    addressed: dict[str, list[str]]


def _prepare_write(
    data: dict,
    json_file: Path,
    db_path: Path,
    root: Path,
    doc_node_id: str | None,
    renames: dict[str, str] | None,
    targets: set[str] | None,
    addresses: dict[str, list[str]] | None,
    pre_text: str | None = None,
) -> _PreWrite:
    """Snapshot a doc before a write and validate its ``addresses``; writes nothing.

    Reads the file as it is on disk (unless *pre_text* is given) and parses
    it once, reads the open offenders of the linked sections the write
    verifies after one scoped refresh (:func:`_pre_write_offenders`), and
    validates every ``addresses`` name against them.

    Args:
        data: The DocJSON dict about to be written.
        json_file: The doc's file.
        db_path: Path to the axiom-graph DB.
        root: Project root.
        doc_node_id: The doc's node id.
        renames: Old dot-path -> new dot-path.
        targets: Dot-paths (after renames) the call writes content-wise.
        addresses: Dot-path (after renames) -> the offenders the call names.
        pre_text: The file's text, when the caller already read it under the
            doc's lock.

    Returns:
        The :class:`_PreWrite` snapshot.

    Raises:
        AddressesError: A name is not a current offender of its section.
    """
    if pre_text is None and json_file.exists():
        pre_text = json_file.read_text(encoding="utf-8", errors="replace")
    pre_dicts: dict[str, dict] = {}
    if pre_text is not None:
        try:
            pre_dicts = doc_stamps.flatten_section_dicts(json.loads(pre_text).get("sections") or [])
        except (json.JSONDecodeError, AttributeError):
            pre_dicts = {}
    pre_sections = {dot: sec.get("content") or "" for dot, sec in pre_dicts.items()}
    renames = renames or {}
    addressed = {dot: sorted(set(names)) for dot, names in (addresses or {}).items() if names}
    offenders = _pre_write_offenders(
        db_path, root, doc_node_id, data, pre_dicts, renames, targets or set(), set(addressed)
    )
    _validate_addresses(doc_node_id, addressed, renames, offenders)
    return _PreWrite(pre_text, pre_sections, pre_dicts, offenders, addressed)


@task(
    purpose="Write a DocJSON doc and re-index it, moving renamed sections, retiring deleted ones, leaving untouched "
    "sections' verification, history and edges alone, stamping the sections the write created or edited and "
    "verifying their text (never a link status), and running the raw-edit classifier over the untouched sections",
    inputs="DocJSON dict, json_file, db_path, project root, project_id, cleanup flag, verifier, doc id, section renames",
    outputs="Section ids recorded as raw DocJSON edits (side effects: file written with tool-write stamps; index rows "
    "upserted, moved, retired; verification for created/edited nodes)",
)
@node_hashing.one_parse_per_file
def save_and_reindex(
    data: dict,
    json_file: Path,
    db_path: Path,
    root: Path,
    project_id: str,
    cleanup_doc_node_id: str | None = None,
    verified_by: str = "agent",
    doc_node_id: str | None = None,
    renames: dict[str, str] | None = None,
    targets: set[str] | None = None,
    link_targets: set[str] | None = None,
    verification_op: str | None = None,
    addresses: dict[str, list[str]] | None = None,
    prepared: _PreWrite | None = None,
    link_patches: list[LinkPatchResult] | None = None,
) -> list[str]:
    """Stamp, write and re-index a doc, preserving and raw-edit-checking what the write did not touch.

    The pipeline compares the doc's sections before the write (read from the
    file on disk) with the sections being written, and treats each one by
    what happened to it:

    - **renamed** (``renames``) -- the section's ``nodes`` row, tags, FTS,
      history, verification and edges move to the new id, and other docs'
      on-disk links to it are rewritten.
    - **vanished** (only when ``cleanup_doc_node_id`` is set) -- its rows,
      tags, FTS, verification and edges are removed; history is kept, and so
      are inbound ``documents`` edges from other docs.
    - **surviving** -- upserted in place by the discovery-mode upsert, so
      its baseline, verification, history and edges are untouched.

    Scanner-derived edges from this doc's nodes that the fresh scan no
    longer produces (an unlinked target, a moved child) are dropped.

    The write verifies the text it wrote, never a link: every section the
    write **created**, or whose content it **edited**, gets a text-only
    verification (:func:`axiom_graph.index.mark_clean.verify_text_conn`), as
    does the doc composite when the file bytes changed and no untouched
    section has drifted from its baseline (an already-stale section keeps the
    composite unverified, so it is not reported VERIFIED by the next check).
    A text-only verification resets the own status only: receipts,
    ``verified_at`` and every link status stay as they were, so no
    LINKED_STALE clears -- not the edited section's, not its doc's, not any
    other node's.  A section whose verification row the write has to create
    pins its open offenders that hold no receipt, read after one scoped
    pre-write refresh of its cone, so the new row's time settles none of
    them.  Untouched sections, link-only changes and pure renames are never
    verified -- a pure rename keeps the verification it moved.

    Tool-write stamps (:mod:`axiom_graph.index.doc_stamps`): every section
    the write created, targeted (*targets*) or whose heading/content it
    changed is stamped with its hash and a ``verified_against`` that mirrors
    its receipts: each linked code node at its current hash, except an
    offender the write left open, which keeps the previous stamp's entry
    (dropped when there was none); a named offender (*addresses*) takes its
    current hash.  A targeted section whose stamp was missing or mismatched
    before the write (a raw DocJSON edit being re-applied) gets the same
    text-only verification, even when its content is unchanged.  A link-only target
    (*link_targets*) has its stamp hash refreshed only when its stamp was
    valid, and is never verified.  Every other section keeps the stamp it
    had on disk -- a stamp in the incoming payload is never trusted -- and
    goes through the raw-edit classifier, which catches a hand edit this
    write would otherwise absorb into the index.

    Callers hold the doc's write lock (:func:`~axiom_graph.index.doc_io.lock_docs`) around the
    load-mutate-save sequence.

    Args:
        data: The DocJSON dict to write.
        json_file: Path to the JSON file on disk.
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        project_id: Project ID prefix for the scanner.
        cleanup_doc_node_id: If provided, sections that no longer exist in
            *data* lose their index rows (for deletes and renames).
        verified_by: Verifier identifier recorded on the sections' text
            verifications (``verify_text_conn``).  Defaults to ``"agent"``.  Callers
            invoking this on behalf of a human (e.g. CLI doc-write) should
            override to ``"human"``.
        doc_node_id: Optional doc node id (e.g. ``proj::docs/arch``).  Taken
            from the scan when omitted; required for ``renames``.
        renames: Old section dot-path -> new section dot-path, including every
            cascaded descendant of a renamed parent.
        targets: Dot-paths (after renames) the call wrote content-wise --
            stamped, and text-verified (``verify_text_conn``) also when
            their pre-write stamp was not valid.
        link_targets: Dot-paths whose links the call changed.
        verification_op: Provenance recorded on the sections' text
            verifications (``doc_edit`` when ``None``).
        addresses: Dot-path (after renames) -> the offenders the call names
            for that section (``addresses=``).  Validated against the
            section's current offenders, read after the pre-write refresh
            and before anything is written; each named receipt target's
            receipt is then recorded at its index-live hash.
        prepared: The pre-write snapshot (:func:`_prepare_write`) when the
            caller already took it under the doc's lock -- a batch validates
            every doc's ``addresses`` before it writes the first file.  Taken
            here when ``None``.
        link_patches: When given, a rename's link-rewrite result is
            appended to it, so the caller can report the files the rewrite
            could not check (unreadable) apart from those it could not
            rewrite (write locks busy).

    Raises:
        AddressesError: A name is not a current offender; nothing was written.

    Returns:
        Section ids the raw-edit classifier recorded as raw DocJSON edits at
        this write (empty when none, or under ``raw_docjson_edits = "off"``).
    """
    from axiom_graph.index.builder import _matched_docs_dir  # noqa: PLC0415
    from axiom_graph.index.link_maintenance import patch_doc_links_batch  # noqa: PLC0415
    from axiom_graph.index.mark_clean import VERIFICATION_OP_DOC_EDIT, PairRecorder, verify_text_conn  # noqa: PLC0415

    口 = Step(
        step_num=1,
        name="Snapshot the doc as it is on disk",
        purpose="Read the pre-write file so created, edited and untouched sections can be told apart, read the open "
        "offenders of the linked sections it verifies after one scoped refresh, validate addresses, and stamp the "
        "sections this write touched with their hash and a verified_against mirroring their receipts",
        critical="An offender the write leaves open keeps its previous stamp entry (or none), so a merge carrying "
        "the stamp never verifies what the edit did not; an invalid addresses name raises before anything is written",
    )
    renames = renames or {}
    if prepared is None:
        prepared = _prepare_write(data, json_file, db_path, root, doc_node_id, renames, targets, addresses)
    pre_text, pre_sections, pre_dicts = prepared.pre_text, prepared.pre_sections, prepared.pre_dicts
    offenders, addressed = prepared.offenders, prepared.addressed
    stamped, force_mark = _stamp_touched_sections(
        db_path,
        root,
        data,
        pre_dicts,
        renames,
        targets or set(),
        link_targets or set(),
        doc_id=doc_node_id,
        offenders=offenders,
        addressed=addressed,
    )

    口 = Step(
        step_num=2,
        name="Write the file",
        purpose="Persist the mutated DocJSON dict atomically (temp file + replace)",
        critical="A failed write leaves the file as it was, never truncated",
    )
    save_doc_json(json_file, data)

    口 = Step(
        step_num=3,
        name="Move renamed section identities",
        purpose="Migrate rows, history, verification and edges of renamed sections, and patch other docs' links",
        critical="New nodes rows must exist before verification moves (FK with ON DELETE CASCADE)",
    )
    moves: dict[str, str] = {}
    if renames:
        if doc_node_id is None:
            raise ValueError("save_and_reindex: renames require doc_node_id")
        moves = {f"{doc_node_id}::{old}": f"{doc_node_id}::{new}" for old, new in renames.items() if old != new}
        rel_path = json_file.relative_to(root).as_posix()
        with db._connect(db_path) as conn:
            _move_section_identities(conn, moves, rel_path)
        patched = patch_doc_links_batch(root, moves)
        if link_patches is not None:
            link_patches.append(patched)

    口 = Step(step_num=4, name="Scan and upsert", purpose="Re-scan the doc and upsert its nodes, edges and doc record")
    _matched_dd = _matched_docs_dir(json_file, root)
    # The scan is kept for this write's later hash lookups of the file (the
    # verification block and the closing refresh), so the file is parsed once.
    nodes, edges, doc_recs, _sec_recs = node_hashing.scan_docjson_file(json_file, root, project_id, _matched_dd)
    doc_id = doc_node_id or (nodes[0].id if nodes else None)
    scanned_ids = {n.id for n in nodes}
    with db._connect(db_path) as conn:
        stored_text = doc_stamps.stored_section_texts(
            conn, [n.id for n in nodes if getattr(n, "subtype", None) == "docjson_section"]
        )
        for node in nodes:
            db.upsert_node_conn(conn, node)
        db.upsert_edges_conn(conn, edges)
        for rec in doc_recs:
            db.upsert_doc(conn, rec)

        口 = Step(
            step_num=5,
            name="Reconcile vanished sections and outbound edges",
            purpose="Retire rows of deleted sections; drop this doc's edges the scan no longer produces",
            critical="Never touches rows or edges owned by another doc",
        )
        vanished: set[str] = set()
        if cleanup_doc_node_id is not None and doc_id is not None:
            vanished = _doc_section_ids_conn(conn, doc_id) - scanned_ids
            _retire_vanished_sections(conn, doc_id, vanished)
        _reconcile_outbound_edges(conn, scanned_ids, {e.id for e in edges})

    口 = Step(
        step_num=6,
        name="Verify the text of created and edited nodes",
        purpose="One text-only verification per section the write created or edited (and per re-applied raw edit), "
        "and of the doc composite, on one connection: the own status is reset while receipts, verified_at and every "
        "link status stay as they were; untouched, link-only and pure-rename nodes are not verified, and untouched "
        "sections go through the raw-edit classifier instead",
        critical="A doc write clears no LINKED_STALE anywhere; a verification row it has to create pins the section's "
        "open offenders that hold no receipt",
    )
    inverse = {new: old for old, new in renames.items()}
    prefix = f"{doc_id}::"
    composite = None
    to_verify: list = []
    for n in nodes:
        if n.id == doc_id:
            if pre_text is None or pre_text != json_file.read_text(encoding="utf-8", errors="replace"):
                composite = n
            continue
        dot = n.id[len(prefix) :]
        source = inverse.get(dot, dot)
        if (
            source not in pre_sections
            or pre_sections[source] != (n.level_2 or "")
            or dot in force_mark
            or dot in addressed
        ):
            to_verify.append(n)
    # The composite covers the whole file, so the writer verifies it only when
    # no untouched section carries unverified drift (e.g. a hand edit).  A
    # re-baselined composite would also let staleness's whole-file fast pass
    # report that drifted section VERIFIED.
    verifying = {n.id for n in to_verify}
    if composite is not None and not _sections_off_baseline(
        db_path, [n for n in nodes if n.id != doc_id and n.id not in verifying]
    ):
        to_verify.append(composite)
    if to_verify:
        # One recorder for the block: the text verifications read only their
        # own hashes (from the disk in either mode), the named receipts read
        # their targets from the index, and the doc file is parsed once.
        recorder = index_pairs = PairRecorder(root, from_index=True)
        with db._connect(db_path) as conn:
            named_nodes = [n for n in to_verify if n.id != doc_id and n.id[len(prefix) :] in addressed]
            receipts: dict[str, dict] = {}
            if named_nodes:
                # Read before any write of this block, from the index the
                # pre-write refresh brought current -- never the disk (an
                # edit nobody has built is never absorbed).
                index_pairs.prepare(conn, named_nodes)
                for n in named_nodes:
                    dot = n.id[len(prefix) :]
                    found = offenders.get(f"{prefix}{inverse.get(dot, dot)}")
                    refreshable = set(addressed[dot]) & (found.receipt_targets if found else frozenset())
                    live = index_pairs.pairs_for(conn, n.id) or {}
                    receipts[n.id] = {t: live[t] for t in sorted(refreshable) if t in live}
            for n in to_verify:
                is_section = n.id != doc_id
                dot = n.id[len(prefix) :]
                found = offenders.get(f"{prefix}{inverse.get(dot, dot)}") if is_section else None
                if found is None:
                    open_targets: frozenset[str] | set[str] = frozenset()
                elif found.carried and not found.vias:
                    # A frozen section carrying LINKED_STALE names no offender,
                    # so every link stays open, as its stamp treats them: a new
                    # row's time must not settle the cause the carry needs.
                    open_targets = recorder.dependency_ids(conn, n.id)
                else:
                    open_targets = found.receipt_targets
                verify_text_conn(
                    conn,
                    recorder,
                    n,
                    reason="auto: docjson write",
                    verified_by=verified_by,
                    verification_op=(verification_op or VERIFICATION_OP_DOC_EDIT)
                    if is_section
                    else VERIFICATION_OP_DOC_EDIT,
                    open_targets=open_targets,
                    receipts=receipts.get(n.id),
                    addresses=addressed.get(dot, ()) if is_section else (),
                )

    # Sections this write did not touch: the same classifier the build runs.
    # The upsert above absorbed their current text into the index, so a hand
    # edit in one of them is caught here or not at all.
    untouched = [n for n in nodes if n.id != doc_id and n.id[len(prefix) :] not in stamped]
    items = doc_stamps.section_inputs(
        untouched, doc_stamps.flatten_section_dicts(data.get("sections") or []), doc_id or "", stored_text
    )
    rel_path = json_file.relative_to(root).as_posix() if _is_under(json_file.resolve(), root) else str(json_file)
    raw_edits = doc_stamps.reconcile_sections(db_path, root, items, file_path=rel_path).raw_edits

    口 = Step(
        step_num=7,
        name="Refresh what the write can move",
        purpose="One scoped refresh over the sections the write re-indexed, retired or renamed away from, so their "
        "statuses and their dependents' are stored now rather than at the next check",
        critical="Runs after every verification the write makes (text verification, tool-stamp), so the stored statuses "
        "read them; never moves the check's journal watermark",
    )
    from axiom_graph.index.refresh import refresh_after_write  # noqa: PLC0415

    refresh_after_write(db_path, root, scanned_ids | vanished | set(moves), config=AxiomGraphConfig.load(root))
    return raw_edits


def _stamp_verified_against(
    linked: list[str],
    current: dict[str, str | None],
    previous: dict,
    found,
    named: set[str],
) -> dict[str, str | None]:
    """The ``verified_against`` a verifying write records: a mirror of the section's receipts.

    Every linked code node gets its current hash, except an open offender
    the write did not name, which keeps the previous stamp's entry (dropped
    when there was none).  A frozen section carrying LINKED_STALE has no
    offender list, so every linked code node counts as open.

    Args:
        linked: The section's linked node ids.
        current: Current code hashes (code targets only).
        previous: The pre-write stamp's ``verified_against`` (``{}`` when none).
        found: The section's :class:`~axiom_graph.index.staleness.CurrentOffenders`
            before the write, or ``None`` when it has none.
        named: The offenders the call named in ``addresses``.

    Returns:
        Code node id -> the hash to record.
    """
    if found is None:
        open_ids: set[str] = set()
    elif found.carried and not found.vias:
        open_ids = set(linked)
    else:
        open_ids = set(found.names)
    open_ids -= named
    out: dict[str, str | None] = {}
    for nid in linked:
        if nid not in current:
            continue
        if nid not in open_ids:
            out[nid] = current[nid]
        elif nid in previous:
            out[nid] = previous[nid]
    return out


def _stamp_touched_sections(
    db_path: Path,
    root: Path,
    data: dict,
    pre_dicts: dict[str, dict],
    renames: dict[str, str],
    targets: set[str],
    link_targets: set[str],
    doc_id: str | None = None,
    offenders: dict | None = None,
    addressed: dict[str, list[str]] | None = None,
) -> tuple[set[str], set[str]]:
    """Write tool-write stamps onto the sections a write touched.

    A verifying stamp (created, targeted or text-changed section) mirrors the
    section's receipts (:func:`_stamp_verified_against`): an offender the
    write left open keeps its previous entry, so a merge carrying the stamp
    never verifies what the edit did not.  The current hashes of every
    section stamped are read in one batched lookup.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.
        data: The DocJSON dict about to be written (mutated in place).
        pre_dicts: The file's sections before the write, by dot-path.
        renames: Old dot-path -> new dot-path.
        targets: Dot-paths the call wrote content-wise.
        link_targets: Dot-paths whose links the call changed.
        doc_id: The doc's node id (keys *offenders*).
        offenders: Pre-write section id -> its current offenders, read after
            the pre-write refresh.
        addressed: Dot-path (after renames) -> the offenders the call names.

    Returns:
        ``(stamped, force_mark)`` -- dot-paths that received a stamp, and the
        subset to text-verify because their pre-write stamp was not valid.
    """
    inverse = {new: old for old, new in renames.items()}
    offenders = offenders or {}
    addressed = addressed or {}
    stamped: set[str] = set()
    force_mark: set[str] = set()
    flat = doc_stamps.flatten_section_dicts(data.get("sections") or [])
    verifying: set[str] = set()
    for dot, sec in flat.items():
        pre = pre_dicts.get(inverse.get(dot, dot))
        text_changed = pre is None or (pre.get("heading") or "", pre.get("content") or "") != (
            sec.get("heading") or "",
            sec.get("content") or "",
        )
        if dot in targets or text_changed:
            verifying.add(dot)
    wanted = [nid for dot in sorted(verifying) for nid in doc_stamps.linked_node_ids(flat[dot])]
    all_hashes = doc_stamps.current_code_hashes(db_path, root, wanted) if wanted else {}
    for dot, sec in flat.items():
        pre = pre_dicts.get(inverse.get(dot, dot))
        created = pre is None
        pre_state = doc_stamps.STAMP_MISSING if created else doc_stamps.stamp_state(pre)
        links_changed = not created and (pre.get("links") or []) != (sec.get("links") or [])
        if dot in verifying:
            previous_stamp = (pre or {}).get(doc_stamps.STAMP_KEY)
            previous = previous_stamp.get("verified_against") if isinstance(previous_stamp, dict) else None
            found = offenders.get(f"{doc_id}::{inverse.get(dot, dot)}") if doc_id and not created else None
            hashes = _stamp_verified_against(
                doc_stamps.linked_node_ids(sec),
                all_hashes,
                previous if isinstance(previous, dict) else {},
                found,
                set(addressed.get(dot, ())),
            )
            sec[doc_stamps.STAMP_KEY] = doc_stamps.make_stamp(sec, hashes)
            stamped.add(dot)
            if not created and pre_state != doc_stamps.STAMP_VALID:
                force_mark.add(dot)
        elif (links_changed or dot in link_targets) and pre_state == doc_stamps.STAMP_VALID:
            # A link change is not a verifying write: refresh the hash, keep
            # what the section was verified against.
            previous = pre.get(doc_stamps.STAMP_KEY) or {}
            sec[doc_stamps.STAMP_KEY] = doc_stamps.make_stamp(sec, previous.get("verified_against") or {})
            stamped.add(dot)
        elif pre is not None and doc_stamps.STAMP_KEY in pre:
            sec[doc_stamps.STAMP_KEY] = pre[doc_stamps.STAMP_KEY]
        else:
            sec.pop(doc_stamps.STAMP_KEY, None)
    return stamped, force_mark


def _raw_edit_note(ids: list[str]) -> str:
    """Result-text note for raw DocJSON edits a write found in untouched sections."""
    return "\n" + doc_stamps.raw_docjson_edit_summary(ids) if ids else ""


def _pre_write_offenders(
    db_path: Path,
    root: Path,
    doc_id: str | None,
    data: dict,
    pre_dicts: dict[str, dict],
    renames: dict[str, str],
    targets: set[str],
    addressed: set[str] = frozenset(),
) -> dict:
    """Bring current the cone of the linked sections a write verifies, then read their open offenders.

    Only sections that existed before the write, had links, and are written
    content-wise (*targets*, or a changed heading or content) are looked at:
    a new section has no offenders, and a section with no links has none of
    its own.  One scoped refresh (the watermark is not moved) and one scoped
    link phase over the batch; nothing at all when no such section exists.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.
        doc_id: The doc's node id.
        data: The DocJSON dict about to be written.
        pre_dicts: The file's sections before the write, by dot-path.
        renames: Old dot-path -> new dot-path.
        targets: Dot-paths (after renames) the call wrote content-wise.
        addressed: Dot-paths (after renames) the call names offenders for;
            always looked at.

    Returns:
        Pre-write section id -> its
        :class:`~axiom_graph.index.staleness.CurrentOffenders`; sections with
        none are omitted.
    """
    from axiom_graph.index.refresh import refresh_before_write  # noqa: PLC0415
    from axiom_graph.index.staleness import current_offenders  # noqa: PLC0415

    if doc_id is None:
        return {}
    inverse = {new: old for old, new in renames.items()}
    ids: set[str] = set()
    for dot, sec in doc_stamps.flatten_section_dicts(data.get("sections") or []).items():
        source = inverse.get(dot, dot)
        pre = pre_dicts.get(source)
        if pre is None:
            continue
        if dot in addressed:
            ids.add(f"{doc_id}::{source}")
            continue
        if not doc_stamps.linked_node_ids(pre):
            continue
        text_changed = (pre.get("heading") or "", pre.get("content") or "") != (
            sec.get("heading") or "",
            sec.get("content") or "",
        )
        if dot in targets or text_changed:
            ids.add(f"{doc_id}::{source}")
    if not ids:
        return {}
    config = AxiomGraphConfig.load(root)
    refresh_before_write(db_path, root, ids, config=config)
    return current_offenders(
        db_path,
        root,
        ids,
        transitive_tags=config.staleness.transitive_tags,
        frozen_tags=config.staleness.frozen_tags,
    )


def _validate_addresses(
    doc_id: str | None, addressed: dict[str, list[str]], renames: dict[str, str], offenders: dict
) -> None:
    """Refuse a call naming anything that is not a current offender of its section.

    Args:
        doc_id: The doc's node id.
        addressed: Dot-path (after renames) -> the names given.
        renames: Old dot-path -> new dot-path.
        offenders: Pre-write section id -> its current offenders.

    Raises:
        AddressesError: Naming the section, the names that are not offenders,
            and its current offenders; a section carried LINKED_STALE on a
            frozen doc also gets the hint that only ``mark_clean`` clears it.
    """
    inverse = {new: old for old, new in renames.items()}
    for dot, names in sorted(addressed.items()):
        section_id = f"{doc_id}::{inverse.get(dot, dot)}"
        found = offenders.get(section_id)
        valid = found.names if found else []
        bad = [name for name in names if name not in valid]
        if bad:
            # A section carried LINKED_STALE on a frozen doc names no
            # offender an edit can clear; only mark_clean clears it.
            carried = " (LINKED_STALE carried on a frozen doc; mark_clean clears it)" if found and found.carried else ""
            raise AddressesError(
                f"ERROR: addresses must name current offenders of {section_id}; not offenders: {', '.join(bad)}. "
                f"Current offenders: {', '.join(valid) if valid else 'none'}{carried}. Nothing was written."
            )


#: How many vias a ``still LINKED_STALE`` line lists before ``(+N more)``.
_STILL_STALE_CAP = 10


def _still_linked_stale_note(db_path: Path, root: Path, section_id: str) -> str:
    """The reply line saying a section the write left is still LINKED_STALE, and through what.

    Read after the closing refresh, so it is what ``check --full`` stores.
    Empty when the section is not LINKED_STALE.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.
        section_id: The section as it stands after the write.

    Returns:
        ``"\n  still LINKED_STALE via: ..."`` (a chain via carries
        ``(clears when it does)``), the frozen or inherited form, or ``""``.
    """
    note = _still_linked_stale_notes(db_path, root, [section_id]).get(section_id)
    return f"\n  {note}" if note else ""


def _still_linked_stale_notes(db_path: Path, root: Path, section_ids: list[str]) -> dict[str, str]:
    """``still LINKED_STALE ...`` notes for the sections a write left stale, in one batched read.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.
        section_ids: The sections as they stand after the write.

    Returns:
        Section id -> its note (``still LINKED_STALE via: ...``, or the
        frozen or inherited form), for the LINKED_STALE ones only.
    """
    from axiom_graph.index.staleness import current_offenders  # noqa: PLC0415

    with db._connect(db_path) as conn:
        rows = db.get_live_rows_conn(conn, section_ids)
    stale = [sid for sid in section_ids if sid in rows and rows[sid]["link_status"] == "LINKED_STALE"]
    if not stale:
        return {}
    config = AxiomGraphConfig.load(root)
    offenders = current_offenders(
        db_path,
        root,
        stale,
        transitive_tags=config.staleness.transitive_tags,
        frozen_tags=config.staleness.frozen_tags,
    )
    notes: dict[str, str] = {}
    for sid in stale:
        found = offenders.get(sid)
        if found is None or not found.vias:
            if found is not None and found.carried:
                notes[sid] = "still LINKED_STALE (carried on a frozen doc; mark_clean clears it)"
            else:
                notes[sid] = "still LINKED_STALE (inherited from its child sections)"
            continue
        shown = [v if v in found.receipt_targets else f"{v} (clears when it does)" for v in found.vias]
        extra = len(shown) - _STILL_STALE_CAP
        listed = ", ".join(shown[:_STILL_STALE_CAP]) + (f" (+{extra} more)" if extra > 0 else "")
        notes[sid] = f"still LINKED_STALE via: {listed}"
    return notes


def _sections_off_baseline(db_path: Path, scanned: list) -> list[str]:
    """Ids of scanned section nodes whose indexed baseline hashes differ from the scan.

    Args:
        db_path: Path to the axiom-graph DB.
        scanned: Section nodes from a fresh scan of the doc file.

    Returns:
        The ids whose stored ``code_hash`` / ``desc_hash`` no longer match
        the file -- sections drifted since they were last verified.
    """
    if not scanned:
        return []
    with db._connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, code_hash, desc_hash FROM nodes WHERE id IN ({', '.join('?' * len(scanned))})",
            [n.id for n in scanned],
        ).fetchall()
    stored = {r[0]: (r[1], r[2]) for r in rows}
    return [n.id for n in scanned if n.id in stored and stored[n.id] != (n.code_hash, n.desc_hash)]


# ---------------------------------------------------------------------------
# Write lock + content hash
# ---------------------------------------------------------------------------


def doc_file_hash(json_file: Path) -> str:
    """Return the ``doc_hash`` of a DocJSON file: :func:`content_hash` of its text.

    The text is read as UTF-8 with universal newlines, so the hash does not
    depend on the platform's line endings.  ``write_doc`` reports it on its
    ``doc_hash`` line and compares its ``expected_hash`` against it.

    Args:
        json_file: The DocJSON file.

    Returns:
        Lower-case hex sha256.
    """
    return content_hash(json_file.read_text(encoding="utf-8", errors="replace"))


def content_hash(content: str | None) -> str:
    """Return the sha256 hex digest of a section's stored content.

    This is the value write results report and ``expected_hash`` compares
    against.

    Args:
        content: The section's stored ``content`` (``None`` hashes as ``""``).

    Returns:
        Lower-case hex sha256 of the UTF-8 content.
    """
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Write inputs: content files + pasted linked-nodes footers
# ---------------------------------------------------------------------------

# ``LINKED_NODES_OPEN`` / ``LINKED_NODES_CLOSE`` -- the marker pair ``read_doc``
# wraps its generated linked-nodes footer in -- live in ``render_agent``.
_MARKER_OPEN_RE = re.compile(rf"^[ \t]*{re.escape(LINKED_NODES_OPEN)}[ \t]*$", re.MULTILINE)
_MARKER_CLOSE_RE = re.compile(rf"^[ \t]*{re.escape(LINKED_NODES_CLOSE)}[ \t]*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)
_UNMARKED_FOOTER_RE = re.compile(r"^[ \t]*\*\*Linked nodes:\*\*[ \t]*$", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\S")


class WriteInputError(ValueError):
    """A write tool's input was rejected before anything was written."""


def strip_linked_nodes_footer(text: str | None) -> tuple[str | None, bool]:
    """Remove pasted ``read_doc`` linked-nodes footers from section content.

    ``read_doc`` appends a generated list of a section's links to the content
    it shows.  An agent that edits that output and writes it back would store
    the generated list as prose.  This removes:

    - every marked block, from a line holding only
      ``<!-- axiom:linked-nodes -->`` through a line holding only
      ``<!-- /axiom:linked-nodes -->``, wherever it appears in the content
      (markers quoted inside a line of prose are not a block);
    - an unmarked ``**Linked nodes:**`` line, but only when everything after
      it is list items or whitespace.  Prose after it means the line is not a
      generated footer, so the content is kept as written.

    A mention of the phrase inside a line of prose is never touched.

    Args:
        text: Incoming section content (``None`` passes through).

    Returns:
        ``(content, stripped)`` -- the content to store and whether anything
        was removed.
    """
    if not text:
        return text, False

    def _in_fence(s: str, pos: int) -> bool:
        # Inside a fenced code block when an odd number of fence lines precede pos.
        return len(_FENCE_RE.findall(s, 0, pos)) % 2 == 1

    out = text
    stripped = False
    while True:
        opener = next((m for m in _MARKER_OPEN_RE.finditer(out) if not _in_fence(out, m.start())), None)
        if opener is None:
            break
        closer = _MARKER_CLOSE_RE.search(out, opener.end())
        if closer is None:
            break
        before, after = out[: opener.start()], out[closer.end() :]
        if not after.strip():
            out = before.rstrip()
        elif not before.strip():
            out = after.lstrip()
        else:
            out = before.rstrip() + "\n\n" + after.lstrip()
        stripped = True
    matches = [m for m in _UNMARKED_FOOTER_RE.finditer(out) if not _in_fence(out, m.start())]
    if matches:
        last = matches[-1]
        tail = out[last.end() :].splitlines()
        if all(not line.strip() or _LIST_ITEM_RE.match(line) for line in tail):
            out = out[: last.start()].rstrip()
            stripped = True
    return out, stripped


def _is_under(child: Path, parent: Path) -> bool:
    """Whether resolved *child* sits at or below resolved *parent* (case-insensitive on Windows)."""
    c, p = os.path.normcase(str(child)), os.path.normcase(str(parent))
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


def read_content_file(root: Path, file_path: str, param: str = "content_file") -> str:
    """Read a write tool's ``content_file`` / ``doc_file`` input.

    The file is read as UTF-8; a leading byte-order mark is dropped, every
    other byte is kept.  A relative path is taken relative to the project
    root.  The resolved path (symlinks and ``..`` followed) must sit under
    the project root or the system temp directory.

    Args:
        root: Absolute project root.
        file_path: The path the caller passed.
        param: Parameter name used in error messages.

    Returns:
        The file's text.

    Raises:
        WriteInputError: The path escapes the allowed roots, does not exist,
            or is not valid UTF-8.
    """
    p = Path(file_path)
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve()
    allowed = [root.resolve(), Path(tempfile.gettempdir()).resolve()]
    if not any(_is_under(resolved, a) for a in allowed):
        raise WriteInputError(
            f"{param} {file_path!r} resolves outside the project root and the system temp directory "
            f"({', '.join(str(a) for a in allowed)})"
        )
    if not resolved.is_file():
        raise WriteInputError(f"{param} {file_path!r} is not a file")
    try:
        return resolved.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WriteInputError(f"{param} {file_path!r} is not valid UTF-8: {exc}") from exc


def _resolve_text_input(root: Path, inline: str | None, file_path: str | None, inline_name: str, file_name: str):
    """Pick inline text or a file's text; both at once is an input error."""
    if file_path is None:
        return inline
    if inline is not None:
        raise WriteInputError(f"provide '{inline_name}' or '{file_name}', not both")
    return read_content_file(root, file_path, file_name)


def _strip_note(count: int) -> str:
    """Result-line note for stripped pasted footers."""
    if not count:
        return ""
    noun = "section" if count == 1 else "sections"
    return f"\n  stripped a pasted linked-nodes footer from {count} {noun} (links live in the section's links array)"


_REGION_CONTEXT = 2
_REGION_MAX_LINES = 40
_REGION_KEEP = 10


def _edited_region(content: str, start: int, end: int) -> str:
    """Render the lines a patch touched, with context, as a fenced result block.

    Args:
        content: The section's new content.
        start: Offset in *content* where the inserted text begins.
        end: Offset in *content* where the inserted text ends (exclusive).

    Returns:
        A block headed ``edited region (lines A-B of N):`` holding the touched
        lines plus up to ``_REGION_CONTEXT`` lines either side, fenced with more
        backticks than any run inside it.  A region over ``_REGION_MAX_LINES``
        lines keeps its first and last ``_REGION_KEEP`` lines around an
        omitted-lines marker.
    """
    lines = content.split("\n")
    first = content.count("\n", 0, start)
    last = content.count("\n", 0, max(end - 1, start))
    lo = max(first - _REGION_CONTEXT, 0)
    hi = min(last + _REGION_CONTEXT, len(lines) - 1)
    shown = lines[lo : hi + 1]
    if len(shown) > _REGION_MAX_LINES:
        omitted = len(shown) - 2 * _REGION_KEEP
        shown = shown[:_REGION_KEEP] + [f"... ({omitted} lines omitted)"] + shown[-_REGION_KEEP:]
    body = "\n".join(shown)
    runs = re.findall(r"`+", body)
    fence = "`" * max(3, max((len(r) for r in runs), default=0) + 1)
    return f"\nedited region (lines {lo + 1}-{hi + 1} of {len(lines)}):\n{fence}\n{body}\n{fence}"


def _doc_id_from_args(arguments: dict) -> str | None:
    """Name the doc a write tool call targets, from its bound arguments."""
    doc_id = arguments.get("doc_id")
    if isinstance(doc_id, str) and doc_id:
        return doc_id
    section_id = arguments.get("section_id")
    if not (isinstance(section_id, str) and section_id):
        for item in arguments.get("links") or []:
            if isinstance(item, dict) and isinstance(item.get("section_id"), str):
                section_id = item["section_id"]
                break
    if not (isinstance(section_id, str) and section_id):
        return None
    root = Path(arguments["project_root"]).resolve()
    parsed = parse_section_id(section_id, docs_roots_for(root))
    return None if isinstance(parsed, str) else parsed[3]


def _renamed_section_ids(arguments: dict) -> list[str]:
    """The full id of the section a single-call ``new_id`` rename moves, from its bound arguments (else ``[]``)."""
    new_id, section_id = arguments.get("new_id"), arguments.get("section_id")
    if not (isinstance(new_id, str) and new_id and isinstance(section_id, str) and section_id):
        return []
    parsed = parse_section_id(section_id, docs_roots_for(Path(arguments["project_root"]).resolve()))
    return [] if isinstance(parsed, str) else [f"{parsed[3]}::{parsed[2]}"]


def _rename_link_files(root: Path, old_ids: list[str]) -> list[Path]:
    """The doc files whose links name a section in *old_ids* or one under it: a rename rewrites them.

    Read-only, before any lock, so a rename can take the write locks of its
    own doc and of these in one sorted acquisition.  A file that starts
    linking the section after this scan is still patched; its lock is then
    taken after the others (bounded by the same timeout).
    """
    from axiom_graph.index.link_maintenance import scan_doc_links  # noqa: PLC0415

    return scan_doc_links(root, prefixes=old_ids).affected if old_ids else []


def _unpatched_links_note(patches: list[LinkPatchResult]) -> str:
    """The notes naming doc files a rename's link rewrite could not rewrite or could not check, or ``""``."""
    lines = link_rewrite_warnings(
        [f for p in patches for f in p.unreadable],
        [f for p in patches for f in p.not_patched],
        "a renamed section's old id",
        "the new id (delete_link + add_link)",
    )
    return "".join(f"\n  WARNING: {line}" for line in lines)


def _doc_write_db(arguments: dict) -> Path | None:
    """The index a doc write tool call writes to, or ``None`` when it has none (the tool reports its own error)."""
    try:
        return require_db(str(Path(arguments["project_root"]).resolve()))
    except Exception:
        return None


def _one_connection(fn):
    """Run a doc write tool on one connection (:func:`axiom_graph.db.operation_connection`).

    Every block of the call -- load, pre-write refresh, re-index, text
    verification, raw-edit classifier, closing refresh -- runs on the one
    connection and commits where it did, so no write transaction is
    lengthened.  The call runs in :func:`axiom_graph.config.config_scope`,
    so every config load inside it, the re-index's and the stamps' included,
    shares one parse of ``axiom-graph.toml``.
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with config_scope():
            db_path = _doc_write_db(sig.bind_partial(*args, **kwargs).arguments)
            if db_path is None:
                return fn(*args, **kwargs)
            with db.operation_connection(db_path):
                return fn(*args, **kwargs)

    return wrapper


def _locks_doc(fn):
    """Run a doc write tool under its doc's write lock, on one connection.

    The lock spans the tool's whole load -> mutate -> write -> re-index
    sequence.  A section rename (``new_id``) also rewrites other docs' links
    to it, so the docs that link it are found first (read-only) and locked
    with its own doc in one sorted acquisition.  A call whose doc cannot be
    resolved runs unlocked so the tool reports its own error; a lock timeout
    returns ``ERROR:`` and writes nothing.  The call runs on one connection,
    as :func:`_one_connection`.
    """
    sig = inspect.signature(fn)

    @_one_connection
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        try:
            root = Path(bound.arguments["project_root"]).resolve()
            doc_id = _doc_id_from_args(bound.arguments)
            doc_node = db.get_node(require_db(str(root)), doc_id) if doc_id else None
            renamed = _renamed_section_ids(bound.arguments) if doc_node is not None else []
        except Exception:  # unresolvable target; the tool reports its own error
            logger.debug(
                "%s: could not resolve the target doc; running without the doc lock", fn.__name__, exc_info=True
            )
            doc_node = None
        if doc_node is None:
            return fn(*args, **kwargs)
        try:
            with lock_docs(root, [root / doc_node.location, *_rename_link_files(root, renamed)]):
                return fn(*args, **kwargs)
        except DocLockTimeout as exc:
            logger.warning("%s: %s -- nothing was written", fn.__name__, exc)
            return f"ERROR: {exc} -- nothing was written"

    return wrapper


# ---------------------------------------------------------------------------
# Doc operations (public surface)
# ---------------------------------------------------------------------------


def _strip_footers_in_tree(sections: list[dict]) -> int:
    """Strip pasted linked-nodes footers from every section, recursively; return how many were stripped."""
    count = 0
    for sec in sections:
        if isinstance(sec, dict):
            if isinstance(sec.get("content"), str):
                sec["content"], stripped = strip_linked_nodes_footer(sec["content"])
                count += stripped
            count += _strip_footers_in_tree(sec.get("sections") or [])
    return count


def _validate_doc_slug(raw_slug: str) -> str | None:
    """Return the error for a write's ``id`` path-slug hint, or ``None`` when it is usable.

    The id is a path-slug filename hint; node-id form and filesystem-illegal
    characters are rejected with messages that point at the right shape.
    """
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
    return None


def _normalize_links_in_tree(sections: list[dict], prefix: str | None = None) -> str | None:
    """Rewrite every section's ``links`` in a sections tree as link objects, in place.

    A bare node-id string becomes ``{"node_id": s}``; an entry without a
    usable node id is an error (see
    :func:`axiom_graph.docjson.parse.normalize_links`, strict mode).

    Args:
        sections: A sections list (possibly nested).
        prefix: The dot-path of the section that holds *sections*; ``None``
            at the top level.

    Returns:
        ``None``, or an ``ERROR: ...`` string naming the first malformed
        entry, its section's dot-path and the accepted shapes.
    """
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        dot_path = f"{prefix}.{sec.get('id')}" if prefix else f"{sec.get('id')}"
        if "links" in sec:
            try:
                sec["links"] = json_doc_scanner.normalize_links(sec["links"], f"section {dot_path!r}")
            except json_doc_scanner.MalformedLinkError as exc:
                return f"ERROR: {exc}; nothing was written"
        err = _normalize_links_in_tree(sec.get("sections") or [], dot_path)
        if err:
            return err
    return None


def _linked_node_ids_in_tree(sections: list[dict]) -> list[str]:
    """Every non-empty ``links[].node_id`` in a sections tree, in document order (duplicates kept)."""
    out: list[str] = []
    for sec in sections:
        for link in sec.get("links") or []:
            nid = link.get("node_id", "").strip()
            if nid:
                out.append(nid)
        out.extend(_linked_node_ids_in_tree(sec.get("sections") or []))
    return out


def _destination_taken(path: Path, docs_dir: Path, raw_slug: str, doc_node_id: str) -> str | None:
    """Return a clone's refusal when its destination already exists, else ``None``.

    The destination exists when either DocJSON extension already has a file
    for the slug, or the doc id is already indexed.

    Args:
        path: Path to the axiom-graph DB.
        docs_dir: The resolved docs root the clone writes under.
        raw_slug: The destination path-slug.
        doc_node_id: The destination doc id.
    """
    existing = doc_ids.existing_docjson_file(docs_dir, raw_slug)
    with db._connect(path) as conn:
        indexed = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (doc_node_id,)).fetchone() is not None
    if existing is None and not indexed:
        return None
    where = existing.name if existing is not None else "indexed"
    return (
        f"ERROR: destination doc {doc_node_id} already exists ({where}) -- clone_doc never overwrites; "
        "nothing was written"
    )


def _save_new_doc(
    path: Path,
    root: Path,
    data: dict,
    docs_root: str | None,
    *,
    clone_source: str | None = None,
    expected_hash: str | None = None,
) -> str:
    """Validate a whole DocJSON doc, write it under a docs root and index it: the shared new-doc write path.

    Used by :func:`axiom_graph_write_doc` (which may overwrite) and
    :func:`axiom_graph_clone_doc` (which never does).  Checks nesting depth,
    dot-path collisions and the ``id`` slug hint, resolves ``docs_root``
    against the configured ``docs_dirs``, strips pasted linked-nodes footers,
    then saves and re-indexes the file under its write lock and records
    LINK_ADDED history for its links.  The linked node ids are looked up in
    one batched read; the file is parsed once (the history rows reuse the
    re-index's scan).

    Args:
        path: Path to the axiom-graph DB.
        root: Absolute project root.
        data: The doc dict (``title`` and ``sections`` present); mutated in
            place (``id`` popped, footers stripped, stamps written).
        docs_root: The configured docs root to write under; ``None`` for the
            primary root.
        clone_source: When set, the doc id of a clone's source: a destination
            that already has a file (either DocJSON extension) or an indexed
            doc node, or that is the source itself, is refused.
        expected_hash: When set, the ``doc_hash`` (:func:`doc_file_hash`)
            the caller last saw for the existing doc; checked under the lock,
            and a mismatch (or no existing file) writes nothing.

    Returns:
        The write summary (file, ``doc id`` and ``doc_hash`` lines, sections written, links
        registered, one ``content_hash`` line per section, unknown node ids),
        or an ``ERROR: ...`` string when validation fails (nothing written).
    """
    # Validate nesting depth before writing
    depth_err = _validate_max_depth(data.get("sections", []))
    if depth_err:
        return depth_err
    collision_err = _dot_path_collision_error(data.get("sections") or [])
    if collision_err:
        return collision_err

    # Derive filename slug from id (if present) or title
    raw_slug = data.get("id", "").strip() if isinstance(data.get("id"), str) else ""
    if raw_slug:
        slug_err = _validate_doc_slug(raw_slug)
        if slug_err:
            return slug_err
    if not raw_slug:
        raw_slug = re.sub(r"[^a-z0-9]+", "-", data["title"].lower()).strip("-")
    if not raw_slug:
        return "ERROR: Could not derive a filename from id or title"

    links_err = _normalize_links_in_tree(data.get("sections") or [])
    if links_err:
        return links_err

    # Check all linked node_ids (nested sections included) in one read.
    linked = _linked_node_ids_in_tree(data.get("sections", []))
    link_count = len(linked)
    if linked:
        with db._connect(path) as conn:
            known = db.get_nodes_conn(conn, linked)
    else:
        known = {}
    unknown_ids = [nid for nid in linked if nid not in known]
    footers_stripped = _strip_footers_in_tree(data.get("sections") or [])

    # Strip top-level "id" -- canonical identity is derived from file path
    data.pop("id", None)

    # Resolve docs_dir + out_file.  The target root defaults to
    # config.scan.docs_dirs[0] (the primary) and may be redirected to any
    # other configured root via ``docs_root`` (honors absolute paths).
    # out_file may already exist (write_doc supports overwrite semantics) or
    # may be a brand-new file.  save_and_reindex handles both the same way:
    # the writer verifies the text it writes, so on first creation every
    # section and the doc composite get own status VERIFIED; on overwrite only
    # the sections created or whose content changed (and the composite, when
    # the bytes changed) do.  Link statuses are never changed by the write.
    _cfg = AxiomGraphConfig.load(root)
    _roots = _cfg.scan.docs_dirs or ["docs"]
    if docs_root is None:
        _selected = _roots[0]
    else:
        # Compare as POSIX paths so ".pev", "./.pev" and ".pev/" all match
        # the configured entry.  Rejected before anything touches disk.
        _want = Path(docs_root.replace("\\", "/")).as_posix()
        _selected = next((r for r in _roots if Path(r.replace("\\", "/")).as_posix() == _want), None)
        if _selected is None:
            return f"ERROR: unknown docs_root {docs_root!r}. Configured docs_dirs: {', '.join(_roots)}"
    _selected_path = Path(_selected)
    docs_dir = _selected_path if _selected_path.is_absolute() else (root / _selected_path)
    from axiom_graph.index.builder import resolve_project_id  # noqa: PLC0415

    _project_id = resolve_project_id(root, path, config=_cfg)

    # Derive the doc node id through the one derivation the scanner uses, so
    # the id this call reports and the id the re-index produces cannot drift.
    # ``raw_slug`` may include subdirectory separators (e.g. ``"adrs/016-foo"``).
    # The id carries no extension, so it is known before the file is resolved.
    new_file = docs_dir / doc_ids.docjson_filename(raw_slug, _cfg.scan.docs_extensions)
    _doc_node_id = doc_ids.derive_doc_id(_project_id, _selected, new_file.relative_to(docs_dir).as_posix())

    if clone_source is not None and _doc_node_id == clone_source:
        return f"ERROR: the destination {_doc_node_id} is the source doc -- clone_doc never overwrites; nothing was written"
    new_file.parent.mkdir(parents=True, exist_ok=True)

    # The lock covers every file the slug can resolve to (either DocJSON
    # extension), so the target is resolved inside it: a writer creating
    # ``x.json`` meanwhile cannot leave this call writing ``x.docjson``.
    candidates = [docs_dir / f"{raw_slug}{ext}" for ext in doc_ids.DOCJSON_EXTENSIONS]
    try:
        with lock_docs(root, candidates):
            # A document that already has a file is rewritten in place,
            # whichever extension it carries -- overwriting an unconverted
            # ``x.json`` must not create an ``x.docjson`` beside it.  A new
            # document gets the configured write extension.
            existing_file = doc_ids.existing_docjson_file(docs_dir, raw_slug, _cfg.scan.docs_extensions)
            out_file = existing_file or new_file
            if clone_source is not None:
                taken = _destination_taken(path, docs_dir, raw_slug, _doc_node_id)
                if taken:
                    return taken
            if expected_hash is not None:
                current = doc_file_hash(existing_file) if existing_file is not None else None
                if current != expected_hash:
                    return f"ERROR: expected_hash does not match {_doc_node_id} -- nothing was written\n" + (
                        f"  current doc_hash: {current}"
                        if current is not None
                        else "  the doc has no file yet (omit expected_hash to create it)"
                    )
            raw_edits = save_and_reindex(
                data,
                out_file,
                path,
                root,
                _project_id,
                doc_node_id=_doc_node_id,
            )
    except DocLockTimeout as exc:
        logger.warning("axiom_graph_write_doc: %s -- nothing was written", exc)
        return f"ERROR: {exc} -- nothing was written"

    # Record LINK_ADDED history for each documents edge the doc now has.  The
    # edges are read off the dict just written -- the sections the scanner
    # indexes, in its order -- so the file is not parsed a second time.  One
    # row per (section, target): a target linked twice is one edge.
    written = doc_stamps.flatten_section_dicts(data.get("sections") or [])
    with db._connect(path) as conn:
        for dot, sec in written.items():
            source = f"{_doc_node_id}::{dot}"
            for target in dict.fromkeys(_linked_node_ids_in_tree([{"links": sec.get("links")}])):
                db.insert_history_row_conn(
                    conn,
                    node_id=source,
                    change_type="LINK_ADDED",
                    meta=json.dumps({"edge_type": "documents", "source": source, "target": target, "actor": "agent"}),
                    preserved=False,
                )

    summary = (
        f"Wrote {out_file.relative_to(root).as_posix()}\n"
        f"  doc id           : {_doc_node_id}\n"
        f"  doc_hash         : {content_hash(dumps_doc_json(data))}\n"
        f"  sections written : {len(written)}\n"
        f"  links registered : {link_count}"
    )
    for dot_path, stored in _section_contents(data.get("sections") or []).items():
        summary += f"\n  {dot_path}  content_hash: {content_hash(stored)}"
    if unknown_ids:
        summary += f"\n  WARN: {len(unknown_ids)} node_id(s) not found in index:"
        for uid in unknown_ids:
            summary += f"\n    ! {uid}"
    return summary + _strip_note(footers_stripped) + _raw_edit_note(raw_edits)


@_one_connection
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
    immediately indexed.  A new document is written as ``<slug>.docjson``
    (the first ``[axiom_graph.scan].docs_extensions`` entry); a document
    that already exists as ``<slug>.json`` or ``<slug>.docjson`` is
    rewritten in that file.  The doc id never carries the extension.

    **Important:** the ``id`` key (if present) is treated as a *path-slug
    filename hint*, not as the canonical node id. The canonical node id is
    derived from the file's path within its docs root, and the result
    reports it on its ``doc id`` line -- take it from there rather than
    rebuilding it. Common mistake: passing the indexed node id back as input.

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
            one (``"links": ["proj::pkg.mod::fn"]``) and saved as an object.
            The id must be non-empty; other keys beside ``node_id`` are
            kept and ignored, and none replaces it (``target``, ``id``,
            ``type`` are not read; the link type is always ``documents``).
            Any other entry, or a ``links`` value that is not a list, is
            an error naming the section's dot-path and the entry, and
            nothing is written.
        docs_root: Which configured documentation root to write under.  Must
            match an entry of ``[axiom_graph.scan].docs_dirs`` (compared as
            POSIX paths); an unknown value is an error listing the valid
            roots.  Defaults to the first entry — the primary root.
        doc_file: Optional path to a UTF-8 file holding the doc JSON -- use
            it for large docs instead of an inline ``doc_json`` (exactly one
            of the two).  Must resolve under the project root or the system
            temp directory; a byte-order mark is dropped.
        expected_hash: Optional compare-and-swap guard for an overwrite: the
            ``doc_hash`` the caller last saw for the existing doc (every
            ``write_doc`` / ``clone_doc`` result reports it; it is the sha256
            of the doc file's text).  Checked under the doc's write lock; a
            mismatch, or no existing doc, returns ``ERROR`` with the current
            ``doc_hash`` and writes nothing.  Omitted: the doc is created or
            overwritten unconditionally.

    The writer verifies the text it writes: every section it creates or whose
    content it changes comes out with own status VERIFIED; link status
    unchanged (an edit never clears LINKED_STALE).  Pasted ``read_doc`` linked-nodes
    footers are removed from every section's content before storing; the
    result says so when it happens.  A doc in which two sections spell one
    dot-path (a flat ``a.b`` beside a ``b`` nested under ``a``, or two
    siblings sharing an id) is refused with an error naming the dot-path and
    every section that spells it, and nothing is written.

    Returns:
        Summary: the file written, the doc id it was indexed under, its
        ``doc_hash``, sections written, links registered, one ``<dot-path>  content_hash: <sha256>``
        line per section, and any unknown node_ids.  Or an
        ``ERROR: ...`` string when validation fails.
    """
    path = require_db(project_root)
    root = Path(project_root).resolve()

    if doc_file is not None:
        if doc_json is not None:
            return "ERROR: provide 'doc_json' or 'doc_file', not both"
        try:
            doc_json = read_content_file(root, doc_file, "doc_file")
        except WriteInputError as exc:
            return f"ERROR: {exc}"
    if doc_json is None:
        return "ERROR: provide 'doc_json' or 'doc_file'"
    if isinstance(doc_json, str):
        try:
            data = json.loads(doc_json)
        except json.JSONDecodeError as exc:
            return f"ERROR: doc JSON is not valid JSON: {exc}"
    else:
        data = doc_json
    if not isinstance(data, dict):
        return "ERROR: doc JSON must be an object"
    for key in ("title", "sections"):
        if key not in data:
            return f"ERROR: doc_json missing required key '{key}'"

    return _save_new_doc(path, root, data, docs_root, expected_hash=expected_hash)


def _resolve_override_ids(sections: list[dict], ids: list[str]) -> tuple[dict[str, list[dict]], list[str]]:
    """Resolve clone override ids against a sections tree.

    Args:
        sections: The doc's top-level sections.
        ids: Section dot-paths to resolve.

    Returns:
        ``(chains, problems)`` -- each resolved id's chain, and one entry per
        id that names no section or more than one (``"x (ambiguous: ...)"``).
    """
    chains: dict[str, list[dict]] = {}
    problems: list[str] = []
    for sec_id in ids:
        matches = _section_path_matches(sections, sec_id)
        if len(matches) == 1:
            chains[sec_id] = matches[0]
        elif matches:
            problems.append(f"{sec_id} (ambiguous: {_describe_chains([[s.get('id') for s in c] for c in matches])})")
        else:
            problems.append(sec_id)
    return chains, problems


@_one_connection
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

    The source is deep-copied: every section's id, heading, content, links,
    tags and nesting, and every extra top-level key (e.g. a ``meta`` block),
    so the copy is the source verbatim except its tool-write stamps, which
    the write regenerates (every section comes out with own status VERIFIED;
    a linked section is stamped against the code as it is now).  The source
    file and its index entries are never modified.  The copy goes through
    :func:`axiom_graph_write_doc`'s save path (depth, dot-path, slug and
    ``docs_root`` checks, link check, write and immediate index), and its
    LINK_ADDED history is recorded for every copied link.

    Every id in ``set_sections`` and ``omit_sections`` is resolved first.
    An id that names no section of the source (or more than one), an id in
    both, or a ``set_sections`` id inside an omitted section is an error
    that names every such id, and nothing is written.  A destination that
    already exists (a file under either DocJSON extension, or an indexed
    doc), or that is the source itself, is refused: ``clone_doc`` never
    overwrites -- ``write_doc`` is the overwrite path.

    Args:
        project_root: Absolute path to the indexed project.
        source_doc_id: Doc id of the document to copy, e.g.
            ``myproject::docs/templates/cycle``.
        new_id: Path-slug filename hint for the copy, with ``write_doc``'s
            ``id`` contract (e.g. ``"pev/cycles/my-cycle"``; never a node id).
        title: Title of the copy.  Defaults to the source's.
        tags: Doc tags of the copy, replacing the source's wholesale.
            Defaults to the source's.
        set_sections: ``{section dot-path: content}`` -- replaces only the
            content of each named section (nested ids in dotted form, e.g.
            ``builder.friction``); heading, links, tags and children are kept.
        omit_sections: Section dot-paths to leave out of the copy, with their
            children.
        docs_root: Which configured docs root to write under, as in
            ``write_doc``.  Defaults to the primary root.

    Returns:
        ``write_doc``'s summary: the file written, the ``doc id`` line the
        copy was indexed under, sections written, links registered and one
        ``<dot-path>  content_hash: <sha256>`` line per section; or an
        ``ERROR: ...`` string (nothing written).
    """
    path = require_db(project_root)
    root = Path(project_root).resolve()

    if not isinstance(new_id, str) or not new_id.strip():
        return "ERROR: new_id must be a non-empty path-slug (e.g. 'pev/cycles/my-cycle')"
    if set_sections is not None and (
        not isinstance(set_sections, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in set_sections.items())
    ):
        return "ERROR: set_sections must map section ids to content strings"
    if omit_sections is not None and (
        not isinstance(omit_sections, list) or not all(isinstance(s, str) for s in omit_sections)
    ):
        return "ERROR: omit_sections must be a list of section ids"
    if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
        return "ERROR: tags must be a list of strings"

    loaded = load_doc_json(path, root, source_doc_id)
    if isinstance(loaded, str):
        return loaded
    data = copy.deepcopy(loaded[0])
    sections: list[dict] = data.get("sections") or []

    set_ids = list(set_sections or {})
    omit_ids = list(dict.fromkeys(omit_sections or []))
    in_both = sorted(set(set_ids) & set(omit_ids))
    omit_chains, omit_problems = _resolve_override_ids(sections, omit_ids)
    # Drop omitted subtrees first, so a set_sections id inside one is unknown.
    for chain in omit_chains.values():
        siblings = (chain[-2].get("sections") or []) if len(chain) > 1 else sections
        siblings[:] = [s for s in siblings if s is not chain[-1]]
    set_chains, set_problems = _resolve_override_ids(sections, [s for s in set_ids if s not in in_both])
    if in_both or omit_problems or set_problems:
        parts = []
        if set_problems:
            parts.append(f"unknown in set_sections: {', '.join(set_problems)}")
        if omit_problems:
            parts.append(f"unknown in omit_sections: {', '.join(omit_problems)}")
        if in_both:
            parts.append(f"in both set_sections and omit_sections: {', '.join(in_both)}")
        return f"ERROR: section overrides do not match {source_doc_id} -- {'; '.join(parts)}; nothing was written"

    for sec_id, chain in set_chains.items():
        chain[-1]["content"] = set_sections[sec_id]
    if title is not None:
        data["title"] = title
    if tags is not None:
        data["tags"] = list(tags)
    data["id"] = new_id.strip()
    for key in ("title", "sections"):
        if key not in data:
            return f"ERROR: source doc {source_doc_id} has no '{key}'"
    return _save_new_doc(path, root, data, docs_root, clone_source=source_doc_id)


#: Default character budget for one ``axiom_graph_read_doc`` call.
READ_DOC_MAX_CHARS = 40_000

# Joined to its neighbours with "\n", this yields the "\n\n---\n\n" that
# separates documents in a multi-doc read.
_DOC_SEPARATOR = "\n---\n"

_OFFSET_NEEDS_ONE = "ERROR: offset needs exactly one section -- pass section_ids=[<the truncated section>]"


def _section_slug(sec_id: str) -> str:
    """Return the part of a section id after its last ``::`` (the dot-path)."""
    return sec_id.split("::")[-1]


def _is_descendant(sec_id: str, ancestor_id: str) -> bool:
    """Whether *sec_id* is a child, grandchild, ... of *ancestor_id* (same doc)."""
    return sec_id.startswith(ancestor_id + ".")


def _expand_subtrees(sections: list[dict], roots: list[dict]) -> list[dict]:
    """Return each root followed by its descendants, de-duplicated, in root order.

    Args:
        sections: All sections of one doc, in depth-first document order.
        roots: The sections asked for.

    Returns:
        Section dicts; a section reached through two roots appears once.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        rid = root["id"]
        for sec in sections:
            sid = sec["id"]
            if (sid == rid or _is_descendant(sid, rid)) and sid not in seen:
                seen.add(sid)
                out.append(sec)
    return out


def _drop_covered(ids: list[str]) -> list[str]:
    """Drop ids that are descendants of an earlier id in the list."""
    kept: list[str] = []
    for sid in ids:
        if not any(_is_descendant(sid, k) for k in kept):
            kept.append(sid)
    return kept


def _doc_meta(path: Path, doc_id: str) -> tuple[list[str], str]:
    """Return a doc's tags and file path from the ``docs`` table.

    Args:
        path: Path to the axiom-graph DB.
        doc_id: The doc node ID.

    Returns:
        ``(tags, file_path)``; ``([], "")`` when the doc has no ``docs`` row.
    """
    with db._connect(path) as conn:
        row = conn.execute("SELECT tags, file_path FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return [], ""
    try:
        tags = json.loads(row["tags"]) if row["tags"] else []
    except (TypeError, ValueError):
        tags = []
    return [str(t) for t in tags], row["file_path"] or ""


class _ReadTarget:
    """One doc's share of a read: its title and the sections to render."""

    def __init__(self, doc_id: str, title: str, sections: list[dict], note: str | None = None) -> None:
        self.doc_id = doc_id
        self.title = title
        self.sections = sections
        self.note = note


@task(
    purpose="Resolve one doc, optionally narrowed by a section slug (exact, then substring), to the sections to "
    "render, each matched section bringing its subtree",
    inputs="db path, doc id, optional section slug",
    outputs="A read target (title, sections, multi-match note) or an ERROR string",
)
def _resolve_doc_target(path: Path, doc_id: str, section: str | None) -> "_ReadTarget | str":
    """Resolve a doc, optionally narrowed by a section slug, to a read target.

    The slug is matched exactly against the dot-path after the last ``::``,
    then as a substring.  Every matched section brings its subtree.

    Args:
        path: Path to the axiom-graph DB.
        doc_id: Full doc node ID.
        section: Optional section slug.

    Returns:
        A ``_ReadTarget``, or an ``ERROR`` string.
    """
    doc_node = db.get_node(path, doc_id)
    if doc_node is None:
        return f"ERROR: doc '{doc_id}' not found. Pass doc_id=\"list\" to see available docs."
    sections = db.get_doc_sections(path, doc_id)
    if section is None:
        return _ReadTarget(doc_id, doc_node.title, sections)

    matched = [s for s in sections if _section_slug(s.get("id", "")) == section]
    if not matched:
        # Fallback: substring match on slug
        matched = [s for s in sections if section in _section_slug(s.get("id", ""))]
    if not matched:
        available = ", ".join(_section_slug(s.get("id", "")) for s in sections)
        return f"ERROR: no section matching '{section}'. Available slugs: {available}"
    root_ids = set(_drop_covered([s["id"] for s in matched]))
    roots = [s for s in matched if s["id"] in root_ids]
    note = None
    if len(roots) > 1:
        ids = "\n".join(f"  {s['id']}" for s in roots)
        note = (
            f"[{len(roots)} sections matched '{section}' -- returning all. "
            f"Use full section_id in axiom_graph_update_section to be specific.]\n{ids}"
        )
    return _ReadTarget(doc_id, doc_node.title, _expand_subtrees(sections, roots), note)


@task(
    purpose="Resolve fully-qualified section ids, possibly spanning docs, into per-doc read targets in the order "
    "given, each id bringing its subtree and no section repeated",
    inputs="db path, section ids",
    outputs="Read targets, with an ERROR string in place of each unknown id",
)
def _resolve_section_ids(path: Path, section_ids: list[str]) -> list["_ReadTarget | str"]:
    """Resolve fully-qualified section ids, possibly spanning docs, in the order given.

    Consecutive ids from the same doc share one target.  Each id brings its
    subtree, and a section already included is not repeated.

    Args:
        path: Path to the axiom-graph DB.
        section_ids: Fully-qualified section ids.

    Returns:
        Read targets, with an ``ERROR`` string in place of an unknown id.
    """
    out: list[_ReadTarget | str] = []
    seen: set[str] = set()
    docs: dict[str, tuple[str | None, list[dict]]] = {}
    for sid in section_ids:
        doc_id = sid.rsplit("::", 1)[0] if "::" in sid else ""
        if doc_id not in docs:
            node = db.get_node(path, doc_id) if doc_id else None
            docs[doc_id] = (node.title, db.get_doc_sections(path, doc_id)) if node is not None else (None, [])
        title, sections = docs[doc_id]
        root = next((s for s in sections if s["id"] == sid), None)
        if title is None or root is None:
            out.append(f"ERROR: section '{sid}' not found")
            continue
        secs = [s for s in _expand_subtrees(sections, [root]) if s["id"] not in seen]
        seen.update(s["id"] for s in secs)
        last = out[-1] if out else None
        if isinstance(last, _ReadTarget) and last.doc_id == doc_id:
            last.sections.extend(secs)
        else:
            out.append(_ReadTarget(doc_id, title, secs))
    return out


class _ReadShown:
    """What a ``read_doc`` shows the statuses of: the read's refresh and the linked nodes of its sections."""

    def __init__(self, refresh, linked: dict[str, list]) -> None:
        self.refresh = refresh
        self.linked = linked


@task(
    purpose="Refresh the statuses a read_doc shows before it renders them: the target docs' envelopes, the "
    "sections it renders (an outline: every section of the doc, whose rendered sizes it reports) and the nodes "
    "those sections link, read in one batched query",
    inputs="db path, project root, read targets, outline flag",
    outputs="The read's refresh (statuses and notes) and each section's linked nodes",
)
def _refresh_read_targets(path: Path, root: Path, targets: list["_ReadTarget | str"], *, outline: bool) -> _ReadShown:
    """Refresh what a ``read_doc`` shows (``refresh_before_read`` over the nodes it names).

    A content read names each target doc's envelope, the sections it renders
    and the nodes they link.  An outline names each target doc's envelope
    and all of its sections and their linked nodes: it tags only the target
    sections, but the sizes it reports are those of the tagged renders.

    Args:
        path: Path to the axiom-graph DB.
        root: Project root.
        targets: Read targets and ``ERROR`` strings.
        outline: Whether the read is an outline.

    Returns:
        :class:`_ReadShown`.
    """
    from axiom_graph.lifecycle.api import ReadRefresh, refresh_before_read  # noqa: PLC0415

    docs = [t for t in targets if isinstance(t, _ReadTarget)]
    section_docs: dict[str, str] = {}
    for t in docs:
        for s in db.get_doc_sections(path, t.doc_id) if outline else t.sections:
            section_docs[s["id"]] = t.doc_id
    linked = linked_nodes(path, section_docs)
    named = [t.doc_id for t in docs] + list(section_docs)
    named += [nid for entries in linked.values() for nid, _, _ in entries]
    if not named:
        return _ReadShown(ReadRefresh(mode=""), linked)
    return _ReadShown(refresh_before_read(path, root, named), linked)


def _with_read_notes(text: str, shown: _ReadShown) -> str:
    """Append the read's notes (behind / structural lines) after a blank line, when it has any."""
    notes = shown.refresh.notes()
    return "\n".join([text, "", *notes]) if notes else text


@task(
    purpose="Render read targets into one Markdown string within the character budget, cutting at section "
    "boundaries and appending the omitted-ids or resume-offset hint",
    inputs="db path, read targets, max_chars, offset into the first section, the read's refreshed statuses",
    outputs="Rendered Markdown with any budget hints",
)
def _pack_read(
    path: Path,
    targets: list["_ReadTarget | str"],
    max_chars: int | None,
    offset: int,
    shown_status: _ReadShown,
) -> str:
    """Render read targets into one budgeted Markdown string.

    Sections are added whole while the running length stays within
    *max_chars*.  The first section that does not fit ends the output, and a
    trailing hint lists the omitted section ids, collapsed to subtree roots.
    When not even the first section fits, it is cut at the budget and the
    hint names the ``offset`` to resume from.  Hints are not counted against
    the budget.

    Args:
        path: Path to the axiom-graph DB.
        targets: Read targets and ``ERROR`` strings, in output order.
        max_chars: Character budget, or ``None`` for no budget.
        offset: Characters of the first section's rendered text to skip.
        shown_status: The read's refresh and linked nodes
            (:func:`_refresh_read_targets`): a doc envelope, section or
            linked node that is not VERIFIED is tagged ``  [STATUS, ...]``.

    Returns:
        The rendered Markdown, with any hints appended.
    """
    tag = shown_status.refresh.tag
    out: list[str] = []
    used = 0
    shown = 0
    stopped = False
    first_section = True
    omitted: list[str] = []
    late_errors: list[str] = []
    truncated: tuple[str, int, int] | None = None
    total = sum(len(t.sections) for t in targets if isinstance(t, _ReadTarget))

    def _emit(text: str) -> None:
        nonlocal used
        used += len(text) + (1 if out else 0)
        out.append(text)

    for i, target in enumerate(targets):
        if isinstance(target, str):
            if stopped:
                late_errors.append(target)
                continue
            if i > 0:
                _emit(_DOC_SEPARATOR)
            _emit(target)
            continue
        if stopped:
            omitted.extend(s["id"] for s in target.sections)
            continue
        mark = len(out)
        if i > 0:
            _emit(_DOC_SEPARATOR)
        if target.note:
            _emit(target.note)
        _emit(_render_doc_header(target.title))
        _emit(_render_doc_meta(target.doc_id, *_doc_meta(path, target.doc_id), tag(target.doc_id)))
        shown_here = 0
        for sec in target.sections:
            if stopped:
                omitted.append(sec["id"])
                continue
            block = _render_section_block(path, target.doc_id, sec, linked=shown_status.linked[sec["id"]], tag=tag)
            start = offset if first_section else 0
            block = block[start:]
            first_section = False
            sep = 1 if out else 0
            if max_chars is None or used + sep + len(block) <= max_chars:
                _emit(block)
                shown += 1
                shown_here += 1
                continue
            stopped = True
            if shown == 0:
                room = max(max_chars - used - sep, 1)
                _emit(block[:room])
                shown += 1
                shown_here += 1
                truncated = (sec["id"], start + room, start + len(block))
            else:
                omitted.append(sec["id"])
        if stopped and shown_here == 0:
            # Nothing of this doc made it in: drop its separator and header.
            del out[mark:]

    hints: list[str] = []
    if truncated is not None:
        sid, nxt, length = truncated
        # The resume call returns the section's children too.
        omitted = [o for o in omitted if not _is_descendant(o, sid)]
        hints.append(
            f"[read_doc: section {sid} truncated at char {nxt} of {length} (max_chars={max_chars}). "
            f'Continue with section_ids=["{sid}"], offset={nxt} -- it returns the rest of the '
            f"section and its children.]"
        )
    if omitted:
        hints.append(
            f"[read_doc: {shown} of {total} sections shown (max_chars={max_chars}); "
            f"{len(omitted)} omitted. Read the rest with section_ids={json.dumps(_drop_covered(omitted))}]"
        )
    body = "\n".join(out)
    tail = late_errors + hints
    if tail:
        # Not stripped: a truncated page must end exactly where the resume offset starts.
        body = body + "\n\n" + "\n".join(tail)
    return body


@task(
    purpose="Render one page of the indexed-doc listing, filtered by prefix and bounded by max_results and "
    "max_chars, ending with the next offset when docs remain",
    inputs="db path, max_chars, offset, max_results, prefix",
    outputs="One line per doc plus a next-offset line",
)
def _list_docs_page(
    path: Path,
    max_chars: int | None,
    offset: int,
    max_results: int | None,
    prefix: str | None,
) -> str:
    """Render one page of ``doc_id="list"``.

    Args:
        path: Path to the axiom-graph DB.
        max_chars: Character budget, or ``None``.
        offset: Docs to skip.
        max_results: Most docs to return, or ``None``.
        prefix: Keep only doc ids starting with it, with or without the
            ``project::`` part.

    Returns:
        One line per doc, then a next-offset line when docs remain.
    """
    docs = db.list_docs(path)
    if not docs:
        return "(no docs indexed)"
    if prefix:
        docs = [d for d in docs if d["id"].startswith(prefix) or d["id"].split("::", 1)[-1].startswith(prefix)]
        if not docs:
            return f"(no docs match prefix '{prefix}')"
    total = len(docs)
    if offset >= total:
        return f"(no docs at offset {offset}; {total} in total)"
    page = docs[offset:] if max_results is None else docs[offset : offset + max_results]
    lines: list[str] = []
    used = 0
    for d in page:
        # Doc ids already start with their docs root (``docs/...``,
        # ``.pev/...``); the file path is listed as well so the id can be
        # matched to a file on disk.
        line = f"{d['id']}  {d['title']}" + (f"  [{d['file_path']}]" if d.get("file_path") else "")
        if max_chars is not None and lines and used + 1 + len(line) > max_chars:
            break
        used += len(line) + (1 if lines else 0)
        lines.append(line)
    nxt = offset + len(lines)
    if nxt < total:
        lines.append("")
        lines.append(f"[{nxt - offset} of {total} docs shown; {total - nxt} more -- next: offset={nxt}]")
    return "\n".join(lines)


def _subtree_size(sizes: dict[str, int], order: list[str], sec_id: str) -> int:
    """Rendered length of *sec_id* and its descendants, joined as ``read_doc`` joins them.

    Args:
        sizes: Rendered block length per section id.
        order: The doc's section ids in depth-first order.
        sec_id: The subtree root.

    Returns:
        The character count ``read_doc`` spends on the subtree.
    """
    members = [sid for sid in order if sid == sec_id or _is_descendant(sid, sec_id)]
    return sum(sizes[sid] for sid in members) + len(members) - 1


@task(
    purpose="Render read targets as a section outline -- one line per section with its full id, heading, rendered "
    "subtree size, child count and any non-VERIFIED status -- cut at a line boundary by the character budget",
    inputs="db path, read targets, max_chars",
    outputs="Outline text, with an omitted-ids hint when the budget cuts it",
)
def _pack_outline(
    path: Path, targets: list["_ReadTarget | str"], max_chars: int | None, shown_status: _ReadShown
) -> str:
    """Render read targets as an outline: section ids, headings and sizes, no bodies.

    Each doc opens with a header line (doc id, title, section count, total
    rendered size).  Each section line is indented by depth and gives the
    section's full id, heading and the rendered size of its subtree -- the
    count a ``read_doc`` of that section spends against ``max_chars``.  A
    line ends with ``[STATUS]`` only when the section's own or link status
    is not VERIFIED; so does a doc's header line, for its envelope.  Hints
    are not counted against the budget.

    Args:
        path: Path to the axiom-graph DB.
        targets: Read targets and ``ERROR`` strings, in output order.
        max_chars: Character budget, or ``None`` for no budget.
        shown_status: The read's refresh and linked nodes
            (:func:`_refresh_read_targets`), naming every section of each
            target doc.

    Returns:
        The outline, with an omitted-ids hint when cut.
    """
    rr = shown_status.refresh
    out: list[str] = []
    used = 0
    shown = 0
    stopped = False
    omitted: list[str] = []
    late_errors: list[str] = []
    total = sum(len(t.sections) for t in targets if isinstance(t, _ReadTarget))

    def _emit(text: str) -> None:
        nonlocal used
        used += len(text) + (1 if out else 0)
        out.append(text)

    for i, target in enumerate(targets):
        if isinstance(target, str):
            if stopped:
                late_errors.append(target)
                continue
            if i > 0:
                _emit(_DOC_SEPARATOR)
            _emit(target)
            continue
        if stopped:
            omitted.extend(s["id"] for s in target.sections)
            continue
        doc_secs = db.get_doc_sections(path, target.doc_id)
        order = [s["id"] for s in doc_secs]
        sizes = {
            s["id"]: len(_render_section_block(path, target.doc_id, s, linked=shown_status.linked[s["id"]], tag=rr.tag))
            for s in doc_secs
        }
        children: dict[str, int] = {}
        for s in doc_secs:
            if s["parent_id"]:
                children[s["parent_id"]] = children.get(s["parent_id"], 0) + 1
        tags, file_path = _doc_meta(path, target.doc_id)
        meta = _render_doc_meta(target.doc_id, tags, file_path, rr.tag(target.doc_id))
        # The length of a whole-doc read: header, meta line and every block, newline-joined.
        doc_total = len(_render_doc_header(target.title)) + len(meta) + sum(sizes.values()) + len(doc_secs) + 1
        base = min((s["depth"] for s in target.sections), default=0)

        mark = len(out)
        if i > 0:
            _emit(_DOC_SEPARATOR)
        if target.note:
            _emit(target.note)
        tag_note = f"  [tags: {', '.join(tags)}]" if tags else ""
        _emit(
            f"{target.doc_id}  {target.title}  ({len(doc_secs)} sections, {doc_total:,} chars){tag_note}"
            f"{rr.tag(target.doc_id)}"
        )
        shown_here = 0
        for sec in target.sections:
            sid = sec["id"]
            if stopped:
                omitted.append(sid)
                continue
            n_children = children.get(sid, 0)
            detail = f"{_subtree_size(sizes, order, sid):,} chars"
            if n_children:
                detail += f", {n_children} subsection{'s' if n_children != 1 else ''}"
            line = f"{'  ' * (sec['depth'] - base)}- {sid}  {sec['heading']}  ({detail}){rr.tag(sid)}"
            if max_chars is None or used + 1 + len(line) <= max_chars:
                _emit(line)
                shown += 1
                shown_here += 1
                continue
            stopped = True
            omitted.append(sid)
        if stopped and shown_here == 0:
            # Nothing of this doc made it in: drop its separator and header.
            del out[mark:]

    hints: list[str] = []
    if omitted:
        hints.append(
            f"[read_doc outline: {shown} of {total} sections shown (max_chars={max_chars}); "
            f"{len(omitted)} omitted. Outline the rest with section_ids={json.dumps(_drop_covered(omitted))}, "
            f"outline=True]"
        )
    body = "\n".join(out)
    tail = late_errors + hints
    if tail:
        body = body + "\n\n" + "\n".join(tail)
    return body


@workflow(
    purpose="Read DocJSON docs, sections or the doc list as Markdown within a character budget, naming what to "
    "read next when output is cut",
    inputs="project root; doc_id / doc_ids / section_ids / section slug; max_chars, offset, max_results, prefix, "
    "outline",
    outputs="Rendered Markdown with budget hints, a section outline, a doc-list page, or an ERROR string",
)
def axiom_graph_read_doc(
    project_root: str,
    doc_id: str = "",
    section: str | None = None,
    doc_ids: list[str] | None = None,
    section_ids: list[str] | None = None,
    max_chars: int | None = READ_DOC_MAX_CHARS,
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
    logger.debug(
        "axiom_graph_read_doc: doc_id=%s, section=%s, batch=%s, section_ids=%s, max_chars=%s, offset=%s, outline=%s",
        doc_id,
        section,
        len(doc_ids) if doc_ids else "no",
        len(section_ids) if section_ids else "no",
        max_chars,
        offset,
        outline,
    )
    口 = Step(
        step_num=1,
        name="Validate budget and offset",
        purpose="Reject a non-positive max_chars, a negative offset, or an offset with outline before touching "
        "the index",
    )
    if max_chars is not None and max_chars <= 0:
        return "ERROR: max_chars must be positive, or None to disable the budget"
    if offset < 0:
        return "ERROR: offset must be >= 0"
    if outline and offset:
        return "ERROR: offset does not apply to outline=True -- an outline is cut by section, not by character"

    path = require_db(project_root)

    with db.operation_connection(path):
        targets: list[_ReadTarget | str]
        if section_ids is not None:
            if not section_ids:
                return "ERROR: section_ids list is empty"
            if offset and len(section_ids) != 1:
                return _OFFSET_NEEDS_ONE
            口 = AutoStep(step_num=2, name="Resolve section_ids")
            targets = _resolve_section_ids(path, section_ids)
        elif doc_ids is not None:
            if not doc_ids:
                return "ERROR: doc_ids list is empty"
            if offset:
                return _OFFSET_NEEDS_ONE
            口 = Step(
                step_num=3,
                name="Resolve doc_ids batch",
                purpose="Resolve each doc in turn; a failing id becomes an ERROR in its slot instead of sinking the batch",
            )
            targets = []
            for did in doc_ids:
                try:
                    口 = AutoStep(step_num=3.1, name="Resolve one batch doc")
                    resolved = _resolve_doc_target(path, did, section)
                except Exception as exc:  # noqa: BLE001 -- one bad id must not sink the batch
                    resolved = f"ERROR ({did}): {exc}"
                targets.append(resolved)
        elif doc_id == "list":
            if outline:
                return 'ERROR: outline=True does not apply to doc_id="list", which already lists docs'
            口 = AutoStep(step_num=4, name="List docs")
            page = _list_docs_page(path, max_chars, offset, max_results, prefix)
            return page
        elif not doc_id:
            return "ERROR: pass doc_id, doc_ids, or section_ids"
        else:
            口 = AutoStep(step_num=5, name="Resolve single doc")
            target = _resolve_doc_target(path, doc_id, section)
            if isinstance(target, str):
                return target
            if offset and (section is None or target.note is not None):
                return _OFFSET_NEEDS_ONE
            targets = [target]

        口 = AutoStep(step_num=6, name="Refresh what the read shows")
        shown = _refresh_read_targets(path, Path(project_root).resolve(), targets, outline=outline)

        口 = Step(
            step_num=7,
            name="Check resume offset",
            purpose="An offset must fall inside the first target section's rendered text",
        )
        if offset:
            first = targets[0]
            if isinstance(first, str):
                return first
            sec0 = first.sections[0]
            length = len(
                _render_section_block(path, first.doc_id, sec0, linked=shown.linked[sec0["id"]], tag=shown.refresh.tag)
            )
            if offset >= length:
                return f"ERROR: offset {offset} is past the end of section {first.sections[0]['id']} ({length} chars)"
        if not outline:
            口 = AutoStep(step_num=8, name="Render within budget")
            text = _pack_read(path, targets, max_chars, offset, shown)
            return _with_read_notes(text, shown)
        口 = AutoStep(step_num=9, name="Render outline")
        text = _pack_outline(path, targets, max_chars, shown)
        return _with_read_notes(text, shown)


@dataclass
class _DocEdit:
    """One doc being edited in memory by section edits, and what its save must be told."""

    doc_node_id: str
    project_part: str
    data: dict
    json_file: Path
    pre_text: str | None = None
    #: Pre-write dot-path -> current dot-path, for every section an item renamed.
    renames: dict[str, str] = field(default_factory=dict)
    targets: set[str] = field(default_factory=set)
    addresses: dict[str, set[str]] = field(default_factory=dict)
    renamed: bool = False
    #: This doc's rename link-rewrite results (files left unchecked or unrewritten).
    link_patches: list[LinkPatchResult] = field(default_factory=list)

    def move(self, moves: dict[str, str]) -> None:
        """Record renames (current dot-path -> new dot-path), composing them with earlier ones."""
        inverse = {cur: orig for orig, cur in self.renames.items()}
        for cur, new in moves.items():
            self.renames[inverse.get(cur, cur)] = new
        self.targets = {moves.get(t, t) for t in self.targets}
        self.addresses = {moves.get(dot, dot): names for dot, names in self.addresses.items()}
        self.renamed = True

    def target(self, dot: str, names: list[str]) -> None:
        """Record that the edit wrote *dot* content-wise, naming *names* as offenders it reconciles."""
        self.targets.add(dot)
        if names:
            self.addresses.setdefault(dot, set()).update(names)

    def addresses_arg(self) -> dict[str, list[str]] | None:
        """The ``addresses`` argument for :func:`save_and_reindex`."""
        return {dot: sorted(names) for dot, names in self.addresses.items()} or None


_UPDATE_ITEM_KEYS = frozenset(
    {"section_id", "content", "heading", "new_id", "after", "tags", "expected_hash", "content_file", "addresses"}
)
_PATCH_ITEM_KEYS = frozenset({"section_id", "new_string", "anchor", "old_string", "content_file", "addresses"})


def _parse_update_item(root: Path, item: dict) -> dict | str:
    """Resolve an ``update_section`` item's inputs before any doc is loaded.

    Args:
        root: Absolute project root (for ``content_file``).
        item: The single-call keys.

    Returns:
        The item with ``content`` resolved and footer-stripped, ``names`` (the
        sorted ``addresses``) and ``footer_stripped`` added; or ``ERROR: ...``.
    """
    try:
        content = _resolve_text_input(root, item.get("content"), item.get("content_file"), "content", "content_file")
    except WriteInputError as exc:
        return f"ERROR: {exc}"
    content, footer_stripped = strip_linked_nodes_footer(content)
    names = sorted(set(item.get("addresses") or []))
    if content is None and not names and all(item.get(k) is None for k in ("heading", "new_id", "after", "tags")):
        return (
            "ERROR: at least one of 'content' (or 'content_file'), 'heading', 'new_id', 'after', 'tags' or "
            "'addresses' must be provided"
        )
    return {**item, "content": content, "names": names, "footer_stripped": footer_stripped}


def _apply_update_item(
    doc: _DocEdit, sec_raw_id: str, section_id: str, item: dict
) -> str | tuple[list[str], str, str | None]:
    """Apply one parsed ``update_section`` item to an in-memory doc.

    Args:
        doc: The doc being edited (mutated in place on success).
        sec_raw_id: The target's dot-path as the doc now stands.
        section_id: The item's full section id (for error text).
        item: A :func:`_parse_update_item` result.

    Returns:
        ``(changes, written_dot, content)`` -- the change labels, the
        section's dot-path and its content after the edit -- or
        ``ERROR: ...``.  A refused item may have
        left the doc partly changed; the caller then writes nothing.
    """
    sections: list[dict] = doc.data.get("sections", [])
    chain = _locate_section(sections, sec_raw_id, f"ERROR: section '{sec_raw_id}' not found in {doc.json_file.name}")
    if isinstance(chain, str):
        return chain
    target = chain[-1]
    siblings: list[dict] = chain[-2]["sections"] if len(chain) > 1 else sections

    expected_hash = item.get("expected_hash")
    if expected_hash is not None:
        current = target.get("content") or ""
        current_hash = content_hash(current)
        if current_hash != expected_hash.strip().lower():
            return (
                f"ERROR: expected_hash does not match the stored content of {section_id} -- "
                f"the section changed since it was read; nothing was written.\n"
                f"  content_hash: {current_hash}\n"
                f"--- current content ---\n{current}"
            )

    changes: list[str] = []
    written_dot = sec_raw_id
    new_id = item.get("new_id")
    if new_id is not None:
        old_short_id = target["id"]  # the target's own id (it may contain a dot)
        if not _SLUG_RE.match(new_id):
            return f"ERROR: new_id '{new_id}' is not slug-safe (must be lowercase alphanumeric plus hyphens)"
        if new_id in {s.get("id") for s in siblings if s is not target}:
            return f"ERROR: sibling section '{new_id}' already exists"
        # Map the target and every cascaded descendant to its new dot-path.
        written_dot = sec_raw_id[: len(sec_raw_id) - len(old_short_id)] + new_id
        moves = {sec_raw_id: written_dot}
        for child_dot in _section_contents(target.get("sections") or [], sec_raw_id):
            moves[child_dot] = written_dot + child_dot[len(sec_raw_id) :]
        # The renamed section and its cascaded children must not take a
        # dot-path another section already spells (``z`` > ``b`` renamed to
        # ``a`` beside a flat ``a.b``).
        collisions_before = _dot_path_counts(sections)
        target["id"] = new_id
        collision_err = _dot_path_collision_error(sections, collisions_before)
        if collision_err:
            return collision_err
        doc.move(moves)
        changes.append(f"id ({old_short_id} → {new_id})")

    after = item.get("after")
    if after is not None:
        after_idx = next((i for i, s in enumerate(siblings) if s.get("id") == after), None)
        if after_idx is None:
            return f"ERROR: sibling '{after}' not found among siblings of '{sec_raw_id}' for 'after' reorder"
        target_idx = next((i for i, s in enumerate(siblings) if s is target), None)
        if target_idx is not None and target_idx != after_idx:
            siblings.pop(target_idx)
            after_idx = next(i for i, s in enumerate(siblings) if s.get("id") == after)
            siblings.insert(after_idx + 1, target)
        # after=self is a no-op
        changes.append("reorder")

    if item.get("tags") is not None:
        target["tags"] = item["tags"]
        changes.append("tags")
    if item.get("content") is not None:
        target["content"] = item["content"]
        changes.append("content")
    if item.get("heading") is not None:
        target["heading"] = item["heading"]
        changes.append("heading")

    doc.target(written_dot, item["names"])
    if item["names"]:
        changes.append("addresses")
    return changes, written_dot, target.get("content")


def _parse_patch_item(root: Path, item: dict) -> dict | str:
    """Resolve and validate a ``patch_section`` item's inputs before any doc is loaded.

    Args:
        root: Absolute project root (for ``content_file``).
        item: The single-call keys.

    Returns:
        The item with ``new_string`` resolved and footer-stripped, ``names``
        and ``footer_stripped`` added; or ``ERROR: ...``.
    """
    new_string = item.get("new_string")
    anchor = item.get("anchor")
    old_string = item.get("old_string")
    if item.get("content_file") is not None:
        if new_string:
            return "ERROR: provide 'new_string' or 'content_file', not both"
        try:
            new_string = read_content_file(root, item["content_file"])
        except WriteInputError as exc:
            return f"ERROR: {exc}"
    if new_string is None:
        return "ERROR: provide 'new_string' or 'content_file'"
    # Only the incoming text is checked for a pasted footer; existing content is kept as written.
    new_string, footer_stripped = strip_linked_nodes_footer(new_string)

    if anchor is not None and old_string is not None:
        return "ERROR: provide exactly one of 'anchor' or 'old_string', not both"
    if anchor is None and old_string is None:
        return "ERROR: provide exactly one of 'anchor' ('$' append / '^' prepend) or 'old_string' (replace)"
    if anchor is not None and anchor not in ("$", "^"):
        return f"ERROR: anchor must be '$' (append/end) or '^' (prepend/start) -- got '{anchor}'"
    if old_string is not None and old_string == "":
        return "ERROR: old_string must not be empty"
    names = sorted(set(item.get("addresses") or []))
    return {**item, "new_string": new_string, "names": names, "footer_stripped": footer_stripped}


def _apply_patch_item(doc: _DocEdit, sec_raw_id: str, section_id: str, item: dict) -> str | tuple[str, str, int]:
    """Apply one parsed ``patch_section`` item to an in-memory doc.

    Args:
        doc: The doc being edited (mutated in place on success).
        sec_raw_id: The target's dot-path.
        section_id: The item's full section id (unused; kept for the applier signature).
        item: A :func:`_parse_patch_item` result.

    Returns:
        ``(mode_desc, new_content, start)`` -- the mode label, the section's
        new content and where the inserted text starts in it -- or
        ``ERROR: ...`` (the doc is untouched).
    """
    del section_id
    sections: list[dict] = doc.data.get("sections", [])
    chain = _locate_section(sections, sec_raw_id, f"ERROR: section '{sec_raw_id}' not found in {doc.json_file.name}")
    if isinstance(chain, str):
        return chain
    target = chain[-1]
    existing = target.get("content", "") or ""
    new_string, anchor, old_string = item["new_string"], item.get("anchor"), item.get("old_string")

    if anchor == "$":
        if existing == "":
            new_content = new_string
        elif existing.endswith("\n"):
            new_content = existing + new_string
        else:
            new_content = existing + "\n" + new_string
        start = len(new_content) - len(new_string)
        mode_desc = "appended to"
    elif anchor == "^":
        if existing == "":
            new_content = new_string
        elif new_string.endswith("\n"):
            new_content = new_string + existing
        else:
            new_content = new_string + "\n" + existing
        start = 0
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
        start = existing.index(old_string)
        mode_desc = "replaced in"

    target["content"] = new_content
    doc.target(sec_raw_id, item["names"])
    if item["names"]:
        mode_desc += ", addresses"
    return mode_desc, new_content, start


def _load_doc_edit(path: Path, root: Path, project_part: str, doc_node_id: str) -> _DocEdit | str:
    """Load one doc for a single-section edit, as :func:`load_doc_json` does."""
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, _doc_node = loaded
    return _DocEdit(doc_node_id, project_part, data, json_file)


def _save_doc_edit(path: Path, root: Path, doc: _DocEdit, prepared: _PreWrite | None = None) -> list[str]:
    """Save and re-index an edited doc once (see :func:`save_and_reindex`); return its raw-edit ids."""
    return save_and_reindex(
        doc.data,
        doc.json_file,
        path,
        root,
        doc.project_part,
        cleanup_doc_node_id=doc.doc_node_id if doc.renamed else None,
        doc_node_id=doc.doc_node_id,
        renames=doc.renames,
        targets=set(doc.targets),
        addresses=doc.addresses_arg(),
        prepared=prepared,
        link_patches=doc.link_patches,
    )


@task(
    purpose="Apply a batch of update_section / patch_section items across any number of docs, all or none: parse "
    "every item, lock every touched doc (and every doc a rename re-points) in one sorted acquisition, apply the "
    "items in order, validate every doc's addresses, then save and re-index each file once",
    inputs="db path, project root, edit items, the tool's item keys, parser, applier and result renderer",
    outputs="The batch result text, or ERROR naming the failing item (nothing written)",
)
def _batch_section_edits(path: Path, root: Path, edits, keys: frozenset, parse, apply, render, verb: str) -> str:
    """Apply a list of section edits across any number of docs: all of them, or none.

    Every item is parsed, then every touched doc's write lock is taken in
    sorted lock-path order, each file is read and parsed once, the items are
    applied in list order (each checked against the doc as the earlier items
    left it), every doc's ``addresses`` are validated, and only then is each
    file saved and re-indexed once.  The whole call runs on the operation's
    one connection; doc nodes are read in one batched query.

    Args:
        path: Path to the axiom-graph DB.
        root: Absolute project root.
        edits: The caller's list of items.
        keys: The item keys the tool accepts.
        parse: The tool's item parser (``_parse_update_item`` / ``_parse_patch_item``).
        apply: The tool's item applier (``_apply_update_item`` / ``_apply_patch_item``).
        render: ``(section_id, applied) -> str``, the item's result line.
        verb: The header's verb (``Updated`` / ``Patched``).

    Returns:
        ``<verb> N section(s) in M doc(s)``, one line per item, then the per-section still-LINKED_STALE
        notes and the stripped-footer and raw-edit notes; or ``ERROR: ...``
        naming the failing item (nothing written).
    """
    if not isinstance(edits, list) or not edits:
        return "ERROR: 'edits' must be a non-empty list of section edit items"

    def _label(idx: int, item, err: str) -> str:
        sid = item.get("section_id") if isinstance(item, dict) else None
        return f"ERROR: edits[{idx}] ({sid!r}): {err.removeprefix('ERROR: ')} -- nothing was written"

    口 = Step(step_num=1, name="Parse every item", purpose="Resolve inputs and section ids before any doc is read")
    roots = docs_roots_for(root)
    parsed_items: list[tuple[dict, tuple[str, str, str, str]]] = []
    for idx, item in enumerate(edits):
        if not isinstance(item, dict):
            return _label(idx, item, "edit item must be an object")
        unknown = set(item) - keys
        if unknown:
            return _label(idx, item, f"unknown edit item key(s): {', '.join(sorted(unknown))}")
        sid = item.get("section_id")
        if not isinstance(sid, str) or not sid:
            return _label(idx, item, "needs a 'section_id'")
        one = parse(root, item)
        if isinstance(one, str):
            return _label(idx, item, one)
        ids = parse_section_id(sid, roots)
        if isinstance(ids, str):
            return _label(idx, item, ids)
        parsed_items.append((one, ids))

    口 = Step(
        step_num=2,
        name="Lock and load every touched doc",
        purpose="One batched doc-node read; the write locks of every touched doc and of every doc a section rename "
        "re-points, in one sorted lock-path acquisition; each file read once",
        critical="Sorted acquisition, so two batches cannot deadlock",
    )
    first_item = {}
    for idx, (_one, ids) in enumerate(parsed_items):
        first_item.setdefault(ids[3], idx)
    with db._connect(path) as conn:
        doc_nodes = db.get_nodes_conn(conn, list(first_item))
    docs: dict[str, _DocEdit] = {}
    for doc_id, idx in first_item.items():
        node = doc_nodes.get(doc_id)
        if node is None:
            return _label(idx, edits[idx], f"doc node not found in index: {doc_id}")
        json_file = root / node.location
        if not json_file.exists():
            return _label(idx, edits[idx], f"JSON doc file not found: {json_file}")
        docs[doc_id] = _DocEdit(doc_id, parsed_items[idx][1][0], {}, json_file)
    order = sorted(docs.values(), key=lambda d: str(d.json_file))
    # A rename rewrites other docs' links to the section: lock them too.
    renamed = [f"{ids[3]}::{ids[2]}" for one, ids in parsed_items if one.get("new_id")]
    lock_files = [doc.json_file for doc in order] + _rename_link_files(root, renamed)

    try:
        # Sorted acquisition under one deadline, released together on timeout.
        with lock_docs(root, lock_files):
            for doc in order:
                doc.pre_text = doc.json_file.read_text(encoding="utf-8", errors="replace")
                doc.data = json.loads(doc.pre_text)

            口 = Step(
                step_num=3,
                name="Apply the items in list order",
                purpose="Each item is checked against its doc as the earlier items left it",
            )
            applied: list = []
            for idx, (one, ids) in enumerate(parsed_items):
                doc = docs[ids[3]]
                inverse = {cur: orig for orig, cur in doc.renames.items()}
                if ids[2] in doc.renames and ids[2] not in inverse:
                    return _label(idx, edits[idx], f"section '{ids[2]}' was renamed earlier in this call")
                result = apply(doc, ids[2], edits[idx]["section_id"], one)
                if isinstance(result, str):
                    return _label(idx, edits[idx], result)
                applied.append(result)

            口 = Step(
                step_num=4,
                name="Validate addresses for every doc",
                purpose="The pre-write snapshot and offender check of every doc, before the first file is written",
                critical="An invalid addresses name in any doc refuses the whole call",
            )
            prepared: dict[str, _PreWrite] = {}
            for doc in order:
                try:
                    prepared[doc.doc_node_id] = _prepare_write(
                        doc.data,
                        doc.json_file,
                        path,
                        root,
                        doc.doc_node_id,
                        doc.renames,
                        doc.targets,
                        doc.addresses_arg(),
                        pre_text=doc.pre_text,
                    )
                except AddressesError as exc:
                    return str(exc)

            口 = Step(step_num=5, name="Save each doc once", purpose="One write and re-index per touched file")
            raw_edits: list[str] = []
            failed: list[str] = []
            for doc in order:
                try:
                    raw_edits += _save_doc_edit(path, root, doc, prepared[doc.doc_node_id])
                except OSError as exc:
                    failed.append(f"{doc.json_file.name}: {exc}")
    except DocLockTimeout as exc:
        logger.warning("batch section edit: %s -- nothing was written", exc)
        return f"ERROR: {exc} -- nothing was written"

    lines = [f"{verb} {len(parsed_items)} section(s) in {len(docs)} doc(s)"]
    lines += [render(edits[idx]["section_id"], applied[idx]) for idx in range(len(parsed_items))]
    finals = list(
        dict.fromkeys(
            f"{docs[ids[3]].doc_node_id}::{docs[ids[3]].renames.get(ids[2], ids[2])}" for _o, ids in parsed_items
        )
    )
    notes = _still_linked_stale_notes(path, root, finals)
    lines += [f"  {sid}: {notes[sid]}" for sid in finals if sid in notes]
    if failed:
        lines.insert(0, f"ERROR: {len(failed)} doc(s) not written: {'; '.join(failed)}")
    stripped = sum(int(one["footer_stripped"]) for one, _ids in parsed_items)
    patches = [p for doc in order for p in doc.link_patches]
    return "\n".join(lines) + _strip_note(stripped) + _raw_edit_note(raw_edits) + _unpatched_links_note(patches)


@_locks_doc
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
    existing sibling, nor give the renamed section or a cascaded child a
    dot-path another section already spells (renaming ``z`` with child ``b``
    to ``a`` beside a flat ``a.b``).  A collision already in the file does
    not block a rename that adds none.

    **Batch mode** (``edits=[...]``) applies many edits, across any number of
    docs, in one call: every item is checked first -- each against its doc
    as the earlier items left it, ``expected_hash`` and ``addresses``
    included -- and only then is each touched file written and re-indexed
    once.  If any item is invalid the call returns an error naming it and
    writes nothing.  A batch leaves the same files and statuses as the same
    edits made one call at a time.

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
        expected_hash: Optional sha256 hex of the section content the caller
            last saw (every write result reports it as ``content_hash``).
            Checked under the doc's write lock; when the stored content no
            longer matches, nothing is written and the error carries the
            current content and hash.
        content_file: Optional path to a UTF-8 file whose text becomes the
            section content -- use it for long or quote-heavy bodies instead
            of an inline ``content`` string.  Mutually exclusive with
            ``content``.  Must resolve under the project root or the system
            temp directory; a byte-order mark is dropped.
        addresses: Optional ids of the section's current offenders this
            edit reconciles (the ``via=`` / ``root=`` ids ``drift_query``
            lists).  Each named offender the section's receipts cover is
            refreshed to its indexed state; the section clears once no
            offender is left.  A doc section on a doc-to-doc chain is
            accepted and refreshes nothing: it clears when that section
            does.  Without it the edit verifies the text only and clears no
            LINKED_STALE.  A name that is not a current offender is an
            error, and nothing is written.
            May be the only argument: it reconciles without editing.
        edits: Batch mode -- a list of items, each an object with this
            tool's single-call keys (``section_id`` required; ``content`` /
            ``content_file``, ``heading``, ``new_id``, ``after``, ``tags``,
            ``expected_hash``, ``addresses``).  When given, leave the
            single-call parameters unset.  An item's ``section_id`` names the
            section as the earlier items left it (after a rename, its new id).

    A pasted ``read_doc`` linked-nodes footer in the new content is removed
    before storing (links live in the section's ``links`` array); the result
    says so when it happens.

    Returns:
        ``Updated ... for section: <id>`` plus a ``content_hash: <sha256>``
        line for the section as stored, or an ``ERROR: ...`` string.  In
        batch mode, ``Updated N section(s) in M doc(s)`` then one
        ``<section id>  (<changes>)  content_hash: <sha256>`` line per item
        (the hash as that item left the section) and a
        ``<section id>: still LINKED_STALE ...`` line per section still stale.
    """
    logger.debug("axiom_graph_update_section: section_id=%s", section_id)

    path = require_db(project_root)
    root = Path(project_root).resolve()

    if edits is not None:
        singles = (content, heading, new_id, after, tags, expected_hash, content_file, addresses)
        if section_id or any(v is not None for v in singles):
            return "ERROR: pass either 'edits' (batch) or the single-section parameters, not both"

        def _render(sid: str, applied: tuple[list[str], str, str | None]) -> str:
            return f"  {sid}  ({', '.join(applied[0])})  content_hash: {content_hash(applied[2])}"

        return _batch_section_edits(
            path, root, edits, _UPDATE_ITEM_KEYS, _parse_update_item, _apply_update_item, _render, "Updated"
        )

    one = _parse_update_item(
        root,
        {
            "content": content,
            "content_file": content_file,
            "heading": heading,
            "new_id": new_id,
            "after": after,
            "tags": tags,
            "expected_hash": expected_hash,
            "addresses": addresses,
        },
    )
    if isinstance(one, str):
        return one

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id, docs_roots_for(root))
    if isinstance(parsed, str):
        return parsed
    project_part, _doc_path_slug, sec_raw_id, doc_node_id = parsed

    doc = _load_doc_edit(path, root, project_part, doc_node_id)
    if isinstance(doc, str):
        return doc
    applied = _apply_update_item(doc, sec_raw_id, section_id, one)
    if isinstance(applied, str):
        return applied
    changes, written_dot, stored = applied

    try:
        raw_edits = _save_doc_edit(path, root, doc)
    except AddressesError as exc:
        return str(exc)

    return (
        f"Updated {', '.join(changes)} for section: {section_id}\n  content_hash: {content_hash(stored)}"
        + _still_linked_stale_note(path, root, f"{doc_node_id}::{written_dot}")
        + _strip_note(int(one["footer_stripped"]))
        + _raw_edit_note(raw_edits)
        + _unpatched_links_note(doc.link_patches)
    )


@_locks_doc
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

    **Batch mode** (``edits=[...]``) applies many patches, across any number
    of docs, in one call: every item is checked first -- each against its
    doc as the earlier items left it, ``addresses`` included -- and only then
    is each touched file written and re-indexed once.  If any item is invalid
    (unknown section, non-unique ``old_string``, a name that is not an
    offender) the call returns an error naming it and writes nothing.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs/architecture::database-layer``.  Dot-path
            notation for nested sections is supported, exactly as in
            ``update_section``.
        new_string: The content to add (append / prepend modes) or the
            replacement string (replace mode).  Inserted verbatim -- never
            parsed for anchor sentinels.  Named to mirror ``Edit``'s
            ``old_string`` / ``new_string`` pair.  Required unless
            ``content_file`` is given.
        anchor: ``"$"`` to append at the end, ``"^"`` to prepend at the start.
            Mutually exclusive with ``old_string``.
        old_string: Replace-mode target.  Must match exactly once within the
            section's current content; missing or non-unique is a hard error
            and the section is left unchanged.  Mutually exclusive with
            ``anchor``.
        content_file: Optional path to a UTF-8 file whose text is used as
            ``new_string`` (omit ``new_string``).  Must resolve under the
            project root or the system temp directory; a byte-order mark is
            dropped.
        addresses: Optional ids of the section's current offenders this
            edit reconciles (the ``via=`` / ``root=`` ids ``drift_query``
            lists).  Each named offender the section's receipts cover is
            refreshed to its indexed state; the section clears once no
            offender is left.  A doc section on a doc-to-doc chain is
            accepted and refreshes nothing: it clears when that section
            does.  Without it the edit verifies the text only and clears no
            LINKED_STALE.  A name that is not a current offender is an
            error, and nothing is written.
        edits: Batch mode -- a list of items, each an object with this
            tool's single-call keys (``section_id`` required; ``new_string``
            / ``content_file``, ``anchor`` or ``old_string``,
            ``addresses``).  When given, leave the single-call parameters
            unset.

    A pasted ``read_doc`` linked-nodes footer in the incoming text
    (``new_string`` / ``content_file``) is removed before it is applied; the
    section's existing content is left as it is.  The result says so when it
    happens.

    Returns:
        A confirmation string naming the mode and section, the new
        ``content_hash``, the section's new length in characters and lines,
        and a fenced ``edited region (lines A-B of N):`` block holding the
        inserted or replaced text plus up to 2 lines of context either side
        (a region over 40 lines keeps its first and last 10 around an
        omitted-lines marker) -- text copied from it matches as a next
        ``old_string``.  On validation / match failure, an ``ERROR:`` string
        (the section is untouched on error).  In batch mode,
        ``Patched N section(s) in M doc(s)`` then one ``<section id>
        (<mode>)  content_hash: <sha256>  length: <chars> chars, <lines>
        lines`` line per item (as that item left the section) and a
        ``<section id>: still LINKED_STALE ...`` line per section still stale.
    """
    logger.debug("axiom_graph_patch_section: section_id=%s anchor=%s", section_id, anchor)

    path = require_db(project_root)
    root = Path(project_root).resolve()

    if edits is not None:
        if section_id or any(v is not None for v in (new_string, anchor, old_string, content_file, addresses)):
            return "ERROR: pass either 'edits' (batch) or the single-section parameters, not both"

        def _render(sid: str, applied: tuple[str, str, int]) -> str:
            mode_desc, new_content, _start = applied
            return (
                f"  {sid}  ({mode_desc})  content_hash: {content_hash(new_content)}"
                f"  length: {len(new_content)} chars, {new_content.count(chr(10)) + 1} lines"
            )

        return _batch_section_edits(
            path, root, edits, _PATCH_ITEM_KEYS, _parse_patch_item, _apply_patch_item, _render, "Patched"
        )

    one = _parse_patch_item(
        root,
        {
            "new_string": new_string,
            "anchor": anchor,
            "old_string": old_string,
            "content_file": content_file,
            "addresses": addresses,
        },
    )
    if isinstance(one, str):
        return one

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id, docs_roots_for(root))
    if isinstance(parsed, str):
        return parsed
    project_part, _doc_path_slug, sec_raw_id, doc_node_id = parsed

    doc = _load_doc_edit(path, root, project_part, doc_node_id)
    if isinstance(doc, str):
        return doc
    applied = _apply_patch_item(doc, sec_raw_id, section_id, one)
    if isinstance(applied, str):
        return applied
    mode_desc, new_content, start = applied

    try:
        raw_edits = _save_doc_edit(path, root, doc)
    except AddressesError as exc:
        return str(exc)

    line_count = new_content.count("\n") + 1
    return (
        f"Patched ({mode_desc}) section: {section_id}\n  content_hash: {content_hash(new_content)}"
        f"\n  length: {len(new_content)} chars, {line_count} lines"
        + _still_linked_stale_note(path, root, f"{doc_node_id}::{sec_raw_id}")
        + _strip_note(int(one["footer_stripped"]))
        + _raw_edit_note(raw_edits)
        + _edited_region(new_content, start, start + len(one["new_string"]))
    )


@_locks_doc
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

    **Batch mode** (``sections=[...]``) adds several sections in one call:
    every item is checked against the doc as the earlier items left it (so
    an item may nest under, or sit after, a section added earlier in the
    same list), and then the doc is written and re-indexed once.  If any
    item is invalid the call returns an error naming it and writes nothing.
    A new section whose dot-path another section already spells (a ``b``
    nested under ``a`` beside a flat ``a.b``) is refused the same way; a
    collision already in the file does not block an insertion that adds none.

    The writer verifies what it creates: every new section comes out
    VERIFIED.  A pasted ``read_doc`` linked-nodes footer in the content is
    removed before storing; the result says so when it happens.

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
        sections: Batch mode -- a list of items, each an object with the
            single-call keys ``section_id``, ``heading`` and optional
            ``content`` / ``content_file``, ``parent_id``, ``after``.  When
            given, leave the single-section parameters unset.
        content_file: Optional path to a UTF-8 file whose text becomes the
            content (instead of ``content``; not both).  Must resolve under
            the project root or the system temp directory; a byte-order mark
            is dropped.

    Returns:
        ``Added section '<id>' to <doc>`` (or ``Added N section(s) to <doc>``
        in batch mode) followed by one ``<dot-path>  content_hash: <sha256>``
        line per new section, or an ``ERROR: ...`` string.
    """
    path = require_db(project_root)
    root = Path(project_root).resolve()

    if sections is not None:
        if not isinstance(sections, list) or not sections:
            return "ERROR: 'sections' must be a non-empty list of section items"
        if section_id or heading or any(v is not None for v in (content, parent_id, after, content_file)):
            return "ERROR: pass either 'sections' (batch) or the single-section parameters, not both"
        items = sections
    else:
        items = [
            {
                "section_id": section_id,
                "heading": heading,
                "content": content,
                "parent_id": parent_id,
                "after": after,
                "content_file": content_file,
            }
        ]

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    # Apply every item to the in-memory doc first; any invalid item aborts
    # the whole call before anything touches disk.
    added: list[tuple[str, str | None]] = []
    stripped = 0
    collisions_before = _dot_path_counts(data.get("sections") or [])
    for idx, item in enumerate(items):
        err = _insert_section(root, data.setdefault("sections", []), item)
        if isinstance(err, str):
            if sections is None:
                return err
            label = item.get("section_id") if isinstance(item, dict) else None
            return f"ERROR: sections[{idx}] ({label!r}): {err.removeprefix('ERROR: ')} -- nothing was written"
        dot_path, stored, was_stripped = err
        added.append((dot_path, stored))
        stripped += was_stripped

    # A new section must not take a dot-path another section already spells
    # (a nested ``a`` > ``b`` beside a flat ``a.b``).
    collision_err = _dot_path_collision_error(data["sections"], collisions_before)
    if collision_err:
        return collision_err

    # Save and re-index ONCE for every item
    project_id = doc_id.split("::")[0]
    raw_edits = save_and_reindex(
        data, json_file, path, root, project_id, doc_node_id=doc_id, targets={dot for dot, _ in added}
    )

    if sections is None:
        lines = [f"Added section '{section_id}' to {doc_id}"]
    else:
        lines = [f"Added {len(added)} section(s) to {doc_id}"]
    for dot_path, stored in added:
        lines.append(f"  {dot_path}  content_hash: {content_hash(stored)}")
    return "\n".join(lines) + _strip_note(stripped) + _raw_edit_note(raw_edits)


def _insert_section(root: Path, sections: list[dict], item: dict) -> str | tuple[str, str | None, bool]:
    """Validate one ``add_section`` item and insert it into the in-memory doc.

    Args:
        root: Absolute project root (for ``content_file``).
        sections: The doc's top-level ``sections`` list, mutated in place.
        item: ``section_id``, ``heading`` and optional ``content`` /
            ``content_file``, ``parent_id``, ``after``.

    Returns:
        An ``ERROR: ...`` string (nothing inserted), or ``(dot_path,
        stored_content, footer_stripped)`` for the inserted section.
    """
    from axiom_graph.docjson.parse import _MAX_DEPTH  # noqa: PLC0415

    if not isinstance(item, dict):
        return "ERROR: section item must be an object"
    unknown = set(item) - {"section_id", "heading", "content", "content_file", "parent_id", "after"}
    if unknown:
        return f"ERROR: unknown section item key(s): {', '.join(sorted(unknown))}"
    section_id = item.get("section_id") or ""
    heading = item.get("heading")
    parent_id = item.get("parent_id")
    after = item.get("after")

    # Validate slug format
    if not isinstance(section_id, str) or not _SLUG_RE.match(section_id):
        return (
            f"ERROR: section_id '{section_id}' is not slug-safe (must be lowercase alphanumeric plus hyphens, no dots)"
        )
    if not isinstance(heading, str) or not heading:
        return f"ERROR: section '{section_id}' needs a non-empty heading"
    try:
        content = _resolve_text_input(root, item.get("content"), item.get("content_file"), "content", "content_file")
    except WriteInputError as exc:
        return f"ERROR: {exc}"
    content, footer_stripped = strip_linked_nodes_footer(content)

    # Determine where to insert
    if parent_id is not None:
        parent_chain = _locate_section(sections, parent_id, f"ERROR: parent section '{parent_id}' not found")
        if isinstance(parent_chain, str):
            return parent_chain
        parent_sec = parent_chain[-1]
        # A top-level parent (chain of one) is depth 0, its child depth 1.
        parent_depth = len(parent_chain) - 1
    else:
        parent_sec = None
        parent_depth = -1  # top-level: new section will be at depth 0

    # The one depth check: the new section's depth must be <= _MAX_DEPTH
    new_depth = parent_depth + 1
    if new_depth > _MAX_DEPTH:
        return (
            f"ERROR: adding section at depth {new_depth} would exceed maximum nesting depth of {_MAX_DEPTH + 1} levels"
        )
    target_list = sections if parent_sec is None else parent_sec.setdefault("sections", [])

    # Check for ID collision with siblings
    if section_id in {s.get("id") for s in target_list}:
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

    dot_path = f"{parent_id}.{section_id}" if parent_id else section_id
    return dot_path, content, footer_stripped


@_locks_doc
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
    path = require_db(project_root)
    root = Path(project_root).resolve()

    # Parse section_id using shared helper
    parsed = parse_section_id(section_id, docs_roots_for(root))
    if isinstance(parsed, str):
        return parsed
    project_part, doc_path_slug, sec_raw_id, doc_node_id = parsed

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    # Find and remove the section from its parent's children
    chain = _locate_section(sections, sec_raw_id, f"ERROR: section '{sec_raw_id}' not found in {json_file.name}")
    if isinstance(chain, str):
        return chain
    target_list: list[dict] = chain[-2]["sections"] if len(chain) > 1 else sections
    target_list.pop(next(i for i, s in enumerate(target_list) if s is chain[-1]))

    # Save and re-index with cleanup
    raw_edits = save_and_reindex(
        data,
        json_file,
        path,
        root,
        project_part,
        cleanup_doc_node_id=doc_node_id,
        doc_node_id=doc_node_id,
    )

    return f"Deleted section '{sec_raw_id}' from {doc_node_id}" + _raw_edit_note(raw_edits)


@_locks_doc
def axiom_graph_add_link(
    project_root: str,
    section_id: str = "",
    node_id: str = "",
    node_ids: list[str] | None = None,
    links: list[dict] | None = None,
) -> str:
    """Add link(s) from doc section(s) to code node(s).

    Loads the section's JSON file, appends the link(s), writes the file back,
    and re-indexes once.  When ``node_ids`` is provided, all links are added
    in a single pass with one re-index — much faster than calling this tool
    N times.

    **Cross-section batch** (``links=[{section_id, node_id}, ...]``) links
    several sections of **one** doc in one call: every item is checked
    first, then the doc is written and re-indexed once.  Items naming
    sections of different docs, or a section that does not exist, make the
    call an error and nothing is written.  A target node missing from the
    index is a warning, not an error.

    Adding a link does not verify the section; verify it with
    ``axiom_graph_mark_clean`` once its content reflects the target.

    A section whose ``links`` already holds an entry without a usable node
    id (a hand edit such as ``{"target": ...}``; see
    :func:`axiom_graph.docjson.parse.normalize_links`) is refused with an
    ``ERROR`` naming the entry, and nothing is written.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs/architecture::database-layer``.
        node_id: The code node ID to link to (single-link mode).
        node_ids: List of code node IDs to link to (batch mode).
            When provided, ``node_id`` is ignored.
        links: Cross-section batch -- a list of ``{"section_id": ...,
            "node_id": ...}`` items, all in one doc.  When given, leave
            ``section_id`` / ``node_id`` / ``node_ids`` unset.
    """
    path = require_db(project_root)
    root = Path(project_root).resolve()

    # Resolve (section_id, node_id) pairs
    pairs: list[tuple[str, str]] = []
    if links is not None:
        if section_id or node_id or node_ids:
            return "ERROR: pass either 'links' (cross-section batch) or section_id with node_id/node_ids, not both"
        if not isinstance(links, list) or not links:
            return "ERROR: 'links' must be a non-empty list of {section_id, node_id} items"
        for idx, item in enumerate(links):
            sid = item.get("section_id") if isinstance(item, dict) else None
            nid = item.get("node_id") if isinstance(item, dict) else None
            if not (isinstance(sid, str) and sid.strip() and isinstance(nid, str) and nid.strip()):
                return f"ERROR: links[{idx}] needs non-empty 'section_id' and 'node_id' -- nothing was written"
            pairs.append((sid.strip(), nid.strip()))
    else:
        targets: list[str] = []
        if node_ids:
            targets = [nid.strip() for nid in node_ids if nid.strip()]
        elif node_id:
            targets = [node_id.strip()]
        if not targets:
            return "ERROR: provide node_id or non-empty node_ids"
        if not section_id:
            return "ERROR: provide section_id"
        pairs = [(section_id, nid) for nid in targets]

    # Every item must name a section of one doc
    roots = docs_roots_for(root)
    parsed_by_sid: dict[str, tuple[str, str, str, str]] = {}
    for sid, _nid in pairs:
        if sid in parsed_by_sid:
            continue
        parsed = parse_section_id(sid, roots)
        if isinstance(parsed, str):
            return parsed
        parsed_by_sid[sid] = parsed
    doc_ids_named = {p[3] for p in parsed_by_sid.values()}
    if len(doc_ids_named) > 1:
        return (
            f"ERROR: links span {len(doc_ids_named)} docs ({', '.join(sorted(doc_ids_named))}); one call links "
            f"sections of one doc -- nothing was written"
        )
    project_part, _slug, _sec, doc_node_id = next(iter(parsed_by_sid.values()))

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    # Resolve every section before mutating anything
    targets_by_sid: dict[str, dict] = {}
    for sid, parsed in parsed_by_sid.items():
        chain = _locate_section(sections, parsed[2], f"ERROR: section '{parsed[2]}' not found in {json_file.name}")
        if isinstance(chain, str):
            return chain
        targets_by_sid[sid] = chain[-1]

    # Append links, skipping duplicates
    added: list[tuple[str, str]] = []
    skipped: list[str] = []
    for sid, nid in pairs:
        target = targets_by_sid[sid]
        try:
            sec_links = json_doc_scanner.normalize_links(target.get("links"), f"section {sid!r}")
        except json_doc_scanner.MalformedLinkError as exc:
            return f"ERROR: {exc}; nothing was written"
        target["links"] = sec_links
        if any(lk.get("node_id") == nid for lk in sec_links):
            skipped.append(nid)
        else:
            sec_links.append({"node_id": nid})
            added.append((sid, nid))

    if not added:
        where = section_id or f"{len(targets_by_sid)} section(s) of {doc_node_id}"
        return f"All {len(skipped)} link(s) already exist on {where}"

    # Save and re-index ONCE for all links
    raw_edits = save_and_reindex(
        data,
        json_file,
        path,
        root,
        project_part,
        doc_node_id=doc_node_id,
        link_targets={p[2] for p in parsed_by_sid.values()},
    )

    # Record LINK_ADDED history for each new link
    for sid, nid in added:
        db.insert_history_row(
            path,
            node_id=sid,
            change_type="LINK_ADDED",
            meta=json.dumps(
                {
                    "edge_type": "documents",
                    "source": sid,
                    "target": nid,
                    "actor": "agent",
                }
            ),
            preserved=False,
        )

    # Warn about targets not in index
    warnings: list[str] = []
    for _sid, nid in added:
        if db.get_node(path, nid) is None:
            warnings.append(f"  WARN: node_id not found in index: {nid}")

    if links is None:
        lines = [f"Added {len(added)} link(s) to {section_id}"]
    else:
        n_secs = len({sid for sid, _ in added})
        lines = [f"Added {len(added)} link(s) across {n_secs} section(s) of {doc_node_id}"]
    if skipped:
        lines.append(f"  Skipped {len(skipped)} duplicate(s)")
    lines.extend(warnings)
    return "\n".join(lines) + _raw_edit_note(raw_edits)


@_locks_doc
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
    parsed = parse_section_id(section_id, docs_roots_for(root))
    if isinstance(parsed, str):
        return parsed
    project_part, doc_path_slug, sec_raw_id, doc_node_id = parsed

    # Load doc JSON using shared helper
    loaded = load_doc_json(path, root, doc_node_id)
    if isinstance(loaded, str):
        return loaded
    data, json_file, doc_node = loaded

    sections: list[dict] = data.get("sections", [])

    chain = _locate_section(sections, sec_raw_id, f"ERROR: section '{sec_raw_id}' not found in {json_file.name}")
    if isinstance(chain, str):
        return chain
    target_sec = chain[-1]

    try:
        links = json_doc_scanner.normalize_links(target_sec.get("links"), f"section {sec_raw_id!r}")
    except json_doc_scanner.MalformedLinkError as exc:
        return f"ERROR: {exc}; nothing was written"
    to_remove = set(targets)
    new_links = [lk for lk in links if lk.get("node_id") not in to_remove]
    removed = [nid for nid in targets if nid in {lk.get("node_id") for lk in links}]
    not_found = [nid for nid in targets if nid not in {lk.get("node_id") for lk in links}]

    if not removed:
        return f"No matching links found on {section_id}"

    target_sec["links"] = new_links

    # Save and re-index ONCE.  The outbound-edge reconcile drops the unlinked
    # documents edges and records their LINK_REMOVED history.
    raw_edits = save_and_reindex(
        data, json_file, path, root, project_part, doc_node_id=doc_node_id, link_targets={sec_raw_id}
    )

    lines = [f"Removed {len(removed)} link(s) from {section_id}"]
    if not_found:
        lines.append(f"  Not found: {', '.join(not_found)}")
    return "\n".join(lines) + _raw_edit_note(raw_edits)


@_locks_doc
def axiom_graph_delete_doc(project_root: str, doc_id: str) -> str:
    """Delete an entire DocJSON document, its JSON file, and all DB artifacts.

    This is a destructive operation. The JSON file is deleted from disk, and
    all DB rows (nodes, edges, sections, tags, FTS, history) are removed.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs/architecture``.
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


@_locks_doc
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
    raw_edits = save_and_reindex(data, json_file, path, root, project_id, doc_node_id=doc_id)

    return f"Updated {', '.join(changes)} for doc: {doc_id}" + _raw_edit_note(raw_edits)


@_one_connection
def axiom_graph_accept_doc_edits(
    project_root: str,
    section_ids: list[str] | None = None,
    all_flagged: bool = False,
    dry_run: bool = False,
    verified_by: str = "agent",
) -> str:
    """Accept hand-edited DocJSON sections: stamp them and verify their text.

    A section edited by hand (a raw DocJSON edit) is indexed but never
    auto-verified, and builds and writes report it once.  Accepting it
    writes a fresh tool-write stamp into the file (under the doc's write
    lock) and verifies the section's text as it stands (a text-only
    verification, op ``accept_raw_docjson_edit``): own status VERIFIED; link
    status unchanged, so a LINKED_STALE section stays LINKED_STALE until it is
    reconciled (``addresses=``, ``mark_clean`` or ``reverify``).  The section's content and hashes
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
        verified_by: Verifier recorded on the verification (``"human"`` from
            the CLI).

    Returns:
        A summary of what was listed or accepted, or ``ERROR: ...``.  All or
        nothing: every named doc is locked up front and every section
        resolved before any file is saved, so one unknown section, missing
        doc or lock timeout writes nothing and the error lists every problem.
    """
    from axiom_graph.index.mark_clean import VERIFICATION_OP_ACCEPT_RAW_DOCJSON_EDIT  # noqa: PLC0415

    path = require_db(project_root)
    root = Path(project_root).resolve()
    flagged = doc_stamps.list_raw_docjson_edits(path, root)

    if dry_run:
        if not flagged:
            return "No sections are flagged as raw DocJSON edits."
        lines = [f"{len(flagged)} section(s) flagged as raw DocJSON edits (edited outside the doc tools):"]
        lines += [f"  {sid}" for sid in flagged]
        lines.append(
            "Accept them with axiom_graph_accept_doc_edits(section_ids=[...]) or all_flagged=True "
            "(CLI: axiom-graph stamps accept <ids>|--all), or re-apply each change with "
            "update_section / patch_section / add_section."
        )
        return "\n".join(lines)
    if section_ids and all_flagged:
        return "ERROR: pass either section_ids or all_flagged=True, not both"
    if not section_ids and not all_flagged:
        return "ERROR: pass section_ids, all_flagged=True, or dry_run=True"
    wanted = list(dict.fromkeys(flagged if all_flagged else section_ids or []))
    if not wanted:
        return "No sections are flagged as raw DocJSON edits -- nothing to accept."

    roots = docs_roots_for(root)
    by_doc: dict[str, list[str]] = {}
    errors: list[str] = []
    for sid in wanted:
        parsed = parse_section_id(sid, roots)
        if isinstance(parsed, str):
            logger.warning("accept_doc_edits: section id not found: %s", sid)
            errors.append(parsed)
            continue
        by_doc.setdefault(parsed[3], []).append(sid)

    # All or nothing: every named doc is locked up front (sorted, one
    # deadline), every section is resolved, and only then is any file saved.
    with db._connect(path) as conn:
        doc_nodes = db.get_nodes_conn(conn, list(by_doc))
    for doc_node_id in by_doc:
        if doc_node_id not in doc_nodes:
            logger.warning("accept_doc_edits: doc node not found in index: %s", doc_node_id)
            errors.append(f"ERROR: doc node not found in index: {doc_node_id}")
    if errors:
        return _accept_refused(errors)

    accepted: list[str] = []
    skipped: list[str] = []
    raw_edits: list[str] = []
    try:
        with lock_docs(root, [root / doc_nodes[d].location for d in by_doc]):
            plans: list[tuple[str, dict, Path, set[str]]] = []
            for doc_node_id, sids in by_doc.items():
                json_file = root / doc_nodes[doc_node_id].location
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    errors.append(f"ERROR: cannot read {json_file.name}: {exc}")
                    continue
                prefix = f"{doc_node_id}::"
                dots: set[str] = set()
                for sid in sids:
                    dot = sid[len(prefix) :]
                    chain = _locate_section(
                        data.get("sections") or [], dot, f"ERROR: section '{dot}' not found in {json_file.name}"
                    )
                    sec = chain[-1] if isinstance(chain, list) else None
                    if sec is None:
                        logger.warning("accept_doc_edits: section %s not resolved in %s", sid, json_file.name)
                        errors.append(chain)
                    elif doc_stamps.stamp_state(sec) == doc_stamps.STAMP_VALID:
                        logger.debug("accept_doc_edits: %s already stamped, skipped", sid)
                        skipped.append(sid)
                    else:
                        logger.debug("accept_doc_edits: accepting %s", sid)
                        dots.add(dot)
                if dots:
                    plans.append((doc_node_id, data, json_file, dots))
            if errors:
                return _accept_refused(errors)
            for doc_node_id, data, json_file, dots in plans:
                raw_edits += save_and_reindex(
                    data,
                    json_file,
                    path,
                    root,
                    doc_node_id.split("::")[0],
                    verified_by=verified_by,
                    doc_node_id=doc_node_id,
                    targets=dots,
                    verification_op=VERIFICATION_OP_ACCEPT_RAW_DOCJSON_EDIT,
                )
                accepted += [f"{doc_node_id}::{d}" for d in sorted(dots)]
    except DocLockTimeout as exc:
        logger.warning("accept_doc_edits: %s -- nothing was written", exc)
        return f"ERROR: {exc} -- nothing was written"

    logger.info("accept_doc_edits: accepted %d section(s), skipped %d", len(accepted), len(skipped))
    lines = [f"Accepted {len(accepted)} section(s): stamped and verified"]
    lines += [f"  {sid}" for sid in accepted]
    if skipped:
        lines.append(f"  Skipped {len(skipped)} already-stamped section(s): {', '.join(skipped)}")
    return "\n".join(lines) + _raw_edit_note(raw_edits)


def _accept_refused(errors: list[str]) -> str:
    """The all-or-nothing refusal of ``accept_doc_edits``: every problem found, nothing written."""
    lines = [f"ERROR: {len(errors)} problem(s) block this accept -- nothing was written"]
    lines += [f"  {e.removeprefix('ERROR: ')}" for e in errors]
    return "\n".join(lines)


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
    purpose="Return old vs new sections for a doc against a baseline commit, following renames",
    inputs="db_path, project_root, doc_id, optional baseline_sha (main repo commit)",
    outputs="dict with old_sections, new_sections, path, baseline_path, baseline_sha, baseline_rev",
)
def get_doc_diff(
    db_path: Path,
    project_root: Path,
    doc_id: str,
    baseline_sha: str | None = None,
) -> dict:
    """Return old vs new sections for a doc against a baseline commit.

    The baseline SHA refers to a commit in the **main** repository.  Docs
    may live inline in the main repository or in a git submodule:
    ``git ls-tree`` on the docs root at the baseline tells them apart, and
    for a submodule yields the docs repo's commit at the baseline, so the
    caller never needs to know it.

    The old side is read from wherever the doc file lived at the baseline:
    a doc renamed or moved since then (committed, or staged in the index)
    diffs against its old path, via
    :func:`axiom_graph.index.git_utils.read_file_at_baseline`.  A doc that
    did not exist at the baseline has no old sections; a doc whose old path
    git cannot resolve is an error, never an all-new diff.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Root directory of the main repository.
        doc_id: The doc ID as stored in the ``docs`` table.
        baseline_sha: A commit SHA in the main repo.  When ``None``, the
            function uses ``HEAD~1`` as a rough default.

    Returns:
        On success: ``{"old_sections": [...], "new_sections": [...],
        "path": ..., "baseline_path": ..., "baseline_sha": ...,
        "baseline_rev": ...}`` -- ``path`` is the doc's current
        project-relative file, ``baseline_path`` the project-relative file
        it was read from at the baseline (``None`` when the doc is new), and
        ``baseline_rev`` the revision read: the docs submodule's commit at
        the baseline for submodule docs, else ``baseline_sha`` itself.
        On failure: ``{"error": "...", "reason": "..."}``; ``error`` is one
        of ``db_error``, ``not_found``, ``bad_path``, ``git_error``,
        ``parse_error`` or ``baseline_path_unresolved`` (the file is missing
        at the baseline and git cannot tell whether it was renamed).
    """
    口 = Step(
        step_num=1,
        name="Look up doc file path",
        purpose="Query the docs table for the doc's file path and compute the submodule-relative path",
    )
    try:
        with db._connect(db_path) as conn:
            row = conn.execute("SELECT file_path FROM docs WHERE id = ?", (doc_id,)).fetchone()
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
        name="Resolve the baseline revision of the docs",
        purpose=(
            "git ls-tree the docs root at the baseline: a submodule entry gives the docs repo's "
            "commit to read from; a folder (inline docs) or no entry reads the main repo at the baseline"
        ),
        outputs="repo to read from, revision, and the doc's path relative to that repo",
        critical="Inline docs and submodule docs both work; only a submodule switches repo and revision",
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
    except subprocess.TimeoutExpired:
        logger.warning("git ls-tree timed out for %s", baseline_sha)
        return {"error": "git_error", "reason": "git ls-tree timed out"}
    except Exception as exc:
        logger.warning("git ls-tree error: %s", exc)
        return {"error": "git_error", "reason": f"git ls-tree error: {exc}"}
    if result.returncode != 0:
        return {
            "error": "git_error",
            "reason": f"git ls-tree failed: {result.stderr.strip()}",
        }
    # ls-tree line: "<mode> <type> <object>\t<path>"; type "commit" is a submodule.
    ls_parts = result.stdout.strip().split()
    if len(ls_parts) >= 3 and ls_parts[1] == "commit":
        read_root = Path(project_root) / docs_root_rel
        baseline_rev = ls_parts[2]
        read_path = relative_path
        path_prefix = docs_root_rel + "/"
    else:
        read_root = Path(project_root)
        baseline_rev = baseline_sha
        read_path = _fp_posix
        path_prefix = ""

    口 = Step(
        step_num=3,
        name="Retrieve old DocJSON at its baseline path",
        purpose=(
            "Read the doc file at the baseline revision, following a rename or move since then "
            "(committed or staged), so a renamed doc diffs against its old path"
        ),
        outputs="old DocJSON and baseline_path; no sections and None when the doc is new",
        critical=(
            "Three outcomes stay distinct: found (real diff), absent (new doc, no old sections), "
            "unresolved (git error or skipped rename detection -> error, never an all-new diff). "
            "An uncommitted rename is followed only once staged; an untracked path reads as new"
        ),
    )
    baseline_file = read_file_at_baseline(read_root, baseline_rev, read_path)
    if baseline_file.status == "error":
        code = "baseline_path_unresolved" if baseline_file.unresolved else "git_error"
        return {"error": code, "reason": baseline_file.reason}
    if baseline_file.status == "absent":
        old_data: dict = {"sections": []}
        baseline_path: str | None = None
    else:
        baseline_path = path_prefix + (baseline_file.path or read_path)
        try:
            old_data = json.loads(baseline_file.content or "")
        except json.JSONDecodeError as exc:
            return {
                "error": "parse_error",
                "reason": f"Old JSON is invalid: {exc}",
            }

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
        "path": _fp_posix,
        "baseline_path": baseline_path,
        "baseline_sha": baseline_sha,
        "baseline_rev": baseline_rev,
    }
