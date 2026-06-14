"""Net "changed since" state-diff tests (Option A) against real git repos.

Covers the acceptance criteria for the net-diff feature:

* (a) edit-then-revert -> node ABSENT (revert-cancel).
* (b) content edit -> content; docstring-only -> desc; both -> content+desc;
      rename -> renamed (not delete+add).
* (c) add-since-baseline -> `added`; add-then-remove cancels.
* (d) a docjson link-only edit is ABSENT from the default net list (deferral).
* (e) deleted-ghost exact-span recovery non-empty + matches baseline blob +
      legacy whole-file fallback.
* (f) the bulk path is O(changed files), not O(nodes) (git-call instrumentation).
* (g) `delete_nodes_by_location` contract preserved (see test_broken_links.py).

These enter the system at ``lifecycle.api.compute_net_diff`` /
``recover_deleted_source`` -- the same api functions the viz endpoint calls.

A note on the index's "current" side (D-2): the net diff compares the baseline
git blob against the DB's **stored** hashes. A discovery-only build marks a
changed node stale but does NOT rewrite its stored hash, so these tests use a
full re-build (``builder.build(discovery_only=False)``) after each commit to
advance the stored hashes -- mirroring the real PR-review re-index flow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from axiom_annotations import workflow, Step

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import record_staleness
from axiom_graph.lifecycle.api import compute_net_diff, recover_deleted_source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commit(project: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=project, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=project, capture_output=True, check=True)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _reindex(project: Path) -> str | None:
    """Full re-build (advances stored hashes) + staleness; return index head SHA."""
    db_path = project / ".axiom_graph" / "graph.db"
    builder.build(project, project_id="proj", discovery_only=False)
    nodes = db.all_nodes(db_path)
    if nodes:
        record_staleness(db_path, project, nodes)
    return db.get_index_head_sha(db_path)


def _node_id(db_path: Path, title: str) -> str:
    matches = [n for n in db.all_nodes(db_path) if n.title == title]
    assert matches, f"no node titled {title!r}"
    return matches[0].id


# ---------------------------------------------------------------------------
# (a) revert-cancel  +  (b) content kind
# ---------------------------------------------------------------------------


@workflow(purpose="Net diff: a content edit labels `content`; reverting it cancels the node to absent")
def test_edit_then_revert_cancels(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1, name="Commit + index a baseline function", purpose="Establish the baseline blob + stored hashes"
    )
    (git_project / "mod.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    sha_a = _commit(git_project, "baseline")
    _reindex(git_project)
    nid = _node_id(git_db_path, "greet")

    口 = Step(
        step_num=2,
        name="Edit the body, commit, re-index -> content",
        purpose="A real content edit is labelled `content`",
    )
    (git_project / "mod.py").write_text('def greet():\n    return "goodbye"\n', encoding="utf-8")
    _commit(git_project, "edit")
    head2 = _reindex(git_project)
    edited = compute_net_diff(git_db_path, git_project, sha_a, head2)
    assert nid in edited.node_ids
    assert edited.change_kinds[nid] == ["content"]

    口 = Step(
        step_num=3,
        name="Revert to the baseline body, commit, re-index -> absent",
        purpose="Reverting the edit cancels the node to absent",
    )
    (git_project / "mod.py").write_text('def greet():\n    return "hello"\n', encoding="utf-8")
    _commit(git_project, "revert")
    head3 = _reindex(git_project)
    reverted = compute_net_diff(git_db_path, git_project, sha_a, head3)
    assert nid not in reverted.node_ids
    assert reverted.change_kinds == {}


# ---------------------------------------------------------------------------
# (b) desc-only and content+desc kinds
# ---------------------------------------------------------------------------


@workflow(purpose="Net diff: a docstring-only edit labels `desc`; editing body + docstring labels `content+desc`")
def test_desc_and_content_desc_kinds(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text('def greet():\n    """Old doc."""\n    return "x"\n', encoding="utf-8")
    sha_a = _commit(git_project, "baseline")
    _reindex(git_project)
    nid = _node_id(git_db_path, "greet")

    # Docstring-only change.
    (git_project / "mod.py").write_text('def greet():\n    """New doc."""\n    return "x"\n', encoding="utf-8")
    _commit(git_project, "doc edit")
    head_desc = _reindex(git_project)
    desc = compute_net_diff(git_db_path, git_project, sha_a, head_desc)
    assert desc.change_kinds.get(nid) == ["desc"]

    # Body + docstring change (relative to the same baseline A).
    (git_project / "mod.py").write_text('def greet():\n    """Newer doc."""\n    return "y"\n', encoding="utf-8")
    _commit(git_project, "both edit")
    head_both = _reindex(git_project)
    both = compute_net_diff(git_db_path, git_project, sha_a, head_both)
    assert both.change_kinds.get(nid) == ["content+desc"]


# ---------------------------------------------------------------------------
# (b) rename -> renamed (not delete+add)
# ---------------------------------------------------------------------------


@workflow(purpose="Net diff: a file rename labels the node `renamed`, not delete+add")
def test_rename_labels_renamed(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
    sha_a = _commit(git_project, "baseline")
    _reindex(git_project)

    subprocess.run(["git", "mv", "mod.py", "renamed_mod.py"], cwd=git_project, capture_output=True, check=True)
    _commit(git_project, "rename")
    head2 = _reindex(git_project)
    nid = _node_id(git_db_path, "greet")

    out = compute_net_diff(git_db_path, git_project, sha_a, head2)
    assert out.change_kinds.get(nid) == ["renamed"]


# ---------------------------------------------------------------------------
# (c) added-since-baseline  +  add-then-remove cancels
# ---------------------------------------------------------------------------


@workflow(purpose="Net diff: a file added since the baseline labels its nodes `added`; add-then-remove cancels")
def test_added_kind_and_add_then_remove_cancels(git_project: Path, git_db_path: Path):
    (git_project / "mod.py").write_text('def greet():\n    return "hi"\n', encoding="utf-8")
    sha_a = _commit(git_project, "baseline")
    _reindex(git_project)

    # Add a brand-new file (path in git's A set).
    (git_project / "new_mod.py").write_text('def fresh():\n    return "new"\n', encoding="utf-8")
    _commit(git_project, "add file")
    head2 = _reindex(git_project)
    new_nid = _node_id(git_db_path, "fresh")

    added = compute_net_diff(git_db_path, git_project, sha_a, head2)
    assert added.change_kinds.get(new_nid) == ["added"]

    # Remove the added file again: path leaves git's A set entirely -> cancels.
    (git_project / "new_mod.py").unlink()
    _commit(git_project, "remove added file")
    head3 = _reindex(git_project)
    removed = compute_net_diff(git_db_path, git_project, sha_a, head3)
    assert new_nid not in removed.node_ids


# ---------------------------------------------------------------------------
# (d) docjson link-only edit is ABSENT from the default net list (deferral)
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Net diff deferral (acceptance d): a DocJSON section whose content is "
        "byte-identical to baseline but whose `documents` link was added is ABSENT "
        "from the default net list. Link/tag net membership rides ADR-021, not this "
        "cycle — the section hash excludes links, so the net diff sees no change."
    )
)
def test_docjson_link_only_edit_is_deferred(git_project: Path, git_db_path: Path):
    import json

    口 = Step(
        step_num=1,
        name="Commit + index a code module and a DocJSON section (no link yet)",
        purpose="Baseline: a section with fixed content and no `documents` link to mod.foo",
    )
    (git_project / "mod.py").write_text("def foo():\n    return 0\n", encoding="utf-8")
    docs_dir = git_project / "docs"
    docs_dir.mkdir()
    section_content = "This section documents the foo helper. Content is fixed across the edit."
    doc_path = docs_dir / "spec.json"

    def _write_doc(links: list[dict]) -> None:
        doc_path.write_text(
            json.dumps(
                {
                    "title": "Spec",
                    "sections": [
                        {
                            "id": "overview",
                            "heading": "Overview",
                            "content": section_content,
                            "links": links,
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    _write_doc(links=[])
    sha_a = _commit(git_project, "baseline: code + unlinked section")
    _reindex(git_project)
    section_id = _node_id(git_db_path, "Overview")

    口 = Step(
        step_num=2,
        name="Add ONLY a `documents` link; section content stays byte-identical",
        purpose=(
            "The JSON file changes (so its path is a net-diff candidate), but the "
            "section's content/desc hash is unchanged — links are excluded from the "
            "section hash."
        ),
    )
    _write_doc(links=[{"node_id": "proj::mod::foo"}])
    # Sanity: the section content text is genuinely identical across the edit.
    assert section_content in doc_path.read_text(encoding="utf-8")
    _commit(git_project, "add documents link only")
    head2 = _reindex(git_project)

    口 = Step(
        step_num=3,
        name="Net diff excludes the link-only-changed section",
        purpose="Deferral holds: the section is absent from node_ids and carries no change kind",
    )
    # Guard against a vacuous pass: the JSON file genuinely changed, so it IS a
    # net-diff *candidate* path (stage 1). The section is dropped at stage 2 (hash
    # compare), not because the file was never a candidate.
    from axiom_graph.index import git_utils

    changes = git_utils.get_name_status_changes(git_project, sha_a, head2)
    candidate_paths = changes.modified | set(changes.renamed.values()) | changes.added
    assert "docs/spec.json" in candidate_paths, (
        "test setup invalid: the doc file must be a net-diff candidate so the "
        "deferral is proven at the hash-compare stage, not by the file being absent"
    )

    out = compute_net_diff(git_db_path, git_project, sha_a, head2)
    assert section_id not in out.node_ids, (
        "A docjson link-only edit (content byte-identical) must be ABSENT from the "
        "default net list — link/tag net membership is deferred to ADR-021"
    )
    assert section_id not in out.change_kinds


# ---------------------------------------------------------------------------
# (f) O(changed files), not O(nodes)
# ---------------------------------------------------------------------------


@workflow(purpose="Net diff git-call count scales with changed files, not total node count")
def test_git_calls_scale_with_changed_files(git_project: Path, git_db_path: Path):
    # Many nodes across many files, but only one file changes after baseline.
    for i in range(6):
        (git_project / f"f{i}.py").write_text(f"def fn{i}():\n    return {i}\n", encoding="utf-8")
    sha_a = _commit(git_project, "baseline many files")
    _reindex(git_project)
    total_nodes = len(db.all_nodes(git_db_path))
    assert total_nodes >= 6

    # Change exactly one file.
    (git_project / "f0.py").write_text("def fn0():\n    return 999\n", encoding="utf-8")
    _commit(git_project, "change one file")
    head2 = _reindex(git_project)

    out = compute_net_diff(git_db_path, git_project, sha_a, head2)
    # 1 name-status call + 1 git-show per CHANGED file (just one). Never O(nodes).
    assert out.git_calls == 2
    assert out.git_calls < total_nodes


# ---------------------------------------------------------------------------
# (e) deleted-ghost source recovery: exact-span + legacy whole-file fallback
# ---------------------------------------------------------------------------


@workflow(purpose="Deleted-ghost recovery: exact-span returns the baseline blob; legacy fallback uses whole-file")
def test_recover_deleted_source_exact_and_legacy(git_project: Path, git_db_path: Path):
    body = 'def greet():\n    return "hello"\n'
    (git_project / "mod.py").write_text(body, encoding="utf-8")
    sha_a = _commit(git_project, "baseline")

    # Exact-span recovery: preserved SHA + span -> sliced baseline source.
    recovered = recover_deleted_source(
        git_project,
        "mod.py",
        preserved_sha=sha_a,
        preserved_level_3="mod.py#L1-L2",
        baseline_sha=None,
    )
    assert recovered is not None
    assert recovered.strip() != ""
    assert "def greet" in recovered
    # The exact span is the whole 2-line file here.
    assert recovered == body

    # Legacy fallback (no preserved span/SHA): whole-file via baseline_sha.
    legacy = recover_deleted_source(
        git_project,
        "mod.py",
        preserved_sha=None,
        preserved_level_3=None,
        baseline_sha=sha_a,
    )
    assert legacy is not None
    assert legacy == body

    # Fully unreachable -> None, never a crash.
    missing = recover_deleted_source(
        git_project,
        "nonexistent.py",
        preserved_sha=None,
        preserved_level_3=None,
        baseline_sha=sha_a,
    )
    assert missing is None


# ---------------------------------------------------------------------------
# (e/g) deletion-path meta enrichment: span + SHA preserved in DELETED meta
# ---------------------------------------------------------------------------


@workflow(purpose="A purged node's DELETED history preserves its level_3_location span and the build SHA")
def test_deleted_meta_preserves_span_and_sha(git_project: Path, git_db_path: Path):
    import json

    (git_project / "gone.py").write_text("def doomed():\n    return 1\n", encoding="utf-8")
    _commit(git_project, "add doomed")
    _reindex(git_project)
    nid = _node_id(git_db_path, "doomed")

    # Delete the file and re-index -> the purge writes a preserved DELETED row.
    (git_project / "gone.py").unlink()
    sha_del = _commit(git_project, "delete doomed")
    _reindex(git_project)

    rows = db.get_history(git_db_path, nid, limit=50)
    deleted = [r for r in rows if r["change_type"] == "DELETED"]
    assert deleted, "expected a preserved DELETED history row"
    meta = json.loads(deleted[0]["meta"])
    # Span preserved for exact-span recovery.
    assert meta.get("level_3_location"), "DELETED meta must carry the level_3_location span"
    # SHA preserved both in meta and in the git_sha column.
    assert meta.get("git_sha"), "DELETED meta must carry the deletion-time SHA"
    assert deleted[0]["git_sha"], "DELETED row's git_sha column must be populated"
