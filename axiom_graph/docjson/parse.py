"""Cortex JSON doc scanner — docs/*.json → AxiomNode + AxiomEdge objects
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

Each section becomes one ``atomic_process`` AxiomNode (subtype=docjson).
The file itself becomes one ``composite_process`` AxiomNode (subtype=docjson).

``composes`` edge:  doc_node → section_node  (structural containment)
``documents`` edge: section_node → link['node_id']  (section documents that code node)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from axiom_annotations import task, Step, AutoStep

from axiom_graph.index.file_state import file_unchanged_since
from axiom_graph.models import AxiomEdge, AxiomNode, hash16, make_edge


def _strip_html(text: str) -> str:
    """Strip HTML tags for search index text."""
    return re.sub(r"<[^>]+>", "", text).strip()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@task(
    purpose="Walk docs_dir for *.json DocJSON files, apply mtime fast-pass, create composite/atomic nodes per file/section, and generate documents edges from links",
    inputs="docs_dir, project_root, project_id, optional stored_mtimes for mtime skip",
    outputs="Tuple of (nodes, edges, doc_records, section_records, files_skipped)",
)
def scan_json_docs(
    docs_dir: Path,
    project_root: Path,
    project_id: str,
    stored_mtimes: dict[str, float] | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge], list[dict], list[dict], int]:
    """Walk docs_dir for *.json files and return (nodes, edges, doc_recs, sec_recs, files_skipped).

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
        purpose="Iterate *.json files in docs_dir, apply mtime fast-pass, delegate to scan_single_json_doc",
    )
    for json_file in sorted(docs_dir.rglob("*.json")):
        # mtime fast-pass
        if stored_mtimes:
            rel = json_file.relative_to(project_root).as_posix()
            stored = stored_mtimes.get(rel)
            if file_unchanged_since(stored, json_file.stat().st_mtime):
                files_skipped += 1
                continue
        try:
            口 = AutoStep(step_num=1.1, name="Scan single DocJSON file")
            nodes, edges, doc_recs, sec_recs = scan_single_json_doc(
                json_file, project_root, project_id, docs_dir=docs_dir
            )
            all_nodes.extend(nodes)
            all_edges.extend(edges)
            all_doc_recs.extend(doc_recs)
            all_sec_recs.extend(sec_recs)
        except Exception as exc:
            logger.warning("json_doc_scanner: failed on %s: %s", json_file.name, exc)

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
) -> tuple[list[AxiomNode], list[AxiomEdge], list[dict], list[dict]]:
    """Scan a single JSON doc file.  Returns (nodes, edges, doc_recs, sec_recs).

    Args:
        json_file: Path to the DocJSON file being scanned.
        project_root: Project root path; used to compute relative paths.
        project_id: Namespace prefix for all node IDs.
        docs_dir: The configured docs root that contains ``json_file``.  The
            canonical doc ID is derived from ``json_file`` relative to this
            root.  When ``None`` (back-compat for existing callers / tests),
            ``project_root / "docs"`` is used; if that does not contain
            ``json_file``, the ID is derived from the path relative to
            ``project_root`` with a leading ``docs.`` stripped when present.

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
    raw_text = json_file.read_text(encoding="utf-8", errors="replace")
    data = json.loads(raw_text)

    # Validate required top-level keys ("id" is optional — derived from path)
    for key in ("title", "sections"):
        if key not in data:
            raise ValueError(f"JSON doc {json_file} missing required key '{key}'")

    # Canonical ID is derived from the file path relative to the configured
    # docs root (e.g. docs/prds/my-doc.json -> "prds.my-doc").  The JSON "id"
    # field, if present, is ignored for identity purposes.
    if docs_dir is not None:
        try:
            raw_id = json_file.relative_to(docs_dir).as_posix().removesuffix(".json").replace("/", ".")
        except ValueError:
            # Caller passed a docs_dir that doesn't contain json_file — fall
            # back to project_root and strip a leading "docs." prefix.
            raw_id = json_file.relative_to(project_root).as_posix().removesuffix(".json").replace("/", ".")
            if raw_id.startswith("docs."):
                raw_id = raw_id[5:]
    else:
        # Back-compat: no docs_dir passed -> use project_root / "docs" if the
        # file lives under it, else relative-to-project with leading "docs."
        # stripped.  This keeps existing callers (tests, MCP helpers) stable.
        legacy_docs = project_root / "docs"
        try:
            raw_id = json_file.relative_to(legacy_docs).as_posix().removesuffix(".json").replace("/", ".")
        except ValueError:
            raw_id = json_file.relative_to(project_root).as_posix().removesuffix(".json").replace("/", ".")
            if raw_id.startswith("docs."):
                raw_id = raw_id[5:]
    title: str = data["title"]
    doc_tags: list[str] = data.get("tags") or []
    raw_sections: list[dict] = data["sections"]

    rel_path = json_file.relative_to(project_root).as_posix()
    file_hash = hash16(raw_text)
    now = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # File-level document node
    # ------------------------------------------------------------------
    doc_id = f"{project_id}::docs.{raw_id}"

    doc_node = AxiomNode(
        id=doc_id,
        node_type="composite_process",
        subtype="docjson",
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
        file_mtime=json_file.stat().st_mtime,
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
        purpose="Recursively walk sections, create atomic AxiomNode per section with documents edges from links",
    )
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
    )

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
) -> None:
    """Recursively walk sections, building nodes/edges/sec_recs.

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
    """
    for pos, section in enumerate(sections):
        _validate_section(section, json_file, pos)

        sec_raw_id: str = section["id"]
        heading: str = section["heading"]
        content: str = section.get("content") or ""
        sec_tags: list[str] = section.get("tags") or []
        links: list[dict] = section.get("links") or []
        level: int = section.get("level", depth + 2)

        # Dot-path: parent.child for nested, plain id for top-level
        dot_path = f"{parent_sec_id}.{sec_raw_id}" if parent_sec_id else sec_raw_id
        section_id = f"{doc_id}::{dot_path}"
        content_hash = hash16(content) if content else hash16("")

        # Section atomic nodes set both code_hash and desc_hash to content_hash.
        # The doc_sections shadow row (synced by _sync_docjson_shadow) also stores
        # content_hash in desc_hash per the four-field invariant — both sides of
        # the staleness comparator agree, so heading-only edits do not flip
        # desc_hash on the section atomic.  Heading edits still surface via
        # CONTENT_UPDATED on the file-level composite node.
        section_node = AxiomNode(
            id=section_id,
            node_type="atomic_process",
            subtype="docjson",
            title=heading,
            location=rel_path,
            source="json_doc_scanner",
            code_hash=content_hash,
            desc_hash=content_hash,
            level_0=doc_title,
            level_1=heading,
            level_2=_strip_html(content)[:4000] if content else None,
            level_3_location=rel_path,
            tags=sec_tags,
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
                )


def _validate_section(section: dict, json_file: Path, pos: int) -> None:
    """Raise ValueError if a section dict is missing required keys."""
    for key in ("id", "heading"):
        if key not in section:
            raise ValueError(f"JSON doc {json_file} section[{pos}] missing required key '{key}'")
