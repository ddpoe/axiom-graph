"""End-to-end regression tests for the consolidated current-node-hash primitive.

These tests reproduce the two failure modes that drove the
``pev-2026-05-02-node-hashing-consolidation`` cycle:

* **US-2** Sibling-class methods (``TestA.test_foo`` vs
  ``TestB.test_foo``) used to share a single ``code_hash`` because the
  buggy implementations keyed by short name -- last-walker-wins.  When
  one was edited, *both* flipped to ``CONTENT_UPDATED`` even though only
  one had actually changed.  The new primitive resolves by qualified
  name; the unedited sibling stays ``VERIFIED`` end-to-end.

* **US-3** Envelopes whose ``id`` carries the literal ``@workflow``
  suffix even when the decorator is ``@task`` (e.g. ``get_stale_tests``
  decorated as ``@task`` -> id ``...::get_stale_tests@workflow``,
  subtype ``'task'``) were chronically stuck ``CONTENT_UPDATED`` because
  the legacy ``compute_current_hashes`` had no envelope branch and the
  legacy ``compute_staleness`` keyed on title (``"get_stale_tests
  @task"`` -- with a space).  After the consolidation, mark_clean ->
  compute_staleness round-trip is ``VERIFIED``.

Tests use ``builder.build()`` for index seeding (per ADR-019 layering
lint -- direct ``db.upsert_node(... discovery_only=False)`` calls are
forbidden in new test files).
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom_graph.index import builder, db
from axiom_graph.index.mark_clean import mark_node_clean
from axiom_graph.index.staleness import compute_staleness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(project: Path) -> None:
    """Full build of ``project``; populates the DB."""
    builder.build(project, project_id="proj", discovery_only=False)


def _find_node_by_suffix(db_path: Path, id_suffix: str):
    """Find a node whose id ends with ``id_suffix``."""
    for n in db.all_nodes(db_path):
        if n.id.endswith(id_suffix):
            return n
    raise AssertionError(f"No node ending in {id_suffix!r} found")


def _bump_mtime(file_path: Path, delta: float = 10.0) -> None:
    """Bump file mtime forward to defeat any mtime-fast-pass shortcut."""
    stat = file_path.stat()
    os.utime(file_path, (stat.st_atime + delta, stat.st_mtime + delta))


# ---------------------------------------------------------------------------
# US-2: Sibling-class methods do not falsely flip together (end-to-end).
# ---------------------------------------------------------------------------


class TestSiblingClassCollisionRegression:
    """The headline bug: TestA.test_foo edit must not flip TestB.test_foo."""

    def test_only_edited_sibling_flips_to_content_updated(self, mini_project: Path) -> None:
        """Edit ``TestA.test_foo``; ``TestB.test_foo`` stays VERIFIED."""
        db_path = mini_project / ".axiom_graph" / "graph.db"

        src_dir = mini_project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        py_file = src_dir / "mod.py"
        py_file.write_text(
            "class TestA:\n"
            "    def test_foo(self):\n"
            "        return 1\n"
            "\n"
            "class TestB:\n"
            "    def test_foo(self):\n"
            "        return 2\n",
            encoding="utf-8",
        )

        # Build seeds the DB with the correct baseline hashes for both
        # sibling-class methods.
        _build(mini_project)

        a_node = _find_node_by_suffix(db_path, "::TestA.test_foo")
        b_node = _find_node_by_suffix(db_path, "::TestB.test_foo")

        # Sanity: the build produced distinct hashes for the two siblings.
        assert a_node.code_hash != b_node.code_hash, "primitive regressed: sibling methods share a hash at build time"

        # Edit ONLY TestA.test_foo's body -- TestB stays unchanged.
        py_file.write_text(
            "class TestA:\n"
            "    def test_foo(self):\n"
            "        return 999  # changed body\n"
            "\n"
            "class TestB:\n"
            "    def test_foo(self):\n"
            "        return 2\n",
            encoding="utf-8",
        )
        _bump_mtime(py_file)

        # Re-read the nodes after build (location/mtime may have updated).
        a_node = _find_node_by_suffix(db_path, "::TestA.test_foo")
        b_node = _find_node_by_suffix(db_path, "::TestB.test_foo")

        statuses = compute_staleness(db_path, mini_project, [a_node, b_node])

        # Expect: A flipped, B is still VERIFIED.
        assert statuses[a_node.id][0] == "CONTENT_UPDATED", (
            "TestA.test_foo should be CONTENT_UPDATED after its body changed"
        )
        assert statuses[b_node.id][0] == "VERIFIED", (
            "TestB.test_foo MUST remain VERIFIED when only TestA changed -- "
            "this is the sibling-class regression the cycle exists to fix."
        )


# ---------------------------------------------------------------------------
# US-3: Workflow / task envelope round-trip cleanly through mark_clean.
# ---------------------------------------------------------------------------


class TestEnvelopeRoundTripRegression:
    """Envelope mark_clean -> compute_staleness must land on VERIFIED.

    The legacy ``compute_current_hashes`` had no envelope branch, so it
    returned the stored hash unchanged.  ``mark_node_clean`` then wrote
    that *stored* hash as the new baseline -- and ``compute_staleness``
    immediately recomputed the *envelope* hash and saw a mismatch,
    flipping back to ``CONTENT_UPDATED`` on the very next check.
    """

    def test_task_envelope_with_workflow_id_suffix_round_trips(self, mini_project: Path) -> None:
        """The exact failure from rows 6 & 7 of the stuck-node list.

        ``get_stale_tests`` is decorated with ``@task`` so its node
        carries ``subtype='task'`` -- but its id ends in the literal
        ``@workflow`` suffix (the envelope id convention).  Title is
        ``"get_stale_tests @task"`` (with a space).
        """
        db_path = mini_project / ".axiom_graph" / "graph.db"

        src_dir = mini_project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        py_file = src_dir / "mod.py"
        py_file.write_text(
            "from axiom_annotations import task\n"
            "\n"
            "@task(\n"
            '    purpose="Compute stale tests",\n'
            '    inputs="db_path",\n'
            '    outputs="list",\n'
            ")\n"
            "def get_stale_tests():\n"
            '    """Doc."""\n'
            "    return []\n",
            encoding="utf-8",
        )

        _build(mini_project)

        # Locate the envelope (id ends @workflow even though decorator is @task).
        env_node = _find_node_by_suffix(db_path, "::get_stale_tests@workflow")
        assert env_node.subtype == "task", f"Expected subtype=task (decorator is @task); got {env_node.subtype}"

        # Corrupt the baseline to mirror production state of the 7 stuck nodes.
        # We use mark_node_clean against a node whose stored hash is wrong --
        # the round-trip must recover.
        env_node.code_hash = "OLD_WRONG_HASH"

        # Step 1: mark the envelope clean.  This should write the
        # *real* envelope_code_hash as the new baseline (legacy bug:
        # writes the stale stored hash unchanged).
        mark_node_clean(
            db_path,
            mini_project,
            env_node,
            reason="cycle pev-2026-05-02 fix",
            verified_by="agent:pev-builder",
        )

        # Step 2: re-read the node -- baseline should be updated.
        updated = db.get_node(db_path, env_node.id)
        assert updated.code_hash != "OLD_WRONG_HASH", (
            "mark_clean failed to update baseline -- envelope dispatch is broken."
        )

        # Step 3: compute_staleness must now report VERIFIED, and stay
        # VERIFIED on a follow-up call (no flip-back).
        statuses_first = compute_staleness(db_path, mini_project, [updated])
        assert statuses_first[updated.id][0] == "VERIFIED", "Envelope failed to land VERIFIED right after mark_clean."

        statuses_second = compute_staleness(db_path, mini_project, [updated])
        assert statuses_second[updated.id][0] == "VERIFIED", (
            "Envelope flipped back to CONTENT_UPDATED on second check -- "
            "this is the chronic-churn regression the cycle exists to fix."
        )

    def test_workflow_envelope_round_trips(self, mini_project: Path) -> None:
        """Same scenario for a ``@workflow`` (subtype='workflow') envelope."""
        db_path = mini_project / ".axiom_graph" / "graph.db"

        src_dir = mini_project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        py_file = src_dir / "mod.py"
        py_file.write_text(
            "from axiom_annotations import workflow\n"
            "\n"
            '@workflow(purpose="orchestrate")\n'
            "def big_pipeline():\n"
            "    return 1\n",
            encoding="utf-8",
        )

        _build(mini_project)

        env_node = _find_node_by_suffix(db_path, "::big_pipeline@workflow")
        env_node.code_hash = "STALE"

        mark_node_clean(
            db_path,
            mini_project,
            env_node,
            reason="round-trip",
            verified_by="agent:pev-builder",
        )

        updated = db.get_node(db_path, env_node.id)
        statuses = compute_staleness(db_path, mini_project, [updated])
        assert statuses[updated.id][0] == "VERIFIED"
        # Second call -- proves no flip-back.
        statuses_again = compute_staleness(db_path, mini_project, [updated])
        assert statuses_again[updated.id][0] == "VERIFIED"
