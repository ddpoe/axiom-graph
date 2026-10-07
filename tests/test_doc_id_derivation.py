"""Tests for the one-identity-per-file property of DocJSON doc IDs.

Tier 3 -- @workflow + Step():
    Every route by which a document enters the index derives one identity for
    it; and a migrated project rebuilds onto its migrated identities without
    inserting a second copy of the doc tree.

Tier 2 -- @workflow(purpose=...):
    A document under a non-primary docs root keeps that root in its ID; a
    nested path and the flat dotted filename that imitates it derive
    different IDs and each round-trips to its own file.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.config import db_path_for
from axiom_graph.index import db
from axiom_graph.index import doc_ids
from axiom_graph.lifecycle import api as lifecycle_api

from tests.fixtures import doc_trees

try:  # pragma: no cover - exercised by the skip marker
    import fastapi  # noqa: F401

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - optional [viz] extra
    _HAS_FASTAPI = False

_skip_no_fastapi = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed (optional [viz] extra)")


def _build(root: Path):
    """Build an index for *root* and return the typed build summary."""
    return lifecycle_api.build_index(db_path_for(root), root, discovery_only=True)


def _node_ids(root: Path) -> set[str]:
    """Return every node ID in *root*'s index."""
    with db._connect(db_path_for(root)) as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM nodes").fetchall()}


def _doc_locations(root: Path) -> dict[str, str]:
    """Return doc envelope ID -> the file path the index records for it."""
    with db._connect(db_path_for(root)) as conn:
        rows = conn.execute(
            "SELECT id, location FROM nodes WHERE subtype = 'docjson_doc'",
        ).fetchall()
    return {r["id"]: r["location"] for r in rows}


def _viz_client(root: Path):
    """Point the viz server module globals at *root* and return a TestClient."""
    from fastapi.testclient import TestClient

    from axiom_graph.config import AxiomGraphConfig
    from axiom_graph.viz import server

    server._PROJECT_ROOT = root
    server._DB_PATH = db_path_for(root)
    server._DFLOW_DB_PATH = None
    config = AxiomGraphConfig.load(root)
    server._PROJECT_ID = config.project_id or root.name
    server._TEST_PATHS = config.scan.test_paths
    server._EXCLUDE_DIRS = config.scan.exclude_dirs
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# Tier 3 -- US-1: every route into the index derives the same identity
# ---------------------------------------------------------------------------


@_skip_no_fastapi
@workflow(
    purpose="Verify a document created through write_doc, one created through the viz endpoint, one moved through the viz endpoint, and a full re-derivation by the build all agree on one identity per file, with no forked node left behind",
)
def test_every_route_into_the_index_derives_one_identity_per_file(tmp_path):
    """Four call sites derived this ID independently, so four could disagree.

    A disagreement does not raise: the loser is simply a second node for the
    same file, and the file's history follows whichever ID the last writer
    used.  So the assertion has to be that the build's identity is the *only*
    identity for the file, not merely that the reported IDs match.
    """
    口 = Step(
        step_num=1,
        name="Index a project",
        purpose="Give the viz endpoints and write_doc an index to write into",
    )
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(root, "docs/seed.json", title="Seed")
    _build(root)
    client = _viz_client(root)

    口 = Step(
        step_num=2,
        name="Create a document through write_doc",
        purpose="The api route agents take",
    )
    from axiom_graph.docjson import api as docjson_api

    written = docjson_api.axiom_graph_write_doc(
        str(root),
        {
            "title": "Written",
            "id": "guides/written",
            "sections": [{"id": "overview", "heading": "Overview", "content": "Body."}],
        },
    )
    assert not str(written).startswith("ERROR"), written
    written_id = doc_ids.derive_doc_id("proj", "docs", "guides/written.json")

    口 = Step(
        step_num=3,
        name="Create a document through the viz endpoint",
        purpose="The route the doc manager takes",
    )
    created = client.post("/api/docs", json={"title": "Created", "subdirectory": "guides"})
    assert created.status_code == 200, created.text
    created_id = created.json()["doc_id"]

    口 = Step(
        step_num=4,
        name="Move a document through the viz endpoint",
        purpose="The route that re-derives an identity for a path that changed",
    )
    moved = client.post(
        f"/api/docs/{created_id}/move",
        json={"destination": "reference", "filename": "created"},
    )
    assert moved.status_code == 200, moved.text
    moved_id = moved.json()["new_id"]
    assert moved_id == doc_ids.derive_doc_id("proj", "docs", "reference/created.json")

    口 = Step(
        step_num=5,
        name="Re-derive everything by a full build",
        purpose="The scanner is the fourth derivation, and the one that owns the index",
    )
    _build(root)
    locations = _doc_locations(root)

    口 = Step(
        step_num=6,
        name="Assert one identity per file",
        purpose="A forked identity shows up as two ids pointing at one file",
    )
    for doc_id, expected_path in (
        (written_id, "docs/guides/written.docjson"),
        (moved_id, "docs/reference/created.docjson"),
    ):
        assert locations.get(doc_id) == expected_path, locations
        owners = [i for i, loc in locations.items() if loc == expected_path]
        assert owners == [doc_id], owners

    # The pre-move identity is gone rather than left beside the new one.
    assert created_id not in locations


# ---------------------------------------------------------------------------
# Tier 2 -- US-1: the two DocJSON collision classes are closed
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a nested path and the flat dotted filename that imitates it derive different doc ids, and that each id round-trips to its own file",
)
def test_a_nested_path_and_a_dotted_filename_derive_separate_identities(tmp_path):
    """``adrs/013-x.json`` and ``adrs.013-x.json`` are two documents, not one.

    Deriving one identity between them is not an error anyone sees: the file
    scanned second overwrites the first, and the survivor's title is the only
    hint that a document went missing.  Round-tripping each ID back to a file
    path is what distinguishes "two ids" from "two ids that both point at the
    same survivor".
    """
    root = doc_trees.class2_collision(tmp_path / "proj")
    _build(root)
    locations = _doc_locations(root)

    nested = doc_ids.derive_doc_id("proj", "docs", "adrs/013-x.json")
    flat = doc_ids.derive_doc_id("proj", "docs", "adrs.013-x.json")
    assert nested != flat
    assert locations.get(nested) == "docs/adrs/013-x.json", locations
    assert locations.get(flat) == "docs/adrs.013-x.json", locations


@workflow(
    purpose="Verify a document under a non-primary docs root carries that root in its id and does not collide with a same-named document under the primary root",
)
def test_the_same_relative_path_under_two_roots_derives_two_identities(tmp_path):
    """One flat ``docs.`` namespace made every configured root the same root.

    A project that configures a second docs root gets one identity per file
    only if the root itself is part of the identity — including a root whose
    name starts with a dot, which is the shape this project's own ``.pev``
    root has.
    """
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs", ".pev"])
    doc_trees.write_doc(root, "docs/test-policy.json", title="From docs")
    doc_trees.write_doc(root, ".pev/test-policy.json", title="From .pev")
    _build(root)
    locations = _doc_locations(root)

    primary = doc_ids.derive_doc_id("proj", "docs", "test-policy.json")
    secondary = doc_ids.derive_doc_id("proj", ".pev", "test-policy.json")
    assert primary != secondary
    assert locations.get(primary) == "docs/test-policy.json", locations
    assert locations.get(secondary) == ".pev/test-policy.json", locations


# ---------------------------------------------------------------------------
# Tier 3 -- US-4: a migrated project rebuilds onto its migrated identities
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a rebuild immediately after a doc-id migration reconciles onto the migrated identities, inserting no new node and marking nothing NOT_FOUND",
)
def test_a_rebuild_after_migrating_inserts_no_new_node_and_retires_nothing(tmp_path):
    """The reconciliation the whole ordering rests on.

    Migrating and then rebuilding is the sequence every consumer runs once.
    If the derivation and the migration's projection disagree by so much as a
    separator, the rebuild inserts the entire doc tree a second time and
    retires the identities the migration just moved everything onto -- which
    is silent, and is exactly the damage the migration existed to avoid.
    """
    口 = Step(
        step_num=1,
        name="Index a project under the derivation the previous release shipped",
        purpose="A migration only has something to move when the index holds retired identities",
    )
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    db_path = db_path_for(root)
    with doc_trees.retired_derivation():
        _build(root)
    before = _node_ids(root)
    assert before

    口 = Step(
        step_num=2,
        name="Migrate every doc identity",
        purpose="Move the index onto the identities the current derivation produces",
    )
    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is True, result.reason
    migrated = _node_ids(root)

    口 = Step(
        step_num=3,
        name="Rebuild, with every document actually re-read",
        purpose="Re-derive every identity from disk against the migrated index",
    )
    # Touching the files defeats the mtime fast-pass.  Without this the build
    # skips every document, derives nothing, and the reconciliation assertion
    # below holds no matter how far the derivation and the projection have
    # drifted apart -- the test would pass on the bug it exists to catch.
    now = time.time() + 1
    for pattern in ("**/*.json", "**/*.md"):
        for path in root.glob(pattern):
            if ".axiom_graph" not in path.parts:
                os.utime(path, (now, now))
    summary = _build(root)
    assert summary.docs_skipped_mtime == 0, summary

    口 = Step(
        step_num=4,
        name="Assert the rebuild reconciled rather than re-inserted",
        purpose="A second copy of the doc tree shows up as new node ids",
    )
    assert _node_ids(root) == migrated

    with db._connect(db_path) as conn:
        not_found = {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM nodes WHERE own_status = 'NOT_FOUND' OR link_status = 'NOT_FOUND'",
            ).fetchall()
        }
    assert not_found == set()
