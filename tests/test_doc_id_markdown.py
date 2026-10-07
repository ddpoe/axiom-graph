"""Tests for Markdown doc-ID derivation and Markdown identity migration.

Tier 2 -- @workflow(purpose=...):
    Markdown derives by path and keeps its extension; a tree holding both
    document classes for one stem is still migratable; a Markdown document
    and its ``#slug`` sections carry history and verification onto their new
    identities.
"""

from __future__ import annotations

from axiom_annotations import workflow

from axiom_graph.config import db_path_for
from axiom_graph.index import db
from axiom_graph.index import doc_ids
from axiom_graph.lifecycle import api as lifecycle_api

from tests.fixtures import doc_trees


def _build(root):
    """Build an index for *root* and return the DB path."""
    path = db_path_for(root)
    lifecycle_api.build_index(path, root, discovery_only=True)
    return path


def _build_as_previous_release(root):
    """Index *root* under the retired rule and return the DB path.

    Used by the rows whose subject is the *migration* rather than the
    derivation: a migration needs retired identities in the index to move.
    See ``doc_trees.retired_derivation``.
    """
    with doc_trees.retired_derivation():
        return _build(root)


def _node_ids(db_path) -> set[str]:
    """Return every node ID in the index."""
    with db._connect(db_path) as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM nodes").fetchall()}


def _verification_ids(db_path) -> set[str]:
    """Return every node ID carrying a verification row."""
    with db._connect(db_path) as conn:
        return {r["node_id"] for r in conn.execute("SELECT node_id FROM node_verification").fetchall()}


def _history_ids(db_path) -> set[str]:
    """Return every node ID carrying a history row."""
    with db._connect(db_path) as conn:
        return {r["node_id"] for r in conn.execute("SELECT DISTINCT node_id FROM node_history").fetchall()}


# ---------------------------------------------------------------------------
# Tier 2 -- US-1: Markdown derives by path, and keeps its extension
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a Markdown document's id derives from its path within its docs root and retains the .md extension, so two same-stem Markdown files and a same-stem DocJSON file are three distinct documents",
)
def test_markdown_ids_derive_by_path_and_retain_the_extension(tmp_path):
    """Stem-only derivation folded three documents into one; path plus suffix does not.

    The extension is the half that keeps the *classes* apart.  Path alone
    separates ``guides/notes.md`` from ``reference/notes.md``, but
    ``overview.md`` and ``overview.json`` share a path — only the retained
    ``.md`` distinguishes them, and DocJSON must still drop ``.json``.
    """
    root = doc_trees.markdown_classes(tmp_path / "proj")
    db_path = _build(root)
    ids = _node_ids(db_path)

    same_stem_pair = {"proj::docs/guides/notes.md", "proj::docs/reference/notes.md"}
    cross_class_pair = {"proj::docs/overview.md", "proj::docs/overview"}
    assert same_stem_pair <= ids
    assert cross_class_pair <= ids
    assert len(same_stem_pair | cross_class_pair) == 4

    # The retired stem-only identity is gone, in both its forms.
    assert "proj::docs.notes" not in ids
    assert "proj::docs.overview" not in ids

    # Section identities hang off the document, so they move with it.
    assert "proj::docs/guides/notes.md#setup" in ids
    assert "proj::docs/reference/notes.md#fields" in ids


# ---------------------------------------------------------------------------
# Tier 2 -- US-1: the class overlap does not refuse the migration
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a project holding both an x.md and an x.json is not refused by the migration gate, and that the two files project to two identities rather than one",
)
def test_a_markdown_and_docjson_stem_overlap_does_not_block_the_migration(tmp_path):
    """Stripping ``.md`` for symmetry would refuse this project outright.

    Both files would project to one identity, the projected-collision gate
    would fire, and the owner would have no way forward short of renaming a
    file.  The suffix rule is what makes the plan reachable.
    """
    root = doc_trees.markdown_classes(tmp_path / "proj")
    db_path = _build_as_previous_release(root)
    plan = lifecycle_api.plan_doc_id_migration(db_path, root)

    assert plan.blocked is False
    assert plan.blocking_collisions == []

    # The gate is over the *projected* set, and it saw both classes: the two
    # same-named files project to two identities, so nothing collides.
    md = next(f for f in doc_ids.enumerate_markdown_files(root, ["docs"]) if f.rel_to_root == "overview.md")
    js = next(f for f in doc_ids.enumerate_doc_files(root, ["docs"]) if f.rel_to_root == "overview.json")
    assert doc_ids.projected_markdown_doc_id("proj", md) == "proj::docs/overview.md"
    assert doc_ids.projected_doc_id("proj", js) == "proj::docs/overview"

    # ``overview.md`` shares its *current* identity with ``overview.json``,
    # and ``guides/notes.md`` with ``reference/notes.md``: one node row per
    # group, so the losers are reported rather than migrated, and never block.
    assert plan.markdown_shadowed == ["docs/guides/notes.md", "docs/overview.md"]

    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is True


# ---------------------------------------------------------------------------
# Tier 2 -- US-4: Markdown identities migrate with their history
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a Markdown document and its #slug section nodes migrate onto their new ids carrying verification and history, despite having no docs table row",
)
def test_markdown_documents_and_their_sections_migrate_with_history(tmp_path):
    """Markdown is ``nodes`` rows and nothing else — the rekey cannot assume a ``docs`` row."""
    root = doc_trees.markdown_only(tmp_path / "proj")
    db_path = _build_as_previous_release(root)

    old_doc_id = "proj::docs.onboarding"
    old_section_id = f"{old_doc_id}#first-day"
    new_doc_id = "proj::docs/handbook/onboarding.md"
    new_section_id = f"{new_doc_id}#first-day"

    assert {old_doc_id, old_section_id} <= _node_ids(db_path)
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT id FROM docs WHERE id = ?", (old_doc_id,)).fetchone() is None

    lifecycle_api.mark_clean_nodes(
        db_path,
        root,
        [old_doc_id, old_section_id],
        "reviewed",
        verified_by="fixture",
    )
    assert {old_doc_id, old_section_id} <= _verification_ids(db_path)

    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is True

    ids_after = _node_ids(db_path)
    assert {new_doc_id, new_section_id} <= ids_after
    assert old_doc_id not in ids_after
    assert old_section_id not in ids_after

    assert {new_doc_id, new_section_id} <= _verification_ids(db_path)
    assert {new_doc_id, new_section_id} <= _history_ids(db_path)

    with db._connect(db_path) as conn:
        renamed = {r["old_id"]: r["new_id"] for r in conn.execute("SELECT old_id, new_id FROM node_renames").fetchall()}
    assert renamed[old_doc_id] == new_doc_id
    assert renamed[old_section_id] == new_section_id


# ---------------------------------------------------------------------------
# Tier 1 -- section-identity discovery reads the scanner's own rule
# ---------------------------------------------------------------------------


def test_markdown_section_slugs_match_the_slugs_the_scanner_creates(tmp_path):
    """The migration enumerates from disk; the scanner enumerates from tokens."""
    root = doc_trees.markdown_only(tmp_path / "proj")
    md_path = root / "docs" / "handbook" / "onboarding.md"

    assert doc_ids.markdown_section_slugs(md_path) == ["first-day", "second-day"]

    db_path = _build_as_previous_release(root)
    scanned = {i for i in _node_ids(db_path) if "#" in i and "onboarding" in i}
    derived = {f"proj::docs.onboarding#{slug}" for slug in doc_ids.markdown_section_slugs(md_path)}
    assert scanned == derived


def test_markdown_enumeration_covers_every_configured_root(tmp_path):
    """Enumeration is per-root, like DocJSON's — a non-primary root is not skipped."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs", ".pev"])
    doc_trees.write_markdown(root, "docs/a/one.md")
    doc_trees.write_markdown(root, ".pev/two.md")

    found = doc_ids.enumerate_markdown_files(root, ["docs", ".pev"])
    assert {(f.root_entry, f.rel_to_root) for f in found} == {("docs", "a/one.md"), (".pev", "two.md")}
