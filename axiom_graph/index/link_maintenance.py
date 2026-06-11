"""DocJSON link maintenance -- patch node_id references on disk after renames.

When a code or doc node is renamed, DocJSON files on disk may contain
``links`` arrays with the old node ID.  This module walks all DocJSON
files and replaces old references with new ones, using atomic file
writes (temp file + os.replace) to mitigate corruption risk.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from axiom_annotations import task


def _patch_sections_recursive(
    sections: list,
    old_id: str,
    new_id: str,
) -> bool:
    """Recursively patch links in a sections list, including nested child sections.

    Args:
        sections: List of section dicts from a DocJSON document.
        old_id: The old node ID to replace.
        new_id: The new node ID to replace with.

    Returns:
        True if any link was modified.
    """
    modified = False
    for section in sections:
        if not isinstance(section, dict):
            continue
        links = section.get("links")
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                if link.get("node_id") == old_id:
                    link["node_id"] = new_id
                    modified = True
        # Recurse into nested sections
        child_sections = section.get("sections")
        if isinstance(child_sections, list):
            if _patch_sections_recursive(child_sections, old_id, new_id):
                modified = True
    return modified


@task(
    purpose="Walk DocJSON files and replace old node_id references with new after rename",
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

    Walks all ``*.json`` files under ``docs/`` in the project root and
    replaces any ``links[].node_id`` matching ``old_id`` with ``new_id``.

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
    # Iterate every configured docs root.  Each root is resolved to an
    # absolute path (absolute entries honored as-is; relative entries
    # resolved against project_root).  Duplicate absolute paths are
    # visited once.
    from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

    try:
        cfg = AxiomGraphConfig.load(project_root)
        docs_entries = cfg.scan.docs_dirs or ["docs"]
    except Exception:
        docs_entries = ["docs"]

    seen: set[str] = set()
    docs_roots: list[Path] = []
    for entry in docs_entries:
        entry_path = Path(entry)
        abs_root = entry_path if entry_path.is_absolute() else (project_root / entry_path)
        key = str(abs_root)
        if key in seen:
            continue
        seen.add(key)
        if abs_root.exists():
            docs_roots.append(abs_root)

    if not docs_roots:
        return 0

    files_patched = 0
    visited_files: set[str] = set()
    for docs_dir in docs_roots:
        for json_file in docs_dir.rglob("*.json"):
            fkey = str(json_file.resolve())
            if fkey in visited_files:
                continue
            visited_files.add(fkey)
            try:
                content = json_file.read_text(encoding="utf-8")
                doc = json.loads(content)
            except (OSError, json.JSONDecodeError):
                continue

            if not isinstance(doc, dict):
                continue

            sections = doc.get("sections")
            if not isinstance(sections, list):
                continue

            modified = _patch_sections_recursive(sections, old_id, new_id)

            if modified:
                _atomic_write_json(json_file, doc)
                files_patched += 1

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
