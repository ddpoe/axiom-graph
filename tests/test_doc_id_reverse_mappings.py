"""Tests for the consumers that read a doc ID backwards rather than derive one.

Tier 2 -- @workflow(purpose=...):
    Feature attribution segments a document's path correctly, and the
    ``::`` arity of a document / section ID survives arbitrary path depth.

Both assert derivation-agnostically: they build their inputs through the
derivation rather than hard-coding a namespace, so they pin the *decomposition*
and not the value the decomposition happens to be applied to.
"""

from __future__ import annotations

from axiom_annotations import workflow

from axiom_graph.config import db_path_for
from axiom_graph.db.docs import split_section_id
from axiom_graph.db.staleness import _build_feature_index
from axiom_graph.index import doc_ids
from axiom_graph.lifecycle import api as lifecycle_api

from tests.fixtures import doc_trees


def _build(root):
    """Build an index for *root* and return the DB path."""
    path = db_path_for(root)
    lifecycle_api.build_index(path, root, discovery_only=True)
    return path


# ---------------------------------------------------------------------------
# Tier 2 -- US-1: feature attribution reads the path, and fails silently
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a code node documented by a section of docs/features/<name>/... is attributed to that feature, so the path walk keeps segmenting the doc id correctly",
)
def test_feature_attribution_segments_the_document_path(tmp_path):
    """Attribution returns ``None`` on a segmentation it cannot read.

    That is a silent answer, not an error: a walk that split on the wrong
    separator would find no ``features`` segment, return ``None`` for every
    document, and empty the feature index without anything failing.  So the
    assertion has to be that a label is actually produced.
    """
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    code_id = "proj::pkg.mod::func"
    doc_trees.write_doc(
        root,
        "docs/features/indexer/design.json",
        title="Indexer design",
        sections=[
            {
                "id": "overview",
                "heading": "Overview",
                "content": "How the indexer works.",
                "links": [{"node_id": code_id}],
            }
        ],
    )
    doc_trees.write_doc(
        root,
        "docs/features/indexer/sub_features/scanning/design.json",
        title="Scanning design",
        sections=[{"id": "overview", "heading": "Overview", "content": "Scanning."}],
    )
    db_path = _build(root)

    assert _build_feature_index(db_path).get(code_id) == "indexer"


# ---------------------------------------------------------------------------
# Tier 2 -- US-3: the :: arity of a document and a section ID
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a document id holds exactly one :: and a section id exactly two however deep the document's path, so the rightmost-:: split that separates them stays correct",
)
def test_doc_and_section_id_arity_survives_path_depth(tmp_path):
    """``split_section_id`` splits on the rightmost ``::`` and nothing else.

    Depth lives in the doc-ID *body*, never in extra ``::`` separators.  If
    a deep path ever introduced one, this split would hand back a truncated
    document ID and a section dot-path with a document fragment glued on.
    """
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(
        root,
        "docs/a/b/c/d/deep.json",
        title="Deep",
        sections=[{"id": "root", "heading": "Root", "content": "x", "sections": [{"id": "child", "heading": "C"}]}],
    )
    _build(root)

    doc_id = doc_ids.derive_doc_id("proj", "docs", "a/b/c/d/deep.json")
    section_id = f"{doc_id}::root.child"

    assert doc_id.count("::") == 1
    assert section_id.count("::") == 2
    assert split_section_id(section_id) == (doc_id, "root.child")

    # A one-segment document is the same grammar, not a special case.
    flat_id = doc_ids.derive_doc_id("proj", "docs", "flat.json")
    assert flat_id.count("::") == 1
    assert split_section_id(f"{flat_id}::only") == (flat_id, "only")
