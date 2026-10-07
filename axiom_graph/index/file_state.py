"""File-state primitives shared by the scanners and the staleness engine.

Two primitives live here:

* :func:`file_unchanged_since` -- the mtime comparison, kept for the callers
  that still seed a decision from it (an index with no per-file record yet).
* The **discovery walk** -- one read of every file a caller tracks, giving its
  whole-file fingerprint and stat (:func:`observe_files`), compared with the
  per-file record the index keeps (``file_state``).  The staleness passes
  compare a file with the content their last re-hash read
  (:func:`files_to_rehash`); ``build`` compares it with the content it last
  parsed (:func:`files_to_parse`).  Content decides; mtime is never trusted on
  its own, because a file restored from a backup keeps an old mtime and a
  touched file keeps its bytes.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from axiom_graph.models import hash16

logger = logging.getLogger(__name__)

# File extensions whose nodes are hashed from raw decoded bytes (the
# tree-sitter scanner reads ``read_bytes().decode(...)``), NOT from
# universal-newline ``read_text``.  A fingerprint must read these the SAME
# way, so it is comparable to the scanned anchor ``code_hash``.
_JS_TS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx"})

#: ``file_state.hashed_fp`` of a file that was missing at its last re-hash
#: (mirrors :data:`axiom_graph.db._core.MISSING_FILE_FP`).
MISSING_FILE_FP = "!missing"


def file_unchanged_since(
    stored_mtime: float | None,
    current_mtime: float,
    *,
    slop: float = 0.0,
) -> bool:
    """Return whether a file looks unchanged since it was last indexed, by mtime alone.

    ``stored_mtime`` comes from the ``nodes.file_mtime`` column: the file's
    on-disk modification time as observed when a build last scanned its
    bytes.  It decides on its own only where no per-file record exists yet
    (the first build after an upgrade seeds the records from it once);
    everywhere else the discovery walk compares content.

    Args:
        stored_mtime: The file modification time recorded in the index, or
            ``None`` if the file has never been indexed.
        current_mtime: The file's current modification time on disk.
        slop: Tolerance, in seconds, added to ``stored_mtime`` before the
            comparison.  Defaults to ``0.0`` (exact comparison).

    Returns:
        ``False`` when ``stored_mtime`` is ``None``.  Otherwise ``True`` when
        ``current_mtime <= stored_mtime + slop``, else ``False``.
    """
    if stored_mtime is None:
        return False
    return current_mtime <= stored_mtime + slop


def file_fingerprint(abs_path: Path) -> str | None:
    """Return the whole-file hash an anchor's ``code_hash`` and the per-file record compare with.

    The file is read the SAME way its scanner hashes it (read-mode parity is
    load-bearing): JS/TS via ``read_bytes().decode``, everything else via
    universal-newline ``read_text``.

    Args:
        abs_path: Absolute path to the file on disk.

    Returns:
        ``hash16`` of the file's text, or ``None`` when it cannot be read.
    """
    try:
        if abs_path.suffix in _JS_TS_EXTENSIONS:
            text = abs_path.read_bytes().decode("utf-8", errors="replace")
        else:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("fingerprint: failed to read %s: %s", abs_path, exc)
        return None
    return hash16(text)


@dataclass(frozen=True)
class FileObservation:
    """One file as the discovery walk read it.

    Attributes:
        fingerprint: :func:`file_fingerprint` of the file, or ``None`` when it
            is missing or unreadable.
        mtime: Modification time, or ``None`` when missing.
        size: Size in bytes, or ``None`` when missing.
    """

    fingerprint: str | None
    mtime: float | None
    size: int | None

    @property
    def missing(self) -> bool:
        """Whether the file could not be read."""
        return self.fingerprint is None

    def as_record(self) -> tuple[str | None, float | None, int | None]:
        """Return ``(fingerprint, mtime, size)`` as the record writers take it."""
        return self.fingerprint, self.mtime, self.size


def observe_file(project_root: Path, location: str) -> FileObservation:
    """Read one tracked file: its fingerprint and stat.

    Args:
        project_root: Absolute project root.
        location: Project-relative POSIX path.

    Returns:
        The :class:`FileObservation`.
    """
    abs_path = project_root / location
    try:
        st = os.stat(abs_path)
    except OSError:
        return FileObservation(None, None, None)
    fp = file_fingerprint(abs_path)
    return FileObservation(fp, st.st_mtime, st.st_size)


def observe_files(project_root: Path, locations: Iterable[str]) -> dict[str, FileObservation]:
    """The discovery walk: read every tracked file once.

    Args:
        project_root: Absolute project root.
        locations: Project-relative POSIX paths (duplicates read once).

    Returns:
        Location -> :class:`FileObservation`.
    """
    return {loc: observe_file(project_root, loc) for loc in dict.fromkeys(locations)}


def files_to_rehash(
    observations: Mapping[str, FileObservation],
    hashed_fps: Mapping[str, str | None],
    *,
    reset_locations: Collection[str] = (),
) -> set[str]:
    """Return the files a staleness pass must re-hash: content other than its last re-hash read.

    A file goes to the per-node ladder when its fingerprint differs from the
    one the last re-hash stored (none stored counts as different), when it
    went missing or came back, or when it holds a node whose baseline a
    verification reset since (*reset_locations*).  Any other file's nodes
    keep the live hashes that re-hash took, which are exactly what a new
    re-hash of the same bytes would produce.

    Args:
        observations: Location -> what the walk read now.
        hashed_fps: Location -> the fingerprint the last re-hash read
            (``MISSING_FILE_FP`` for a file that was missing), or ``None``.
        reset_locations: Files holding a node with the reset marker.

    Returns:
        The locations to re-hash.
    """
    out: set[str] = set()
    for loc, obs in observations.items():
        stored = hashed_fps.get(loc)
        now = obs.fingerprint if obs.fingerprint is not None else MISSING_FILE_FP
        if stored is None or stored != now or loc in reset_locations:
            out.add(loc)
    return out


def files_to_parse(
    observations: Mapping[str, FileObservation],
    parsed_fps: Mapping[str, str | None],
    stored_mtimes: Mapping[str, float | None],
    *,
    has_record: Collection[str],
    walk_everything: bool = False,
    mtime_moved_parses: bool = False,
) -> set[str]:
    """Return the files ``build`` must parse: content other than the last parse read.

    A file is parsed when its fingerprint differs from the one the last
    build parsed; when the index stores no scan mtime for it (a new file,
    or one a migration or a failed per-file pass cleared); when it has no
    record yet and the mtime rule says it changed (the seeding case, so an
    upgraded index parses exactly what the build before it would have);
    or when the walk-everything switch is on.  With *mtime_moved_parses*
    (``build``) a file whose mtime moved past its scan mtime is parsed too,
    so touching or re-saving a file still forces its re-scan; without it a
    file whose bytes match the last parse is never parsed, whatever its mtime.

    Args:
        observations: Location -> what the walk read now (missing files are skipped).
        parsed_fps: Location -> the fingerprint the last build parsed, or ``None``.
        stored_mtimes: Location -> the index's scan mtime (``nodes.file_mtime``).
        has_record: Locations that have a per-file record.
        walk_everything: Treat every file as changed (the full walk; unexposed).
        mtime_moved_parses: Also parse a file whose mtime moved past its scan mtime.

    Returns:
        The locations to parse.
    """
    out: set[str] = set()
    for loc, obs in observations.items():
        if obs.missing:
            continue
        stored_mtime = stored_mtimes.get(loc)
        mtime_moved = not file_unchanged_since(stored_mtime, obs.mtime or 0.0)
        if walk_everything or stored_mtime is None:
            out.add(loc)
        elif loc not in has_record or mtime_moved_parses:
            if mtime_moved or (loc in has_record and parsed_fps.get(loc) != obs.fingerprint):
                out.add(loc)
        elif parsed_fps.get(loc) != obs.fingerprint:
            out.add(loc)
    return out


class BuildDiscovery:
    """The build's discovery walk: one read per walked file, and the parse decision for it.

    Each scanner asks :meth:`should_parse` for every file it walks.  The file
    is read once (fingerprint and stat, taken before any parse, so a recorded
    fingerprint is never newer than the bytes indexed) and judged by
    :func:`files_to_parse` with the mtime trigger on: content other than the
    last parse, no scan mtime, or an mtime that moved past the scan mtime.
    A file with no parse record yet is judged by the mtime rule alone (the
    upgrade seed).  What was read is kept for the per-file records.

    Attributes:
        observed: Location -> what the walk read, for every file asked about.
        parsed: The locations judged to need a parse.
    """

    def __init__(
        self,
        project_root: Path,
        parsed_fps: Mapping[str, str | None],
        stored_mtimes: Mapping[str, float | None],
        *,
        parse_everything: bool = False,
    ) -> None:
        """Set up the walk.

        Args:
            project_root: Absolute project root.
            parsed_fps: Location -> the fingerprint the last build parsed
                (locations with none are left out).
            stored_mtimes: Location -> the index's scan mtime.
            parse_everything: Parse every walked file (``init``, a full rescan).
        """
        self._root = Path(project_root)
        self._parsed_fps = dict(parsed_fps)
        self._stored_mtimes = stored_mtimes
        self._parse_everything = parse_everything
        self.observed: dict[str, FileObservation] = {}
        self.parsed: set[str] = set()

    def location(self, abs_path: Path) -> str:
        """Return *abs_path* as a project-relative POSIX location."""
        return Path(abs_path).relative_to(self._root).as_posix()

    def should_parse(self, abs_path: Path) -> bool:
        """Read one walked file and say whether the build parses it.

        Args:
            abs_path: Absolute path of a file a scanner walked.

        Returns:
            ``True`` to parse the file, ``False`` to keep its index entries.
        """
        loc = self.location(abs_path)
        obs = self.observed.get(loc)
        if obs is None:
            obs = observe_file(self._root, loc)
            self.observed[loc] = obs
        parse = self._parse_everything or bool(
            files_to_parse(
                {loc: obs},
                self._parsed_fps,
                self._stored_mtimes,
                has_record=self._parsed_fps.keys(),
                mtime_moved_parses=True,
            )
        )
        if obs.missing:
            parse = True
        if parse:
            self.parsed.add(loc)
        return parse

    def parse_records(self, locations: Iterable[str]) -> dict[str, tuple[str | None, float | None, int | None]]:
        """Return ``(fingerprint, mtime, size)`` of the parsed *locations* the walk read.

        Args:
            locations: Files whose parse completed.

        Returns:
            Location -> record, for the record writer.
        """
        return {
            loc: self.observed[loc].as_record()
            for loc in locations
            if loc in self.observed and not self.observed[loc].missing
        }

    def seed_records(self) -> dict[str, tuple[str | None, float | None, int | None]]:
        """Return the record of every walked file left unparsed that has no parse record yet (the upgrade seed).

        Returns:
            Location -> ``(fingerprint, mtime, size)``.
        """
        return {
            loc: obs.as_record()
            for loc, obs in self.observed.items()
            if loc not in self.parsed and loc not in self._parsed_fps and not obs.missing
        }


def stat_behind(
    project_root: Path,
    records: Mapping[str, tuple[float | None, int | None]],
) -> int:
    """Count the tracked files whose stat moved since the index last observed them (the ``"off"`` probe).

    One stat per file, no read.  A file with no stored stat counts as behind.

    Args:
        project_root: Absolute project root.
        records: Location -> the ``(mtime, size)`` stored at the last observation.

    Returns:
        The number of files that may have changed.
    """
    behind = 0
    for loc, (mtime, size) in records.items():
        try:
            st = os.stat(project_root / loc)
        except OSError:
            if mtime is not None:
                behind += 1
            continue
        if mtime is None or st.st_mtime != mtime or st.st_size != size:
            behind += 1
    return behind
