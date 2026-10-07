"""Directory walking shared by the walkers of one operation.

A build walks the project several times: the Python and JS/TS files from the
project root, every config directory, and every docs root (for the doc-id
namespace gate and for each doc scanner).  :class:`TreeListing` lists each
directory once per operation and hands every walker the same listing, and a
walker given skip names never descends into a directory carrying one.  The
cost of a walk then follows the indexed tree, not whatever else sits on disk
under the root (a checkout's ``.claude/worktrees`` holds whole copies of the
repository).

The walkers return what the ``Path.rglob`` / ``Path.glob`` calls they replace
returned on the running interpreter, in the same order, before their callers'
filters.  ``pathlib``'s walk changed between Python versions (see
``_GLOB_MODEL``), so the walkers follow the version they run on:

* a name pattern matches the way ``pathlib`` matches it -- case-insensitive
  on Windows, case-sensitive elsewhere -- whatever the entry's type; a caller
  that wants files only asks for them;
* a symlinked directory is listed by its parent but never descended into by
  ``**``;
* a directory that cannot be listed contributes nothing;
* an entry whose name is a skip name is neither returned nor descended into.
  A skip name is matched against single path components, so this drops
  exactly the paths the "any component below the root is a skip name" filter
  dropped, without listing them first.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import sys
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath

#: Whether ``pathlib`` matches names case-sensitively on this platform.
_CASE_SENSITIVE = os.path.normcase("Aa") == "Aa"

#: Whether a directory entry can be a reparse point that is not a symlink (a
#: junction): only then is an entry's attribute word worth reading.
_IS_WINDOWS = os.name == "nt"

#: The ``pathlib`` glob implementation the running interpreter has; the
#: walkers reproduce its order and its ``**`` semantics.
#:
#: * ``"selectors"`` (3.10, 3.11): ``**`` selects from the directories in a
#:   depth-first pre-order walk and de-duplicates what it selects; a
#:   component without wildcards keeps the pattern's spelling and is checked
#:   with ``Path.is_dir`` / ``Path.exists`` instead of a listing.
#: * ``"walk"`` (3.12): ``**`` selects from the directories ``Path.walk``
#:   yields -- a directory's children when it is expanded, the first child
#:   expanded first; every component is matched against a listing.
#: * ``"globber"`` (3.13 and later): ``glob``'s globber -- a directory's
#:   children when it is expanded, the last child expanded first; a trailing
#:   ``**`` selects files as well as directories; ``**`` inside a component
#:   acts as ``*``; nothing is de-duplicated; a component without wildcards
#:   keeps the pattern's spelling and is not checked.
if sys.version_info >= (3, 13):
    _GLOB_MODEL = "globber"
elif sys.version_info >= (3, 12):
    _GLOB_MODEL = "walk"
else:
    _GLOB_MODEL = "selectors"

#: Whether a pattern's trailing separator is kept as a final empty component
#: (select directories only); Python 3.10 drops it.
_TRAILING_SEP_KEPT = sys.version_info >= (3, 11)

#: Whether ``**`` descends into what ``DirEntry.is_dir()`` follows to a
#: directory, symlinks excepted (3.10), rather than into what
#: ``DirEntry.is_dir(follow_symlinks=False)`` reports (3.11 and later).
_DESCEND_FOLLOWS = sys.version_info < (3, 11)

#: The characters that make a pattern component a wildcard.
_MAGIC = re.compile(r"[*?[]")


@dataclass(frozen=True)
class WalkEntry:
    """One directory entry as its listing returned it.

    Attributes:
        name: The entry's name.
        is_dir: A directory, not followed through a symlink -- what ``**``
            descends into.
        is_dir_target: A directory or a symlink to one (``DirEntry.is_dir()``)
            -- what a non-final pattern component may select.
        is_file: A file or a symlink to one (``Path.is_file()``).
        is_link: A symlink, or on Windows another reparse point (a junction):
            its resolved path is not its parent's resolved path plus its name.
    """

    name: str
    is_dir: bool
    is_dir_target: bool
    is_file: bool
    is_link: bool


def _flag(probe: Callable[[], bool]) -> bool:
    """Return *probe()*, or ``False`` when it raises ``OSError``."""
    try:
        return bool(probe())
    except OSError:
        return False


def _is_reparse_point(entry: os.DirEntry) -> bool:
    """Whether *entry* is a Windows reparse point (symlink, junction, ...); always ``False`` elsewhere."""
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _entry(entry: os.DirEntry) -> WalkEntry:
    """Record what the walkers need of one ``os.scandir`` entry (no extra system call for a plain entry).

    The reparse-point probe runs on Windows only: there ``DirEntry.stat`` with
    ``follow_symlinks=False`` answers from the listing, while elsewhere it
    would cost an ``lstat`` per entry to learn nothing ``is_symlink`` did not.
    """
    is_dir_target = _flag(entry.is_dir)
    is_symlink = _flag(entry.is_symlink)
    if _DESCEND_FOLLOWS:
        is_dir = is_dir_target and not is_symlink
    else:
        is_dir = _flag(lambda: entry.is_dir(follow_symlinks=False))
    return WalkEntry(
        name=entry.name,
        is_dir=is_dir,
        is_dir_target=is_dir_target,
        is_file=_flag(entry.is_file),
        is_link=is_symlink or (_IS_WINDOWS and _flag(lambda: _is_reparse_point(entry))),
    )


def name_matcher(patterns: Iterable[str]) -> Callable[[str], bool]:
    """Return a predicate matching a file name against any of *patterns* the way ``pathlib`` globs do.

    Args:
        patterns: Single-component glob patterns (``*.py``).

    Returns:
        ``name -> bool``: case-insensitive on Windows, case-sensitive elsewhere.
    """
    flags = 0 if _CASE_SENSITIVE else re.IGNORECASE
    compiled = [re.compile(fnmatch.translate(p), flags).match for p in patterns]
    if len(compiled) == 1:
        (only,) = compiled
        return lambda name: only(name) is not None
    return lambda name: any(m(name) is not None for m in compiled)


def suffix_matcher(suffixes: Iterable[str]) -> Callable[[str], bool]:
    """Return a predicate matching the names ``Path.rglob(f"*{suffix}")`` matches, for any of *suffixes*."""
    return name_matcher([f"*{s}" for s in suffixes])


class TreeListing:
    """Directory listings shared by every walker of one operation.

    Each directory is listed at most once, however many walkers pass through
    it; the listing is a snapshot taken the first time it is asked for.

    Attributes:
        listings: How many directories have been listed (``os.scandir`` calls).
    """

    def __init__(self) -> None:
        """Start with no directory listed."""
        self._listed: dict[str, tuple[WalkEntry, ...]] = {}
        self.listings = 0

    def entries(self, directory: str | os.PathLike) -> tuple[WalkEntry, ...]:
        """Return *directory*'s entries, listing it on the first request only.

        Args:
            directory: The directory.

        Returns:
            Its entries in listing order; empty when it cannot be listed.
        """
        path = os.fspath(directory)
        key = os.path.normcase(path)
        listed = self._listed.get(key)
        if listed is None:
            self.listings += 1
            try:
                with os.scandir(path) as it:
                    listed = tuple(_entry(e) for e in it)
            except OSError:
                listed = ()
            self._listed[key] = listed
        return listed

    def _child_dirs(self, directory: Path, skip_names: Collection[str]) -> list[Path]:
        """Return the directories ``**`` descends into from *directory*, in listing order, skip names dropped."""
        return [directory / e.name for e in self.entries(directory) if e.is_dir and e.name not in skip_names]

    def _rglob_dirs(self, root: Path, skip_names: Collection[str]) -> list[Path]:
        """Return *root* and every directory below it in the order ``**`` selects from them.

        A walk never follows a symlink, and a directory carrying a skip name
        is not visited.  The order is this interpreter's (``_GLOB_MODEL``):

        * ``"selectors"``: depth-first pre-order -- a directory, then each of
          its child directories' subtrees in listing order;
        * ``"walk"`` / ``"globber"``: the root, then for each directory
          expanded its child directories in listing order; the next directory
          expanded is the first (``"walk"``) or last (``"globber"``) child
          not yet expanded, depth first.
        """
        if _GLOB_MODEL == "selectors":
            preorder: list[Path] = []
            pending = [root]
            while pending:
                current = pending.pop()
                preorder.append(current)
                pending.extend(reversed(self._child_dirs(current, skip_names)))
            return preorder
        order = [root]
        stack = [root]
        while stack:
            children = self._child_dirs(stack.pop(), skip_names)
            order.extend(children)
            stack.extend(children if _GLOB_MODEL == "globber" else reversed(children))
        return order

    def hits(
        self,
        root: str | os.PathLike,
        name_filter: Callable[[str], bool] | None = None,
        *,
        skip_names: Collection[str] = (),
        files_only: bool = False,
    ) -> list[tuple[Path, bool]]:
        """Return the entries below *root* that :meth:`matches` returns, each with whether it sits behind a link.

        Args:
            root: The walk root.
            name_filter: Keeps an entry by its name; ``None`` keeps every entry.
            skip_names: Names never returned nor descended into.
            files_only: Keep files (and symlinks to files) only.

        Returns:
            ``(path, linked)`` pairs: *linked* is set when the entry or a
            directory between it and *root* is a link, so its resolved path is
            not *root*'s resolved path plus its relative path.
        """
        root = Path(root)
        out: list[tuple[Path, bool]] = []
        linked_dirs: set[Path] = set()
        for directory in self._rglob_dirs(root, skip_names):
            behind_link = directory in linked_dirs
            for e in self.entries(directory):
                if e.name in skip_names:
                    continue
                if e.is_dir and (behind_link or e.is_link):
                    linked_dirs.add(directory / e.name)
                if files_only and not e.is_file:
                    continue
                if name_filter is None or name_filter(e.name):
                    out.append((directory / e.name, behind_link or e.is_link))
        return out

    def matches(
        self,
        root: str | os.PathLike,
        name_filter: Callable[[str], bool] | None = None,
        *,
        skip_names: Collection[str] = (),
        files_only: bool = False,
    ) -> list[Path]:
        """Return the entries below *root* whose name passes *name_filter*, as ``Path.rglob`` would.

        ``Path(root).rglob(pattern)`` is ``matches(root, name_matcher([pattern]))``
        when *skip_names* is empty: the same paths in the same order, of any
        entry type.  *skip_names* drops every path with a component below
        *root* in it, without listing what it drops.

        Args:
            root: The walk root.
            name_filter: Keeps an entry by its name; ``None`` keeps every entry.
            skip_names: Names never returned nor descended into.
            files_only: Keep files (and symlinks to files) only.

        Returns:
            Absolute paths under *root*.
        """
        return [path for path, _linked in self.hits(root, name_filter, skip_names=skip_names, files_only=files_only)]

    def glob(self, root: str | os.PathLike, pattern: str, *, skip_names: Collection[str] = ()) -> Iterator[Path]:
        """Yield what ``Path(root).glob(pattern)`` yields, minus the paths with a skip-name component below *root*.

        Args:
            root: The directory the pattern is relative to.
            pattern: A relative glob pattern (``src/**/*.ts``).
            skip_names: Names never selected nor descended into.

        Yields:
            Matching paths, in this interpreter's ``pathlib`` order.

        Raises:
            ValueError: For an empty pattern; before Python 3.13, for ``**``
                inside a component as well.
            NotImplementedError: For an absolute pattern.
        """
        if not pattern:
            raise ValueError(f"Unacceptable pattern: {pattern!r}")
        parsed = PurePath(pattern)
        if parsed.drive or parsed.root:
            raise NotImplementedError("Non-relative patterns are unsupported")
        parts = list(parsed.parts)
        if _GLOB_MODEL == "globber" and not parts:
            raise ValueError(f"Unacceptable pattern: {pattern!r}")
        if _TRAILING_SEP_KEPT and pattern[-1] in (os.sep, os.altsep):
            parts.append("")
        if _GLOB_MODEL != "globber":
            for part in parts:
                if part != "**" and "**" in part:
                    raise ValueError("Invalid pattern: '**' can only be an entire path component")
        root = Path(root)
        if not root.is_dir():
            return
        skip = frozenset(skip_names) if skip_names else frozenset()
        if _GLOB_MODEL == "globber":
            yield from self._select_globber(root, tuple(parts), skip, exists=False)
        elif _GLOB_MODEL == "walk":
            yield from self._select(root, tuple(parts), skip)
        else:
            yield from self._select_selectors(root, tuple(parts), skip)

    def _select_selectors(self, directory: Path, parts: tuple[str, ...], skip_names: frozenset[str]) -> Iterator[Path]:
        """One Python 3.10 / 3.11 ``pathlib`` selector: match ``parts[0]`` below *directory*, recurse on the rest."""
        if not parts or not parts[0]:
            yield directory
            return
        pattern, rest = parts[0], parts[1:]
        if pattern == "**":
            yielded: set[Path] = set()
            for start in self._rglob_dirs(directory, skip_names):
                for path in self._select_selectors(start, rest, skip_names):
                    if path not in yielded:
                        yielded.add(path)
                        yield path
            return
        if _MAGIC.search(pattern) is None:
            if pattern in skip_names:
                return
            path = directory / pattern
            if _flag(path.is_dir if rest else path.exists):
                yield from self._select_selectors(path, rest, skip_names)
            return
        match = name_matcher([pattern])
        for e in self.entries(directory):
            if rest and not e.is_dir_target:
                continue
            if e.name in skip_names:
                continue
            if match(e.name):
                yield from self._select_selectors(directory / e.name, rest, skip_names)

    def _select_globber(
        self, path: Path, parts: tuple[str, ...], skip_names: frozenset[str], *, exists: bool
    ) -> Iterator[Path]:
        """One Python 3.13+ ``glob`` globber selector: select ``parts[0]`` below *path*, recurse on the rest.

        *exists* says *path* came from a listing, so a final component needs
        no existence check.
        """
        if not parts:
            if exists or os.path.lexists(path):
                yield path
            return
        part, rest = parts[0], parts[1:]
        if part == "**":
            while rest and rest[0] == "**":
                rest = rest[1:]
            if rest:
                for i, start in enumerate(self._rglob_dirs(path, skip_names)):
                    yield from self._select_globber(start, rest, skip_names, exists=exists or i > 0)
                return
            # A trailing ``**``: the directory itself, then every entry below
            # it, each directory's entries when it is expanded.
            if exists or os.path.lexists(os.path.join(path, "")):
                yield path
            stack = [path]
            while stack:
                current = stack.pop()
                for e in self.entries(current):
                    if e.name in skip_names:
                        continue
                    child = current / e.name
                    yield child
                    if e.is_dir:
                        stack.append(child)
            return
        if part == "":
            if exists or os.path.lexists(os.path.join(path, "")):
                yield path
            return
        if part == "..":
            yield from self._select_globber(path / part, rest, skip_names, exists=exists)
            return
        if _MAGIC.search(part) is None:
            # A literal component swallows the literal components after it.
            names = [part]
            while rest and _MAGIC.search(rest[0]) is None:
                names.append(rest[0])
                rest = rest[1:]
            if any(name in skip_names for name in names):
                return
            target = path.joinpath(*[name for name in names if name])
            if names[-1] == "":
                if os.path.lexists(os.path.join(target, "")):
                    yield target
                return
            yield from self._select_globber(target, rest, skip_names, exists=False)
            return
        match = name_matcher([part])
        for e in self.entries(path):
            if e.name in skip_names or not match(e.name):
                continue
            if not rest:
                yield path / e.name
            elif e.is_dir_target:
                yield from self._select_globber(path / e.name, rest, skip_names, exists=True)

    def _select(self, directory: Path, parts: tuple[str, ...], skip_names: frozenset[str]) -> Iterator[Path]:
        """One Python 3.12 ``pathlib`` selector: match ``parts[0]`` in *directory* and recurse on the rest."""
        if not parts or not parts[0]:
            yield directory
            return
        pattern, rest = parts[0], parts[1:]
        if pattern == "**":
            while rest and rest[0] == "**":
                rest = rest[1:]
            dedupe = "**" in rest
            yielded: set[Path] = set()
            for start in self._rglob_dirs(directory, skip_names):
                for path in self._select(start, rest, skip_names):
                    if dedupe:
                        if path in yielded:
                            continue
                        yielded.add(path)
                    yield path
            return
        if pattern == "..":
            yield from self._select(directory / "..", rest, skip_names)
            return
        dir_only = bool(rest)
        match = name_matcher([pattern])
        for e in self.entries(directory):
            if dir_only and not e.is_dir_target:
                continue
            if e.name in skip_names:
                continue
            if match(e.name):
                yield from self._select(directory / e.name, rest, skip_names)
