"""Cortex config scanner — .claude/ config files → AxiomNode objects.

Scans a config directory (e.g. .claude/) for markdown, JSON, and YAML files
and produces composite_process nodes with whole-file hashing for change
detection. No section parsing — each file is a single node.

Entry point:
    scan_config_dir(config_dir, project_root, project_id, prefix,
                    stored_mtimes) -> tuple[list[AxiomNode], list[AxiomEdge], int]
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from axiom_annotations import task

from axiom_graph.index.file_state import file_unchanged_since
from axiom_graph.models import AxiomEdge, AxiomNode, hash16


_SUPPORTED_EXTENSIONS = {".md", ".json", ".yaml", ".yml", ".toml"}


def _first_line(text: str) -> str:
    """Return the first non-empty, non-heading line as a summary."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        return line[:200]
    return ""


def _dotpath(rel: str) -> str:
    """Convert a relative path to a dotpath for node IDs.

    Examples:
        .claude/settings.local.json → claude.settings-local
        .claude/skills/axiom-annotations-markers/SKILL.md → claude.skills.axiom-annotations-markers.SKILL
    """
    # Remove the leading dot from directory name (e.g. .claude → claude)
    parts = rel.replace("\\", "/").split("/")
    if parts and parts[0].startswith("."):
        parts[0] = parts[0].lstrip(".")

    # Remove extension from filename
    if parts:
        stem = parts[-1]
        for ext in _SUPPORTED_EXTENSIONS:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        # Replace dots in filename with hyphens to avoid ID confusion
        stem = stem.replace(".", "-")
        parts[-1] = stem

    return ".".join(parts)


@task(
    purpose="Walk config_dir for supported files, apply mtime fast-pass, "
    "create composite_process nodes with whole-file hashing",
    inputs="config_dir, project_root, project_id, prefix, optional stored_mtimes",
    outputs="Tuple of (nodes, edges, files_skipped)",
)
def scan_config_dir(
    config_dir: Path,
    project_root: Path,
    project_id: str,
    prefix: str = "config",
    stored_mtimes: dict[str, float] | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge], int]:
    """Walk config_dir for config files and return (nodes, edges, files_skipped).

    Args:
        config_dir: Absolute path to the config directory to scan.
        project_root: Project root for computing relative paths.
        project_id: Project identifier for node ID prefix.
        prefix: Node ID prefix (e.g. "config" → "{project_id}::config.claude.…").
        stored_mtimes: ``{rel_path: mtime}`` map from the DB. Files whose
            current mtime is <= the stored value are skipped.

    Returns:
        Tuple of (nodes, edges, files_skipped_by_mtime).
    """
    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []
    files_skipped = 0

    if not config_dir.exists():
        return nodes, edges, 0

    for path in sorted(config_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue

        # mtime fast-pass
        rel_path = path.relative_to(project_root).as_posix()
        if stored_mtimes:
            stored = stored_mtimes.get(rel_path)
            if file_unchanged_since(stored, path.stat().st_mtime):
                files_skipped += 1
                continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("config_scanner: failed to read %s: %s", path.name, exc)
            continue

        dotpath = _dotpath(rel_path)
        node_id = f"{project_id}::{prefix}.{dotpath}"
        file_hash = hash16(text)
        file_mtime = path.stat().st_mtime

        summary = _first_line(text)
        title = path.name

        node = AxiomNode(
            id=node_id,
            node_type="composite_process",
            subtype="config",
            title=title,
            location=rel_path,
            source="config_scanner",
            code_hash=file_hash,
            level_0=title,
            level_1=summary or title,
            level_2=text[:4000] if text else None,
            level_3_location=rel_path,
            desc_hash=file_hash,
            file_mtime=file_mtime,
        )
        nodes.append(node)

    return nodes, edges, files_skipped
