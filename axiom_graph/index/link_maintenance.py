"""DocJSON link maintenance -- patch node_id references on disk after renames.

When a code or doc node is renamed, DocJSON files on disk may contain
``links`` arrays with the old node ID.  This module walks all DocJSON
files and replaces old references with new ones, using atomic file
writes (temp file + os.replace) to mitigate corruption risk.

Doc links point at *sections* far more often than at document envelopes,
so every rewrite here is prefix-aware: renaming ``doc`` to ``newdoc`` also
rewrites ``doc::some.section`` to ``newdoc::some.section``.

``patch_doc_links`` rewrites one rename and walks the doc tree once.
``patch_doc_links_batch`` rewrites a whole map and *still* walks the doc
tree once -- driving a bulk rename through the single-rename entry point
costs one full tree walk per rename.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from axiom_annotations import task


def _remap_node_id(node_id: str, mapping: dict[str, str]) -> str | None:
    """Return the rewritten node ID for *node_id*, or ``None`` when unaffected.

    Matches an exact document (or code) ID, and also a section ID whose
    document half is in *mapping* -- section IDs are ``{doc_id}::{dot.path}``,
    so only the document half moves.

    Args:
        node_id: The link target as written on disk.
        mapping: Old -> new IDs.

    Returns:
        The new ID, or ``None`` when the link is untouched.
    """
    direct = mapping.get(node_id)
    if direct is not None:
        return direct
    parts = node_id.split("::")
    if len(parts) == 3:
        envelope = f"{parts[0]}::{parts[1]}"
        moved = mapping.get(envelope)
        if moved is not None:
            return f"{moved}::{parts[2]}"
    return None


def _patch_sections_recursive(
    sections: list,
    old_id: str | None = None,
    new_id: str | None = None,
    *,
    mapping: dict[str, str] | None = None,
) -> bool:
    """Recursively rewrite link targets in a sections list, including children.

    Args:
        sections: List of section dicts from a DocJSON document.
        old_id: The old node ID to replace (single-rename form).
        new_id: The replacement node ID (single-rename form).
        mapping: Old -> new IDs (batch form).  Takes precedence over
            ``old_id`` / ``new_id`` when supplied.

    Returns:
        True if any link was modified.
    """
    if mapping is None:
        mapping = {old_id: new_id} if old_id is not None and new_id is not None else {}
    modified = False
    for section in sections:
        if not isinstance(section, dict):
            continue
        links = section.get("links")
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                current = link.get("node_id")
                if not isinstance(current, str):
                    continue
                replacement = _remap_node_id(current, mapping)
                if replacement is not None and replacement != current:
                    link["node_id"] = replacement
                    modified = True
        # Recurse into nested sections
        child_sections = section.get("sections")
        if isinstance(child_sections, list):
            if _patch_sections_recursive(child_sections, mapping=mapping):
                modified = True
    return modified


def _docs_roots(project_root: Path) -> list[Path]:
    """Return the existing configured docs roots for *project_root*.

    Absolute entries are honoured as written, relative entries resolve
    against the project root, and a repeated root is visited once.

    Args:
        project_root: Absolute path to the project root.

    Returns:
        Absolute paths of every configured docs root that exists.
    """
    from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

    try:
        cfg = AxiomGraphConfig.load(project_root)
        docs_entries = cfg.scan.docs_dirs or ["docs"]
    except Exception:
        docs_entries = ["docs"]

    seen: set[str] = set()
    roots: list[Path] = []
    for entry in docs_entries:
        entry_path = Path(entry)
        abs_root = entry_path if entry_path.is_absolute() else (project_root / entry_path)
        key = str(abs_root)
        if key in seen:
            continue
        seen.add(key)
        if abs_root.exists():
            roots.append(abs_root)
    return roots


@task(
    purpose="Rewrite every DocJSON links[].node_id reference for a whole rename map in a single walk of the doc tree",
    inputs="project_root, mapping of old -> new node ids",
    outputs="(files_patched, files_read) — counts for one pass over the tree",
)
def patch_doc_links_batch(project_root: Path, mapping: dict[str, str]) -> tuple[int, int]:
    """Apply a whole rename map to on-disk DocJSON links in one pass.

    Walking the doc tree once for the entire map is the difference between
    ``renames x files`` file parses and ``files`` file parses; a full-tree
    doc-ID migration drives thousands of renames.

    Section-targeted links are rewritten alongside envelope-targeted ones:
    a mapping entry for a document also moves every ``{doc}::{section}``
    reference to it.

    Args:
        project_root: Absolute path to the project root.
        mapping: Old -> new node IDs.

    Returns:
        ``(files_patched, files_read)``.
    """
    if not mapping:
        return 0, 0
    roots = _docs_roots(Path(project_root))
    if not roots:
        return 0, 0

    files_patched = 0
    files_read = 0
    visited: set[str] = set()
    for docs_dir in roots:
        for json_file in docs_dir.rglob("*.json"):
            fkey = str(json_file.resolve())
            if fkey in visited:
                continue
            visited.add(fkey)
            try:
                doc = json.loads(json_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            files_read += 1
            if not isinstance(doc, dict):
                continue
            sections = doc.get("sections")
            if not isinstance(sections, list):
                continue
            if _patch_sections_recursive(sections, mapping=mapping):
                _atomic_write_json(json_file, doc)
                files_patched += 1
    return files_patched, files_read


@task(
    purpose="Walk DocJSON files and replace old node_id references with new after rename, including section-targeted links",
    inputs="project_root, db_path, old_id, new_id",
    outputs="int — count of files modified",
)
def patch_doc_links(
    project_root: Path,
    db_path: Path,
    old_id: str,
    new_id: str,
) -> int:
    """Patch DocJSON files on disk, replacing old_id with new_id in links arrays.

    Walks every ``*.json`` file under the configured docs roots and rewrites
    ``links[].node_id`` entries that match ``old_id`` exactly *or* that target
    a section of it (``{old_id}::{dot.path}``).  Most doc links point at
    sections, so an exact-match-only rewrite leaves the majority behind.

    Uses atomic writes (write to temp file, then os.replace) to avoid
    corruption if the process is interrupted.

    Args:
        project_root: Absolute path to the project root.
        db_path: Path to the axiom-graph SQLite database (unused currently,
            reserved for future lookup of doc file paths).
        old_id: The old node ID to replace in link references.
        new_id: The new node ID to replace with.

    Returns:
        Number of files patched.
    """
    files_patched, _files_read = patch_doc_links_batch(project_root, {old_id: new_id})
    return files_patched


def _atomic_write_json(file_path: Path, data: dict) -> None:
    """Write JSON data to a file atomically using temp file + os.replace.

    Args:
        file_path: Target file path.
        data: JSON-serializable dict to write.
    """
    dir_path = file_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, str(file_path))
    except BaseException:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
