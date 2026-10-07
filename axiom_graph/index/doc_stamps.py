"""DocJSON tool-write stamps and raw DocJSON edit detection.

Every DocJSON write tool records, on each section it creates or edits, a
small stamp object under :data:`STAMP_KEY`:

    ``{"hash": <sha256 of the section's heading, content and links>,
       "verified_against": {<code node id>: <its code hash>, ...}}``

``hash`` proves the section's text is exactly what a tool wrote.
``verified_against`` mirrors the section's receipts: for every code
(non-DocJSON) node the section links to, the hash the section's links were
last verified against.  A write gives each linked code node its current
hash, except an offender the write left open, which keeps the previous
stamp's entry (or none), so the stamp never claims a verification the
write did not make.
The stamp travels with the file through ``git merge`` / ``pull``, which
the index's verification rows do not, so a build on another checkout can
tell "written by a tool" from "edited by hand", and, for a tool write,
which linked code is still at the version the writer verified it against.

A build takes a tool-written section one dimension at a time.  Its text is
verified, because the stamp hash proves a tool wrote exactly this text.  Its
links get a receipt each only where ``verified_against`` records the linked
code node's current hash; every other link keeps what this index had, and
a verification row the build has to create holds open those links that
are open offenders (it never settles them by its own time): every linked
code node the stamp does not vouch for, and any other link with a change
that counts.  So a section the writer left
LINKED_STALE through code nobody re-checked arrives LINKED_STALE through
that code, and only that code.

The stamp never feeds a section's ``level_2`` or its hashes -- the
scanner reads only ``heading`` / ``content`` / ``links`` -- so writing a
stamp never makes a section CONTENT_UPDATED.

A tool-written section is taken this way when its text is new to the
index **or** when it is stored own-drifted (CONTENT_UPDATED /
DESC_UPDATED): an index that took in the text without adopting it (an
earlier build) adopts it later, even though the file's mtime has not
moved since.  A hand edit fails its stamp and stays a raw edit; a doc
envelope is never adopted.

:func:`classify_section` is the single rule; :func:`reconcile_sections`
applies it.  The build, the write path (for sections a write did not
touch) and the single-file rescan all call :func:`reconcile_sections`.
:func:`adopt_drifted_stamps` is the sweep ``build`` (for the files it did
not parse) and ``check`` run before their staleness pass: it reads each
file holding an own-drifted section once and reconciles the valid-stamped
ones.  The read tools' refresh does not sweep.

This is a primitive module (index layer): it reads and writes index rows
and is called by the builder and by ``axiom_graph.docjson.api``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from axiom_annotations import Step, task

from axiom_graph.index import db
from axiom_graph.index.status import CONTENT_UPDATED, DESC_UPDATED
from axiom_graph.models import hash16

if TYPE_CHECKING:
    from axiom_graph.models import AxiomNode

logger = logging.getLogger(__name__)

#: Section key holding the tool-write stamp.
STAMP_KEY = "axiom_stamp"

#: History change_type recorded once per raw DocJSON edit (per section and
#: section hash).  Never a staleness input and never a diff baseline.
RAW_DOCJSON_EDIT = "RAW_DOCJSON_EDIT"

#: ``[axiom_graph.docjson] raw_docjson_edits`` values.
RAW_EDITS_WARN = "warn"
RAW_EDITS_OFF = "off"

#: Stamp states.
STAMP_VALID = "valid"
STAMP_MISMATCHED = "mismatched"
STAMP_MISSING = "missing"

#: Verdicts of :func:`classify_section`.
VERDICT_QUIET = "quiet"
VERDICT_TOOL_WRITE = "tool_write"
#: A tool write whose stamp vouches for some of the section's linked code
#: only (the rest moved since the write, or the writer left it open).
VERDICT_TOOL_WRITE_PARTIAL = "tool_write_partial"
VERDICT_RAW_EDIT = "raw_edit"

_DOCJSON_SUBTYPES = frozenset({"docjson", "docjson_doc", "docjson_section"})

#: Stored own statuses that mark a section as own-drifted (its text is not
#: what its last verification saw).
_OWN_DRIFT_STATUSES = frozenset({CONTENT_UPDATED, DESC_UPDATED})


# ---------------------------------------------------------------------------
# Canonical hash + stamp construction
# ---------------------------------------------------------------------------


def section_stamp_hash(section: dict) -> str:
    """Return the canonical hash of a section's own heading, content and links.

    Child sections are excluded: they carry their own stamps.  The canonical
    form is compact, key-sorted JSON of ``{"content", "heading", "links"}``
    (missing values as ``""`` / ``[]``), so the writer and the build compute
    the same value.

    Args:
        section: A DocJSON section dict.

    Returns:
        Lower-case sha256 hex digest.
    """
    payload = {
        "heading": section.get("heading") or "",
        "content": section.get("content") or "",
        "links": section.get("links") or [],
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamp_state(section: dict | None) -> str:
    """Return whether *section* carries a stamp matching its current text.

    Args:
        section: A DocJSON section dict, or ``None``.

    Returns:
        :data:`STAMP_VALID`, :data:`STAMP_MISMATCHED` or :data:`STAMP_MISSING`.
    """
    stamp = section.get(STAMP_KEY) if isinstance(section, dict) else None
    if not isinstance(stamp, dict) or not isinstance(stamp.get("hash"), str):
        return STAMP_MISSING
    return STAMP_VALID if stamp["hash"] == section_stamp_hash(section) else STAMP_MISMATCHED


def linked_node_ids(section: dict) -> list[str]:
    """Return the node ids a section's ``links`` array names, in order.

    An entry with no usable id (a hand edit such as ``{"node_id": 123}``) is
    skipped, as the build skips it.
    """
    out: list[str] = []
    links = section.get("links")
    for link in links if isinstance(links, list) else []:
        nid = link.get("node_id") if isinstance(link, dict) else link
        nid = nid.strip() if isinstance(nid, str) else ""
        if nid and nid not in out:
            out.append(nid)
    return out


def current_code_hashes(db_path: Path, root: Path, node_ids: list[str]) -> dict[str, str | None]:
    """Return the current code hash of every indexed code node among *node_ids*.

    DocJSON targets (sections, docs) and ids the index does not hold are
    left out: ``verified_against`` covers the code a section documents.
    Hashes are computed from the file on disk the way ``mark_node_clean``
    computes them, falling back to the stored hash where the file or the
    node cannot be read.  A module node (Python or JS/TS) is hashed over its
    whole file text, as the scanners set its ``code_hash``: its stored hash
    is not kept current by a long-lived index, and a stamp recording it
    would read stale in any freshly built index (a clone, CI).  One query
    loads the nodes and each file is parsed
    once, however many of its nodes are asked for, so a caller batches every
    section it stamps or classifies into one call.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.
        node_ids: Candidate target ids.

    Returns:
        ``{node_id: code_hash}`` for the code targets, in *node_ids* order.
    """
    from axiom_graph.scanners.node_hashing import current_node_hashes_for_file  # noqa: PLC0415

    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return {}
    with db._connect(db_path) as conn:
        nodes = db.get_nodes_conn(conn, ids)
    by_location: dict[str, list] = {}
    for nid in ids:
        node = nodes.get(nid)
        if node is None or (node.subtype or "") in _DOCJSON_SUBTYPES:
            continue
        by_location.setdefault(node.location or "", []).append(node)
    found: dict[str, str | None] = {}
    for location, group in by_location.items():
        current: dict[str, tuple[str | None, str | None]] = {}
        if location:
            try:
                current = current_node_hashes_for_file(root / location, group, root)
            except Exception:  # pragma: no cover -- unreadable target file
                logger.debug("doc stamps: could not hash %s", location, exc_info=True)
        module_hash: str | None = None
        if location and any(node.id not in current and node.subtype == "module" for node in group):
            try:
                module_hash = hash16((root / location).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                logger.debug("doc stamps: could not read module %s", location, exc_info=True)
        for node in group:
            hit = current.get(node.id)
            if hit is not None:
                found[node.id] = hit[0]
            elif node.subtype == "module" and module_hash is not None:
                found[node.id] = module_hash
            else:
                found[node.id] = node.code_hash
    return {nid: found[nid] for nid in ids if nid in found}


def make_stamp(section: dict, verified_against: dict[str, str | None]) -> dict:
    """Build the stamp object for *section* as it stands now."""
    return {"hash": section_stamp_hash(section), "verified_against": dict(sorted(verified_against.items()))}


def flatten_section_dicts(sections: list, prefix: str | None = None) -> dict[str, dict]:
    """Flatten a DocJSON sections tree into ``{dot_path: section_dict}``.

    Args:
        sections: A DocJSON ``sections`` list (possibly nested).
        prefix: Dot-path of the parent (``None`` at the top level).

    Returns:
        Every section dict keyed by its dot-path.  Malformed entries are
        skipped.  When two sections spell one dot-path the first in document
        order keeps it and the later one is skipped with its subsections,
        as the scanner indexes them.
    """
    out: dict[str, dict] = {}

    def _walk(secs: list, parent: str | None) -> None:
        for sec in secs or []:
            if not isinstance(sec, dict) or not isinstance(sec.get("id"), str):
                continue
            dot = f"{parent}.{sec['id']}" if parent else sec["id"]
            if dot in out:
                continue
            out[dot] = sec
            _walk(sec.get("sections") or [], dot)

    _walk(sections, prefix)
    return out


def load_section_dicts(json_file: Path) -> dict[str, dict]:
    """Read a DocJSON file and return its sections keyed by dot-path (``{}`` if unreadable)."""
    try:
        data = json.loads(json_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return flatten_section_dicts(data.get("sections") or [])


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StampVerdict:
    """What :func:`classify_section` decided for one section.

    Attributes:
        verdict: :data:`VERDICT_QUIET`, :data:`VERDICT_TOOL_WRITE`,
            :data:`VERDICT_TOOL_WRITE_PARTIAL` or :data:`VERDICT_RAW_EDIT`.
        vouched: For a tool write, the linked code nodes whose
            ``verified_against`` entry equals their current hash: the links
            whose receipts the section takes.  Empty otherwise.
    """

    verdict: str
    vouched: frozenset[str] = frozenset()


def classify_section(
    section: dict,
    *,
    text_changed: bool,
    legacy_gate: bool,
    current_hashes: dict[str, str | None],
    own_drifted: bool = False,
) -> StampVerdict:
    """Decide what an indexing event should do with one DocJSON section.

    The single rule every indexing path applies:

    - **valid stamp, text unchanged and the section not own-drifted** --
      nothing to do.
    - **valid stamp, heading/content new or changed, or the section
      own-drifted** (stored CONTENT_UPDATED / DESC_UPDATED: its text is not
      the text its last verification saw, e.g. an earlier build indexed a
      merged tool write without adopting it) -- a tool wrote it (it arrived
      by merge, pull or the same DB), so its text is verified.
      Its links are taken one at a time: a linked code node whose
      ``verified_against`` entry equals its current hash is vouched for (the
      writer verified it at this version).  When every linked code node is
      vouched for, the verdict is :data:`VERDICT_TOOL_WRITE` (a full
      verification); otherwise :data:`VERDICT_TOOL_WRITE_PARTIAL`: the text
      and the vouched links only, every other link keeping what the index
      had.  Links to DocJSON nodes have no ``verified_against`` entry and
      are not counted either way.
    - **stamp present but not matching** -- a raw DocJSON edit.
    - **no stamp** -- a raw DocJSON edit when the text is new or changed at
      this indexing event (and *legacy_gate* is on); an untouched legacy
      section is left alone (own drift does not change that).

    Args:
        section: The section dict from the file.
        text_changed: Whether the section's heading or content is new to the
            index, or differs from the indexed text, at this event.
        legacy_gate: Whether an unstamped new/changed section counts as a
            raw edit (off when the index was empty before this build).
        current_hashes: Current code hashes of the section's linked code
            nodes (see :func:`current_code_hashes`).
        own_drifted: Whether the section's stored own status is
            CONTENT_UPDATED or DESC_UPDATED.  Read only for a valid stamp.

    Returns:
        The :class:`StampVerdict`.
    """
    state = stamp_state(section)
    if state == STAMP_MISMATCHED:
        return StampVerdict(VERDICT_RAW_EDIT)
    if state == STAMP_MISSING:
        return StampVerdict(VERDICT_RAW_EDIT if (text_changed and legacy_gate) else VERDICT_QUIET)
    if not (text_changed or own_drifted):
        return StampVerdict(VERDICT_QUIET)
    recorded = section[STAMP_KEY].get("verified_against") or {}
    if not isinstance(recorded, dict):
        recorded = {}
    vouched = frozenset(nid for nid, current in current_hashes.items() if nid in recorded and recorded[nid] == current)
    full = len(vouched) == len(current_hashes)
    return StampVerdict(VERDICT_TOOL_WRITE if full else VERDICT_TOOL_WRITE_PARTIAL, vouched)


# ---------------------------------------------------------------------------
# Applying the classifier
# ---------------------------------------------------------------------------


@dataclass
class SectionInput:
    """One indexed section handed to :func:`reconcile_sections`.

    Attributes:
        node: The section node the scan produced.
        section: The section dict from the file.
        text_changed: Heading/content new to the index or differing from the
            indexed text at this event.
        is_new: The section had no index row before this event.
        own_drifted: The section's stored own status is CONTENT_UPDATED or
            DESC_UPDATED.  ``None`` (the default) lets
            :func:`reconcile_sections` read it, in one batched query, for the
            items where it decides the verdict (a valid stamp on unchanged
            text).
    """

    node: "AxiomNode"
    section: dict
    text_changed: bool
    is_new: bool
    own_drifted: bool | None = None


@dataclass
class ReconcileResult:
    """Outcome of :func:`reconcile_sections`.

    Attributes:
        verified: Section ids verified in full as tool writes
            (``agent:tool-stamp``): text and every link.
        text_verified: Tool-written sections whose stamp vouches for only
            some of their linked code: the text is verified and those links
            take receipts; every other link keeps what the index had (an
            open offender is held open on a verification row this event
            created).
        raw_edits: Sections recorded as raw DocJSON edits **at this event**
            (a raw edit already recorded with the same section hash is not
            repeated).
    """

    verified: list[str] = field(default_factory=list)
    text_verified: list[str] = field(default_factory=list)
    raw_edits: list[str] = field(default_factory=list)


def raw_edit_mode(root: Path) -> str:
    """Return the project's ``[axiom_graph.docjson] raw_docjson_edits`` setting."""
    from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

    try:
        return AxiomGraphConfig.load(root).docjson.raw_docjson_edits
    except Exception:  # pragma: no cover -- unreadable config
        return RAW_EDITS_WARN


def _raw_edit_recorded(conn, node_id: str, section_hash: str) -> bool:
    rows = conn.execute(
        "SELECT meta FROM node_history WHERE node_id = ? AND change_type = ?", (node_id, RAW_DOCJSON_EDIT)
    ).fetchall()
    for r in rows:
        try:
            if json.loads(r["meta"] or "{}").get("section_hash") == section_hash:
                return True
        except ValueError:
            continue
    return False


def reconcile_sections(
    db_path: Path,
    root: Path,
    items: list[SectionInput],
    *,
    file_path: str,
    legacy_gate: bool = True,
    mode: str | None = None,
    git_sha: str | None = None,
) -> ReconcileResult:
    """Classify indexed sections and apply the verdicts.

    An item whose ``own_drifted`` is ``None`` and whose verdict depends on
    it (valid stamp, unchanged text) has its stored own status read, in one
    batched query for all such items.

    - A full tool write (:data:`VERDICT_TOOL_WRITE`) is verified with
      provenance ``agent:tool-stamp`` / op ``tool_stamp``: a new section
      through the baseline helper (its row is fresh), a changed one through
      ``mark_node_clean`` (which resets its baseline the way the write path
      would).
    - A partial one (:data:`VERDICT_TOOL_WRITE_PARTIAL`) gets a text
      verification (:func:`axiom_graph.index.mark_clean.verify_text_conn`,
      same provenance) and a receipt for each vouched link at the linked
      node's current version.  An existing verification row keeps its
      ``verified_at`` and every other receipt.  A row it has to create pins
      open the open offenders it gives no receipt: every linked code node
      the stamp does not vouch for (the writer verified another version, or
      left it open), and every link the stamp holds no entry for (a doc
      envelope) that has a change the new row's time would otherwise
      settle.  All partial writes share one connection.
    - A raw DocJSON edit is never verified.  Under ``mode="warn"`` it gets
      one preserved ``RAW_DOCJSON_EDIT`` history row per (section, section
      hash) and is reported; under ``"off"`` nothing is recorded.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.
        items: The sections this event indexed and should check.
        file_path: Repo-relative DocJSON path, recorded on history rows.
        legacy_gate: See :func:`classify_section`.
        mode: ``"warn"`` / ``"off"``; read from config when ``None``.
        git_sha: HEAD sha recorded on history rows, when known.

    Returns:
        :class:`ReconcileResult`.
    """
    from axiom_graph.index.mark_clean import (  # noqa: PLC0415
        VERIFICATION_OP_TOOL_STAMP,
        VERIFIED_BY_TOOL_STAMP,
        PairRecorder,
        mark_node_clean,
        verify_text_conn,
    )

    result = ReconcileResult()
    if not items:
        return result
    recorder = PairRecorder(root)
    mode = mode or raw_edit_mode(root)
    new_verified: list[str] = []
    partial: list[tuple[SectionInput, frozenset[str], frozenset[str]]] = []
    valid = [stamp_state(item.section) == STAMP_VALID for item in items]
    # Own drift decides the verdict only for a valid stamp on unchanged text;
    # one batched read for every such item the caller did not judge.
    unjudged = [
        item.node.id
        for item, ok in zip(items, valid)
        if ok and not item.text_changed and not item.is_new and item.own_drifted is None
    ]
    stored: dict[str, tuple[str, str]] = {}
    if unjudged:
        with db._connect(db_path) as conn:
            stored = db.get_staleness_for_conn(conn, unjudged)
    drifted = [
        bool(item.own_drifted)
        if item.own_drifted is not None
        else stored.get(item.node.id, ("", ""))[0] in _OWN_DRIFT_STATUSES
        for item in items
    ]
    # Linked-code hashes only matter for a section with a valid stamp whose
    # text changed or drifted; unstamped and mismatched sections never read
    # them.  One batched lookup for every such section.
    item_targets = [
        linked_node_ids(item.section) if ok and (item.text_changed or drift) else []
        for item, ok, drift in zip(items, valid, drifted)
    ]
    wanted = [nid for targets in item_targets for nid in targets]
    all_hashes = current_code_hashes(db_path, root, wanted) if wanted else {}
    for item, targets, drift in zip(items, item_targets, drifted):
        hashes = {nid: all_hashes[nid] for nid in targets if nid in all_hashes}
        decided = classify_section(
            item.section,
            text_changed=item.text_changed,
            legacy_gate=legacy_gate,
            current_hashes=hashes,
            own_drifted=drift,
        )
        verdict = decided.verdict
        nid = item.node.id
        if verdict == VERDICT_TOOL_WRITE:
            if item.is_new:
                new_verified.append(nid)
            else:
                mark_node_clean(
                    db_path,
                    root,
                    item.node,
                    reason="tool-write stamp matches the linked code",
                    verified_by=VERIFIED_BY_TOOL_STAMP,
                    verification_op=VERIFICATION_OP_TOOL_STAMP,
                    pairs=recorder,
                )
                result.verified.append(nid)
        elif verdict == VERDICT_TOOL_WRITE_PARTIAL:
            partial.append((item, decided.vouched, frozenset(hashes)))
        elif verdict == VERDICT_RAW_EDIT and mode != RAW_EDITS_OFF:
            section_hash = section_stamp_hash(item.section)
            with db._connect(db_path) as conn:
                if _raw_edit_recorded(conn, nid, section_hash):
                    continue
                conn.execute(
                    "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (
                        nid,
                        db._now_utc(),
                        RAW_DOCJSON_EDIT,
                        git_sha,
                        json.dumps(
                            {
                                "file": file_path,
                                "section_hash": section_hash,
                                "stamp": stamp_state(item.section),
                                "reason": "edited outside the doc tools",
                            }
                        ),
                    ),
                )
            result.raw_edits.append(nid)
    if new_verified:
        with db._connect(db_path) as conn:
            result.verified.extend(
                db.write_baseline_verifications_conn(
                    conn,
                    new_verified,
                    verified_by=VERIFIED_BY_TOOL_STAMP,
                    verification_op=VERIFICATION_OP_TOOL_STAMP,
                    reason="tool-write stamp matches the linked code",
                    git_sha=git_sha,
                    pairs_for=recorder.pairs_for,
                )
            )
    if partial:
        with db._connect(db_path) as conn:
            recorder.prepare(conn, [item.node for item, _vouched, _covered in partial])
            deps = {item.node.id: recorder.dependency_ids(conn, item.node.id) for item, _vouched, _covered in partial}
            # Links the stamp has no entry for (a doc envelope): open on a
            # section with no row exactly when they have a change that counts
            # (Pass 1's rule for a section never verified).  One batched read.
            # No ``realigned_now``: the staleness pass that finds nodes back at
            # their baseline runs after this, so a doc envelope this same
            # merge reverted is still pinned.  The over-pin is conservative
            # (the link stays stale until verified) and narrow.
            unvouchable = sorted({t for item, _v, covered in partial for t in deps[item.node.id] - covered})
            changed = db.effective_change_rows_conn(conn, unvouchable) if unvouchable else {}
            for item, vouched, covered in partial:
                nid = item.node.id
                current = recorder.pairs_for(conn, nid) or {}
                open_targets = {t for t in deps[nid] if (t in covered and t not in vouched) or t in changed}
                verify_text_conn(
                    conn,
                    recorder,
                    item.node,
                    reason="tool-write stamp: text verified, and the links whose code it was verified against",
                    verified_by=VERIFIED_BY_TOOL_STAMP,
                    verification_op=VERIFICATION_OP_TOOL_STAMP,
                    open_targets=open_targets,
                    receipts={t: current[t] for t in sorted(vouched) if t in current},
                )
                result.text_verified.append(nid)
    return result


def stored_section_texts(conn, node_ids: list[str]) -> dict[str, tuple[str | None, str]]:
    """Return ``{id: (level_1, level_2)}`` for the given ids that have a row (call before upserting)."""
    out: dict[str, tuple[str | None, str]] = {}
    ids = list(dict.fromkeys(node_ids))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            f"SELECT id, level_1, level_2 FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk
        ).fetchall()
        for r in rows:
            out[r["id"]] = (r["level_1"], r["level_2"] or "")
    return out


def section_inputs(
    nodes: list["AxiomNode"],
    sections_by_dot: dict[str, dict],
    doc_id: str,
    stored: dict[str, tuple[str | None, str]],
    *,
    unchanged_ids: set[str] | frozenset[str] = frozenset(),
) -> list[SectionInput]:
    """Pair scanned section nodes with their file dicts and pre-index text.

    Args:
        nodes: Scanned nodes (non-section nodes are ignored).
        sections_by_dot: The file's sections keyed by dot-path.
        doc_id: The doc envelope id the sections belong to.
        stored: Pre-upsert ``{id: (level_1, level_2)}`` (see
            :func:`stored_section_texts`).
        unchanged_ids: Ids to treat as text-unchanged regardless of
            ``stored`` (e.g. sections whose rows were just moved by a rename).

    Returns:
        One :class:`SectionInput` per scanned section of *doc_id*.
    """
    prefix = f"{doc_id}::"
    out: list[SectionInput] = []
    for n in nodes:
        if getattr(n, "subtype", None) != "docjson_section" or not n.id.startswith(prefix):
            continue
        sec = sections_by_dot.get(n.id[len(prefix) :])
        if sec is None:
            continue
        before = stored.get(n.id)
        is_new = before is None and n.id not in unchanged_ids
        if n.id in unchanged_ids:
            changed = False
        else:
            changed = before is None or before != (n.level_1, n.level_2 or "")
        out.append(SectionInput(node=n, section=sec, text_changed=changed, is_new=is_new))
    return out


@task(
    purpose="Adopt the tool-write stamps of DocJSON sections stored own-drifted: a section whose valid stamp proves a "
    "tool wrote its current text is verified the way a merged tool write is, whatever its file's mtime",
    inputs="db_path, project root, files to skip (the ones this operation already reconciled), git sha",
    outputs="ReconcileResult: sections verified in full and sections whose text (and vouched links) were verified",
    critical="Only valid stamps are taken (a hand edit stays a raw edit and is never verified); envelopes are never "
    "adopted; each holding file is parsed once and the drifted rows are read in one query",
)
def adopt_drifted_stamps(
    db_path: Path,
    root: Path,
    *,
    skip_locations: set[str] | frozenset[str] = frozenset(),
    git_sha: str | None = None,
) -> ReconcileResult:
    """Verify the own-drifted DocJSON sections whose stamp proves a tool wrote their text.

    A section indexed without being adopted (for example by a build that
    predates adoption on drift, or one whose stamp check failed) stays
    CONTENT_UPDATED while its file's mtime stands still, so no rescan sees
    it again.  This sweep finds the sections stored own-drifted
    (CONTENT_UPDATED / DESC_UPDATED), reads each holding file once, keeps
    the sections whose stamp is valid and hands them to
    :func:`reconcile_sections` as own-drifted, which applies the usual
    verdicts: a full tool write or a text-plus-vouched-links one.  Sections
    with a missing or mismatched stamp are left untouched (a raw edit is
    recorded by the scan that indexed it, never here).  ``build`` and
    ``check`` run it before their staleness pass; the read tools do not.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.
        skip_locations: Repo-relative DocJSON paths the caller already
            reconciled at this event (a build's scanned files).
        git_sha: HEAD sha recorded on history rows, when known.

    Returns:
        :class:`ReconcileResult` (``raw_edits`` is always empty).
    """
    result = ReconcileResult()
    口 = Step(
        step_num=1,
        name="Find the drifted sections",
        purpose="One indexed query for the DocJSON sections stored CONTENT_UPDATED / DESC_UPDATED, grouped by file, "
        "and one batched read of their nodes",
    )
    with db._connect(db_path) as conn:
        by_location = {
            loc: ids for loc, ids in db.get_own_drifted_sections_conn(conn).items() if loc not in skip_locations
        }
        if not by_location:
            return result
        nodes = db.get_nodes_conn(conn, [nid for ids in by_location.values() for nid in ids])

    口 = Step(
        step_num=2,
        name="Adopt file by file",
        purpose="Each holding file is read once and its valid-stamped drifted sections reconciled together",
    )
    for location, ids in by_location.items():
        口 = Step(
            step_num=2.1,
            name="Adopt one file's valid stamps",
            purpose="Parse the file once, keep the drifted sections whose stamp is valid, and reconcile them as "
            "own-drifted",
        )
        sections = load_section_dicts(root / location)
        items: list[SectionInput] = []
        for nid in ids:
            node = nodes.get(nid)
            sec = sections.get(db.split_section_id(nid)[1])
            if node is None or sec is None or stamp_state(sec) != STAMP_VALID:
                continue
            items.append(SectionInput(node=node, section=sec, text_changed=False, is_new=False, own_drifted=True))
        if not items:
            continue
        # Valid stamps only, so no raw-edit verdict can arise: the mode is
        # passed rather than read from the config once per file.
        res = reconcile_sections(db_path, root, items, file_path=location, mode=RAW_EDITS_WARN, git_sha=git_sha)
        result.verified.extend(res.verified)
        result.text_verified.extend(res.text_verified)
    logger.debug(
        "doc stamps: adopted %d drifted section(s) (%d in full, %d text only) from %d file(s)",
        len(result.verified) + len(result.text_verified),
        len(result.verified),
        len(result.text_verified),
        len(by_location),
    )
    return result


# ---------------------------------------------------------------------------
# Listing + reporting
# ---------------------------------------------------------------------------


def list_raw_docjson_edits(db_path: Path, root: Path) -> list[str]:
    """Return the sections currently flagged as raw DocJSON edits.

    A section is flagged when it has a ``RAW_DOCJSON_EDIT`` history row and
    its stamp in the file is still missing or mismatched -- re-applying the
    edit with a doc tool or accepting it stamps the section and clears it.
    Empty under ``raw_docjson_edits = "off"``.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root.

    Returns:
        Flagged section ids, sorted.
    """
    if raw_edit_mode(root) == RAW_EDITS_OFF:
        return []
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT h.node_id, n.location FROM node_history h JOIN nodes n ON n.id = h.node_id "
            "WHERE h.change_type = ?",
            (RAW_DOCJSON_EDIT,),
        ).fetchall()
    by_file: dict[str, list[str]] = {}
    for r in rows:
        by_file.setdefault(r["location"] or "", []).append(r["node_id"])
    flagged: list[str] = []
    for location, ids in by_file.items():
        sections = load_section_dicts(root / location) if location else {}
        for nid in ids:
            parts = nid.split("::")
            if len(parts) != 3:
                continue
            sec = sections.get(parts[2])
            if sec is not None and stamp_state(sec) != STAMP_VALID:
                flagged.append(nid)
    return sorted(flagged)


def raw_docjson_edit_summary(ids: list[str]) -> str:
    """The summary a build or write reports for newly found raw DocJSON edits.

    Names the count, both fixes, the list mode and the switch that silences
    it, in two lines.

    Args:
        ids: Sections recorded as raw DocJSON edits at this event.

    Returns:
        The summary text, or ``""`` when *ids* is empty.
    """
    if not ids:
        return ""
    return (
        f"{len(ids)} DocJSON section(s) were edited outside the doc tools (raw DocJSON edits) and are not verified. "
        "Fix: re-apply the change with update_section / patch_section / add_section, or accept the "
        "current text with axiom_graph_accept_doc_edits (CLI: axiom-graph stamps accept <ids>|--all).\n"
        "See which sections with axiom_graph_accept_doc_edits(dry_run=True) or axiom-graph stamps accept --list; "
        'set [axiom_graph.docjson] raw_docjson_edits = "off" to silence this warning.'
    )
