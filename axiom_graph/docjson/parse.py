"""Cortex JSON doc scanner — docs/*.docjson and docs/*.json → AxiomNode + AxiomEdge objects
plus doc/doc_section record dicts for the DocJSON tables.

Entry points:
    scan_json_docs(docs_dir, project_root, project_id)
        -> tuple[list[AxiomNode], list[AxiomEdge], list[dict], list[dict]]

    scan_single_json_doc(json_file, project_root, project_id)
        -> tuple[list[AxiomNode], list[AxiomEdge], list[dict], list[dict]]

JSON document format::

    {
        "title": "Architecture Overview",
        "tags": ["optional", "list"],
        "sections": [
            {
                "id": "database-layer",
                "heading": "Database Layer",
                "content": "Prose content for this section.",
                "links": [
                    {"node_id": "axiom_graph::axiom_graph.index.db"}
                ]
            }
        ]
    }

Each section becomes one ``atomic_process`` AxiomNode
(subtype=docjson_section) — a first-class graph node carrying the full
section content in ``level_2`` plus ``doc_position`` / ``doc_level``
render metadata (ADR-021 envelope model).  The file itself becomes one
``composite_process`` AxiomNode (subtype=docjson_doc), the lone envelope.

``composes`` edge:  doc_node → section_node  (structural containment)
``documents`` edge: section_node → link['node_id']  (section documents that code node)

A ``links`` entry is ``{"node_id": "<id>"}`` (other keys are ignored) or a
bare node-id string.  The scanner drops any other entry with a warning and
keeps the section; the doc write tools refuse it (:func:`normalize_links`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from axiom_annotations import task, Step, AutoStep

from axiom_graph.index import doc_ids
from axiom_graph.index.file_state import file_unchanged_since
from axiom_graph.models import AxiomEdge, AxiomNode, hash16, make_edge


def resolve_doc_root(
    json_file: Path,
    project_root: Path,
    docs_dir: Path | None,
    docs_root_entry: str | None = None,
) -> tuple[str, str]:
    """Return ``(root_entry, rel_to_root)`` for a DocJSON file.

    Kept as a name in this module because the viz doc endpoints resolve a
    freshly-written file's root through it; the resolution itself is shared
    with the Markdown scanner and lives in :mod:`axiom_graph.index.doc_ids`.

    Args:
        json_file: Absolute path to the DocJSON file.
        project_root: Absolute project root.
        docs_dir: The docs root the caller believes contains the file.
        docs_root_entry: That root as configured, when the caller knows it.

    Returns:
        ``(root_entry, rel_to_root)`` -- a configured-root string and the
        file's POSIX path within it.
    """
    return doc_ids.resolve_doc_root(json_file, project_root, docs_dir, docs_root_entry)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@task(
    purpose="Walk docs_dir for DocJSON files (*.docjson and *.json by default), apply mtime fast-pass, create composite/atomic nodes per file/section, and generate documents edges from links",
    inputs="docs_dir, project_root, project_id, optional stored_mtimes for mtime skip, configured docs_extensions",
    outputs="Tuple of (nodes, edges, doc_records, section_records, files_skipped)",
)
def scan_json_docs(
    docs_dir: Path,
    project_root: Path,
    project_id: str,
    stored_mtimes: dict[str, float] | None = None,
    docs_root_entry: str | None = None,
    extensions: tuple[str, ...] | list[str] | None = None,
    warnings: list[str] | None = None,
    parse_filter=None,
    listing=None,
) -> tuple[list[AxiomNode], list[AxiomEdge], list[dict], list[dict], int]:
    """Walk docs_dir for DocJSON files and return (nodes, edges, doc_recs, sec_recs, files_skipped).

    ``parse_filter``, when given, takes each walked file's absolute path and
    decides whether it is parsed (``build``'s discovery walk); it replaces
    the ``stored_mtimes`` fast-pass.

    Parameters
    ----------
    docs_dir:
        Directory to scan (e.g. ``project_root / "docs"``).
    project_root:
        Root of the project (used to compute repo-relative paths).
    project_id:
        Namespace prefix for all node IDs.
    stored_mtimes:
        ``{rel_path: mtime}`` map from the DB.  Files whose current mtime
        is <= the stored value are skipped entirely.
    docs_root_entry:
        ``docs_dir`` exactly as configured in ``docs_dirs``.  Supplies the
        namespace half of every derived doc ID; resolved from ``docs_dir``
        when omitted.
    extensions:
        ``config.scan.docs_extensions`` -- the extensions walked.  When
        ``x.json`` and ``x.docjson`` both exist only one is scanned: a
        document beats a data file, then ``.docjson`` beats ``.json`` (the
        build reports a pair of documents as a collision).
        Defaults to every DocJSON extension.
    warnings:
        Optional list that receives each scanned file's duplicate-dot-path
        and dropped-links-entry warnings (see :func:`scan_single_json_doc`;
        logged at DEBUG when ``None``), and one warning per document that
        fails to scan (a ``.docjson`` file that is not valid JSON or lacks
        ``title`` / ``sections``, a ``.json`` file that is not valid JSON,
        or any document with a section missing ``id`` or ``heading``), which
        is left out of the build.  A ``.json`` file that is valid JSON but
        not a document (a data file beside the docs) is skipped silently.
    listing:
        The operation's shared directory listing
        (:class:`~axiom_graph.index.walk.TreeListing`); a fresh one when omitted.

    Returns
    -------
    tuple
        ``(nodes, edges, doc_records, section_records, files_skipped)``
    """
    all_nodes: list[AxiomNode] = []
    all_edges: list[AxiomEdge] = []
    all_doc_recs: list[dict] = []
    all_sec_recs: list[dict] = []
    files_skipped = 0

    if not docs_dir.exists():
        return all_nodes, all_edges, all_doc_recs, all_sec_recs, 0

    口 = Step(
        step_num=1,
        name="Scan DocJSON files",
        purpose="Iterate the DocJSON-extension files in docs_dir (one per doc identity), apply mtime fast-pass, delegate to scan_single_json_doc",
    )
    for json_file in doc_ids.indexable_docjson_files(docs_dir, extensions, listing=listing):
        if parse_filter is not None:
            if not parse_filter(json_file):
                files_skipped += 1
                continue
        # mtime fast-pass
        elif stored_mtimes:
            rel = json_file.relative_to(project_root).as_posix()
            stored = stored_mtimes.get(rel)
            if file_unchanged_since(stored, json_file.stat().st_mtime):
                files_skipped += 1
                continue
        try:
            口 = AutoStep(step_num=1.1, name="Scan single DocJSON file")
            nodes, edges, doc_recs, sec_recs = scan_single_json_doc(
                json_file,
                project_root,
                project_id,
                docs_dir=docs_dir,
                docs_root_entry=docs_root_entry,
                warnings=warnings,
            )
            all_nodes.extend(nodes)
            all_edges.extend(edges)
            all_doc_recs.extend(doc_recs)
            all_sec_recs.extend(sec_recs)
        except Exception as exc:
            logger.warning("json_doc_scanner: failed on %s: %s", json_file.name, exc)
            # A plain JSON data file beside the docs stays silent; a .docjson file,
            # or a .json file that is a document (title + sections) or is not
            # valid JSON, is named.
            if warnings is not None and (
                json_file.suffix == ".docjson"
                or doc_ids.classify_doc_file(json_file) != doc_ids.DOC_FILE_NOT_A_DOCUMENT
            ):
                rel = json_file.relative_to(project_root).as_posix()
                warnings.append(f"DocJSON file {rel} was not indexed: {exc}")

    return all_nodes, all_edges, all_doc_recs, all_sec_recs, files_skipped


@task(
    purpose="Scan a single DocJSON file: create file-level composite node, per-section atomic nodes with content hashes for staleness comparison, and documents edges from links",
    inputs="json_file path, project_root, project_id, docs_dir",
    outputs="Tuple of (nodes, edges, doc_records, section_records)",
)
def scan_single_json_doc(
    json_file: Path,
    project_root: Path,
    project_id: str,
    docs_dir: Path | None = None,
    docs_root_entry: str | None = None,
    warnings: list[str] | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge], list[dict], list[dict]]:
    """Scan a single JSON doc file.  Returns (nodes, edges, doc_recs, sec_recs).

    Two sections that spell one dot-path (a flat ``a.b`` beside a ``b``
    nested under ``a``, or two siblings sharing an id) never fail the scan
    and are never merged: the first in document order is indexed, each later
    one is skipped with its subsections, and one warning per dot-path names
    the file and the dot-path.

    A ``links`` entry without a usable node id (see :func:`normalize_links`:
    ``{"target": ...}``, ``{}``, an empty ``node_id``, ``{"node_id": 123}``)
    never fails the scan either: that entry alone is dropped, the section is
    indexed with its other links, and one warning per entry names the file,
    the section dot-path and the entry.  The doc write tools refuse the same
    entries outright.

    Args:
        json_file: Path to the DocJSON file being scanned.
        project_root: Project root path; used to compute relative paths.
        project_id: Namespace prefix for all node IDs.
        docs_dir: The configured docs root that contains ``json_file``.  The
            canonical doc ID is derived from ``json_file`` relative to this
            root.  When ``None`` (back-compat for existing callers / tests),
            ``project_root / "docs"`` is used; if that does not contain
            ``json_file``, the path is taken relative to ``project_root``
            with a leading ``docs`` segment consumed.
        docs_root_entry: ``docs_dir`` exactly as configured in ``docs_dirs``.
            Supplies the namespace half of the derived doc ID; resolved from
            ``docs_dir`` when omitted.
        warnings: Optional list that receives the duplicate-dot-path and
            dropped-links-entry warnings; when ``None`` they are logged at
            DEBUG only, since the staleness and write-path rescans call this
            repeatedly.

    Raises:
        ValueError: If the JSON is missing required keys (``title``,
            ``sections``).
    """
    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []
    doc_recs: list[dict] = []
    sec_recs: list[dict] = []

    口 = Step(
        step_num=1,
        name="Parse JSON and create document composite node",
        purpose="Read file, validate keys, derive doc ID, create composite AxiomNode and doc record",
    )
    # Sample the mtime BEFORE reading the bytes.  Stamping a value newer than
    # the bytes we indexed would let the next build skip the file permanently;
    # stamping an older value only costs a redundant re-scan.
    file_mtime = json_file.stat().st_mtime
    raw_text = json_file.read_text(encoding="utf-8", errors="replace")
    data = json.loads(raw_text)

    # Validate required top-level keys ("id" is optional — derived from path)
    for key in ("title", "sections"):
        if key not in data:
            raise ValueError(f"JSON doc {json_file} missing required key '{key}'")

    # Canonical identity comes from the containing docs root and the file's
    # path within it.  The JSON "id" field, if present, is ignored for
    # identity purposes.  Both halves are resolved by the shared helper and
    # the rule itself lives in one place, so this scanner cannot fall out of
    # step with write_doc, the viz endpoints, or the migration projection.
    _root_entry, _rel_to_root = resolve_doc_root(json_file, project_root, docs_dir, docs_root_entry)
    title: str = data["title"]
    doc_tags: list[str] = data.get("tags") or []
    raw_sections: list[dict] = data["sections"]

    rel_path = json_file.relative_to(project_root).as_posix()
    file_hash = hash16(raw_text)
    now = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # File-level document node
    # ------------------------------------------------------------------
    doc_id = doc_ids.derive_doc_id(project_id, _root_entry, _rel_to_root)

    doc_node = AxiomNode(
        id=doc_id,
        node_type="composite_process",
        subtype="docjson_doc",
        title=title,
        location=rel_path,
        source="json_doc_scanner",
        code_hash=file_hash,
        desc_hash=file_hash,
        level_0=title,
        level_1=title,
        level_2=raw_text[:4000] if raw_text else None,
        level_3_location=rel_path,
        tags=doc_tags,
        file_mtime=file_mtime,
    )
    nodes.append(doc_node)

    doc_rec: dict = {
        "id": doc_id,
        "title": title,
        "tags": json.dumps(doc_tags) if doc_tags else None,
        "file_path": rel_path,
        "desc_hash": file_hash,
        "updated_at": now,
    }
    doc_recs.append(doc_rec)

    # ------------------------------------------------------------------
    # Section nodes (recursive walk supports nested sections)
    # ------------------------------------------------------------------
    口 = Step(
        step_num=2,
        name="Walk sections and create per-section atomic nodes",
        purpose="Recursively walk sections, create atomic AxiomNode per section with documents edges from links; a section whose dot-path an earlier one took is skipped with its subsections",
    )
    skipped: dict[str, int] = {}
    dropped_links: list[str] = []
    _walk_sections(
        raw_sections,
        parent_node_id=doc_id,
        parent_sec_id=None,
        depth=0,
        doc_id=doc_id,
        doc_title=title,
        rel_path=rel_path,
        json_file=json_file,
        now=now,
        nodes=nodes,
        edges=edges,
        sec_recs=sec_recs,
        seen=set(),
        skipped=skipped,
        dropped_links=dropped_links,
    )

    口 = Step(
        step_num=3,
        name="Report duplicate section dot-paths and dropped links entries",
        purpose="Warn once per dot-path that a later section repeated, naming the file (the walk indexed only the first), and once per links entry the walk dropped for having no usable node id",
    )
    messages = [
        (
            f"duplicate section dot-path '{dot_path}' in {rel_path}: {count + 1} sections spell it; "
            f"indexed the first in document order and skipped the later {'one' if count == 1 else count} "
            "(with any subsections) -- rename one so each section has its own id"
        )
        for dot_path, count in skipped.items()
    ]
    messages.extend(f"{msg} -- indexed the section without it" for msg in dropped_links)
    for msg in messages:
        if warnings is not None:
            warnings.append(msg)
        else:
            logger.debug(msg)

    return nodes, edges, doc_recs, sec_recs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MAX_DEPTH = 2  # depth 0, 1, 2 — three levels max


def _walk_sections(
    sections: list[dict],
    *,
    parent_node_id: str,
    parent_sec_id: str | None,
    depth: int,
    doc_id: str,
    doc_title: str,
    rel_path: str,
    json_file: Path,
    now: str,
    nodes: list[AxiomNode],
    edges: list[AxiomEdge],
    sec_recs: list[dict],
    seen: set[str],
    skipped: dict[str, int],
    dropped_links: list[str],
) -> None:
    """Recursively walk sections, building nodes/edges/sec_recs.

    A section whose dot-path an earlier section already took is skipped
    with its subsections, so the two are never merged into one node.  A
    ``links`` entry without a usable node id is dropped (the section is
    indexed without it) and reported in ``dropped_links``.

    Args:
        sections: List of section dicts at this level.
        parent_node_id: Node ID of the parent (doc node or parent section node).
        parent_sec_id: Dot-path section ID prefix (None for top-level).
        depth: Current nesting depth (0 = top-level).
        doc_id: Full doc node ID.
        doc_title: Document title (used for level_0).
        rel_path: Repo-relative file path.
        json_file: Absolute path to the JSON file (for error messages).
        now: ISO timestamp.
        nodes: Accumulator list for AxiomNode objects.
        edges: Accumulator list for AxiomEdge objects.
        sec_recs: Accumulator list for doc_section record dicts.
        seen: Dot-paths already indexed in this doc (shared across levels).
        skipped: ``{dot_path: count}`` of later sections skipped because
            their dot-path was already taken.
        dropped_links: Accumulator for one message per dropped ``links``
            entry, naming the file, the section dot-path and the entry.
    """
    for pos, section in enumerate(sections):
        _validate_section(section, json_file, pos)

        sec_raw_id: str = section["id"]
        heading: str = section["heading"]
        content: str = section.get("content") or ""
        sec_tags: list[str] = section.get("tags") or []
        level: int = section.get("level", depth + 2)

        # Dot-path: parent.child for nested, plain id for top-level
        dot_path = f"{parent_sec_id}.{sec_raw_id}" if parent_sec_id else sec_raw_id
        if dot_path in seen:
            skipped[dot_path] = skipped.get(dot_path, 0) + 1
            continue
        seen.add(dot_path)
        # Lenient: a malformed entry costs only itself, never the section or the doc.
        links = normalize_links(section.get("links"), f"{rel_path} section '{dot_path}'", dropped=dropped_links)
        section_id = f"{doc_id}::{dot_path}"
        content_hash = hash16(content) if content else hash16("")

        # Section atomic nodes set both code_hash and desc_hash to content_hash.
        # code_hash is the staleness BASELINE (preserved across discovery
        # upserts); desc_hash always mirrors the CURRENT content hash — both
        # sides of the staleness comparator agree, so heading-only edits do
        # not flip desc_hash on the section atomic.  Heading edits still
        # surface via CONTENT_UPDATED on the file-level composite node.
        # level_2 carries the FULL raw content (one column, one source of
        # truth — DOC_SECTION_LONG and read_doc both consume it).
        section_node = AxiomNode(
            id=section_id,
            node_type="atomic_process",
            subtype="docjson_section",
            title=heading,
            location=rel_path,
            source="json_doc_scanner",
            code_hash=content_hash,
            desc_hash=content_hash,
            level_0=doc_title,
            level_1=heading,
            level_2=content if content else None,
            level_3_location=rel_path,
            tags=sec_tags,
            doc_position=pos,
            doc_level=level,
        )
        nodes.append(section_node)

        sec_rec: dict = {
            "id": section_id,
            "doc_id": doc_id,
            "heading": heading,
            "level": level,
            "tags": json.dumps(sec_tags) if sec_tags else None,
            "content": content,
            "desc_hash": content_hash,
            "position": pos,
            "parent_id": f"{doc_id}::{parent_sec_id}" if parent_sec_id else None,
            "depth": depth,
            "updated_at": now,
        }
        sec_recs.append(sec_rec)

        # composes edge: parent → section
        edges.append(make_edge("composes", parent_node_id, section_id))

        # documents edge: section → each linked code node
        for link in links:
            linked_node_id = link.get("node_id", "").strip()
            if linked_node_id:
                edges.append(make_edge("documents", section_id, linked_node_id))

        # Recurse into child sections (skip beyond max depth with warning)
        child_sections = section.get("sections") or []
        if child_sections:
            if depth >= _MAX_DEPTH:
                import warnings

                warnings.warn(
                    f"JSON doc {json_file}: section '{dot_path}' exceeds max "
                    f"nesting depth ({_MAX_DEPTH}); children skipped",
                    stacklevel=2,
                )
            else:
                _walk_sections(
                    child_sections,
                    parent_node_id=section_id,
                    parent_sec_id=dot_path,
                    depth=depth + 1,
                    doc_id=doc_id,
                    doc_title=doc_title,
                    rel_path=rel_path,
                    json_file=json_file,
                    now=now,
                    nodes=nodes,
                    edges=edges,
                    sec_recs=sec_recs,
                    seen=seen,
                    skipped=skipped,
                    dropped_links=dropped_links,
                )


def section_dot_path_collisions(sections: list) -> list[tuple[str, list[list[dict]]]]:
    """Return every dot-path that more than one section of a doc spells.

    A section's dot-path is its ancestors' ids and its own id joined with
    ``.``, and an id may itself contain a ``.``, so a flat ``a.b`` and a
    ``b`` nested under ``a`` spell the same dot-path, as do two siblings
    that share an id.  Malformed entries (non-dicts, missing ids) are
    skipped.

    Args:
        sections: A DocJSON ``sections`` list (possibly nested).

    Returns:
        One ``(dot_path, chains)`` pair per colliding dot-path, in document
        order of its first section.  Each chain lists the section dicts from
        the top level down to one of the colliding sections; the chains are
        in document order.
    """
    found: dict[str, list[list[dict]]] = {}

    def _walk(secs: list, prefix: str | None, ancestors: list[dict]) -> None:
        for sec in secs or []:
            if not isinstance(sec, dict) or not isinstance(sec.get("id"), str) or not sec["id"]:
                continue
            dot = f"{prefix}.{sec['id']}" if prefix else sec["id"]
            chain = [*ancestors, sec]
            found.setdefault(dot, []).append(chain)
            _walk(sec.get("sections") or [], dot, chain)

    _walk(sections, None, [])
    return [(dot, chains) for dot, chains in found.items() if len(chains) > 1]


class MalformedLinkError(ValueError):
    """A section's ``links`` holds an entry without a usable node id, or is not a list."""


#: The accepted shapes of a ``links`` entry, as error messages state them.
LINKS_SHAPE = 'links entries are {"node_id": "<id>"} or a node-id string'

#: Appended when a link object names its target under any key but ``node_id``.
_NODE_ID_HINT = 'use "node_id"; the link type is always documents'


def _usable_id(value: object) -> bool:
    """True when *value* is a string with something besides whitespace in it."""
    return isinstance(value, str) and bool(value.strip())


def normalize_links(links: object, where: str, dropped: list[str] | None = None) -> list[dict]:
    """Return a section's ``links`` as link objects, a bare node-id string read as ``{"node_id": s}``.

    An entry is usable when it is a node-id string, or a dict whose
    ``node_id`` is a string, neither one empty or only whitespace.  A dict's
    other keys (the viz editor writes ``relationship``) are kept and
    ignored.  No other key stands in for ``node_id``.  ``None``, a missing
    value and ``[]`` mean no links; any other value that is not a list is
    malformed.

    Strict (``dropped`` is ``None``, the doc write tools) raises on the first
    malformed entry.  Lenient (``dropped`` is a list, the scanner) drops each
    malformed entry, keeps the rest, and appends one message per dropped
    entry (or one for a ``links`` value that is not a list).

    Args:
        links: The section's ``links`` value.
        where: The section, as the error message names it.
        dropped: When given, the list that receives one message per dropped
            entry instead of raising.

    Returns:
        The usable links as dicts; a dict entry is kept as it is.

    Raises:
        MalformedLinkError: In strict mode, when ``links`` is neither empty
            nor a list, or an entry is not usable.
    """

    def _bad(msg: str) -> None:
        if dropped is None:
            raise MalformedLinkError(msg)
        dropped.append(msg)

    if links is None or (isinstance(links, list) and not links):
        return []
    if not isinstance(links, list):
        _bad(f"{where}: links must be a list, got {json.dumps(links, default=repr)}; {LINKS_SHAPE}")
        return []
    out: list[dict] = []
    for entry in links:
        if isinstance(entry, str) and _usable_id(entry):
            out.append({"node_id": entry})
        elif isinstance(entry, dict) and _usable_id(entry.get("node_id")):
            out.append(entry)
        else:
            hint = f"; {_NODE_ID_HINT}" if isinstance(entry, dict) and "node_id" not in entry else ""
            _bad(f"{where}: links entry {json.dumps(entry, default=repr)} has no usable node id; {LINKS_SHAPE}{hint}")
    return out


def _validate_section(section: dict, json_file: Path, pos: int) -> None:
    """Raise ValueError if a section dict is missing required keys."""
    for key in ("id", "heading"):
        if key not in section:
            raise ValueError(f"JSON doc {json_file} section[{pos}] missing required key '{key}'")
