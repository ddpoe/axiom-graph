"""Doc-ID enumeration, projection, and collision detection over the doc tree.

Doc node IDs are derived from a DocJSON file's path relative to the
configured docs root it was found under: the root verbatim, then the path
within it, ``/`` retained as the joiner and the extension (``.docjson`` or
``.json``) dropped.  Markdown
follows the same shape but keeps ``.md``, which is what stops ``x.md`` and
``x.json`` converging on one identity.  Enumeration deduplicates by resolved
path, so the derivation is injective -- two distinct files cannot derive one
identity.

That was not always true.  Until the doc-ID migration, every configured root
collapsed into a single ``docs.`` namespace and ``/`` was rewritten to
``.``; neither transform is injective, so two files could derive one
identity and the last-scanned one silently won.  The retired rule survives
here as :func:`current_doc_id` because it defines the namespace a
pre-migration index holds.

This module owns the derivation and is the instrument for what it used to
cost:

``enumerate_doc_files``
    Walk every configured docs root **on disk** and return one
    :class:`DocFile` per ``*.docjson`` / ``*.json`` file found.  Enumerating from disk
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
    The identity a file resolved to under the **retired** rule.  Frozen:
    it is the ``old_id`` half of every migration mapping and the "what does
    an unmigrated index hold" half of the reconciliation gate, so it must
    keep answering for the retired namespace forever.

``project_doc_ids``
    The identity a file resolves to under the live rule, addressed as the
    migration's target.  Delegates to :func:`derive_doc_id`.

``find_collisions``
    Duplicate groups over any ID index, with their source files.

``dotted_filenames``
    Files whose stem carries extra dots -- advisory only; a dot in a
    filename never changes a derived ID, it only makes two paths more
    likely to converge on one.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from axiom_annotations import task

from axiom_graph.index.walk import TreeListing, suffix_matcher


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


# ---------------------------------------------------------------------------
# The derivation rule — one home
#
# Every site that derives a doc ID, decides whether a string *is* one, or
# reconstructs one from a path routes through this block.  Nothing else in
# the codebase may spell the rule out for itself: a second implementation is
# how four sites came to disagree in the first place.
#
# ``current_doc_id`` below is deliberately NOT part of this block.  It is the
# frozen pre-migration rule -- the ``old_id`` half of every migration mapping
# and the "what does the index hold" half of the reconciliation gate -- and it
# must keep answering for the retired namespace after the live rule moves on.
# ---------------------------------------------------------------------------


#: Joiner between path segments inside a doc-ID body.
DOC_ID_PATH_SEP = "/"

#: Every extension a DocJSON document may carry, in precedence order.  The
#: default ``docs_extensions`` lists them in this order, so ``.docjson`` is
#: the default write extension.  This order -- not the configured one --
#: decides which file wins when ``x.json`` and ``x.docjson`` sit in one
#: folder: ``.docjson`` is the newer format and always wins.  The extension
#: never reaches a doc ID: :func:`strip_docjson_extension` drops it.
DOCJSON_EXTENSIONS: tuple[str, ...] = (".docjson", ".json")


def strip_docjson_extension(rel_path: str) -> str:
    """Return *rel_path* without its DocJSON extension.

    Either extension is dropped, in any letter case, so ``x.json`` and
    ``x.docjson`` name one document.  A path carrying neither is returned
    unchanged.

    Args:
        rel_path: A file path or name.

    Returns:
        The path with a trailing ``.docjson`` / ``.json`` removed.
    """
    lowered = rel_path.lower()
    for ext in DOCJSON_EXTENSIONS:
        if lowered.endswith(ext):
            return rel_path[: -len(ext)]
    return rel_path


def has_docjson_extension(path: Path | str, extensions: tuple[str, ...] | list[str] | None = None) -> bool:
    """Return whether *path* carries a DocJSON document extension.

    Says nothing about the file's content -- ordinary JSON data carries
    ``.json`` too.  Classify the file before treating it as a document.

    Args:
        path: A file path.
        extensions: The extensions to accept.  Defaults to every DocJSON
            extension.

    Returns:
        True when the path ends with one of *extensions* (case-insensitive).
    """
    name = Path(path).name.lower()
    return any(name.endswith(ext) for ext in (extensions or DOCJSON_EXTENSIONS))


def validate_docjson_extensions(raw: object) -> tuple[list[str], list[str]]:
    """Split a configured ``docs_extensions`` value into valid and dropped entries.

    Only the DocJSON extensions are accepted; a leading dot is optional and
    case is ignored.  A missing, empty, or entirely invalid value means the
    default, so a document can never be written with an extension the
    scanner does not read.

    Args:
        raw: The configured value, as loaded from TOML.

    Returns:
        ``(extensions, dropped)`` -- the extensions to use, first entry the
        write extension, and the configured entries that were not valid.
    """
    if not isinstance(raw, list):
        return list(DOCJSON_EXTENSIONS), ([] if raw is None else [str(raw)])
    valid: list[str] = []
    dropped: list[str] = []
    for item in raw:
        text = str(item).strip().lower()
        ext = text if text.startswith(".") else f".{text}"
        if ext in DOCJSON_EXTENSIONS:
            if ext not in valid:
                valid.append(ext)
        else:
            dropped.append(str(item))
    return (valid or list(DOCJSON_EXTENSIONS)), dropped


def docjson_files_under(
    root: Path,
    extensions: tuple[str, ...] | list[str] | None = None,
    *,
    listing: TreeListing | None = None,
) -> list[Path]:
    """Return every file under *root* carrying one of *extensions*, sorted.

    Path strings only -- nothing is opened.  Files of every extension are
    returned, including two that derive one identity; use
    :func:`indexable_docjson_files` where only one of them may be indexed.

    Args:
        root: Directory to walk recursively.
        extensions: The extensions to walk.  Defaults to every DocJSON
            extension.
        listing: The operation's shared directory listing; a fresh one
            when omitted.

    Returns:
        Absolute paths, sorted.
    """
    listing = listing if listing is not None else TreeListing()
    return sorted(set(listing.matches(root, suffix_matcher(extensions or DOCJSON_EXTENSIONS), files_only=True)))


def indexable_docjson_files(
    root: Path,
    extensions: tuple[str, ...] | list[str] | None = None,
    *,
    listing: TreeListing | None = None,
) -> list[Path]:
    """Return the files under *root* a scan should index, one per identity.

    When ``x.json`` and ``x.docjson`` sit in one folder they derive one doc
    ID, and indexing both would let the scan order decide which one the
    index holds.  :func:`_preferred_file` picks one: a DocJSON document over
    a data file, then ``.docjson`` over ``.json`` whatever the order of
    *extensions*.  The build reports a pair of documents as a collision.

    Args:
        root: Directory to walk recursively.
        extensions: The configured extensions to walk.  Defaults to every
            DocJSON extension.
        listing: The operation's shared directory listing; a fresh one
            when omitted.

    Returns:
        Absolute paths, sorted, at most one per extension-less path.
    """
    groups: dict[str, list[Path]] = {}
    for path in docjson_files_under(root, extensions, listing=listing):
        groups.setdefault(strip_docjson_extension(path.as_posix()), []).append(path)
    return sorted(_preferred_file(paths) for paths in groups.values())


def _extension_rank(path: Path | str) -> int:
    """Return the position of *path*'s extension in :data:`DOCJSON_EXTENSIONS`."""
    name = Path(path).name.lower()
    return next((i for i, ext in enumerate(DOCJSON_EXTENSIONS) if name.endswith(ext)), len(DOCJSON_EXTENSIONS))


def _preferred_file(paths: list[Path]) -> Path:
    """Return the one file of *paths* (one extension-less path) that owns it.

    A lone file is returned without being opened.  Among several, a file
    that classifies as a DocJSON document beats one that does not -- so a
    ``.docjson`` data file never hides a ``.json`` document -- and among
    equals ``.docjson`` beats ``.json``.

    Args:
        paths: Files differing only by DocJSON extension.

    Returns:
        The file that owns the shared identity.
    """
    if len(paths) == 1:
        return paths[0]
    return min(
        sorted(paths),
        key=lambda p: (classify_doc_file(p) != DOC_FILE_DOCUMENT, _extension_rank(p)),
    )


def extension_collision_winner(sources: list[str]) -> str | None:
    """Return which file a scan indexes when *sources* differ only by extension.

    *sources* are the documents of one build collision group -- each one
    already classifies as a DocJSON document -- so ``.docjson`` wins, as it
    does in :func:`indexable_docjson_files`.

    Args:
        sources: Repo-relative paths of one collision group.

    Returns:
        The source :func:`indexable_docjson_files` keeps, or ``None`` when
        the sources do not share one extension-less path.
    """
    if len({strip_docjson_extension(s) for s in sources}) != 1:
        return None
    return min(sorted(sources), key=_extension_rank)


def existing_docjson_file(
    directory: Path, slug: str, extensions: tuple[str, ...] | list[str] | None = None
) -> Path | None:
    """Return the file a document *slug* already has in *directory*, if any.

    A write to an existing document goes to the file that already holds it,
    whichever extension it carries, so overwriting an unconverted ``x.json``
    never creates an ``x.docjson`` beside it.  When both exist, the one a
    scan indexes is returned (see :func:`_preferred_file`).

    Args:
        directory: The docs root the slug is relative to.
        slug: Path-slug of the document, without an extension.
        extensions: The configured extensions; a file carrying any other
            extension is not looked for.  Defaults to every DocJSON
            extension.

    Returns:
        The existing file, or ``None`` when the slug has no file yet.
    """
    allowed = {e.lower() for e in (extensions or DOCJSON_EXTENSIONS)}
    found = [
        Path(directory) / f"{slug}{ext}"
        for ext in DOCJSON_EXTENSIONS
        if ext in allowed and (Path(directory) / f"{slug}{ext}").is_file()
    ]
    return _preferred_file(found) if found else None


def docjson_filename(slug: str, extensions: tuple[str, ...] | list[str] | None = None) -> str:
    """Return the filename a new document *slug* is written as.

    Args:
        slug: Path-slug of the document, without an extension.
        extensions: The configured extensions; the first is the write
            extension.  Defaults to every DocJSON extension.

    Returns:
        ``slug`` plus the write extension.
    """
    return f"{slug}{(extensions or DOCJSON_EXTENSIONS)[0]}"


def normalise_root_entry(entry: str) -> str:
    """Return a configured docs-root entry in canonical namespace form.

    ``./docs``, ``docs/``, and ``docs\\`` all name one root and must derive one
    namespace prefix, or the scanner and the migration projection disagree
    about a document's identity.  Absolute entries are returned as POSIX
    strings unchanged.

    Args:
        entry: A ``config.scan.docs_dirs`` entry as written.

    Returns:
        The canonical form, or ``""`` when *entry* names nothing.
    """
    text = str(entry).replace("\\", "/").strip()
    if not text:
        return ""
    normalised = Path(text).as_posix()
    return "" if normalised in (".", "") else normalised


def derive_doc_id(project_id: str, root_entry: str, rel_to_root: str) -> str:
    """Return the doc node ID for a DocJSON file.

    The single live derivation for DocJSON documents: the configured root
    verbatim -- a leading dot included, so ``.pev`` namespaces its own
    documents -- then the file's path within that root, with ``/`` retained as
    the joiner.  The extension -- ``.docjson`` or ``.json`` -- is dropped: a
    DocJSON document's ID never carries it, so converting a file from one
    extension to the other keeps its identity.

    Both halves are what make the derivation injective.  Keeping the root
    means two files at the same relative path under two roots are two
    documents; keeping ``/`` means ``adrs/013-x.json`` and ``adrs.013-x.json``
    are two documents.

    Args:
        project_id: Project ID prefix.
        root_entry: The configured docs root containing the file, as written.
        rel_to_root: POSIX path of the file relative to that root.

    Returns:
        The full doc node ID.
    """
    stem = strip_docjson_extension(rel_to_root.replace("\\", "/"))
    return f"{project_id}::{join_doc_id_body(normalise_root_entry(root_entry), stem)}"


def derive_markdown_doc_id(project_id: str, root_entry: str, rel_to_root: str) -> str:
    """Return the doc node ID for a Markdown document.

    The single live derivation for markdown documents.  Kept apart from
    :func:`derive_doc_id` because the two document classes do not share a
    rule: markdown retains its ``.md`` extension so that ``x.md`` and
    ``x.json`` in one directory cannot converge on one identity.

    The asymmetry with :func:`derive_doc_id` is deliberate and is the reason
    a project holding both files is migratable rather than permanently
    refused.  It is not an oversight to tidy up.

    Args:
        project_id: Project ID prefix.
        root_entry: The configured docs root containing the file, as written.
        rel_to_root: POSIX path of the file relative to that root.

    Returns:
        The full doc node ID, extension included.
    """
    rel = rel_to_root.replace("\\", "/")
    return f"{project_id}::{join_doc_id_body(normalise_root_entry(root_entry), rel)}"


def doc_id_root_prefixes(root_entries: list[str] | tuple[str, ...] | None) -> list[str]:
    """Return the doc-ID body prefixes a document ID may legitimately start with.

    Used to decide whether the middle segment of a ``proj::body::tail`` string
    names a *document* or a *code module*.  Derived from the configured roots
    rather than from punctuation, so the answer stays correct — and the error
    message stays useful — as the derivation changes.

    Args:
        root_entries: ``config.scan.docs_dirs`` as configured.  May be empty.

    Returns:
        Prefixes, longest first so that nested roots match greedily.  Empty
        when no root is configured -- the caller then reports that the ID is
        under none of them, which is true.
    """
    prefixes = {
        f"{normalised}{DOC_ID_PATH_SEP}"
        for entry in (root_entries or ())
        if (normalised := normalise_root_entry(entry))
    }
    return sorted(prefixes, key=len, reverse=True)


def doc_id_body_prefix(root_rel: str) -> str:
    """Return the doc-ID body prefix for a project-relative docs subtree.

    A consumer-site nav root such as ``docs/consumer`` publishes documents
    whose IDs begin with the body prefix this returns; callers append
    :data:`DOC_ID_PATH_SEP` and the path within that subtree.

    Args:
        root_rel: Project-relative POSIX path of the subtree.

    Returns:
        The doc-ID body prefix, without a trailing separator.
    """
    return normalise_root_entry(root_rel)


def join_doc_id_body(prefix: str, rel_path: str) -> str:
    """Join a doc-ID body prefix with a path beneath it.

    The one place a caller reconstructing an ID from a path decides how
    segments are joined, so a reconstruction cannot fall out of step with
    :func:`derive_doc_id`.

    Args:
        prefix: A body prefix from :func:`doc_id_body_prefix`.
        rel_path: POSIX path beneath that prefix, without an extension.

    Returns:
        The doc-ID body — everything after ``{project_id}::``.
    """
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p and p != "."]
    head = [prefix] if prefix else []
    return DOC_ID_PATH_SEP.join([*head, *parts]) if parts else prefix


# --- Section-ID grammar ----------------------------------------------------
#
# Not part of the block above: these separators are unaffected by the path
# joiner and do not change when the derivation does.  They live here because
# the two document classes attach sections to their envelope differently, and
# every consumer that enumerates or rekeys a document's sections has to be
# told which class it is holding.

#: Separator between a DocJSON document's ID and a section's dot-path.
DOCJSON_SECTION_SEP = "::"

#: Separator between a Markdown document's ID and an H2 section's slug.
MARKDOWN_SECTION_SEP = "#"


# ---------------------------------------------------------------------------
# Root resolution — which configured root contains a file, and where in it
# ---------------------------------------------------------------------------


def _root_entry_for(root_dir: Path, project_root: Path) -> str:
    """Return the configured-root form of *root_dir* relative to the project.

    Args:
        root_dir: Absolute path of a docs root.
        project_root: Absolute project root.

    Returns:
        The root entry in canonical namespace form, falling back to the
        absolute POSIX path when the root sits outside the project.
    """
    root_dir = Path(root_dir)
    for base in (Path(project_root), Path(project_root).resolve()):
        for candidate in (root_dir, root_dir.resolve()):
            try:
                return normalise_root_entry(candidate.relative_to(base).as_posix())
            except (ValueError, OSError):
                continue
    return normalise_root_entry(root_dir.as_posix())


def resolve_doc_root(
    doc_file: Path,
    project_root: Path,
    docs_dir: Path | None,
    docs_root_entry: str | None = None,
) -> tuple[str, str]:
    """Return ``(root_entry, rel_to_root)`` for a document file.

    The two values the derivation needs, resolved through the same three
    cases the scanners have always covered: the caller named the containing
    root, the caller named a root that does not contain the file, or no root
    was named at all.  When every candidate root is exhausted the path is
    taken relative to the project with a leading ``docs`` segment consumed,
    which is what the primary root would have produced.

    Class-agnostic: DocJSON and Markdown resolve their root identically, and
    only the ID rule applied to the result differs.

    Args:
        doc_file: Absolute path to the document file.
        project_root: Absolute project root.
        docs_dir: The docs root the caller believes contains the file.
        docs_root_entry: That root as configured, when the caller knows it.

    Returns:
        ``(root_entry, rel_to_root)`` -- a configured-root string and the
        file's POSIX path within it.
    """
    candidates: list[tuple[str | None, Path]] = []
    if docs_dir is not None:
        candidates.append((docs_root_entry, Path(docs_dir)))
    candidates.append((None, Path(project_root) / "docs"))
    for entry, root_dir in candidates:
        try:
            rel = doc_file.relative_to(root_dir).as_posix()
        except ValueError:
            continue
        return (entry or _root_entry_for(root_dir, project_root), rel)

    rel = doc_file.relative_to(project_root).as_posix()
    head, _, remainder = rel.partition("/")
    if remainder and head == "docs":
        return ("docs", remainder)
    return ("docs", rel)


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
    """Every DocJSON-extension file under the docs roots, split by what it is.

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


def _real_path(path: Path) -> Path | None:
    """Return *path* resolved, or ``None`` when it cannot be."""
    try:
        return path.resolve()
    except OSError:  # pragma: no cover - unreadable path
        return None


def _dedupe_key(path: Path, root: Path, real_root: Path | None, linked: bool) -> str | None:
    """Return the identity two docs roots reaching one file agree on: its resolved path.

    When nothing between *root* and the file is a link, the resolved path is
    the root's resolved path plus the file's path within it, so a plain tree
    costs no system call per file; a linked file is resolved itself.

    Args:
        path: The file.
        root: The docs root it was found under.
        real_root: *root* resolved (``None`` when it could not be).
        linked: Whether the file or a directory between it and *root* is a link.

    Returns:
        The resolved path as a string, or ``None`` when it cannot be resolved.
    """
    if real_root is not None and not linked:
        return str(real_root / path.relative_to(root))
    resolved = _real_path(path)
    return str(resolved) if resolved is not None else None


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
        normalised = normalise_root_entry(entry)
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
    purpose="Walk every configured docs root on disk and return one record per DocJSON-extension file found (*.docjson and *.json by default)",
    inputs="project_root, configured docs_dirs entries, configured docs_extensions",
    outputs="list[DocFile] — one per file, deduplicated by resolved path, unclassified",
)
def enumerate_doc_files(
    project_root: Path,
    docs_entries: list[str],
    extensions: tuple[str, ...] | list[str] | None = None,
    *,
    listing: TreeListing | None = None,
) -> list[DocFile]:
    """Return every DocJSON-extension file under the configured roots, from disk.

    Path strings only -- no file is opened, so this says nothing about
    whether a file is a *document*.  Pass the result through
    :func:`classify_doc_files` before deriving any identity from it.
    Deduplicated by resolved path so overlapping roots (``docs`` and
    ``docs/pev``) do not double-count a file; the first configured root that
    contains it wins, matching the scanner's first-root-wins visit order.

    Every configured extension is walked, and ``x.json`` beside ``x.docjson``
    are *both* returned: they derive one ID, which is exactly what makes
    :func:`find_collisions` report them.

    Args:
        project_root: Absolute project root.
        docs_entries: ``config.scan.docs_dirs`` as configured.
        extensions: ``config.scan.docs_extensions``.  Defaults to every
            DocJSON extension.
        listing: The operation's shared directory listing; a fresh one
            when omitted.

    Returns:
        One :class:`DocFile` per file, ordered by root then by path.
    """
    project_root = Path(project_root)
    listing = listing if listing is not None else TreeListing()
    matcher = suffix_matcher(extensions or DOCJSON_EXTENSIONS)
    out: list[DocFile] = []
    seen: set[str] = set()
    for entry, abs_root in resolve_docs_roots(project_root, docs_entries):
        real_root = _real_path(abs_root)
        for json_file, linked in sorted(set(listing.hits(abs_root, matcher, files_only=True))):
            key = _dedupe_key(json_file, abs_root, real_root, linked)
            if key is None:  # pragma: no cover - unreadable path
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


@task(
    purpose="Walk every configured docs root on disk and return one record per *.md file found",
    inputs="project_root, configured docs_dirs entries",
    outputs="list[DocFile] — one per Markdown file, deduplicated by resolved path",
)
def enumerate_markdown_files(
    project_root: Path,
    docs_entries: list[str],
    *,
    listing: TreeListing | None = None,
) -> list[DocFile]:
    """Return every ``*.md`` file under the configured roots, read from disk.

    The Markdown sibling of :func:`enumerate_doc_files`.  No classification
    step follows it: the Markdown scanner admits every ``*.md`` it walks, so
    every file returned here becomes a doc node.  That is the whole reason
    the two enumerations stay apart rather than sharing a glob — ``*.json``
    under a docs root is *not* necessarily a document, and ``*.md`` always
    is.

    Args:
        project_root: Absolute project root.
        docs_entries: ``config.scan.docs_dirs`` as configured.
        listing: The operation's shared directory listing; a fresh one
            when omitted.

    Returns:
        One :class:`DocFile` per file, ordered by root then by path.
    """
    project_root = Path(project_root)
    listing = listing if listing is not None else TreeListing()
    matcher = suffix_matcher([".md"])
    out: list[DocFile] = []
    seen: set[str] = set()
    for entry, abs_root in resolve_docs_roots(project_root, docs_entries):
        real_root = _real_path(abs_root)
        for md_file, linked in sorted(set(listing.hits(abs_root, matcher))):
            key = _dedupe_key(md_file, abs_root, real_root, linked)
            if key is None:  # pragma: no cover - unreadable path
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                rel_path = md_file.relative_to(project_root).as_posix()
            except ValueError:
                rel_path = md_file.as_posix()
            out.append(
                DocFile(
                    path=md_file,
                    rel_path=rel_path,
                    rel_to_root=md_file.relative_to(abs_root).as_posix(),
                    root_entry=entry,
                )
            )
    return out


def markdown_section_slugs(md_path: Path) -> list[str]:
    """Return the slug of every H2 section in a Markdown file, in order.

    Delegates the split and the slug rule to the Markdown scanner itself, so
    this module's idea of "a section identity" cannot drift from the set of
    section nodes a build actually creates.

    Args:
        md_path: Absolute path to a Markdown file.

    Returns:
        Slugs in document order.  Empty when the file cannot be read or
        carries no H2 headings.
    """
    from axiom_graph.scanners.doc_scanner import markdown_section_slugs as _slugs  # noqa: PLC0415

    try:
        text = Path(md_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _slugs(text)


# ---------------------------------------------------------------------------
# Classification — which of those files are actually documents
# ---------------------------------------------------------------------------


def classify_doc_file(path: Path) -> str:
    """Return what a ``*.json`` / ``*.docjson`` file under a docs root actually is.

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
    purpose="Split enumerated DocJSON-extension files into DocJSON documents, ordinary data files, and unreadable files",
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
    """Return the doc ID *doc_file* resolved to under the retired derivation.

    The path relative to the containing docs root, ``.json`` stripped, ``/``
    rewritten to ``.``, under a single flat ``docs.`` namespace.  Frozen: it
    answers for the namespace a pre-migration index holds and must not follow
    the live rule.  ``.docjson`` is stripped too: no pre-migration index ever
    held a ``.docjson`` path, so a converted file maps to the retired ID its
    ``.json`` form did and the reconciliation gate reads a converted tree
    correctly.

    Args:
        project_id: Project ID prefix.
        doc_file: The file whose identity is being derived.

    Returns:
        The full doc node ID.
    """
    raw_id = strip_docjson_extension(doc_file.rel_to_root).replace("/", ".")
    return f"{project_id}::docs.{raw_id}"


def current_markdown_doc_id(project_id: str, doc_file: DocFile) -> str:
    """Return the doc ID a Markdown file resolves to under today's derivation.

    A read-only mirror of ``scanners.doc_scanner``'s pre-migration rule: the
    filename **stem** alone, under the flat ``docs.`` namespace.  The path
    above the file and the ``.md`` extension are both discarded, which is
    what makes two Markdown files in different directories converge — and
    what makes a Markdown file converge with a DocJSON file of the same stem.

    Frozen alongside :func:`current_doc_id`: it answers for the retired
    namespace and must not follow the live rule.

    Args:
        project_id: Project ID prefix.
        doc_file: The file whose identity is being derived.

    Returns:
        The full doc node ID.
    """
    return f"{project_id}::docs.{Path(doc_file.rel_to_root).stem}"


def projected_doc_id(project_id: str, doc_file: DocFile) -> str:
    """Return the doc ID *doc_file* would resolve to under per-root namespacing.

    The migration's target half.  Delegates to :func:`derive_doc_id` rather
    than restating it: the whole ordering rests on a migrated index and a
    rebuild landing on the same string, and two implementations of one rule is
    how they come to differ by a separator without anyone noticing.

    Args:
        project_id: Project ID prefix.
        doc_file: The file whose identity is being projected.

    Returns:
        The projected doc node ID.
    """
    return derive_doc_id(project_id, doc_file.root_entry, doc_file.rel_to_root)


def projected_markdown_doc_id(project_id: str, doc_file: DocFile) -> str:
    """Return the ID a Markdown file would resolve to under per-root namespacing.

    Identical to :func:`projected_doc_id` except that the ``.md`` extension is
    **kept**.  Retaining it is what makes the derivation injective across
    document classes: ``x.md`` and ``x.json`` in one directory project to two
    identities rather than one, so a project holding both is migratable
    instead of permanently refused.

    Delegates to :func:`derive_markdown_doc_id` for the same reason
    :func:`projected_doc_id` delegates.

    Args:
        project_id: Project ID prefix.
        doc_file: The file whose identity is being projected.

    Returns:
        The projected doc node ID, extension included.
    """
    return derive_markdown_doc_id(project_id, doc_file.root_entry, doc_file.rel_to_root)


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


def live_doc_id_index(project_id: str, doc_files: list[DocFile]) -> dict[str, list[str]]:
    """Map each doc ID the **scanner produces today** to the files deriving it.

    The build's overlap advisory reads this rather than
    :func:`current_doc_id_index`: an advisory keyed on a namespace nothing
    produces reports overlaps that cannot happen and stays silent about the
    ones that can.

    Note:
        Under the live rule the only way two files produce one ID is the
        extension: ``x.json`` beside ``x.docjson`` derive one identity,
        because the extension never reaches an ID.  :func:`derive_doc_id`
        keeps the root and the ``/`` joiner, and :func:`enumerate_doc_files`
        deduplicates by resolved path, so no other pair of distinct files can
        converge.  :func:`find_collisions` also does real work for the
        migration preview, which reasons over the *retired* namespace.

    Args:
        project_id: Project ID prefix.
        doc_files: Files from :func:`enumerate_doc_files`.

    Returns:
        Doc ID -> repo-relative source paths, in enumeration order.
    """
    index: dict[str, list[str]] = {}
    for doc_file in doc_files:
        doc_id = derive_doc_id(project_id, doc_file.root_entry, doc_file.rel_to_root)
        index.setdefault(doc_id, []).append(doc_file.rel_path)
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


@task(
    purpose="Project every Markdown document and H2 section identity under per-root doc-ID namespacing",
    inputs="project_id, enumerated Markdown DocFile records",
    outputs="DocIdProjection — markdown document and section mappings plus projected-ID sources",
)
def project_markdown_doc_ids(project_id: str, doc_files: list[DocFile]) -> DocIdProjection:
    """Project every Markdown document and H2 section to its new identity.

    The Markdown sibling of :func:`project_doc_ids`.  Three things differ and
    all three are why this is a separate walk rather than a parameter:

    - sections attach with :data:`MARKDOWN_SECTION_SEP`, not ``::``;
    - a section identity is an H2 slug, not a nested dot-path;
    - the new ID keeps ``.md``, so it cannot converge with a DocJSON sibling.

    Mappings carry ``kind="markdown_doc"`` / ``"markdown_section"`` so a
    caller holding both projections can tell which rekey shape applies.

    Args:
        project_id: Project ID prefix.
        doc_files: Files from :func:`enumerate_markdown_files`.

    Returns:
        A :class:`DocIdProjection` covering Markdown documents and sections.
        ``unreadable`` lists documents that yielded no section identity — for
        Markdown that means "no H2 headings", which is ordinary, not an error.
    """
    projection = DocIdProjection()
    for doc_file in doc_files:
        old_doc_id = current_markdown_doc_id(project_id, doc_file)
        new_doc_id = projected_markdown_doc_id(project_id, doc_file)
        projection.documents.append(
            DocIdMapping(
                old_id=old_doc_id,
                new_id=new_doc_id,
                kind="markdown_doc",
                file_path=doc_file.rel_path,
            )
        )
        projection.new_id_sources.setdefault(new_doc_id, []).append(doc_file.rel_path)

        slugs = markdown_section_slugs(doc_file.path)
        if not slugs:
            projection.unreadable.append(doc_file.rel_path)
        for slug in slugs:
            projection.sections.append(
                DocIdMapping(
                    old_id=f"{old_doc_id}{MARKDOWN_SECTION_SEP}{slug}",
                    new_id=f"{new_doc_id}{MARKDOWN_SECTION_SEP}{slug}",
                    kind="markdown_section",
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


# ---------------------------------------------------------------------------
# Consumer protection — the reconciliation gate and the malformed-ID advisory
#
# Two mechanisms, deliberately kept apart.  They answer different questions
# and neither substitutes for the other:
#
#   * the **gate** asks "does the index hold a namespace this code no longer
#     produces?"  Nothing is malformed in that case -- the stored IDs are
#     well-formed IDs of a retired namespace -- so a malformed-ID detector is
#     silent while the whole doc tree is re-inserted as new nodes;
#   * the **advisory** asks "is this ID structurally usable at all?"  It is
#     namespace-agnostic on purpose: an old-form ID must never be reported as
#     malformed, or upgrading a consumer would drown the gate's one clear
#     message in a warning per document.
# ---------------------------------------------------------------------------


#: The index and the code agree about the doc namespace.
RECONCILIATION_CLEAR = "clear"

#: The index holds IDs under a namespace this code no longer derives.
RECONCILIATION_UNMIGRATED = "unmigrated"

#: There is no index yet, or it holds no doc identities to reconcile.
RECONCILIATION_NO_INDEX = "no_index"


@dataclass(frozen=True)
class DocIdReconciliation:
    """Whether the stored doc namespace matches the one this code derives.

    Attributes:
        state: :data:`RECONCILIATION_CLEAR`,
            :data:`RECONCILIATION_UNMIGRATED`, or
            :data:`RECONCILIATION_NO_INDEX`.
        stale_ids: Stored doc IDs that no file on disk derives, but whose
            file *would* derive a different ID under the live rule.  Sorted.
        expected_ids: The IDs those files derive today.  Same order as
            ``stale_ids``, so a report can show the move.
        documents_checked: Documents on disk the check considered.
    """

    state: str
    stale_ids: list[str] = field(default_factory=list)
    expected_ids: list[str] = field(default_factory=list)
    documents_checked: int = 0

    @property
    def blocked(self) -> bool:
        """Whether a build must refuse rather than re-insert the doc tree."""
        return self.state == RECONCILIATION_UNMIGRATED


class DocIdNamespaceError(RuntimeError):
    """A build was refused because the index holds a retired doc namespace."""


def reconcile_doc_namespace(
    stored_doc_ids: set[str],
    project_id: str,
    doc_files: list[DocFile],
    markdown_files: list[DocFile] | None = None,
) -> DocIdReconciliation:
    """Compare the doc identities on disk against the ones the index holds.

    Per file, both the retired and the live identity are derived.  A file
    counts against the verdict only when the index holds its **old** ID and
    not its **new** one -- which is precisely the state in which a build would
    insert the document afresh and retire everything it had accumulated.

    A predicate over data, not a version stamp: after a migration no file's
    old ID is in the index, so the condition is permanently false.  A
    hand-edited old-form row re-firing it is not a false positive -- that row
    genuinely will be re-inserted-as-new on the next build.

    The build passes only the DocJSON namespace suspects
    (:func:`namespace_suspects`, then classified), not every DocJSON
    document, plus every Markdown file: a DocJSON file that is no suspect
    cannot count against the verdict, so the verdict is the one every
    document would give.  ``documents_checked`` then counts the suspects
    and the Markdown files, and the state is
    :data:`RECONCILIATION_NO_INDEX` when there are neither.

    Args:
        stored_doc_ids: Every doc envelope ID currently in the index.
        project_id: Project ID prefix.
        doc_files: The DocJSON documents to judge -- in a build, the
            classified namespace suspects.
        markdown_files: Markdown files from :func:`enumerate_markdown_files`.

    Returns:
        A :class:`DocIdReconciliation`; its ``documents_checked`` counts the
        files given.
    """
    pairs: list[tuple[str, str]] = [
        (current_doc_id(project_id, f), derive_doc_id(project_id, f.root_entry, f.rel_to_root)) for f in doc_files
    ]
    pairs.extend(
        (current_markdown_doc_id(project_id, f), derive_markdown_doc_id(project_id, f.root_entry, f.rel_to_root))
        for f in markdown_files or []
    )
    if not stored_doc_ids or not pairs:
        return DocIdReconciliation(state=RECONCILIATION_NO_INDEX, documents_checked=len(pairs))

    stale = sorted(
        {(old, new) for old, new in pairs if old != new and old in stored_doc_ids and new not in stored_doc_ids}
    )
    if not stale:
        return DocIdReconciliation(state=RECONCILIATION_CLEAR, documents_checked=len(pairs))
    return DocIdReconciliation(
        state=RECONCILIATION_UNMIGRATED,
        stale_ids=[old for old, _new in stale],
        expected_ids=[new for _old, new in stale],
        documents_checked=len(pairs),
    )


def namespace_suspects(stored_doc_ids: set[str], project_id: str, doc_files: list[DocFile]) -> list[DocFile]:
    """Return the files whose identity could count against :func:`reconcile_doc_namespace`, unclassified.

    A document counts against the verdict only when the index holds its
    retired ID and not its live one.  A file failing that test contributes
    nothing whatever its class, so classifying only these files gives the
    same verdict as classifying every file, and an index that holds no
    retired ID opens none.

    Args:
        stored_doc_ids: Every doc envelope ID currently in the index.
        project_id: Project ID prefix.
        doc_files: Files from :func:`enumerate_doc_files`.

    Returns:
        The suspects, in enumeration order.
    """
    if not stored_doc_ids:
        return []
    out: list[DocFile] = []
    for doc_file in doc_files:
        old = current_doc_id(project_id, doc_file)
        if old not in stored_doc_ids:
            continue
        new = derive_doc_id(project_id, doc_file.root_entry, doc_file.rel_to_root)
        if old != new and new not in stored_doc_ids:
            out.append(doc_file)
    return out


def malformed_doc_ids(stored_doc_ids: set[str], project_id: str) -> list[str]:
    """Return stored doc IDs that are not structurally usable, sorted.

    Structure only -- never namespace.  A document envelope ID is
    ``{project_id}::{body}``: one ``::``, both halves non-empty, and the
    prefix matching this project.  An ID under a *retired* namespace passes
    every one of those tests, which is the point: the reconciliation gate
    owns that case and must not have its one message buried under a warning
    per document.

    Args:
        stored_doc_ids: Every doc envelope ID currently in the index.
        project_id: Project ID prefix.

    Returns:
        Malformed IDs, sorted.
    """
    out: list[str] = []
    for doc_id in stored_doc_ids:
        head, sep, body = doc_id.partition("::")
        if not sep or head != project_id or not body.strip() or "::" in body:
            out.append(doc_id)
    return sorted(out)


def dotted_filenames(doc_files: list[DocFile]) -> list[str]:
    """Return repo-relative paths whose filename stem carries extra dots.

    The dots survive into the derived ID verbatim, so a dotted stem no longer
    converges with the ID a directory of the same name would produce:
    ``templates.protocol.json`` and ``templates/protocol.json`` are two
    documents.  What the advisory is still good for is the reading of the ID
    rather than its uniqueness -- a dotted filename beside a same-named
    directory makes the ID *look* like a path it is not, which misleads prose
    references, hand-written links, and any tool that splits an ID on ``.``.
    Advisory only.

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
def doc_id_signals(
    project_id: str,
    doc_files: list[DocFile],
    *,
    known_documents: Collection[str] = (),
) -> DocIdSignals:
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
        known_documents: Repo-relative paths the caller already knows to be
            DocJSON documents (bytes unchanged since one was found there);
            a suspect among them is not opened.

    Returns:
        A :class:`DocIdSignals` naming DocJSON documents only.
    """
    collisions = find_collisions(live_doc_id_index(project_id, doc_files))
    dotted = dotted_filenames(doc_files)
    suspect_paths = {src for c in collisions for src in c.sources} | set(dotted)
    if not suspect_paths:
        return DocIdSignals()

    suspects = [f for f in doc_files if f.rel_path in suspect_paths]
    known = {f.rel_path for f in suspects if f.rel_path in known_documents}
    unknown = [f for f in suspects if f.rel_path not in known]
    document_paths = known | {f.rel_path for f in classify_doc_files(unknown).documents}
    # Re-derive rather than filter the groups in place: dropping a source can
    # turn a cross-root group into a within-root one, or leave a group with a
    # single source and no collision at all.
    kept = [f for f in doc_files if f.rel_path not in suspect_paths or f.rel_path in document_paths]
    return DocIdSignals(
        collisions=find_collisions(live_doc_id_index(project_id, kept)),
        dotted=dotted_filenames(kept),
    )
