"""Doc-ID enumeration, projection, and collision detection over the doc tree.

Doc node IDs are derived from a DocJSON file's path relative to the
configured docs root it was found under.  Two lossy transforms are folded
into that derivation: every configured root collapses into a single
``docs.`` namespace, and ``/`` is rewritten to ``.``.  Neither transform is
injective, so two distinct files can derive one identity and the
last-scanned file silently wins.

This module is the read-only instrument for that problem:

``enumerate_doc_files``
    Walk every configured docs root **on disk** and return one
    :class:`DocFile` per ``*.json`` file found.  Enumerating from disk
    rather than from the ``docs`` table (or from the set of files a build
    walked) is what makes the collision signal survive the build's mtime
    fast-pass.

``classify_doc_file`` / ``classify_doc_files``
    Split those files into the three populations that matter: DocJSON
    **documents** (the only class carrying an identity), ordinary JSON
    **data files** that happen to live under a docs root, and files that
    are **unreadable** or not valid JSON.  Only the first class can
    collide, project, or be advised on; the second must produce no signal
    at all; the third has no identity either but stays visible.

``doc_id_signals``
    The build-time findings -- overlaps and dotted filenames -- computed
    over documents only.

``current_doc_id`` / ``current_doc_id_index``
    The identity a file resolves to *today*.  A read-only mirror of the
    derivation in :mod:`axiom_graph.docjson.parse`; this module never
    feeds the scanner, and the scanner never calls it.

``project_doc_ids``
    The identity a file *would* resolve to under per-root-prefix
    namespacing with ``/`` retained as the joiner.  Planner input only.

``find_collisions``
    Duplicate groups over any ID index, with their source files.

``dotted_filenames``
    Files whose stem carries extra dots -- advisory only; a dot in a
    filename never changes a derived ID, it only makes two paths more
    likely to converge on one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from axiom_annotations import task


#: A doc ID whose sources span more than one configured root.
COLLISION_CROSS_ROOT = "cross_root"

#: A doc ID whose sources all live under one configured root.
COLLISION_WITHIN_ROOT = "within_root"

#: A DocJSON document -- the only class of file that carries a doc identity.
DOC_FILE_DOCUMENT = "document"

#: Valid JSON that is not a document (no ``title`` / ``sections``).  The
#: scanner refuses it, so it never becomes a doc node: it has no identity to
#: collide, to project, or to advise on, and must produce no signal at all.
DOC_FILE_NOT_A_DOCUMENT = "not_a_document"

#: A file that could not be read, or is not valid JSON.  It has no identity
#: either -- but unlike a data file it is something the scanner would try to
#: index and fail on, so it stays visible to the operator.
DOC_FILE_UNREADABLE = "unreadable"

#: The scanner walks sections to depth 2 (three levels); deeper children are
#: dropped with a warning.  Mirrored here so projected section IDs match the
#: set of sections that actually become nodes.
_MAX_SECTION_DEPTH = 2


@dataclass(frozen=True)
class DocFile:
    """One DocJSON file found under a configured docs root.

    Attributes:
        path: Absolute path to the file.
        rel_path: POSIX path relative to the project root.
        rel_to_root: POSIX path relative to the docs root it was found under.
        root_entry: The docs root exactly as configured (``docs``, ``.pev``).
    """

    path: Path
    rel_path: str
    rel_to_root: str
    root_entry: str


@dataclass(frozen=True)
class DocIdCollision:
    """Two or more files deriving one doc ID.

    Attributes:
        doc_id: The single identity the sources converge on.
        sources: Repo-relative paths of the colliding files, sorted.
        kind: ``cross_root`` when the sources span configured roots,
            ``within_root`` when one root's path shape is to blame.
    """

    doc_id: str
    sources: list[str]
    kind: str


@dataclass
class DocTreeScan:
    """Every ``*.json`` under the configured docs roots, split by what it is.

    The split exists because ``*.json`` under a docs root is not the same
    set as "the documents".  A project is free to keep a config blob, a
    fixture, or a data export beside its docs; those files can never become
    doc nodes, so any doc-ID finding about them is a false positive.

    Attributes:
        documents: DocJSON files, in enumeration order.  The only class
            that carries a doc identity.
        non_documents: Repo-relative paths of valid JSON that is not a
            document.  Ignored entirely -- no projected ID, no collision
            candidacy, no advisory, no contribution to a migration gate.
        unreadable: Repo-relative paths that could not be read or parsed.
            No identity either, but reported: the scanner would fail on
            them, and a corrupt document is a real problem.
    """

    documents: list[DocFile] = field(default_factory=list)
    non_documents: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


@dataclass
class DocIdSignals:
    """Whole-tree doc-ID findings a build should report.

    Attributes:
        collisions: Duplicate groups over the *current* derivation,
            counting DocJSON documents only.
        dotted: Repo-relative paths of documents whose stem carries extra
            dots.  Advisory.
    """

    collisions: list[DocIdCollision] = field(default_factory=list)
    dotted: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DocIdMapping:
    """One node's identity move.

    Attributes:
        old_id: The identity today.
        new_id: The identity under the projection.
        kind: ``doc`` for a document envelope, ``section`` for a section.
        file_path: Repo-relative path of the DocJSON file that owns it.
    """

    old_id: str
    new_id: str
    kind: str
    file_path: str


@dataclass
class DocIdProjection:
    """Old -> new IDs for every document envelope and every section.

    Attributes:
        documents: One mapping per DocJSON file.
        sections: One mapping per section node, in document order.
        new_id_sources: Projected doc ID -> repo-relative source paths.
        unreadable: Repo-relative paths of documents that contributed no
            section identity -- an empty ``sections`` list, or one the
            section walk could not use.
    """

    documents: list[DocIdMapping] = field(default_factory=list)
    sections: list[DocIdMapping] = field(default_factory=list)
    new_id_sources: dict[str, list[str]] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)

    @property
    def total_nodes(self) -> int:
        """Number of node identities the projection moves."""
        return len(self.documents) + len(self.sections)

    def as_mapping(self) -> dict[str, str]:
        """Return the document-level old -> new mapping.

        Section IDs are derived from their document's mapping by suffix, so
        callers that rewrite links or drive renames only need this map.
        """
        return {m.old_id: m.new_id for m in self.documents}


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def resolve_docs_roots(project_root: Path, docs_entries: list[str]) -> list[tuple[str, Path]]:
    """Resolve configured docs entries to ``(entry, absolute path)`` pairs.

    Relative entries resolve against *project_root*; absolute entries are
    honoured as written.  Entries that do not exist on disk are dropped, and
    a repeated entry is visited once.

    Args:
        project_root: Absolute project root.
        docs_entries: ``config.scan.docs_dirs`` as configured.

    Returns:
        One pair per surviving root, in configuration order.
    """
    seen: set[str] = set()
    out: list[tuple[str, Path]] = []
    for entry in docs_entries:
        normalised = str(entry).replace("\\", "/").rstrip("/")
        if not normalised:
            continue
        entry_path = Path(normalised)
        abs_root = entry_path if entry_path.is_absolute() else (project_root / entry_path)
        try:
            key = str(abs_root.resolve())
        except OSError:  # pragma: no cover - unreadable path
            continue
        if key in seen:
            continue
        seen.add(key)
        if abs_root.is_dir():
            out.append((normalised, abs_root))
    return out


@task(
    purpose="Walk every configured docs root on disk and return one record per *.json file found",
    inputs="project_root, configured docs_dirs entries",
    outputs="list[DocFile] — one per file, deduplicated by resolved path, unclassified",
)
def enumerate_doc_files(project_root: Path, docs_entries: list[str]) -> list[DocFile]:
    """Return every ``*.json`` file under the configured roots, read from disk.

    Path strings only -- no file is opened, so this says nothing about
    whether a file is a *document*.  Pass the result through
    :func:`classify_doc_files` before deriving any identity from it.
    Deduplicated by resolved path so overlapping roots (``docs`` and
    ``docs/pev``) do not double-count a file; the first configured root that
    contains it wins, matching the scanner's first-root-wins visit order.

    Args:
        project_root: Absolute project root.
        docs_entries: ``config.scan.docs_dirs`` as configured.

    Returns:
        One :class:`DocFile` per file, ordered by root then by path.
    """
    project_root = Path(project_root)
    out: list[DocFile] = []
    seen: set[str] = set()
    for entry, abs_root in resolve_docs_roots(project_root, docs_entries):
        for json_file in sorted(abs_root.rglob("*.json")):
            try:
                key = str(json_file.resolve())
            except OSError:  # pragma: no cover - unreadable path
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                rel_path = json_file.relative_to(project_root).as_posix()
            except ValueError:
                rel_path = json_file.as_posix()
            out.append(
                DocFile(
                    path=json_file,
                    rel_path=rel_path,
                    rel_to_root=json_file.relative_to(abs_root).as_posix(),
                    root_entry=entry,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Classification — which of those files are actually documents
# ---------------------------------------------------------------------------


def classify_doc_file(path: Path) -> str:
    """Return what a ``*.json`` file under a docs root actually is.

    Applies the scanner's own admission test -- a JSON object carrying both
    ``title`` and ``sections`` -- so this module's idea of "a document"
    cannot drift from the set of files that become doc nodes.  The two
    non-document outcomes are deliberately kept apart rather than merged
    into one "not usable" bucket: an ordinary data file beside the docs is
    not a problem and must be silent, whereas a file that cannot be parsed
    at all is one and must not be.

    A document whose ``sections`` are present but malformed still classifies
    as a document -- it has an identity, it simply contributes no section
    identities, which :func:`project_doc_ids` reports separately.

    Args:
        path: Absolute path to the file.

    Returns:
        :data:`DOC_FILE_DOCUMENT`, :data:`DOC_FILE_NOT_A_DOCUMENT`, or
        :data:`DOC_FILE_UNREADABLE`.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return DOC_FILE_UNREADABLE
    if isinstance(data, dict) and "title" in data and "sections" in data:
        return DOC_FILE_DOCUMENT
    return DOC_FILE_NOT_A_DOCUMENT


@task(
    purpose="Split enumerated *.json files into DocJSON documents, ordinary data files, and unreadable files",
    inputs="DocFile records from enumerate_doc_files",
    outputs="DocTreeScan — only the documents carry a doc identity",
)
def classify_doc_files(doc_files: list[DocFile]) -> DocTreeScan:
    """Partition enumerated files into documents, data files, and unreadable.

    Opens every file.  Callers that only need to know about a handful of
    suspect paths should classify just those (see :func:`doc_id_signals`).

    Args:
        doc_files: Files from :func:`enumerate_doc_files`.

    Returns:
        A :class:`DocTreeScan`, preserving enumeration order in each class.
    """
    scan = DocTreeScan()
    for doc_file in doc_files:
        kind = classify_doc_file(doc_file.path)
        if kind == DOC_FILE_DOCUMENT:
            scan.documents.append(doc_file)
        elif kind == DOC_FILE_UNREADABLE:
            scan.unreadable.append(doc_file.rel_path)
        else:
            scan.non_documents.append(doc_file.rel_path)
    return scan


# ---------------------------------------------------------------------------
# Derivation — current and projected
# ---------------------------------------------------------------------------


def current_doc_id(project_id: str, doc_file: DocFile) -> str:
    """Return the doc ID *doc_file* resolves to under today's derivation.

    A read-only mirror of ``docjson.parse.scan_single_json_doc``: the path
    relative to the containing docs root, ``.json`` stripped, ``/`` rewritten
    to ``.``, under a single flat ``docs.`` namespace.

    Args:
        project_id: Project ID prefix.
        doc_file: The file whose identity is being derived.

    Returns:
        The full doc node ID.
    """
    raw_id = doc_file.rel_to_root.removesuffix(".json").replace("/", ".")
    return f"{project_id}::docs.{raw_id}"


def projected_doc_id(project_id: str, doc_file: DocFile) -> str:
    """Return the doc ID *doc_file* would resolve to under per-root namespacing.

    The configured root is used verbatim as the namespace prefix -- a leading
    dot is preserved, so ``.pev/test-policy.json`` projects to
    ``{project_id}::.pev/test-policy`` -- and ``/`` survives as the path
    joiner, which makes the derivation injective over paths.

    Args:
        project_id: Project ID prefix.
        doc_file: The file whose identity is being projected.

    Returns:
        The projected doc node ID.
    """
    stem = doc_file.rel_to_root.removesuffix(".json")
    return f"{project_id}::{doc_file.root_entry}/{stem}"


def section_dot_paths(json_path: Path) -> list[str]:
    """Return the dot-path of every section in a DocJSON file, in order.

    Mirrors the scanner's section walk: children join to their parent with a
    ``.``, and nesting deeper than the scanner's maximum is not walked (those
    children never become nodes, so they have no identity to move).

    Args:
        json_path: Absolute path to a DocJSON file.

    Returns:
        Dot-paths in document order.  Empty when the file is unreadable or
        is not a DocJSON document.

    Raises:
        OSError: Never -- read failures return an empty list.
    """
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    sections = data.get("sections")
    if not isinstance(sections, list):
        return []

    out: list[str] = []

    def _walk(items: list, prefix: str | None, depth: int) -> None:
        for section in items:
            if not isinstance(section, dict):
                continue
            raw_id = section.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                continue
            dot_path = f"{prefix}.{raw_id}" if prefix else raw_id
            out.append(dot_path)
            children = section.get("sections")
            if isinstance(children, list) and children and depth < _MAX_SECTION_DEPTH:
                _walk(children, dot_path, depth + 1)

    _walk(sections, None, 0)
    return out


def current_doc_id_index(project_id: str, doc_files: list[DocFile]) -> dict[str, list[str]]:
    """Map each currently-derived doc ID to the files that derive it.

    Args:
        project_id: Project ID prefix.
        doc_files: Files from :func:`enumerate_doc_files`.

    Returns:
        Doc ID -> repo-relative source paths, in enumeration order.
    """
    index: dict[str, list[str]] = {}
    for doc_file in doc_files:
        index.setdefault(current_doc_id(project_id, doc_file), []).append(doc_file.rel_path)
    return index


@task(
    purpose="Project every document and section identity under per-root doc-ID namespacing",
    inputs="project_id, enumerated DocFile records",
    outputs="DocIdProjection — document and section mappings plus projected-ID sources",
)
def project_doc_ids(project_id: str, doc_files: list[DocFile]) -> DocIdProjection:
    """Project every document envelope and every section to its new identity.

    Sections keep their ``{doc_id}::{dot.path}`` shape -- only the document
    half of a section ID moves.  Reading each file is required to enumerate
    its sections; a document that yields none contributes its envelope
    mapping and is listed in ``unreadable``.

    Expects *doc_files* to be DocJSON documents -- ``DocTreeScan.documents``
    from :func:`classify_doc_files`.  Handing it the raw enumeration would
    project identities for ordinary JSON data files that can never become
    doc nodes.

    Args:
        project_id: Project ID prefix.
        doc_files: Documents from :func:`classify_doc_files`.

    Returns:
        A :class:`DocIdProjection` covering documents and sections.
    """
    projection = DocIdProjection()
    for doc_file in doc_files:
        old_doc_id = current_doc_id(project_id, doc_file)
        new_doc_id = projected_doc_id(project_id, doc_file)
        projection.documents.append(
            DocIdMapping(
                old_id=old_doc_id,
                new_id=new_doc_id,
                kind="doc",
                file_path=doc_file.rel_path,
            )
        )
        projection.new_id_sources.setdefault(new_doc_id, []).append(doc_file.rel_path)

        dot_paths = section_dot_paths(doc_file.path)
        if not dot_paths:
            projection.unreadable.append(doc_file.rel_path)
        for dot_path in dot_paths:
            projection.sections.append(
                DocIdMapping(
                    old_id=f"{old_doc_id}::{dot_path}",
                    new_id=f"{new_doc_id}::{dot_path}",
                    kind="section",
                    file_path=doc_file.rel_path,
                )
            )
    return projection


# ---------------------------------------------------------------------------
# Collision gate + advisories
# ---------------------------------------------------------------------------


@task(
    purpose="Report doc IDs that more than one file derives, with their source paths",
    inputs="doc ID -> source paths index",
    outputs="list[DocIdCollision] — one per duplicate group, sorted by ID",
)
def find_collisions(id_index: dict[str, list[str]]) -> list[DocIdCollision]:
    """Return every duplicate group in an ID index.

    Classifies each group by whether its sources span configured roots
    (``cross_root``) or converge inside one root (``within_root``), using the
    first path segment as the root discriminator.

    Args:
        id_index: Doc ID -> repo-relative source paths.

    Returns:
        One :class:`DocIdCollision` per ID with two or more distinct sources,
        sorted by doc ID.
    """
    out: list[DocIdCollision] = []
    for doc_id, sources in id_index.items():
        distinct = sorted(set(sources))
        if len(distinct) < 2:
            continue
        roots = {src.split("/", 1)[0] for src in distinct}
        kind = COLLISION_CROSS_ROOT if len(roots) > 1 else COLLISION_WITHIN_ROOT
        out.append(DocIdCollision(doc_id=doc_id, sources=distinct, kind=kind))
    return sorted(out, key=lambda c: c.doc_id)


def dotted_filenames(doc_files: list[DocFile]) -> list[str]:
    """Return repo-relative paths whose filename stem carries extra dots.

    A dot in a filename never changes the derived ID -- it makes the derived
    ID indistinguishable from one a directory would have produced, which is
    how two paths converge.  Advisory only.

    Args:
        doc_files: Files from :func:`enumerate_doc_files`.

    Returns:
        Repo-relative paths, sorted.
    """
    return sorted(d.rel_path for d in doc_files if "." in d.path.stem)


@task(
    purpose="Report the doc-id overlaps and dotted filenames a build should warn about, counting DocJSON documents only",
    inputs="project_id, enumerated DocFile records",
    outputs="DocIdSignals — collision groups and dotted filenames, documents only",
)
def doc_id_signals(project_id: str, doc_files: list[DocFile]) -> DocIdSignals:
    """Return the build-time doc-ID findings for an enumerated doc tree.

    Only a DocJSON document carries an identity, so only a document can
    overlap with another or be advised on.  An ordinary JSON data file under
    a docs root is invisible here however its path is shaped -- including
    when two such files would derive one ID between them.

    Classification opens files, so it is applied only to the files a signal
    would actually name.  A file that neither shares a derived ID with
    another file nor carries a dotted stem contributes nothing to either
    list whatever its class, so restricting the reads to those "suspects"
    is lossless and leaves a clean tree at zero reads.

    Args:
        project_id: Project ID prefix.
        doc_files: Files from :func:`enumerate_doc_files`.

    Returns:
        A :class:`DocIdSignals` naming DocJSON documents only.
    """
    collisions = find_collisions(current_doc_id_index(project_id, doc_files))
    dotted = dotted_filenames(doc_files)
    suspect_paths = {src for c in collisions for src in c.sources} | set(dotted)
    if not suspect_paths:
        return DocIdSignals()

    suspects = [f for f in doc_files if f.rel_path in suspect_paths]
    document_paths = {f.rel_path for f in classify_doc_files(suspects).documents}
    # Re-derive rather than filter the groups in place: dropping a source can
    # turn a cross-root group into a within-root one, or leave a group with a
    # single source and no collision at all.
    kept = [f for f in doc_files if f.rel_path not in suspect_paths or f.rel_path in document_paths]
    return DocIdSignals(
        collisions=find_collisions(current_doc_id_index(project_id, kept)),
        dotted=dotted_filenames(kept),
    )
