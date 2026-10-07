"""Tests for the doc-ID prose sweep and its two guardrails.

Tier 3 -- @workflow + Step():
    Stale references in DocJSON content and in an ordinary repository file
    are rewritten, a file carrying uncommitted changes is skipped and named,
    an unresolvable placeholder is left alone, and a DocJSON-content review
    report comes back.

Tier 2 -- @workflow(purpose=...):
    A reference naming a section has only its document half rewritten; the
    section path survives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.config import db_path_for
from axiom_graph.lifecycle import api as lifecycle_api

from tests.fixtures import doc_trees


def _git(root: Path, *args: str) -> None:
    """Run a git command in *root*, raising on failure."""
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_everything(root: Path) -> None:
    """Initialise a repository at *root* and commit every file in it."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "sweep@example.invalid")
    _git(root, "config", "user.name", "Sweep Fixture")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")


def _mapping(root: Path) -> dict[str, str]:
    """Return the old -> new document mapping the migration would apply."""
    return lifecycle_api.plan_doc_id_migration(db_path_for(root), root).as_mapping()


# ---------------------------------------------------------------------------
# Tier 3 -- US-5: the sweep, with both guardrails, in one flow
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify the prose sweep rewrites resolvable references in DocJSON content and in ordinary repository files, skips and names files carrying uncommitted changes, leaves unresolvable placeholders alone, and returns a DocJSON-content review report",
)
def test_the_sweep_rewrites_committed_prose_and_skips_everything_it_cannot_undo(tmp_path):
    """The guardrails are only worth anything together.

    Rewriting ~1,500 sites of prose that no test can verify is acceptable
    exactly because the run is revertible and reviewable.  A sweep that
    rewrote an uncommitted file would destroy work with no undo; a sweep
    with no report would leave a person no way to check the result.
    """
    口 = Step(
        step_num=1,
        name="Build a tree carrying every reference kind",
        purpose="DocJSON prose, an ordinary repo file, a dirty file, and a placeholder",
    )
    root = doc_trees.prose_references(tmp_path / "proj")
    doc_trees.write_raw(
        root,
        "skills/placeholder.md",
        "Call read_doc on proj::docs.no-such-document for an example.\n",
    )
    doc_trees.write_raw(root, "notes/uncommitted.md", "Placeholder, replaced below.\n")
    _commit_everything(root)

    口 = Step(
        step_num=2,
        name="Leave one file with uncommitted changes",
        purpose="The file the sweep must refuse to touch",
    )
    (root / "notes" / "uncommitted.md").write_text(
        "Work in progress about proj::docs.target that is not committed.\n",
        encoding="utf-8",
    )

    口 = Step(
        step_num=3,
        name="Sweep",
        purpose="Apply the migration's mapping to everything the migration cannot reach",
    )
    mapping = _mapping(root)
    result = lifecycle_api.sweep_doc_id_prose(root, mapping)

    口 = Step(
        step_num=4,
        name="Assert the committed references moved",
        purpose="DocJSON section content and an ordinary repository file are both in scope",
    )
    guide = (root / "docs" / "guide.json").read_text(encoding="utf-8")
    assert mapping["proj::docs.target"] in guide
    assert "Read proj::docs.target::payload before" not in guide

    readme = (root / "README.md").read_text(encoding="utf-8")
    assert readme.strip() == f"See {mapping['proj::docs.target']} for the overview."

    assert "docs/guide.json" in result.files_rewritten
    assert "README.md" in result.files_rewritten

    口 = Step(
        step_num=5,
        name="Assert the uncommitted file was skipped and named",
        purpose="Skipped means untouched on disk and reported, not silently dropped",
    )
    assert result.skipped_uncommitted == ["notes/uncommitted.md"]
    assert "notes/uncommitted.md" not in result.files_rewritten
    assert "proj::docs.target" in (root / "notes" / "uncommitted.md").read_text(encoding="utf-8")

    口 = Step(
        step_num=6,
        name="Assert the placeholder was left exactly as written",
        purpose="A reference naming no document has no target to be rewritten to",
    )
    placeholder = (root / "skills" / "placeholder.md").read_text(encoding="utf-8")
    assert "proj::docs.no-such-document" in placeholder
    assert [ref.text for ref in result.unresolved] == ["proj::docs.no-such-document"]

    口 = Step(
        step_num=7,
        name="Assert a review report came back",
        purpose="The sweep touches prose no test can judge, so a person reviews it",
    )
    report = result.report
    assert "docs/guide.json" in report
    assert mapping["proj::docs.target"] in report
    assert "notes/uncommitted.md" in report
    assert "proj::docs.no-such-document" in report


# ---------------------------------------------------------------------------
# Tier 2 -- US-5: only the document half of a section reference moves
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a prose reference naming a section has only its document half rewritten, with the section dot-path carried across unchanged",
)
def test_a_section_reference_keeps_its_section_path(tmp_path):
    """A document ID is a prefix of every one of its section IDs.

    A rewrite that matched the document half greedily would either truncate
    the section path or, matching the shorter token first, leave a half-moved
    identity behind that resolves to nothing.
    """
    root = doc_trees.prose_references(tmp_path / "proj")
    _commit_everything(root)

    mapping = _mapping(root)
    result = lifecycle_api.sweep_doc_id_prose(root, mapping)

    section_rewrites = [r for r in result.rewrites if r.old_text.endswith("::payload")]
    assert len(section_rewrites) == 1, result.rewrites
    rewrite = section_rewrites[0]
    assert rewrite.old_text == "proj::docs.target::payload"
    assert rewrite.new_text == f"{mapping['proj::docs.target']}::payload"

    guide = (root / "docs" / "guide.json").read_text(encoding="utf-8")
    assert f"Read {mapping['proj::docs.target']}::payload before starting." in guide


# ---------------------------------------------------------------------------
# Tier 1 -- the sweep never writes when git cannot say what is committed
# ---------------------------------------------------------------------------


def test_a_tree_that_is_not_a_repository_is_swept_by_nobody(tmp_path):
    """Not knowing which files are committed is not a licence to rewrite them."""
    root = doc_trees.prose_references(tmp_path / "proj")
    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root))

    assert result.files_rewritten == []
    assert result.references_rewritten == 0
    assert "README.md" in result.skipped_uncommitted
    assert "proj::docs.target" in (root / "README.md").read_text(encoding="utf-8")


def test_a_file_that_goes_unreadable_before_the_write_is_not_counted_as_rewritten(tmp_path, monkeypatch):
    """The report's count is the artifact a person checks, so it may not over-claim.

    A file readable when the sweep scans it and unreadable when the sweep
    comes back to write it is never written; counting its references among the
    applied ones would report work that did not happen.
    """
    root = doc_trees.prose_references(tmp_path / "proj")
    _commit_everything(root)
    mapping = _mapping(root)

    target = (root / "README.md").resolve()
    real_read_text = Path.read_text
    reads: list[Path] = []

    def _readable_once(self, *args, **kwargs):
        if self.resolve() == target:
            reads.append(self)
            if len(reads) > 1:
                raise OSError("file went away between the scan and the write")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _readable_once)
    result = lifecycle_api.sweep_doc_id_prose(root, mapping)
    monkeypatch.undo()

    assert "README.md" not in result.files_rewritten
    assert [r.file_path for r in result.rewrites if r.file_path == "README.md"] == []
    assert result.references_rewritten == len(result.rewrites)
    assert "proj::docs.target" in (root / "README.md").read_text(encoding="utf-8")

    # Everything readable was still swept.
    assert "docs/guide.json" in result.files_rewritten


def test_a_reference_repeated_on_one_line_is_reported_once_with_its_count(tmp_path):
    """A DocJSON section's content is one JSON line, so a repeat shares a line.

    Listing each occurrence would give the reviewer two identical entries and
    a header count that overstates what there is to check.
    """
    root = doc_trees.prose_references(tmp_path / "proj")
    doc_trees.write_doc(
        root,
        "docs/twice.json",
        title="Twice",
        sections=[
            {
                "id": "body",
                "heading": "Body",
                "content": "Start at proj::docs.target, then come back to proj::docs.target.",
            }
        ],
    )
    _commit_everything(root)

    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root), dry_run=True)

    repeated = [r for r in result.rewrites if r.file_path == "docs/twice.json"]
    assert len(repeated) == 2, "every occurrence is still recorded"

    section = result.report.split("### docs/twice.json", 1)[1].split("###", 1)[0]
    entries = [line for line in section.splitlines() if line.startswith("- L")]
    assert len(entries) == 1, section
    assert entries[0].endswith("`proj::docs.target`  (×2)")

    distinct_in_docjson = len({r for r in result.rewrites if r.in_docjson_content})
    assert f"DocJSON section content : {distinct_in_docjson} reference(s)" in result.report


def test_a_dry_run_reports_everything_and_writes_nothing(tmp_path):
    """The default mode: see the rewrites before any of them land."""
    root = doc_trees.prose_references(tmp_path / "proj")
    _commit_everything(root)
    before = (root / "README.md").read_text(encoding="utf-8")

    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root), dry_run=True)

    assert result.references_rewritten > 0
    assert result.files_rewritten == []
    assert (root / "README.md").read_text(encoding="utf-8") == before
    assert "DRY RUN" in result.report


# ---------------------------------------------------------------------------
# Tier 1 -- the sweep reads and writes only files git tracks
# ---------------------------------------------------------------------------


def test_an_ignored_directory_and_a_nested_checkout_under_it_are_neither_reported_nor_written(tmp_path):
    """``git checkout`` cannot restore an ignored file, so the sweep may not touch one.

    A nested worktree under an ignored path is another branch's checkout; a
    build directory is generated output.  Both hold resolvable references and
    both look committed to ``git status --porcelain``, which never lists them.
    """
    root = doc_trees.prose_references(tmp_path / "proj")
    doc_trees.write_raw(root, ".gitignore", "worktrees/\n_build/\n")
    doc_trees.write_raw(root, "_build/page.html", "Rendered from proj::docs.target.\n")
    _commit_everything(root)

    nested = root / "worktrees" / "other-branch"
    doc_trees.write_raw(nested, "notes.md", "Another branch reads proj::docs.target.\n")
    _commit_everything(nested)

    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root))

    touched = {r.file_path for r in result.rewrites} | {r.file_path for r in result.unresolved}
    assert not any(p.startswith(("worktrees/", "_build/")) for p in touched), touched
    assert "proj::docs.target" in (nested / "notes.md").read_text(encoding="utf-8")
    assert "proj::docs.target" in (root / "_build" / "page.html").read_text(encoding="utf-8")
    assert "README.md" in result.files_rewritten


def test_an_untracked_file_is_neither_reported_nor_written(tmp_path):
    """An untracked file has no committed version to restore it to."""
    root = doc_trees.prose_references(tmp_path / "proj")
    _commit_everything(root)
    doc_trees.write_raw(root, "scratch/untracked.md", "Draft mentioning proj::docs.target.\n")

    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root))

    assert all(r.file_path != "scratch/untracked.md" for r in result.rewrites)
    assert all(r.file_path != "scratch/untracked.md" for r in result.unresolved)
    assert "scratch/untracked.md" not in result.files_rewritten
    assert "scratch/untracked.md" not in result.skipped_uncommitted
    assert "proj::docs.target" in (root / "scratch" / "untracked.md").read_text(encoding="utf-8")
    assert "README.md" in result.files_rewritten


def test_an_ignored_document_under_a_docs_root_is_not_swept(tmp_path):
    """The DocJSON-content bucket obeys the same rule as every other file."""
    root = doc_trees.prose_references(tmp_path / "proj")
    doc_trees.write_raw(root, ".gitignore", "docs/generated/\n")
    doc_trees.write_doc(
        root,
        "docs/generated/copy.json",
        title="Copy",
        sections=[{"id": "body", "heading": "Body", "content": "Mirrors proj::docs.target."}],
    )
    _commit_everything(root)

    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root))

    assert all(r.file_path != "docs/generated/copy.json" for r in result.rewrites)
    assert "proj::docs.target" in (root / "docs" / "generated" / "copy.json").read_text(encoding="utf-8")
    assert "docs/guide.json" in result.files_rewritten


# ---------------------------------------------------------------------------
# Tier 1 -- the uncommitted-changes guard sees every dirty file
# ---------------------------------------------------------------------------


def test_a_dirty_file_with_a_non_ascii_name_is_skipped(tmp_path):
    """Git escapes non-ASCII paths in its default output; the guard must still match them."""
    root = doc_trees.prose_references(tmp_path / "proj")
    doc_trees.write_raw(root, "notes/café.md", "Committed text.\n")
    _commit_everything(root)
    (root / "notes" / "café.md").write_text("Edited, mentions proj::docs.target.\n", encoding="utf-8")

    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root))

    assert "notes/café.md" in result.skipped_uncommitted
    assert "notes/café.md" not in result.files_rewritten
    assert "proj::docs.target" in (root / "notes" / "café.md").read_text(encoding="utf-8")


def test_a_dirty_file_is_skipped_when_the_project_sits_inside_a_larger_repository(tmp_path):
    """Git reports paths from the repository root; the sweep works from the project root."""
    repo = tmp_path / "monorepo"
    root = doc_trees.prose_references(repo / "packages" / "proj")
    _commit_everything(repo)
    (root / "README.md").write_text("Uncommitted edit about proj::docs.target.\n", encoding="utf-8")

    result = lifecycle_api.sweep_doc_id_prose(root, _mapping(root))

    assert "README.md" in result.skipped_uncommitted
    assert "README.md" not in result.files_rewritten
    assert (root / "README.md").read_text(encoding="utf-8").startswith("Uncommitted edit")
    assert "docs/guide.json" in result.files_rewritten
