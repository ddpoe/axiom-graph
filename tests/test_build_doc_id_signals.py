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
from axiom_graph.lifecycle import api as lifecycle_api

from tests.fixtures import doc_trees


def _overlap_warnings(summary) -> list[str]:
    """Return warnings that name a duplicate doc id."""
    return [w for w in summary.warnings if "duplicate doc id" in w]


def _dotted_warnings(summary) -> list[str]:
    """Return warnings that advise on a dotted DocJSON filename."""
    return [w for w in summary.warnings if "dotted DocJSON filename" in w]


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
    purpose="Verify an incremental build reports a doc-id overlap even when one of the two colliding files is skipped by the mtime fast-pass",
)
def test_overlap_reported_when_one_colliding_file_is_mtime_skipped(tmp_path):
    """A collision against an already-indexed, unchanged file still reports."""
    口 = Step(
        step_num=1,
        name="Index a clean tree",
        purpose="Build a project whose docs derive distinct ids so the first build is quiet",
    )
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(root, "docs/adrs/013-x.json", title="Nested ADR")
    first = _build(root)
    assert not _overlap_warnings(first)

    口 = Step(
        step_num=2,
        name="Age the indexed file past the mtime fast-pass",
        purpose="Leave the pre-existing file untouched so the next build skips reading it",
    )
    old = time.time() - 3600
    os.utime(root / "docs" / "adrs" / "013-x.json", (old, old))

    口 = Step(
        step_num=3,
        name="Introduce a colliding sibling",
        purpose="Add a dotted-path file that derives the same doc id as the skipped file",
    )
    doc_trees.write_doc(root, "docs/adrs.013-x.json", title="Flat ADR")

    口 = Step(
        step_num=4,
        name="Rebuild and assert the overlap is named",
        purpose="The signal must come from the whole tree, not the files walked this build",
    )
    second = _build(root)
    # The premise the test rests on: the aged file really was left unread.
    # Without this a build that had stopped skipping doc files would still
    # produce the overlap warning and the test would pass for the wrong
    # reason.  One doc file existed before this build and no Markdown does,
    # so the count can only be that file.
    assert second.docs_skipped_mtime == 1, second
    overlaps = _overlap_warnings(second)
    assert len(overlaps) == 1, second.warnings
    assert "proj::docs.adrs.013-x" in overlaps[0]
    assert "docs/adrs/013-x.json" in overlaps[0]
    assert "docs/adrs.013-x.json" in overlaps[0]


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

    # The advisory is about the filename shape and nothing else: alone in a
    # project of its own, the same file derives a byte-identical doc id.
    solo_root = tmp_path / "solo"
    doc_trees.write_toml(solo_root, ["docs"])
    doc_trees.write_doc(solo_root, "docs/release.2.1.notes.json", title="Dotted")
    _build(solo_root)
    assert "proj::docs.release.2.1.notes" in _doc_ids(solo_root)
    assert "proj::docs.release.2.1.notes" in _doc_ids(dotted_root)


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
    assert {"proj::docs.real", "proj::docs.section-less"} <= _doc_ids(root)


@workflow(
    purpose="Verify a doc-id overlap between two real documents is still reported when a non-document JSON file shares the same derived id",
)
def test_document_overlap_is_still_reported_beside_non_documents(tmp_path):
    """Ignoring data files must not turn into ignoring the collision."""
    root = doc_trees.data_files_beside_docs(tmp_path / "proj")
    doc_trees.write_doc(root, "docs/adrs/013-x.json", title="Nested ADR")
    doc_trees.write_doc(root, "docs/adrs.013-x.json", title="Flat ADR")
    summary = _build(root)

    overlaps = _overlap_warnings(summary)
    assert len(overlaps) == 1, summary.warnings
    assert "proj::docs.adrs.013-x" in overlaps[0]
    assert "docs/adrs/013-x.json" in overlaps[0]
    assert "docs/adrs.013-x.json" in overlaps[0]
    # ...and the dotted advisory names the document, not the data file.
    assert [w for w in _dotted_warnings(summary) if "docs/data.chart.json" in w] == []
    assert len([w for w in _dotted_warnings(summary) if "docs/adrs.013-x.json" in w]) == 1
