"""Tests for the build-time doc-id overlap and dotted-filename signals.

Tier 2 -- @workflow(purpose=...):
    Clean tree stays quiet; dotted filename is advised without changing the
    derived id; ordinary JSON beside the docs is silent while a real
    document overlap beside it is still reported.

Tier 3 -- @workflow + Step():
    An overlap introduced against an already-indexed file is reported even
    though the pre-existing file is skipped by the mtime fast-pass.
"""

from __future__ import annotations

import os
import time

from axiom_annotations import Step, workflow

from axiom_graph.config import db_path_for
from axiom_graph.index import doc_ids
from axiom_graph.lifecycle import api as lifecycle_api

from tests.fixtures import doc_trees


def _overlap_warnings(summary) -> list[str]:
    """Return warnings that name a duplicate doc id."""
    return [w for w in summary.warnings if "duplicate doc id" in w]


def _dotted_warnings(summary) -> list[str]:
    """Return warnings that advise on a dotted DocJSON filename."""
    return [w for w in summary.warnings if "dotted DocJSON filename" in w]


def _live_overlap_groups(root_entry: str, rel_paths: tuple[str, ...]) -> dict[str, list[str]]:
    """Group *rel_paths* by the doc id the scanner derives for them today.

    Only groups of two or more come back -- those are the overlaps a build has
    to report.  Derived rather than written out, because the advisory's
    subject is the namespace the scanner *actually produces*: an expectation
    spelled by hand pins whichever namespace was live when it was typed, and
    an advisory reporting a namespace nothing produces is precisely the defect
    being guarded against.

    Args:
        root_entry: The configured docs root the files live under.
        rel_paths: POSIX paths within that root.

    Returns:
        Doc id -> repo-relative source paths, duplicate groups only.
    """
    groups: dict[str, list[str]] = {}
    for rel in rel_paths:
        groups.setdefault(doc_ids.derive_doc_id("proj", root_entry, rel), []).append(f"{root_entry}/{rel}")
    return {doc_id: sources for doc_id, sources in groups.items() if len(sources) > 1}


def _assert_overlaps_are_the_live_ones(summary, root_entry: str, rel_paths: tuple[str, ...]) -> None:
    """Assert the build's overlap warnings are exactly the live duplicate groups.

    Args:
        summary: The build summary to read warnings from.
        root_entry: The configured docs root the files live under.
        rel_paths: POSIX paths within that root that might overlap.
    """
    expected = _live_overlap_groups(root_entry, rel_paths)
    overlaps = _overlap_warnings(summary)
    assert len(overlaps) == len(expected), summary.warnings
    for doc_id, sources in expected.items():
        named = [w for w in overlaps if doc_id in w]
        assert len(named) == 1, summary.warnings
        for source in sources:
            assert source in named[0]


def _build(root):
    """Run a build against *root* and return the typed summary."""
    return lifecycle_api.build_index(db_path_for(root), root, discovery_only=True)


def _doc_ids(root) -> set[str]:
    """Return every DocJSON doc-envelope id in the index."""
    from axiom_graph.index import db

    with db._connect(db_path_for(root)) as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM docs").fetchall()}


# ---------------------------------------------------------------------------
# Tier 3 -- the criterion the walked-file-set warning fails
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify an incremental build's doc-id signals still name a file that the mtime fast-pass skipped, and report overlaps in the namespace the scanner derives",
)
def test_signals_still_name_a_file_the_mtime_fast_pass_skipped(tmp_path):
    """A signal about an already-indexed, unchanged file still reports.

    The property is that the signals come from a walk of the whole doc tree
    rather than from the set of files this build happened to read.  The dotted
    advisory carries it here because it is a per-file finding: it can only
    name the aged file if the aged file was enumerated.
    """
    口 = Step(
        step_num=1,
        name="Index a tree holding one dotted filename",
        purpose="Build a project whose single document is the one the next build will skip",
    )
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(root, "docs/adrs.013-x.json", title="Flat ADR")
    first = _build(root)
    assert not _overlap_warnings(first)
    assert len(_dotted_warnings(first)) == 1, first.warnings

    口 = Step(
        step_num=2,
        name="Age the indexed file past the mtime fast-pass",
        purpose="Leave the pre-existing file untouched so the next build skips reading it",
    )
    old = time.time() - 3600
    os.utime(root / "docs" / "adrs.013-x.json", (old, old))

    口 = Step(
        step_num=3,
        name="Introduce a sibling at the path the dotted name imitates",
        purpose="Give the build a freshly-walked file to work on beside the skipped one",
    )
    doc_trees.write_doc(root, "docs/adrs/013-x.json", title="Nested ADR")

    口 = Step(
        step_num=4,
        name="Rebuild and assert the skipped file is still named",
        purpose="The signal must come from the whole tree, not the files walked this build",
    )
    second = _build(root)
    # The premise the test rests on: the aged file really was left unread.
    # Without this a build that had stopped skipping doc files would still
    # produce the warning and the test would pass for the wrong reason.  One
    # doc file existed before this build and no Markdown does, so the count
    # can only be that file.
    assert second.docs_skipped_mtime == 1, second
    # Already advised on by the first build, so not repeated; but the
    # whole-tree pass still enumerated the skipped file, which keeps it in
    # the stored set rather than letting it drop out as if it were gone.
    assert not _dotted_warnings(second), second.warnings
    from axiom_graph.index import db

    assert db.read_annotation_store(db_path_for(root)).dotted == ["docs/adrs.013-x.json"]
    _assert_overlaps_are_the_live_ones(second, "docs", ("adrs/013-x.json", "adrs.013-x.json"))


# ---------------------------------------------------------------------------
# Tier 2 -- negative case and advisory
# ---------------------------------------------------------------------------


@workflow(purpose="Verify a clean multi-root tree produces neither an overlap warning nor a dotted-filename advisory")
def test_clean_tree_emits_no_doc_id_signals(tmp_path):
    """Distinct identities and plain filenames leave the build quiet."""
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    summary = _build(root)
    assert not _overlap_warnings(summary)
    assert not _dotted_warnings(summary)


@workflow(purpose="Verify a dotted DocJSON filename is advised on once and does not change the derived doc id")
def test_dotted_filename_advisory_does_not_change_derived_id(tmp_path):
    """The advisory names the file; the identity it derives is unchanged."""
    dotted_root = doc_trees.dotted_filenames(tmp_path / "dotted")
    summary = _build(dotted_root)
    advisories = _dotted_warnings(summary)
    assert len(advisories) == 1, summary.warnings
    assert "docs/release.2.1.notes.json" in advisories[0]

    # Reported once: an unchanged rebuild stays quiet, and a second dotted
    # file is the only one the next build names.
    assert not _dotted_warnings(_build(dotted_root))
    doc_trees.write_doc(dotted_root, "docs/plans.second.json", title="Second dotted")
    third = _dotted_warnings(_build(dotted_root))
    assert len(third) == 1, third
    assert "docs/plans.second.json" in third[0]

    # The advisory is about the filename shape and nothing else: alone in a
    # project of its own, the same file derives a byte-identical doc id.
    solo_root = tmp_path / "solo"
    doc_trees.write_toml(solo_root, ["docs"])
    doc_trees.write_doc(solo_root, "docs/release.2.1.notes.json", title="Dotted")
    _build(solo_root)
    derived = doc_ids.derive_doc_id("proj", "docs", "release.2.1.notes.json")
    assert derived in _doc_ids(solo_root)
    assert derived in _doc_ids(dotted_root)


@workflow(
    purpose="Verify ordinary JSON data files under a docs root produce no doc-id overlap or dotted-filename signal even when their paths would derive one doc id between them",
)
def test_non_document_json_under_a_docs_root_produces_no_signal(tmp_path):
    """Only DocJSON carries an identity, so only DocJSON can be reported."""
    root = doc_trees.data_files_beside_docs(tmp_path / "proj")
    summary = _build(root)

    assert not _overlap_warnings(summary), summary.warnings
    assert not _dotted_warnings(summary), summary.warnings
    # The real documents beside them still index normally.
    assert {
        doc_ids.derive_doc_id("proj", "docs", "real.json"),
        doc_ids.derive_doc_id("proj", "docs", "section-less.json"),
    } <= _doc_ids(root)


@workflow(
    purpose="Verify the doc-id overlaps a build reports are exactly the duplicate groups of the namespace the scanner derives, and are still reported beside non-document JSON that would derive the same id",
)
def test_document_overlap_is_still_reported_beside_non_documents(tmp_path):
    """Ignoring data files must not turn into ignoring the collision."""
    root = doc_trees.data_files_beside_docs(tmp_path / "proj")
    doc_trees.write_doc(root, "docs/adrs/013-x.json", title="Nested ADR")
    doc_trees.write_doc(root, "docs/adrs.013-x.json", title="Flat ADR")
    summary = _build(root)

    # The two data files -- docs/data/chart.json and docs/data.chart.json --
    # are deliberately left out of the expectation: whatever id they derive
    # between them, neither is a document and neither may be reported.
    _assert_overlaps_are_the_live_ones(summary, "docs", ("adrs/013-x.json", "adrs.013-x.json"))
    # ...and the dotted advisory names the document, not the data file.
    assert [w for w in _dotted_warnings(summary) if "docs/data.chart.json" in w] == []
    assert len([w for w in _dotted_warnings(summary) if "docs/adrs.013-x.json" in w]) == 1
