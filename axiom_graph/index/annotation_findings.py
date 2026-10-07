"""Annotation findings shared by ``build`` and ``check``.

The build stores each scanned file's raw annotation findings and AutoStep
records (:mod:`axiom_graph.db.findings`); ``check`` reads them and rescans
in memory only the files edited since the last build.  This module holds
the logic both commands share, so they report the same thing:

- **Merge.** The current per-file results are the stored rows of every
  walked file, with the files rescanned this run replaced by their rescans.
- **B4 at read time.** Whether an AutoStep's target is decorated depends on
  another file, so B4 is resolved when read, against the whole index's live
  nodes, following package re-exports.  ``check`` overlays its in-memory
  rescans on the index first.
- **Rule config at read time.** The store is unfiltered; the
  ``[validation]`` config is applied here, to both sides of the diff, so a
  config change takes effect without a rescan and a just-disabled rule's
  findings never read as resolved.
- **New and resolved.** A finding's identity is its file, rule, function
  and message, never its line, and findings are compared as a multiset
  against the store as it stood before this run.

The scanners' signatures and their ``findings_out`` / ``autosteps_out``
contracts are untouched: findings are grouped per file by the callers.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections import Counter
from collections.abc import Callable, Collection, Container, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from axiom_annotations import task

from axiom_graph.index import db
from axiom_graph.scanners import module_scanner
from axiom_graph.workflows.validation import AutoStepRecord, validate_autostep_targets

logger = logging.getLogger(__name__)


def all_rules_enabled(rule_id: str) -> bool:  # noqa: ARG001 -- the scanners' rule-guard signature
    """Rule guard that enables every rule, so the store holds unfiltered findings.

    Args:
        rule_id: The rule a scanner is about to report.

    Returns:
        Always ``True``.
    """
    return True


@dataclass
class FileAnnotations:
    """One file's raw annotation results.

    Attributes:
        findings: Scanner findings as ``ValidationFinding.to_dict`` dicts.
        autosteps: AutoStep records as dicts of the ``AutoStepRecord`` fields.
    """

    findings: list[dict] = field(default_factory=list)
    autosteps: list[dict] = field(default_factory=list)

    @classmethod
    def from_scan(cls, findings: Iterable, autosteps: Iterable[AutoStepRecord]) -> FileAnnotations:
        """Build from what a scanner appended to its ``findings_out`` / ``autosteps_out``.

        Args:
            findings: ``ValidationFinding`` objects.
            autosteps: ``AutoStepRecord`` objects.

        Returns:
            The file's results as plain dicts.
        """
        return cls(findings=[f.to_dict() for f in findings], autosteps=[asdict(rec) for rec in autosteps])


@dataclass
class FindingsOutcome:
    """The current annotation findings of a run and how they differ from the store.

    Attributes:
        files: Walked file -> its current raw results (rescanned this run, or
            as stored).
        b4: B4 findings resolved this run, unfiltered.
        findings: Current findings after the rule config, sorted by file and
            line, each with a ``new`` flag.
        new: How many of *findings* are new.
        resolved: How many previously stored findings (after the rule
            config) are gone.
        recorded: ``False`` when the build could not write the store, so
            the files it scanned have no current rows.
    """

    files: dict[str, FileAnnotations] = field(default_factory=dict)
    b4: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    new: int = 0
    resolved: int = 0
    recorded: bool = True


#: A line reference inside a finding message (B1's "first seen at line 8").
_LINE_REF = re.compile(r"\bline \d+\b")


def finding_identity(finding: Mapping) -> tuple:
    """Return a finding's identity: file, rule, function and message, never its line.

    Line references inside the message (B1 names the line of the first
    occurrence) are masked too, so a finding that only moved keeps its
    identity.

    Args:
        finding: A finding dict.

    Returns:
        The identity tuple.
    """
    message = finding.get("message")
    if isinstance(message, str):
        message = _LINE_REF.sub("line #", message)
    return (finding.get("module"), finding.get("rule_id"), finding.get("function"), message)


def diff_findings(
    previous: Iterable[dict],
    current: Iterable[dict],
    is_rule_enabled: Callable[[str], bool] = all_rules_enabled,
) -> tuple[list[dict], int]:
    """Flag each current finding new or not, as a multiset against *previous*.

    Both sides are filtered through *is_rule_enabled* first, so a rule the
    config disables is neither listed nor counted as resolved.  Two
    identical findings count twice: one stored copy matches one current
    copy, earlier lines first.

    Args:
        previous: Findings stored before this run.
        current: Findings this run computed.
        is_rule_enabled: Rule-config guard.

    Returns:
        ``(findings, resolved)``: the enabled current findings sorted by
        file and line, each a copy with ``new: bool``, and the number of
        enabled previous findings no current finding matched.
    """
    remaining = Counter(finding_identity(f) for f in previous if is_rule_enabled(f.get("rule_id")))
    flagged: list[dict] = []
    ordered = sorted(
        (f for f in current if is_rule_enabled(f.get("rule_id"))),
        key=lambda f: (f.get("module") or "", f.get("line") or 0, f.get("rule_id") or "", f.get("message") or ""),
    )
    for finding in ordered:
        key = finding_identity(finding)
        is_new = remaining[key] <= 0
        if not is_new:
            remaining[key] -= 1
        flagged.append({**finding, "new": is_new})
    resolved = sum(count for count in remaining.values() if count > 0)
    return flagged, resolved


def make_target_resolver(
    live_ids: Container[str],
    star: dict[str, list[str]],
    named: dict[str, list[tuple[str, dict[str, str]]]],
) -> Callable[[str], str | None]:
    """Return a resolver mapping a recorded AutoStep target to the live node it names.

    A target that is a live node is itself.  One that is not is followed
    through the re-export relation, the way the build's link resolver
    follows a delegate link; a target that still names nothing resolves to
    ``None``.

    Args:
        live_ids: Ids of every live node.
        star: Star re-export sources per module id.
        named: Named re-export bindings per module id.

    Returns:
        Callable taking a recorded target id and returning a live node id or
        ``None``.
    """
    from axiom_graph.index import builder  # noqa: PLC0415 -- builder imports this module

    hops = builder.reexport_hops(star, named)

    def node_exists(node_id: str) -> bool:
        return node_id in live_ids

    def resolve(target: str) -> str | None:
        if target in live_ids:
            return target
        module_id, _, symbol = target.rpartition("::")
        if not module_id or not symbol:
            return None
        return builder.resolve_symbol_through_reexports(module_id, symbol, node_exists, hops)

    return resolve


def _prefetch_b4(autosteps: list[AutoStepRecord], live_ids, star, named) -> None:
    """Batch-read what B4 asks of a lazily read live set and re-export relation.

    Only for containers that read the index on demand (they offer
    ``prefetch``); a plain set or dict is left alone.  Answers do not
    change, only how many reads give them: the recorded targets and their
    envelopes in one batch, then the re-export closure of the targets that
    are not live, one batch per level.

    Args:
        autosteps: The records B4 resolves.
        live_ids: Ids of every live node.
        star: Star re-export sources per module id.
        named: Named re-export bindings per module id.
    """
    from axiom_graph.scanners._step_helpers import envelope_id_for  # noqa: PLC0415

    from axiom_graph.index import builder  # noqa: PLC0415 -- builder imports this module

    targets = [rec.target_node_id for rec in autosteps if rec.has_next_call and rec.target_node_id]
    prefetch_ids = getattr(live_ids, "prefetch", None)
    if prefetch_ids is not None:
        prefetch_ids([*targets, *(envelope_id_for(t) for t in targets)])
    builder.prefetch_reexport_closure(live_ids, star, named, targets)


def resolve_b4(
    records: Iterable[dict],
    live_ids: Container[str],
    star: dict[str, list[str]],
    named: dict[str, list[tuple[str, dict[str, str]]]],
) -> list[dict]:
    """Resolve rule B4 for stored AutoStep records against the live index.

    A target is decorated when the envelope its decorator mints is a live
    node; a target that names no live node, even through re-exports, is
    unresolved.  The result is unfiltered by the rule config.

    Args:
        records: AutoStep records as dicts of the ``AutoStepRecord`` fields.
        live_ids: Ids of every live node, envelopes included.
        star: Star re-export sources per module id.
        named: Named re-export bindings per module id.

    Returns:
        B4 findings as ``ValidationFinding.to_dict`` dicts.
    """
    autosteps = [AutoStepRecord(**record) for record in records]
    _prefetch_b4(autosteps, live_ids, star, named)
    findings = validate_autostep_targets(
        autosteps,
        envelope_node_ids=live_ids,
        resolve_target=make_target_resolver(live_ids, star, named),
    )
    return [f.to_dict() for f in findings]


def compute_findings(
    stored: db.StoredAnnotations,
    *,
    walked: Iterable[str],
    rescanned: Mapping[str, FileAnnotations],
    live_ids: Container[str],
    star: dict[str, list[str]],
    named: dict[str, list[tuple[str, dict[str, str]]]],
    is_rule_enabled: Callable[[str], bool],
    hidden: Container[str] = (),
) -> FindingsOutcome:
    """Merge stored and rescanned results, resolve B4, and diff against the store.

    Args:
        stored: The store as it stood before this run.
        walked: Every file this run walks (rescanned, unchanged, or whose
            scan raised).
        rescanned: Files scanned this run -> their results.  A walked file
            not here contributes its stored rows.
        live_ids: Ids of every live node for B4.
        star: Star re-export sources per module id.
        named: Named re-export bindings per module id.
        is_rule_enabled: The ``[validation]`` rule guard.
        hidden: Stored files that are neither current nor gone (JS/TS files
            while the JS scanner is unavailable): left out of both sides.

    Returns:
        The :class:`FindingsOutcome`.
    """
    files: dict[str, FileAnnotations] = {}
    for file in sorted(set(walked)):
        if file in rescanned:
            files[file] = rescanned[file]
        elif file in stored.findings or file in stored.autosteps:
            files[file] = FileAnnotations(
                findings=list(stored.findings.get(file, [])), autosteps=list(stored.autosteps.get(file, []))
            )

    b4 = resolve_b4((rec for result in files.values() for rec in result.autosteps), live_ids, star, named)

    previous = [f for file, rows in stored.findings.items() if file not in hidden for f in rows]
    previous.extend(f for f in stored.b4 if f.get("module") not in hidden)
    current = [f for result in files.values() for f in result.findings] + b4
    findings, resolved = diff_findings(previous, current, is_rule_enabled)
    return FindingsOutcome(
        files=files,
        b4=b4,
        findings=findings,
        new=sum(1 for f in findings if f["new"]),
        resolved=resolved,
    )


def live_node_lookup(db_path: Path, project_root: Path, rescanned_nodes: list, *, walked: Collection[str] = ()):
    """Return the ids of every live node, with in-memory rescans overlaid, answered on demand.

    The build's live-node rule: a node is live when the index holds it for
    a file that still exists and was not rescanned, it is not NOT_FOUND, or
    a rescan produced it.

    A :class:`~axiom_graph.index.builder.LiveNodeLookup` with the rescans as
    its own nodes and nothing snapshotted: an id the rescans produced is
    live; any other is judged from its index row when asked (not NOT_FOUND,
    not in a rescanned file, its file still on disk).  Bind it to a
    connection with ``reading_on``.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Absolute project root.
        rescanned_nodes: Nodes produced by this run's rescans.
        walked: Files this run's walk read from disk; they are known to exist.

    Returns:
        The lookup.
    """
    from axiom_graph.index import builder  # noqa: PLC0415 -- builder imports this module

    return builder.LiveNodeLookup(db_path, project_root, rescanned_nodes, walked=walked)


class _OverlaidReexportHalf:
    """One side of an :class:`_OverlaidReexportRelation`, answering ``get`` like the dict it stands for."""

    def __init__(self, relation: _OverlaidReexportRelation, index: int) -> None:
        self._relation = relation
        self._index = index

    def get(self, module_id: str, default=None):
        """Return the module's entries, or *default* when it has none."""
        entries = self._relation.module(module_id)[self._index]
        return entries if entries else default

    def prefetch(self, module_ids: Iterable[str]) -> None:
        """Read the index rows of every module in *module_ids* in one batch."""
        self._relation.prefetch(module_ids)


class _OverlaidReexportRelation:
    """The index's re-export relation with in-memory rescans overlaid, read per module.

    A module a rescan produced answers from the rescan's ``depends_on``
    edges only; any other from its index rows, plus any rescanned edge it
    is the source of, sorted as the whole read sorts them.

    Args:
        conn: Open connection to the axiom-graph DB.
        rescanned_nodes: Nodes produced by this run's rescans.
        rescanned_edges: Edges produced by this run's rescans.
    """

    def __init__(self, conn, rescanned_nodes: list, rescanned_edges: list) -> None:  # noqa: D107
        from axiom_graph.index import builder  # noqa: PLC0415 -- builder imports this module

        self._reader = builder.ReexportRelationReader(conn)
        self._replaced = {node.id for node in rescanned_nodes}
        self._star, self._named = builder.reexport_relation_from_rows(
            [(e.from_id, e.to_id, e.meta) for e in rescanned_edges if e.edge_type == "depends_on"]
        )
        self.star = _OverlaidReexportHalf(self, 0)
        self.named = _OverlaidReexportHalf(self, 1)

    def prefetch(self, module_ids: Iterable[str]) -> None:
        """Read the index rows of every module in *module_ids* the rescans did not replace."""
        self._reader.prefetch(m for m in module_ids if m not in self._replaced)

    def module(self, module_id: str) -> tuple[list[str], list[tuple[str, dict[str, str]]]]:
        """Return ``(star sources, named bindings)`` of one module."""
        star = list(self._star.get(module_id, []))
        named = list(self._named.get(module_id, []))
        if module_id not in self._replaced:
            index_star, index_named = self._reader.module(module_id)
            star = sorted(index_star + star)
            named = sorted(index_named + named, key=lambda pair: pair[0])
        return star, named


def overlaid_reexport_relation(conn, rescanned_nodes: list = (), rescanned_edges: list = ()):
    """Return the index's re-export relation with in-memory rescans overlaid, read per module on demand.

    The ``depends_on`` rows of every node a rescan produced are replaced by
    the rescan's own.

    Args:
        conn: Open connection to the axiom-graph DB; it must stay open while
            the halves are read.
        rescanned_nodes: Nodes produced by this run's rescans.
        rescanned_edges: Edges produced by this run's rescans.

    Returns:
        ``(star, named)`` (see ``builder.reexport_relation_from_rows``):
        each answers ``.get(module_id, default)`` and offers ``prefetch``.
    """
    relation = _OverlaidReexportRelation(conn, list(rescanned_nodes), list(rescanned_edges))
    return relation.star, relation.named


@task(
    purpose=(
        "Store the raw annotation findings and AutoStep records of the files this build scanned, drop the rows "
        "of files it no longer walks, resolve B4 against the whole index, and report the findings that are new"
    ),
    inputs="db_path, the store as read before this build, walked and scanned files, live node ids, dotted filenames",
    outputs="FindingsOutcome: current findings (rule config applied, each flagged new or not), new and resolved counts",
    critical=(
        "Called after the doc-id namespace gate and after the index is written, so a refused build stores nothing "
        "and B4 sees this build's nodes, and before the scanned-file mtime stamp: a failed write is reported as "
        "recorded=False so the build leaves those files unstamped.  A file whose scan raised keeps its stored "
        "rows, and a hidden file keeps its stored B4 rows; never raises"
    ),
)
def record_annotation_findings(
    db_path: Path,
    stored: db.StoredAnnotations,
    *,
    walked: set[str],
    scanned: Mapping[str, FileAnnotations],
    hidden: Container[str],
    live_ids: Container[str],
    dotted: list[str] | None,
    is_rule_enabled: Callable[[str], bool],
    warnings: list[str],
) -> FindingsOutcome:
    """Write this build's annotation results to the store and report what changed.

    Args:
        db_path: Path to the axiom-graph DB.
        stored: The store as read before this build.
        walked: Every code file this build walked.
        scanned: Files this build scanned successfully -> their raw results.
        hidden: Stored files kept but not current (JS/TS files while the JS
            scanner is unavailable).
        live_ids: Ids of every live node after this build's upserts.
        dotted: Every dotted DocJSON filename on disk, or ``None`` when the
            pass that enumerates them failed (the stored set is kept).
        is_rule_enabled: The ``[validation]`` rule guard.
        warnings: The build's warnings list; a failure is appended here.

    Returns:
        The :class:`FindingsOutcome`; an empty one with ``recorded=False``
        when the store could not be written (nothing is written then).
    """
    from axiom_graph.index import builder  # noqa: PLC0415 -- builder imports this module

    try:
        with (
            db._connect(db_path) as conn,
            live_ids.reading_on(conn) if isinstance(live_ids, builder.LiveNodeLookup) else contextlib.nullcontext(),
        ):
            # The relation is read per module B4 reaches, not every marker.
            relation = builder.ReexportRelationReader(conn)
            star, named = relation.star, relation.named
            outcome = compute_findings(
                stored,
                walked=walked,
                rescanned=scanned,
                live_ids=live_ids,
                star=star,
                named=named,
                is_rule_enabled=is_rule_enabled,
                hidden=hidden,
            )
            gone = sorted(file for file in stored.files if file not in walked and file not in hidden)
            # A hidden file's B4 rows are kept, like its per-file rows, so they
            # do not read as new when the JS scanner returns.
            kept_b4 = [f for f in stored.b4 if f.get("module") in hidden]
            b4_rows = outcome.b4 + kept_b4
            dotted_changed = dotted is not None and list(dotted) != list(stored.dotted)
            if not scanned and not gone and not dotted_changed and b4_rows == list(stored.b4):
                # Nothing to write: the store already holds exactly this result
                # (no commit, so a no-change build leaves the store untouched).
                return outcome
            for file, result in scanned.items():
                db.replace_file_annotations_conn(conn, file, result.findings, result.autosteps)
            db.delete_file_annotations_conn(conn, gone)
            db.replace_b4_findings_conn(conn, b4_rows)
            if dotted is not None:
                db.replace_dotted_filenames_conn(conn, dotted)
        return outcome
    except Exception as exc:  # pragma: no cover -- report-only, never fails a build
        warnings.append(f"annotation findings could not be recorded: {exc}")
        logger.warning("annotation findings store error: %s", exc)
        return FindingsOutcome(recorded=False)


def js_scanning_available() -> bool:
    """Return whether the JS/TS scanners can run (tree-sitter is installed).

    Returns:
        ``True`` when JS/TS and xstate files can be scanned.
    """
    from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER  # noqa: PLC0415 -- optional dependency

    return bool(HAS_TREE_SITTER)


def rescan_in_memory(
    project_root: Path,
    project_id: str,
    py_files: Iterable[Path],
    js_files: Iterable[Path] = (),
    source_roots: Sequence[Path] | None = None,
) -> tuple[dict[str, FileAnnotations], list, list]:
    """Scan code files for their annotation results without writing anything.

    Runs the same scanners as the build, with every rule enabled.  A file
    whose scan raises is left out, so the caller falls back to its stored
    rows.

    Args:
        project_root: Absolute project root.
        project_id: Project id prefix for node ids.
        py_files: Python files to scan.
        js_files: JS/TS files to scan (JS and xstate scanners).
        source_roots: The Python import roots the build resolves imports
            with (``None``: the project root only).  Pass the build's, so
            the scans link what the build links.

    Returns:
        ``(results, nodes, edges)``: file -> raw results, and the nodes and
        edges the scans produced (for overlaying on the index).
    """
    results: dict[str, FileAnnotations] = {}
    nodes: list = []
    edges: list = []
    for path in py_files:
        rel = path.relative_to(project_root).as_posix()
        findings: list = []
        autosteps: list = []
        try:
            file_nodes, file_edges = module_scanner.scan_module(
                path,
                project_root,
                project_id,
                findings_out=findings,
                autosteps_out=autosteps,
                is_rule_enabled=all_rules_enabled,
                source_roots=source_roots,
            )
        except Exception as exc:  # pragma: no cover -- falls back to the stored rows
            logger.debug("annotation rescan failed on %s: %s", rel, exc)
            continue
        nodes.extend(file_nodes)
        edges.extend(file_edges)
        results[rel] = FileAnnotations.from_scan(findings, autosteps)

    js_files = list(js_files)
    if js_files:
        from axiom_graph.scanners import js_scanner, xstate_scanner  # noqa: PLC0415 -- optional tree-sitter

        for path in js_files:
            rel = path.relative_to(project_root).as_posix()
            findings = []
            autosteps = []
            try:
                js_nodes, js_edges = js_scanner.scan_js_module(
                    path,
                    project_root,
                    project_id,
                    findings_out=findings,
                    autosteps_out=autosteps,
                    is_rule_enabled=all_rules_enabled,
                )
                xs_nodes, xs_edges = xstate_scanner.scan_xstate_module(
                    path,
                    project_root,
                    project_id,
                    findings_out=findings,
                    is_rule_enabled=all_rules_enabled,
                )
            except Exception as exc:  # pragma: no cover -- falls back to the stored rows
                logger.debug("annotation rescan failed on %s: %s", rel, exc)
                continue
            nodes.extend(js_nodes + xs_nodes)
            edges.extend(js_edges + xs_edges)
            results[rel] = FileAnnotations.from_scan(findings, autosteps)
    return results, nodes, edges


__all__ = [
    "FileAnnotations",
    "FindingsOutcome",
    "all_rules_enabled",
    "compute_findings",
    "diff_findings",
    "finding_identity",
    "live_node_lookup",
    "overlaid_reexport_relation",
    "js_scanning_available",
    "make_target_resolver",
    "record_annotation_findings",
    "rescan_in_memory",
    "resolve_b4",
]
