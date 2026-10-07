"""Behavioural tests for the ``.docjson`` document extension.

Converting a document from ``.json`` to ``.docjson`` must keep it the same
document, and ``axiom-graph doc-ids rename-extension`` must convert a tree
safely.  Every scenario runs in a real git repository built under
``tmp_path``; documents are written from Python, indexed and edited through
the api layer.

Tier 3 -- @workflow + Step():
    A ``git mv`` to ``.docjson`` keeps every identity, history row and
    verification; the rename command previews, then converts a tree.

Tier 2 -- @workflow(purpose=...):
    The rename command refuses, naming the cause, and renames nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.docjson.api import axiom_graph_read_doc, axiom_graph_update_section
from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import (
    build_index,
    compute_check_summary,
    fetch_history,
    mark_clean_nodes,
)

from tests.fixtures import doc_trees

CODE_ID = "proj::src.mod::foo"
DOC_ID = "proj::docs/guide/spec"

#: History change types that would mean the document was not carried over.
_DISCONTINUITY = {"DELETED", "NOT_FOUND", "RENAMED", "RAW_DOCJSON_EDIT"}


def _git(root: Path, *args: str) -> str:
    """Run git in *root* and return stdout."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True, encoding="utf-8").stdout


def _git_project(root: Path) -> Path:
    """Create a git-tracked project with one function a document can link to."""
    doc_trees.write_toml(root, ["docs"])
    (root / ".gitignore").write_text(".axiom_graph/\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("def foo():\n    return 0\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _spec_sections() -> list[dict]:
    return [
        {"id": "intro", "heading": "Intro", "content": "Original intro.", "links": [{"node_id": CODE_ID}]},
        {
            "id": "body",
            "heading": "Body",
            "content": "Body text.",
            "sections": [{"id": "detail", "heading": "Detail", "content": "Detail text."}],
        },
    ]


def _doc_node_ids(db_path: Path) -> set[str]:
    return {n.id for n in db.all_nodes(db_path) if n.id == DOC_ID or n.id.startswith(f"{DOC_ID}::")}


def _history(db_path: Path, node_ids: set[str]) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for node_id in sorted(node_ids):
        rows = fetch_history(db_path, node_id, max_results=500).rows
        out[node_id] = [(r.change_type, r.scanned_at, r.meta) for r in rows]
    return out


def _verifications(db_path: Path, node_ids: set[str]) -> dict[str, dict | None]:
    return {node_id: db.get_verification(db_path, node_id) for node_id in sorted(node_ids)}


def _all_change_types(db_path: Path) -> set[str]:
    with db._connect(db_path) as conn:
        return {r["change_type"] for r in conn.execute("SELECT change_type FROM node_history").fetchall()}


# ---------------------------------------------------------------------------
# Tier 3 -- US-1: converting a document keeps its identity
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a git mv from .json to .docjson followed by a rebuild keeps every document and section id, the history and verification rows, the staleness, and the doc tools working on the new path",
)
def test_git_mv_to_docjson_keeps_the_document_identity(tmp_path):
    """Renaming the file is not a new document: nothing is retired, nothing re-verified."""
    口 = Step(
        step_num=1,
        name="Index a .json document with history and verification",
        purpose="A committed .json doc, one section edited through the doc tools and one marked clean",
    )
    root = _git_project(tmp_path / "proj")
    doc_trees.write_doc(root, "docs/guide/spec.json", title="Spec", sections=_spec_sections())
    db_path = _db_path(str(root))
    build_index(db_path, root)
    result = axiom_graph_update_section(str(root), f"{DOC_ID}::intro", content="Edited intro.")
    assert "Updated" in result, result
    mark_clean_nodes(db_path, root, [f"{DOC_ID}::body.detail"], "reviewed", verified_by="agent:test")
    _commit(root, "spec as json")
    build_index(db_path, root)

    口 = Step(
        step_num=2,
        name="Snapshot identities, history, verification and staleness",
        purpose="The state a conversion must leave untouched",
    )
    ids_before = _doc_node_ids(db_path)
    assert ids_before == {DOC_ID, f"{DOC_ID}::intro", f"{DOC_ID}::body", f"{DOC_ID}::body.detail"}
    history_before = _history(db_path, ids_before)
    verification_before = _verifications(db_path, ids_before)
    statuses_before = {k: v for k, v in compute_check_summary(db_path, root).statuses.items() if k in ids_before}
    assert any(history_before.values())
    assert verification_before[f"{DOC_ID}::body.detail"] is not None

    口 = Step(
        step_num=3,
        name="Convert the file with git mv and rebuild",
        purpose="The upgrade path a consumer takes",
    )
    _git(root, "mv", "docs/guide/spec.json", "docs/guide/spec.docjson")
    _commit(root, "spec as docjson")
    build_index(db_path, root)

    口 = Step(
        step_num=4,
        name="Assert the document is the same document",
        purpose="Ids, prior history, verification and staleness all carried over; nothing retired",
    )
    assert _doc_node_ids(db_path) == ids_before
    history_after = _history(db_path, ids_before)
    for node_id, rows in history_before.items():
        assert set(rows) <= set(history_after[node_id]), node_id
    assert _verifications(db_path, ids_before) == verification_before
    statuses_after = {k: v for k, v in compute_check_summary(db_path, root).statuses.items() if k in ids_before}
    assert statuses_after == statuses_before
    assert not (_all_change_types(db_path) & _DISCONTINUITY)

    口 = Step(
        step_num=5,
        name="Assert the index points at the new file",
        purpose="The docs table and the node location name the .docjson path",
    )
    assert db.get_node(db_path, DOC_ID).location == "docs/guide/spec.docjson"
    paths = db.get_all_doc_file_paths(db_path)
    assert "docs/guide/spec.docjson" in paths
    assert "docs/guide/spec.json" not in paths

    口 = Step(
        step_num=6,
        name="Use the doc tools on the converted document",
        purpose="read_doc and update_section resolve it by its unchanged id",
    )
    assert "Edited intro." in axiom_graph_read_doc(str(root), DOC_ID)
    result = axiom_graph_update_section(str(root), f"{DOC_ID}::body", content="Body after conversion.")
    assert "Updated" in result, result
    on_disk = json.loads((root / "docs" / "guide" / "spec.docjson").read_text(encoding="utf-8"))
    assert on_disk["sections"][1]["content"] == "Body after conversion."
    assert not (root / "docs" / "guide" / "spec.json").exists()


# ---------------------------------------------------------------------------
# Tier 2 -- US-2: .docjson is a first-class document extension
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a build indexes .docjson and .json documents side by side at nested paths across two configured roots, each under the extension-free id the derivation gives",
)
@pytest.mark.parametrize("ext", [".docjson", ".json"])
def test_both_extensions_are_indexed_under_one_id_shape(tmp_path, ext):
    """The extension never reaches an id, at any depth, under any root."""
    other = ".json" if ext == ".docjson" else ".docjson"
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs", "specs"])
    doc_trees.write_doc(root, f"docs/a/b/x{ext}", title="X")
    doc_trees.write_doc(root, f"specs/c/y{other}", title="Y")
    db_path = _db_path(str(root))
    build_index(db_path, root)

    locations = {n.id: n.location for n in db.all_nodes(db_path) if n.subtype == "docjson_doc"}
    assert locations == {"proj::docs/a/b/x": f"docs/a/b/x{ext}", "proj::specs/c/y": f"specs/c/y{other}"}
    from axiom_graph.index import doc_ids

    assert doc_ids.derive_doc_id("proj", "docs", f"a/b/x{ext}") == doc_ids.derive_doc_id("proj", "docs", "a/b/x.json")


@workflow(
    purpose="Verify write_doc creates a new document with the first configured extension, reports an extension-free id, and rewrites an existing .json document in place without creating a sibling",
)
def test_write_doc_picks_the_extension_and_never_creates_a_sibling(tmp_path):
    """New documents get the write extension; existing ones keep their file."""
    from axiom_graph.docjson.api import axiom_graph_write_doc

    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(root, "docs/legacy.json", title="Legacy")
    db_path = _db_path(str(root))
    build_index(db_path, root)

    new = axiom_graph_write_doc(str(root), {"id": "fresh", "title": "Fresh", "sections": []})
    assert "doc id           : proj::docs/fresh\n" in new, new
    assert (root / "docs" / "fresh.docjson").is_file()
    assert not (root / "docs" / "fresh.json").exists()

    sections = [{"id": "s", "heading": "S", "content": "rewritten"}]
    over = axiom_graph_write_doc(str(root), {"id": "legacy", "title": "Legacy", "sections": sections})
    assert "Wrote docs/legacy.json" in over, over
    assert not (root / "docs" / "legacy.docjson").exists()
    assert json.loads((root / "docs" / "legacy.json").read_text(encoding="utf-8"))["sections"] == sections

    json_only = tmp_path / "json-only"
    doc_trees.write_toml(json_only, ["docs"], extra='docs_extensions = [".json"]\n')
    (json_only / "docs").mkdir()
    build_index(_db_path(str(json_only)), json_only)
    axiom_graph_write_doc(str(json_only), {"id": "plain", "title": "Plain", "sections": []})
    assert (json_only / "docs" / "plain.json").is_file()


# ---------------------------------------------------------------------------
# Tier 2 -- US-3: collisions are reported, not silently resolved
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify x.json beside x.docjson is reported by the build with the doc id and both paths, and by the migration gate, while a JSON data file beside them produces nothing; .docjson is indexed whatever the docs_extensions order",
)
@pytest.mark.parametrize(
    "extra",
    ["", 'docs_extensions = [".json", ".docjson"]\n'],
    ids=["default-order", "json-listed-first"],
)
def test_extension_collision_is_reported_by_build_and_gate(tmp_path, extra):
    """One identity, two files: the build says which one it indexed, and it is the .docjson."""
    from axiom_graph.lifecycle.api import plan_doc_id_migration

    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"], extra=extra)
    doc_trees.write_doc(root, "docs/x.json", title="Old")
    doc_trees.write_doc(root, "docs/x.docjson", title="New")
    doc_trees.write_json(root, "docs/layout.json", {"columns": 3})
    db_path = _db_path(str(root))
    summary = build_index(db_path, root)

    collision = [w for w in summary.warnings if "duplicate doc id" in w]
    assert len(collision) == 1, summary.warnings
    assert "proj::docs/x" in collision[0]
    assert "docs/x.json" in collision[0] and "docs/x.docjson" in collision[0]
    assert "indexed docs/x.docjson" in collision[0]
    assert not [w for w in summary.warnings if "layout.json" in w]
    assert db.get_node(db_path, "proj::docs/x").title == "New"

    plan = plan_doc_id_migration(db_path, root)
    assert plan.blocked
    named = {src for c in plan.blocking_collisions for src in c.sources}
    assert {"docs/x.json", "docs/x.docjson"} <= named
    assert "docs/layout.json" not in named


def test_docjson_data_file_does_not_hide_a_json_document(tmp_path):
    """The winner is chosen among documents: a .docjson data file loses to a .json document."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(root, "docs/y.json", title="Real")
    doc_trees.write_json(root, "docs/y.docjson", {"columns": 3})
    db_path = _db_path(str(root))
    summary = build_index(db_path, root)

    assert db.get_node(db_path, "proj::docs/y").location == "docs/y.json"
    assert not [w for w in summary.warnings if "duplicate doc id" in w], summary.warnings


def test_existing_file_lookup_honours_configured_extensions_and_docjson_precedence(tmp_path):
    """write_doc's in-place lookup: .docjson wins, unconfigured extensions are not looked for."""
    from axiom_graph.index import doc_ids

    docjson = "x" + ".docjson"
    doc_trees.write_doc(tmp_path, "x.json", title="Old")
    doc_trees.write_doc(tmp_path, docjson, title="New")

    assert doc_ids.existing_docjson_file(tmp_path, "x") == tmp_path / docjson
    assert doc_ids.existing_docjson_file(tmp_path, "x", [".json", ".docjson"]) == tmp_path / docjson
    assert doc_ids.existing_docjson_file(tmp_path, "x", [".json"]) == tmp_path / "x.json"
    assert doc_ids.strip_docjson_extension("A/B.DocJSON") == "A/B"
    assert doc_ids.strip_docjson_extension("A/B.JSON") == "A/B"


def test_invalid_docs_extensions_entry_warns_and_is_dropped(tmp_path):
    """An entry that is not a DocJSON extension is named in a build warning and ignored."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"], extra='docs_extensions = [".yaml", ".json"]\n')
    doc_trees.write_doc(root, "docs/z.json", title="Z")
    db_path = _db_path(str(root))
    summary = build_index(db_path, root)

    dropped = [w for w in summary.warnings if "docs_extensions" in w]
    assert len(dropped) == 1 and "'.yaml'" in dropped[0], summary.warnings
    assert db.get_node(db_path, "proj::docs/z") is not None


def test_render_site_accepts_an_index_docjson_folder_landing(tmp_path):
    """index.docjson is a folder landing, so pairing it with landing: is ambiguous."""
    from axiom_graph.docjson.render_consumer import validate_site_nav

    source_root = tmp_path / "consumer"
    folder = source_root / "features"
    folder.mkdir(parents=True)
    (folder / ("index" + ".docjson")).write_text("{}", encoding="utf-8")
    nav_data = {
        "site_name": "T",
        "root": "docs/consumer",
        "show": [{"features": {"landing": "overview", "show": []}}],
        "_project_id": "proj",
    }
    errors = validate_site_nav(nav_data, source_root=source_root)
    assert any("ambiguous" in e.lower() for e in errors), errors


# ---------------------------------------------------------------------------
# Tier 1 -- viz create/import/move follow the extension rule
# ---------------------------------------------------------------------------


def _viz_client(root: Path):
    """Point the viz server module globals at *root* and return a TestClient."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom_graph.config import AxiomGraphConfig
    from axiom_graph.viz import server

    server._PROJECT_ROOT = root
    server._DB_PATH = _db_path(str(root))
    server._DFLOW_DB_PATH = None
    config = AxiomGraphConfig.load(root)
    server._PROJECT_ID = config.project_id or root.name
    server._TEST_PATHS = config.scan.test_paths
    server._EXCLUDE_DIRS = config.scan.exclude_dirs
    return TestClient(server.app)


@pytest.mark.parametrize(
    ("extra", "expected"),
    [("", ".docjson"), ('docs_extensions = [".json"]\n', ".json")],
    ids=["default", "json-only"],
)
def test_viz_import_writes_the_configured_extension(tmp_path, extra, expected):
    """A viz import gets the first docs_extensions entry, like write_doc."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"], extra=extra)
    doc_trees.write_doc(root, "docs/seed.json", title="Seed")
    build_index(_db_path(str(root)), root)
    client = _viz_client(root)

    resp = client.post("/api/docs/import", json={"doc": {"title": "Imported", "sections": []}})
    assert resp.status_code == 200, resp.text
    written = sorted(p.name for p in (root / "docs").iterdir() if p.name.startswith("imported"))
    assert written == [f"imported{expected}"]


def test_viz_move_keeps_a_json_documents_extension(tmp_path):
    """A move is not a conversion: a .json document stays .json."""
    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(root, "docs/old.json", title="Old")
    build_index(_db_path(str(root)), root)
    client = _viz_client(root)

    resp = client.post("/api/docs/proj::docs/old/move", json={"destination": "reference"})
    assert resp.status_code == 200, resp.text
    assert (root / "docs" / "reference" / "old.json").is_file()
    assert not (root / "docs" / "reference" / ("old" + ".docjson")).exists()
    assert not (root / "docs" / "old.json").exists()


# ---------------------------------------------------------------------------
# Tier 3 -- US-4: one command converts a tree, safely
# ---------------------------------------------------------------------------


def _rename_tree(tmp_path: Path) -> Path:
    """Two tracked documents, a tracked data file, and an untracked document, indexed."""
    root = _git_project(tmp_path / "proj")
    doc_trees.write_doc(root, "docs/alpha.json", title="Alpha")
    doc_trees.write_doc(root, "docs/nested/beta.json", title="Beta")
    doc_trees.write_json(root, "docs/layout.json", {"columns": 3})
    _commit(root, "tree")
    doc_trees.write_doc(root, "docs/draft.json", title="Draft")
    build_index(_db_path(str(root)), root)
    return root


@workflow(
    purpose="Verify rename-extension previews exactly the tracked documents it would rename and the files it skips without writing, then renames them with git mv so the index matches disk and every id is preserved",
)
def test_rename_extension_previews_then_converts_a_tree(tmp_path):
    """Preview is a pure read; execute is git renames plus a re-index."""
    from axiom_graph.lifecycle.api import execute_docjson_extension_rename, plan_docjson_extension_rename

    口 = Step(step_num=1, name="Index a mixed tree", purpose="Tracked docs, tracked data JSON, untracked doc")
    root = _rename_tree(tmp_path)
    db_path = _db_path(str(root))
    ids_before = {n.id for n in db.all_nodes(db_path) if n.subtype in ("docjson_doc", "docjson_section")}
    status_before = _git(root, "status", "--porcelain")

    口 = Step(step_num=2, name="Preview", purpose="List renames and skipped files, write nothing")
    plan = plan_docjson_extension_rename(db_path, root)
    assert [(r.old_path, r.new_path) for r in plan.renames] == [
        ("docs/alpha.json", "docs/alpha.docjson"),
        ("docs/nested/beta.json", "docs/nested/beta.docjson"),
    ]
    assert plan.untracked == ["docs/draft.json"]
    assert plan.data_files == ["docs/layout.json"]
    assert plan.refusals == []
    assert _git(root, "status", "--porcelain") == status_before
    assert (root / "docs" / "alpha.json").is_file()

    口 = Step(step_num=3, name="Execute", purpose="git mv each tracked document, then re-index")
    result = execute_docjson_extension_rename(db_path, root)
    assert result.executed and len(result.renamed) == 2

    口 = Step(step_num=4, name="Assert git saw renames and the index matches disk", purpose="Ids preserved")
    staged = _git(root, "status", "--porcelain")
    assert "R  docs/alpha.json -> docs/alpha.docjson" in staged
    assert "R  docs/nested/beta.json -> docs/nested/beta.docjson" in staged
    assert (root / "docs" / "draft.json").is_file()
    assert (root / "docs" / "layout.json").is_file()
    ids_after = {n.id for n in db.all_nodes(db_path) if n.subtype in ("docjson_doc", "docjson_section")}
    assert ids_after == ids_before
    assert db.get_node(db_path, "proj::docs/alpha").location == "docs/alpha.docjson"
    assert db.get_node(db_path, "proj::docs/nested/beta").location == "docs/nested/beta.docjson"


# ---------------------------------------------------------------------------
# Tier 2 -- US-4: execute refuses, naming the cause
# ---------------------------------------------------------------------------


def _make_dirty(root: Path) -> None:
    doc_trees.write_doc(root, "docs/alpha.json", title="Alpha, edited")


def _make_target_exist(root: Path) -> None:
    doc_trees.write_doc(root, "docs/alpha.docjson", title="Alpha twin")
    _commit(root, "twin")


def _make_unmigrated(root: Path) -> None:
    db_path = _db_path(str(root))
    db_path.unlink()
    with doc_trees.retired_derivation():
        build_index(db_path, root)


def _make_docjson_unscanned(root: Path) -> None:
    doc_trees.write_toml(root, ["docs"], extra='docs_extensions = [".json"]\n')
    _commit(root, "json only")


@workflow(
    purpose="Verify rename-extension execute refuses on a dirty document, an existing .docjson target, an index that still needs doc-ids execute, or a docs_extensions that does not scan .docjson, naming the cause and renaming nothing",
)
@pytest.mark.parametrize(
    ("arrange", "cause"),
    [
        (_make_dirty, "uncommitted changes"),
        (_make_target_exist, "target already exists"),
        (_make_unmigrated, "doc-ids execute"),
        (_make_docjson_unscanned, "not in [axiom_graph.scan] docs_extensions"),
    ],
    ids=["dirty-document", "existing-target", "unmigrated-index", "docjson-not-scanned"],
)
def test_rename_extension_refuses_and_renames_nothing(tmp_path, arrange, cause):
    """Every refusal is checked before the first git mv."""
    from axiom_graph.lifecycle.api import execute_docjson_extension_rename

    root = _rename_tree(tmp_path)
    arrange(root)
    result = execute_docjson_extension_rename(_db_path(str(root)), root)

    assert not result.executed
    assert any(cause in r for r in result.refused), result.refused
    assert (root / "docs" / "alpha.json").is_file()
    assert (root / "docs" / "nested" / "beta.json").is_file()
    assert "R " not in _git(root, "status", "--porcelain")


def test_rename_extension_refuses_outside_git(tmp_path):
    """Without git there is no `git mv`, so execute refuses and renames nothing."""
    from axiom_graph.lifecycle.api import execute_docjson_extension_rename

    root = tmp_path / "proj"
    doc_trees.write_toml(root, ["docs"])
    doc_trees.write_doc(root, "docs/alpha.json", title="Alpha")
    result = execute_docjson_extension_rename(_db_path(str(root)), root)

    assert not result.executed
    assert any("not a git repository" in r for r in result.refused), result.refused
    assert (root / "docs" / "alpha.json").is_file()


def test_rename_extension_cli_previews_without_writing(tmp_path):
    """The CLI prints the renames and skipped files and leaves the tree alone."""
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli

    root = _rename_tree(tmp_path)
    status_before = _git(root, "status", "--porcelain")
    out = CliRunner().invoke(cli, ["doc-ids", "rename-extension", str(root)])

    assert out.exit_code == 0, out.output
    assert "docs/alpha.json" in out.output and "docs/alpha.docjson" in out.output
    assert "docs/draft.json" in out.output
    assert "docs/layout.json" in out.output
    assert _git(root, "status", "--porcelain") == status_before
    assert (root / "docs" / "alpha.json").is_file()
