"""DocJSON link maintenance -- patch node_id references on disk after renames.

When a code or doc node is renamed, DocJSON files on disk may contain
``links`` arrays with the old node ID.  This module walks all DocJSON
files and replaces old references with new ones.  Affected files are
rewritten under their write locks with atomic saves
(:mod:`axiom_graph.index.doc_io`).

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
import logging
from collections.abc import Callable, Collection
from pathlib import Path
from typing import NamedTuple

from axiom_annotations import AutoStep, Step, task

from axiom_graph.index import doc_ids

logger = logging.getLogger(__name__)


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

    A section's ``axiom_stamp.verified_against`` entry for a rewritten id
    moves to the new id with its hash unchanged.

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
            for pos, link in enumerate(links):
                if isinstance(link, str):
                    # A bare node-id string is shorthand for {"node_id": s}.
                    link = links[pos] = {"node_id": link}
                if not isinstance(link, dict):
                    continue
                current = link.get("node_id")
                if not isinstance(current, str):
                    continue
                replacement = _remap_node_id(current, mapping)
                if replacement is not None and replacement != current:
                    link["node_id"] = replacement
                    modified = True
        # The tool-write stamp names the linked code it was checked against;
        # its entry follows the link to the new id, hash unchanged.
        stamp = section.get("axiom_stamp")
        against = stamp.get("verified_against") if isinstance(stamp, dict) else None
        if isinstance(against, dict):
            for key in list(against):
                replacement = _remap_node_id(key, mapping) if isinstance(key, str) else None
                if replacement is None or replacement == key:
                    continue
                value = against.pop(key)
                against.setdefault(replacement, value)
                modified = True
        # Recurse into nested sections
        child_sections = section.get("sections")
        if isinstance(child_sections, list):
            if _patch_sections_recursive(child_sections, mapping=mapping):
                modified = True
    return modified


def _references(sections: list, match: Callable[[str], bool]) -> bool:
    """Whether any link target or ``verified_against`` key in *sections* (recursively) satisfies *match*.

    Args:
        sections: List of section dicts from a DocJSON document.
        match: Predicate over a node id.

    Returns:
        True on the first matching id.
    """
    for section in sections:
        if not isinstance(section, dict):
            continue
        for link in section.get("links") or []:
            nid = link if isinstance(link, str) else link.get("node_id") if isinstance(link, dict) else None
            if isinstance(nid, str) and match(nid):
                return True
        stamp = section.get("axiom_stamp")
        against = stamp.get("verified_against") if isinstance(stamp, dict) else None
        if isinstance(against, dict) and any(isinstance(k, str) and match(k) for k in against):
            return True
        children = section.get("sections")
        if isinstance(children, list) and _references(children, match):
            return True
    return False


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


class LinkPatchResult(NamedTuple):
    """What one :func:`patch_doc_links_batch` pass did.

    Attributes:
        files_patched: Files rewritten.
        files_read: DocJSON files parsed in the read-only scan.
        unreadable: Files under a docs root that could not be read or parsed
            (their links, if any, were not patched).
        not_patched: Files that needed a rewrite but were left unchanged
            because their write locks were not all taken in time.
    """

    files_patched: int
    files_read: int
    unreadable: list[str]
    not_patched: list[str]

    @property
    def skipped(self) -> list[str]:
        """Every file the pass did not get through: the unreadable ones, then the unpatched ones.

        Only ``not_patched`` files are known to still link a renamed-away
        id; an ``unreadable`` file is any unparseable doc file in the tree,
        whether or not it links the id.  Report the two apart
        (:func:`link_rewrite_warnings`).
        """
        return self.unreadable + self.not_patched


def link_rewrite_warnings(
    unreadable: Collection[str], not_patched: Collection[str], old_ref: str, new_ref: str
) -> list[str]:
    """The warning lines for the files a rename's link rewrite did not get through, unreadable apart from locked.

    A ``not_patched`` file needed the rewrite and still links *old_ref*; an
    ``unreadable`` file could not be parsed, so whether it links *old_ref*
    at all is unknown.

    Args:
        unreadable: Files that could not be read or parsed.
        not_patched: Files that needed a rewrite but whose write locks were busy.
        old_ref: How to name the renamed-away id (an id, or a phrase).
        new_ref: How to name the id those links should point at.

    Returns:
        Zero, one or two lines, without a ``WARNING:`` prefix; files are
        de-duplicated in order.
    """
    lines: list[str] = []
    locked = list(dict.fromkeys(not_patched))
    if locked:
        lines.append(
            f"{len(locked)} DocJSON file(s) still link {old_ref} (their write locks were busy, so the link "
            f"rewrite was not applied): {', '.join(locked)} -- point those links at {new_ref}"
        )
    unparsed = [f for f in dict.fromkeys(unreadable) if f not in locked]
    if unparsed:
        lines.append(
            f"{len(unparsed)} DocJSON file(s) could not be checked for links to {old_ref} (unreadable): "
            f"{', '.join(unparsed)} -- once they parse, point any such links at {new_ref}"
        )
    return lines


class LinkScan(NamedTuple):
    """What a read-only :func:`scan_doc_links` walk found.

    Attributes:
        affected: Files with at least one reference the scan matched.
        files_read: DocJSON files parsed.
        unreadable: Files that could not be read or parsed.
    """

    affected: list[Path]
    files_read: int
    unreadable: list[str]


def _load_doc(json_file: Path) -> dict | None:
    """Parse *json_file*; ``None`` when it is not a JSON object.  Read and parse errors propagate."""
    doc = json.loads(json_file.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else None


@task(
    purpose="Read-only walk of the doc tree: the DocJSON files whose links or stamp entries name an id the rename "
    "map moves, or an id at or under one of the given prefixes",
    inputs="project_root, mapping of old -> new node ids, old id prefixes",
    outputs="LinkScan -- affected files, files read, unreadable files",
)
def scan_doc_links(
    project_root: Path, mapping: dict[str, str] | None = None, prefixes: Collection[str] = ()
) -> LinkScan:
    """Find the doc files a rename would rewrite, without locking or writing anything.

    A reference is affected when :func:`_remap_node_id` moves it under
    *mapping*, or when it equals a *prefixes* entry or names a dot-path
    under one (``{prefix}.{child}``) -- what a section rename moves, known
    before the renamed section's doc is loaded.  Each file is parsed once.

    Args:
        project_root: Absolute path to the project root.
        mapping: Old -> new node ids.
        prefixes: Old node ids whose descendants move with them.

    Returns:
        A :class:`LinkScan`; files that cannot be read are logged and listed.
    """
    mapping = mapping or {}
    heads = tuple(prefixes)

    def match(node_id: str) -> bool:
        moved = _remap_node_id(node_id, mapping)
        if moved is not None and moved != node_id:
            return True
        return any(node_id == p or node_id.startswith(p + ".") for p in heads)

    files_read = 0
    unreadable: list[str] = []
    affected: list[Path] = []
    if not mapping and not heads:
        return LinkScan(affected, files_read, unreadable)
    visited: set[str] = set()
    for docs_dir in _docs_roots(Path(project_root)):
        for json_file in doc_ids.docjson_files_under(docs_dir):
            fkey = str(json_file.resolve())
            if fkey in visited:
                continue
            visited.add(fkey)
            try:
                doc = _load_doc(json_file)
            except (OSError, ValueError) as exc:
                logger.warning("link patch: cannot read %s (%s); its links were not checked", json_file, exc)
                unreadable.append(str(json_file))
                continue
            files_read += 1
            sections = doc.get("sections") if doc is not None else None
            if isinstance(sections, list) and _references(sections, match):
                affected.append(json_file)
    return LinkScan(affected, files_read, unreadable)


@task(
    purpose="Rewrite every DocJSON links[].node_id reference for a whole rename map: one read-only walk of the doc "
    "tree finds the affected files, then each is re-read, patched and atomically saved under the write locks of all "
    "of them",
    inputs="project_root, mapping of old -> new node ids",
    outputs="LinkPatchResult -- files patched, files read, unreadable files, files left unpatched on lock timeout",
)
def patch_doc_links_batch(project_root: Path, mapping: dict[str, str]) -> LinkPatchResult:
    """Apply a whole rename map to on-disk DocJSON links in one pass.

    Walking the doc tree once for the entire map is the difference between
    ``renames x files`` file parses and ``files`` file parses; a full-tree
    doc-ID migration drives thousands of renames.

    Section-targeted links are rewritten alongside envelope-targeted ones:
    a mapping entry for a document also moves every ``{doc}::{section}``
    reference to it.

    Two phases, so no doc is rewritten from a stale read: a read-only walk
    finds the files the map changes; then the write locks of all of them are
    taken (:func:`~axiom_graph.index.doc_io.lock_docs`: sorted, one deadline,
    re-entrant, so a caller already holding one of them -- a section rename
    patching links in its own doc -- does not wait for itself), and each is
    re-read, patched and saved atomically
    (:func:`~axiom_graph.index.doc_io.save_doc_json`).  A file that cannot be
    read or parsed is logged and returned, not silently skipped; a lock
    timeout leaves every affected file unchanged and returns them.

    Args:
        project_root: Absolute path to the project root.
        mapping: Old -> new node IDs.

    Returns:
        A :class:`LinkPatchResult`.
    """
    from axiom_graph.index.doc_io import lock_docs, save_doc_json  # noqa: PLC0415
    from axiom_graph.index.doc_lock import DocLockTimeout  # noqa: PLC0415

    if not mapping:
        return LinkPatchResult(0, 0, [], [])
    root = Path(project_root)

    口 = AutoStep(step_num=1, name="Find affected files")
    affected, files_read, unreadable = scan_doc_links(root, mapping)
    if not affected:
        return LinkPatchResult(0, files_read, unreadable, [])

    口 = Step(
        step_num=2,
        name="Lock, re-read, patch and save",
        purpose="Under every affected doc's write lock, patch each file as it is now and replace it atomically",
        critical="Locks are sorted and share one deadline; a timeout writes nothing",
    )
    files_patched = 0
    try:
        with lock_docs(root, affected):
            for json_file in affected:
                try:
                    doc = _load_doc(json_file)
                except (OSError, ValueError) as exc:
                    logger.warning("link patch: cannot re-read %s (%s); its links were not patched", json_file, exc)
                    unreadable.append(str(json_file))
                    continue
                sections = doc.get("sections") if doc is not None else None
                if isinstance(sections, list) and _patch_sections_recursive(sections, mapping=mapping):
                    save_doc_json(json_file, doc)
                    files_patched += 1
    except DocLockTimeout as exc:
        logger.warning("link patch: %s -- %d file(s) left unpatched", exc, len(affected))
        return LinkPatchResult(0, files_read, unreadable, [str(f) for f in affected])
    return LinkPatchResult(files_patched, files_read, unreadable, [])


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

    Walks every ``*.docjson`` / ``*.json`` file under the configured docs roots and rewrites
    ``links[].node_id`` entries that match ``old_id`` exactly *or* that target
    a section of it (``{old_id}::{dot.path}``).  Most doc links point at
    sections, so an exact-match-only rewrite leaves the majority behind.

    Files are rewritten under their write locks with atomic saves (see
    :func:`patch_doc_links_batch`).

    Args:
        project_root: Absolute path to the project root.
        db_path: Path to the axiom-graph SQLite database (unused currently,
            reserved for future lookup of doc file paths).
        old_id: The old node ID to replace in link references.
        new_id: The new node ID to replace with.

    Returns:
        Number of files patched.
    """
    return patch_doc_links_batch(project_root, {old_id: new_id}).files_patched
