"""Tests for doc-ID enumeration, projection, and collision detection.

Tier 1 -- plain pytest:
    Enumeration from disk, current/projected derivation, section dot-paths,
    dotted-filename advisory, and the classification that keeps ordinary
    JSON beside the docs out of every signal.

Tier 2 -- @workflow(purpose=...):
    The gate reporting a cross-root duplicate group.
"""

from __future__ import annotations

from axiom_annotations import workflow

from axiom_graph.index.doc_ids import (
    COLLISION_CROSS_ROOT,
    COLLISION_WITHIN_ROOT,
    DOC_FILE_DOCUMENT,
    DOC_FILE_NOT_A_DOCUMENT,
    DOC_FILE_UNREADABLE,
    classify_doc_file,
    classify_doc_files,
    current_doc_id,
    current_doc_id_index,
    doc_id_signals,
    dotted_filenames,
    enumerate_doc_files,
    find_collisions,
    project_doc_ids,
    projected_doc_id,
    section_dot_paths,
)

from tests.fixtures import doc_trees


# ---------------------------------------------------------------------------
# Tier 1 -- enumeration and derivation
# ---------------------------------------------------------------------------


def test_enumerate_walks_every_configured_root(tmp_path):
    """Every DocJSON under every configured root is enumerated from disk."""
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    files = enumerate_doc_files(root, ["docs", "specs"])
    assert {f.rel_path for f in files} == {
        "docs/alpha.json",
        "docs/adrs/013-envelope.json",
        "specs/beta.json",
    }
    assert {f.root_entry for f in files} == {"docs", "specs"}


def test_enumerate_visits_a_file_once_when_roots_overlap(tmp_path):
    """A file reachable from two overlapping roots is enumerated once."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs", "docs/nested"])
    doc_trees.write_doc(root, "docs/nested/inner.json")
    files = enumerate_doc_files(root, ["docs", "docs/nested"])
    assert [f.rel_path for f in files] == ["docs/nested/inner.json"]
    assert files[0].root_entry == "docs"


def test_a_deep_path_keeps_every_segment_in_both_derivations(tmp_path):
    """Directory depth becomes dots today and stays a path once projected."""
    root = doc_trees.deep_paths(tmp_path / "proj")
    by_rel = {f.rel_path: f for f in enumerate_doc_files(root, ["docs"])}

    deep = by_rel["docs/a/b/c/deep.json"]
    assert current_doc_id("proj", deep) == "proj::docs.a.b.c.deep"
    assert projected_doc_id("proj", deep) == "proj::docs/a/b/c/deep"
    assert current_doc_id("proj", by_rel["docs/shallow.json"]) == "proj::docs.shallow"


def test_projected_id_keeps_the_root_prefix_and_path_joiner(tmp_path):
    """The configured root is the namespace verbatim and ``/`` stays the joiner."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs", ".pev"])
    doc_trees.write_doc(root, "docs/adrs/013-x.json")
    doc_trees.write_doc(root, ".pev/test-policy.json")
    by_rel = {f.rel_path: f for f in enumerate_doc_files(root, ["docs", ".pev"])}

    assert current_doc_id("proj", by_rel["docs/adrs/013-x.json"]) == "proj::docs.adrs.013-x"
    assert projected_doc_id("proj", by_rel["docs/adrs/013-x.json"]) == "proj::docs/adrs/013-x"
    assert current_doc_id("proj", by_rel[".pev/test-policy.json"]) == "proj::docs.test-policy"
    assert projected_doc_id("proj", by_rel[".pev/test-policy.json"]) == "proj::.pev/test-policy"


def test_section_dot_paths_follow_nesting_to_scanner_depth(tmp_path):
    """Nested sections join with dots and stop at the scanner's maximum depth."""
    root = doc_trees.nested_sections(tmp_path / "proj")
    paths = section_dot_paths(root / "docs" / "tree.json")
    assert paths == ["root", "root.child", "root.child.grandchild", "sibling"]


def test_projection_covers_documents_and_sections(tmp_path):
    """Every section moves alongside its document, keeping its dot-path."""
    root = doc_trees.nested_sections(tmp_path / "proj")
    projection = project_doc_ids("proj", enumerate_doc_files(root, ["docs"]))

    assert [m.old_id for m in projection.documents] == ["proj::docs.tree"]
    assert [m.new_id for m in projection.documents] == ["proj::docs/tree"]
    assert projection.total_nodes == 5
    assert ("proj::docs.tree::root.child", "proj::docs/tree::root.child") in [
        (m.old_id, m.new_id) for m in projection.sections
    ]
    assert projection.as_mapping() == {"proj::docs.tree": "proj::docs/tree"}


def test_dotted_filename_advisory_names_only_dotted_stems(tmp_path):
    """Only files whose stem carries extra dots are advised on."""
    root = doc_trees.dotted_filenames(tmp_path / "proj")
    assert dotted_filenames(enumerate_doc_files(root, ["docs"])) == ["docs/release.2.1.notes.json"]


def test_clean_tree_has_no_collisions(tmp_path):
    """A tree with distinct identities produces no duplicate groups."""
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    files = enumerate_doc_files(root, ["docs", "specs"])
    assert find_collisions(current_doc_id_index("proj", files)) == []
    assert find_collisions(project_doc_ids("proj", files).new_id_sources) == []


def test_within_root_collision_is_classified_and_projection_resolves_it(tmp_path):
    """``adrs/013-x.json`` and ``adrs.013-x.json`` collide today, not once projected."""
    root = doc_trees.class2_collision(tmp_path / "proj")
    files = enumerate_doc_files(root, ["docs"])

    collisions = find_collisions(current_doc_id_index("proj", files))
    assert [c.doc_id for c in collisions] == ["proj::docs.adrs.013-x"]
    assert collisions[0].kind == COLLISION_WITHIN_ROOT
    assert collisions[0].sources == ["docs/adrs.013-x.json", "docs/adrs/013-x.json"]

    assert find_collisions(project_doc_ids("proj", files).new_id_sources) == []


# ---------------------------------------------------------------------------
# Tier 1 -- classification: which files under a docs root carry an identity
# ---------------------------------------------------------------------------


def test_classification_separates_documents_data_files_and_unreadable(tmp_path):
    """Three outcomes, because the three populations need different handling."""
    root = doc_trees.data_files_beside_docs(tmp_path / "proj")

    assert classify_doc_file(root / "docs" / "real.json") == DOC_FILE_DOCUMENT
    assert classify_doc_file(root / "docs" / "section-less.json") == DOC_FILE_DOCUMENT
    assert classify_doc_file(root / "docs" / "data" / "chart.json") == DOC_FILE_NOT_A_DOCUMENT
    assert classify_doc_file(root / "docs" / "broken.json") == DOC_FILE_UNREADABLE
    assert classify_doc_file(root / "docs" / "absent.json") == DOC_FILE_UNREADABLE


def test_classify_doc_files_partitions_the_enumerated_tree(tmp_path):
    """Data files are set aside; unreadable ones stay visible."""
    root = doc_trees.data_files_beside_docs(tmp_path / "proj")
    scan = classify_doc_files(enumerate_doc_files(root, ["docs"]))

    assert sorted(f.rel_path for f in scan.documents) == ["docs/real.json", "docs/section-less.json"]
    assert sorted(scan.non_documents) == ["docs/data.chart.json", "docs/data/chart.json"]
    assert scan.unreadable == ["docs/broken.json"]


def test_signals_ignore_data_files_and_keep_document_findings(tmp_path):
    """A data-file pair that would collide is silent; a document pair is not."""
    root = doc_trees.data_files_beside_docs(tmp_path / "proj")
    files = enumerate_doc_files(root, ["docs"])

    # Unclassified, the raw derivation sees the data-file pair as a collision.
    raw = find_collisions(current_doc_id_index("proj", files))
    assert [c.doc_id for c in raw] == ["proj::docs.data.chart"]
    assert dotted_filenames(files) == ["docs/data.chart.json"]

    # Classified, they are invisible: neither can ever become a doc node.
    silent = doc_id_signals("proj", files)
    assert silent.collisions == []
    assert silent.dotted == []

    doc_trees.write_doc(root, "docs/adrs/013-x.json", title="Nested ADR")
    doc_trees.write_doc(root, "docs/adrs.013-x.json", title="Flat ADR")
    signals = doc_id_signals("proj", enumerate_doc_files(root, ["docs"]))

    assert [c.doc_id for c in signals.collisions] == ["proj::docs.adrs.013-x"]
    assert signals.collisions[0].sources == ["docs/adrs.013-x.json", "docs/adrs/013-x.json"]
    assert signals.dotted == ["docs/adrs.013-x.json"]


def test_signals_reclassify_a_group_that_loses_a_data_file(tmp_path):
    """Dropping a source can change what kind of collision is left."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs", "specs"])
    doc_trees.write_doc(root, "docs/adrs/013-x.json", title="Nested ADR")
    doc_trees.write_doc(root, "docs/adrs.013-x.json", title="Flat ADR")
    doc_trees.write_json(root, "specs/adrs/013-x.json", {"rows": []})

    files = enumerate_doc_files(root, ["docs", "specs"])
    raw = find_collisions(current_doc_id_index("proj", files))
    assert len(raw[0].sources) == 3
    assert raw[0].kind == COLLISION_CROSS_ROOT

    signals = doc_id_signals("proj", files)
    assert [c.doc_id for c in signals.collisions] == ["proj::docs.adrs.013-x"]
    assert signals.collisions[0].sources == ["docs/adrs.013-x.json", "docs/adrs/013-x.json"]
    assert signals.collisions[0].kind == COLLISION_WITHIN_ROOT


# ---------------------------------------------------------------------------
# Tier 2 -- the cross-root half of "covers both collision classes"
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify the collision gate reports a duplicate group when the same relative path exists under two configured docs roots",
)
def test_gate_reports_cross_root_duplicate_group(tmp_path):
    """One relative path under two roots is one identity and two sources."""
    root = doc_trees.class1_collision(tmp_path / "proj")
    files = enumerate_doc_files(root, ["docs", "specs"])

    collisions = find_collisions(current_doc_id_index("proj", files))

    assert len(collisions) == 1
    assert collisions[0].doc_id == "proj::docs.collide"
    assert collisions[0].kind == COLLISION_CROSS_ROOT
    assert collisions[0].sources == ["docs/collide.json", "specs/collide.json"]
