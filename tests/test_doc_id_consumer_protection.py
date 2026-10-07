"""Tests for the two doc-namespace consumer protections and for idempotence.

Tier 3 -- @workflow + Step():
    A build against an index holding a doc namespace this code no longer
    derives is refused, names the way out, and writes nothing.

Tier 2 -- @workflow(purpose=...):
    The CLI prints the refusal as a clean error naming both migration
    commands, not a traceback.  The refusal clears once the migration has run and never fires on a
    project with nothing indexed; a second ``doc-ids execute`` reports that
    there is nothing to do and takes no backup; a structurally malformed
    stored id is warned about and skipped without stopping the build or
    deciding the verdict for the rest of the tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli
from axiom_graph.config import db_path_for
from axiom_graph.index import db
from axiom_graph.index import doc_ids
from axiom_graph.lifecycle import api as lifecycle_api
from axiom_graph.models import AxiomNode

from tests.fixtures import doc_trees


def _build(root):
    """Build an index for *root* and return the typed build summary."""
    return lifecycle_api.build_index(db_path_for(root), root, discovery_only=True)


def _rows(db_path: Path, table: str) -> list[tuple]:
    """Return every row of *table*, ordered by id, as plain tuples."""
    with db._connect(db_path) as conn:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]


def _node_ids(db_path: Path) -> set[str]:
    """Return every node ID in the index."""
    with db._connect(db_path) as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM nodes").fetchall()}


def _document_bytes(root: Path) -> dict[str, bytes]:
    """Return the on-disk bytes of every document file under *root*."""
    out: dict[str, bytes] = {}
    for pattern in ("**/*.json", "**/*.md"):
        for path in sorted(root.glob(pattern)):
            if ".axiom_graph" in path.parts:
                continue
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _backups(db_path: Path) -> list[str]:
    """Return the names of every pre-migration backup beside the index."""
    return sorted(p.name for p in db_path.parent.glob(f"{db_path.name}.pre-doc-id-migration-*"))


# ---------------------------------------------------------------------------
# Reproducing the consumer-upgrade situation
# ---------------------------------------------------------------------------


def _upgraded_doc_id(project_id: str, root_entry: str, rel_to_root: str) -> str:
    """A DocJSON identity under a shape no released derivation produces."""
    return f"{project_id}::upgraded/{root_entry}/{rel_to_root.removesuffix('.json')}"


def _upgraded_markdown_doc_id(project_id: str, root_entry: str, rel_to_root: str) -> str:
    """A Markdown identity under a shape no released derivation produces."""
    return f"{project_id}::upgraded/{root_entry}/{rel_to_root}"


def _upgrade_the_derivation(monkeypatch) -> None:
    """Make the live rule differ from the one the existing index was built with.

    An upgraded consumer is a project whose index holds the identities the
    *previous* release derived while the running code derives different ones.
    Pinning the frozen rule to whatever the index was actually built with, and
    the live rule to a shape no release produces, reproduces that mismatch
    without depending on which derivation happens to be compiled in -- so
    these tests assert the gate's behaviour rather than the value of the rule
    underneath it.

    Args:
        monkeypatch: The pytest fixture the substitutions are registered on.
    """
    built_with = doc_ids.derive_doc_id
    built_with_markdown = doc_ids.derive_markdown_doc_id
    monkeypatch.setattr(
        doc_ids,
        "current_doc_id",
        lambda project_id, f: built_with(project_id, f.root_entry, f.rel_to_root),
    )
    monkeypatch.setattr(
        doc_ids,
        "current_markdown_doc_id",
        lambda project_id, f: built_with_markdown(project_id, f.root_entry, f.rel_to_root),
    )
    monkeypatch.setattr(doc_ids, "derive_doc_id", _upgraded_doc_id)
    monkeypatch.setattr(doc_ids, "derive_markdown_doc_id", _upgraded_markdown_doc_id)
    monkeypatch.setattr(
        doc_ids,
        "projected_doc_id",
        lambda project_id, f: _upgraded_doc_id(project_id, f.root_entry, f.rel_to_root),
    )
    monkeypatch.setattr(
        doc_ids,
        "projected_markdown_doc_id",
        lambda project_id, f: _upgraded_markdown_doc_id(project_id, f.root_entry, f.rel_to_root),
    )


def _insert_malformed_doc_node(db_path: Path, node_id: str) -> None:
    """Put a document envelope carrying a structurally unusable ID in the index.

    Args:
        db_path: Path to the axiom-graph DB.
        node_id: The malformed identity to store.
    """
    node = AxiomNode(
        id=node_id,
        node_type="composite_process",
        subtype="markdown_doc",
        title="Corrupt envelope",
        location="docs/corrupt.md",
        source="doc_scanner",
        code_hash="0" * 16,
        level_0="Corrupt envelope",
        level_1="Corrupt envelope",
    )
    with db._connect(db_path) as conn:
        db.upsert_node_conn(conn, node)


# ---------------------------------------------------------------------------
# Tier 3 -- US-2: an unmigrated index is refused, and nothing is written
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a build against an index holding a retired doc namespace is refused with the migration command named, and that no node row, no docs row and no document on disk is changed",
)
def test_an_unmigrated_index_refuses_the_build_and_changes_nothing(tmp_path, monkeypatch):
    """Refusing is only worth anything if the refusal costs the operator nothing.

    ``docs`` rows are the half that can be lost quietly: the scan loop upserts
    them as it goes, so a gate placed after the loop would already have
    written new-namespace rows by the time it refused.  Asserting the table is
    byte-for-byte unchanged is what makes this test able to fail on that.
    """
    口 = Step(
        step_num=1,
        name="Index the project under the derivation of the day",
        purpose="Produce the index a consumer would be holding before they upgrade",
    )
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    db_path = db_path_for(root)
    _build(root)
    assert _node_ids(db_path)

    口 = Step(
        step_num=2,
        name="Snapshot everything the refusal promises not to touch",
        purpose="Capture the node table, the docs table, and the documents on disk",
    )
    nodes_before = _rows(db_path, "nodes")
    docs_before = _rows(db_path, "docs")
    files_before = _document_bytes(root)
    assert docs_before

    口 = Step(
        step_num=3,
        name="Upgrade to a release that derives different identities",
        purpose="Put the stored namespace and the live namespace out of step, as an upgrade does",
    )
    _upgrade_the_derivation(monkeypatch)

    口 = Step(
        step_num=4,
        name="Build, and assert the refusal names the way out",
        purpose="A refusal that does not say how to proceed leaves the operator stuck",
    )
    with pytest.raises(doc_ids.DocIdNamespaceError) as excinfo:
        _build(root)
    message = str(excinfo.value)
    assert "doc-ids execute" in message
    assert "Nothing was written" in message

    口 = Step(
        step_num=5,
        name="Assert the index and the tree are exactly as they were",
        purpose="The docs table is the clause a gate in the wrong place would fail",
    )
    assert _rows(db_path, "docs") == docs_before
    assert _rows(db_path, "nodes") == nodes_before
    assert _document_bytes(root) == files_before


# ---------------------------------------------------------------------------
# Tier 2 -- US-2: the CLI prints the refusal as an error, not a traceback
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify axiom-graph build against an unmigrated index exits non-zero with a clean error that names doc-ids preview and doc-ids execute for the project, rather than raising a traceback",
)
def test_the_build_command_reports_the_refusal_as_a_clean_error(tmp_path, monkeypatch):
    """An operator reads the refusal in a terminal, so it has to read as one.

    Click prints an unhandled exception as a traceback; a ``ClickException``
    is printed as ``Error: <message>`` with exit status 1.  The runner keeps
    the original exception on the result, which is what tells the two apart.
    """
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    _build(root)
    _upgrade_the_derivation(monkeypatch)

    result = CliRunner().invoke(cli, ["build", str(root)])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), f"the refusal escaped as {result.exception!r}"
    message = result.stderr
    assert message.startswith("Error: "), message
    assert f"axiom-graph doc-ids preview {root}" in message
    assert f"axiom-graph doc-ids execute {root}" in message
    assert message.index("doc-ids preview") < message.index("doc-ids execute"), "preview is the first step"
    assert "Nothing was written" in message


# ---------------------------------------------------------------------------
# Tier 2 -- US-2: the gate is a predicate over data, not a tripwire
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify the doc-namespace refusal clears permanently once the migration has run, and that a project with nothing indexed yet is never gated",
)
def test_the_gate_clears_after_migrating_and_a_fresh_project_is_never_gated(tmp_path, monkeypatch):
    """Two halves of the same claim: the gate fires on a condition, and it ends.

    A durable objection to a build-time refusal is that it becomes a permanent
    tripwire.  It cannot be one: after the migration no file's retired
    identity is in the index, so the condition it tests is false forever.
    """
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    db_path = db_path_for(root)
    _build(root)
    _upgrade_the_derivation(monkeypatch)

    blocked = lifecycle_api.check_doc_id_reconciliation(db_path, root)
    assert blocked.state == doc_ids.RECONCILIATION_UNMIGRATED
    assert blocked.blocked is True

    assert lifecycle_api.execute_doc_id_migration(db_path, root).executed is True

    cleared = lifecycle_api.check_doc_id_reconciliation(db_path, root)
    assert cleared.state == doc_ids.RECONCILIATION_CLEAR
    assert cleared.blocked is False

    # And the build the gate refused now runs, reconciling onto the migrated
    # identities rather than inserting a second copy of the doc tree.
    ids_before = _node_ids(db_path)
    _build(root)
    assert _node_ids(db_path) == ids_before

    # A project with nothing indexed has nothing to reconcile.  ``no_index``
    # is a verdict of its own rather than a weak ``clear``, and it must never
    # stand between a new project and its first build.
    fresh = doc_trees.clean_multi_root(tmp_path / "fresh")
    fresh_db = db_path_for(fresh)
    assert not fresh_db.exists()
    assert lifecycle_api.check_doc_id_reconciliation(fresh_db, fresh).state == doc_ids.RECONCILIATION_NO_INDEX
    _build(fresh)
    assert _node_ids(fresh_db)


# ---------------------------------------------------------------------------
# Tier 2 -- US-2: execute is idempotent, and says so
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a second doc-ids execute reports that there is nothing to do, takes no backup, and leaves the index untouched",
)
def test_a_second_execute_reports_nothing_to_do_and_takes_no_backup(tmp_path, monkeypatch):
    """Being harmless is not the same as being reported as a no-op.

    A second run always found no rows to clone, so it never did damage -- but
    it still copied the database and announced N documents migrated, which
    reads exactly like a second migration having happened.  The pre-check is
    what turns that into an answer.
    """
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    db_path = db_path_for(root)
    _build(root)
    _upgrade_the_derivation(monkeypatch)

    first = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert first.executed is True
    assert first.documents_migrated > 0
    assert first.backup_path is not None
    backups_after_first = _backups(db_path)
    nodes_after_first = _rows(db_path, "nodes")

    second = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert second.executed is False
    assert second.reason == "already_migrated"
    assert second.documents_migrated == 0
    # No backup at all: the answer is reached before the copy, which is what
    # separates "there was nothing to do" from "it did nothing".
    assert second.backup_path is None
    assert _backups(db_path) == backups_after_first
    assert _rows(db_path, "nodes") == nodes_after_first


# ---------------------------------------------------------------------------
# Tier 2 -- US-2: the malformed-ID advisory warns, skips, and never decides
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a structurally malformed doc id in the index produces a clear build warning and is skipped without stopping the build or deciding the doc-namespace verdict for the rest of the tree",
)
def test_a_malformed_doc_id_is_warned_about_and_skipped_without_stopping_the_build(tmp_path, monkeypatch):
    """The advisory protects a corrupt index; the gate protects an unmigrated one.

    Keeping them apart matters in both directions: a malformed row must not
    stop a build, and excluding it must not empty the set the gate reasons
    over -- otherwise one corrupt row silently disarms the refusal for every
    other document in the tree.
    """
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    db_path = db_path_for(root)
    _build(root)
    _insert_malformed_doc_node(db_path, "no-project-separator-at-all")

    summary = _build(root)
    advisories = [w for w in summary.warnings if "malformed doc id" in w]
    assert len(advisories) == 1, summary.warnings
    assert "no-project-separator-at-all" in advisories[0]

    # Skipped rather than fatal: the build ran to completion and the documents
    # standing beside the corrupt row indexed exactly as they always do.
    expected = {
        doc_ids.derive_doc_id("proj", "docs", "alpha.json"),
        doc_ids.derive_doc_id("proj", "specs", "beta.json"),
    }
    assert expected <= set(db.all_doc_ids(db_path))

    # And it does not decide the verdict for the tree: a genuine namespace
    # mismatch standing beside it is still refused.
    _insert_malformed_doc_node(db_path, "no-project-separator-at-all")
    _upgrade_the_derivation(monkeypatch)
    with pytest.raises(doc_ids.DocIdNamespaceError):
        _build(root)
