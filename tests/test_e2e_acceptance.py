"""E2E acceptance stories for V1 release.

Tier 3 scenario tests — @workflow + Step().

All tests use the ``git_project`` fixture (real git repo + axiom-graph DB).
No git mocking — every git operation hits a real temporary repository.
These are the ship criteria: V1 ships when all pass.

Build mechanics:
- ``_init`` (discovery_only=False): resets baselines AND creates CONTENT_ONLY
  history rows — required for LINKED_STALE propagation.
- ``_build`` (discovery_only=True): preserves baselines, detects CONTENT_UPDATED
  via hash mismatch, but does NOT create CONTENT_ONLY rows.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


from axiom_annotations import workflow, Step

from axiom_graph.index import builder, db
from axiom_graph.index.mark_clean import mark_node_clean
from axiom_graph.index.staleness import record_staleness
from axiom_graph.models import AxiomEdge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commit(project: Path, message: str) -> str:
    """Stage all, commit, return the full SHA."""
    subprocess.run(
        ["git", "add", "-A"],
        cwd=project,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=project,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init(
    project: Path,
    transitive_tags: list[str] | None = None,
) -> dict:
    """axiom-graph init — full build (discovery_only=False) + record_staleness.

    Creates CONTENT_ONLY/CONTENT_AND_DESC history rows for changed code
    AND resets baselines.  Required for LINKED_STALE propagation.
    """
    db_path = project / ".axiom_graph" / "graph.db"
    summary = builder.build(project, project_id="proj", discovery_only=False)
    nodes = db.all_nodes(db_path)
    if nodes:
        record_staleness(db_path, project, nodes, transitive_tags=transitive_tags)
    return summary


def _build(
    project: Path,
    transitive_tags: list[str] | None = None,
) -> dict:
    """axiom-graph build — discovery only + record_staleness.

    Preserves baselines (detects CONTENT_UPDATED via hash mismatch) but
    does NOT create CONTENT_ONLY history rows.
    """
    db_path = project / ".axiom_graph" / "graph.db"
    summary = builder.build(project, project_id="proj", discovery_only=True)
    nodes = db.all_nodes(db_path)
    if nodes:
        record_staleness(db_path, project, nodes, transitive_tags=transitive_tags)
    return summary


def _write_doc(
    docs_dir: Path,
    filename: str,
    title: str,
    sections: list[dict],
    tags: list[str] | None = None,
) -> Path:
    """Write a DocJSON file and return its path."""
    docs_dir.mkdir(exist_ok=True)
    doc_path = docs_dir / filename
    doc: dict = {"title": title, "sections": sections}
    if tags:
        doc["tags"] = tags
    doc_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc_path


def _add_edge(db_path: Path, from_id: str, edge_type: str, to_id: str) -> None:
    """Insert an edge (mirrors axiom-graph link / add_link MCP tool)."""
    edge = AxiomEdge(
        id=f"{from_id}::{edge_type}::{to_id}",
        edge_type=edge_type,
        from_id=from_id,
        to_id=to_id,
    )
    db.upsert_edge(db_path, edge)


def _staleness(db_path: Path) -> dict[str, tuple[str, str]]:
    """Return {node_id: (own_status, link_status)} from persisted staleness."""
    return db.get_all_staleness(db_path)


def _find_node(db_path: Path, title: str):
    """Find a node by exact title match, return the first match."""
    nodes = db.all_nodes(db_path)
    matches = [n for n in nodes if n.title == title]
    if not matches:
        # Fall back to substring
        matches = [n for n in nodes if title in n.title]
    assert matches, f"No node with title {title!r}"
    return matches[0]


def _checkpoint(db_path: Path, project: Path) -> str:
    """Insert CHECKPOINT markers on all nodes, return the git SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    git_sha = result.stdout.strip()[:12]
    nodes = db.all_nodes(db_path)
    for node in nodes:
        db.insert_history_row(
            db_path,
            node_id=node.id,
            change_type="CHECKPOINT",
            git_sha=git_sha,
            preserved=True,
        )
    return git_sha


# ---------------------------------------------------------------------------
# E1: Full staleness cascade
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Edit a function body and verify the full staleness cascade: "
        "CONTENT_UPDATED on code (discovery build), LINKED_STALE on doc and "
        "test (full build), and composite module inheriting worst child status."
    ),
)
def test_e1_full_staleness_cascade(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write function, doc, and test file; commit and init",
        purpose="Establish a clean baseline with code, docs, and tests all indexed",
        outputs="All nodes VERIFIED after init",
    )
    (git_project / "mod.py").write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    _write_doc(
        git_project / "docs",
        "spec.json",
        "Spec",
        [
            {
                "id": "greet-section",
                "heading": "Greet",
                "content": "Documents the greet function.",
                "links": [{"node_id": "proj::mod::greet"}],
            }
        ],
    )
    (git_project / "test_mod.py").write_text(
        "from mod import greet\n"
        "\n"
        "def test_greet():\n"
        '    """Validate greet returns hello."""\n'
        '    assert greet() == "hello"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Add greet with doc and test")
    _init(git_project)

    greet_node = _find_node(git_db_path, "greet")
    doc_section_id = "proj::docs.spec::greet-section"
    test_node = _find_node(git_db_path, "test_greet")

    口 = Step(
        step_num=2,
        name="Link test to the function, re-baseline",
        purpose="Create the validates edge (the documents link is declared in the DocJSON links array) so staleness propagates",
        outputs="Edges in DB, all VERIFIED",
    )
    _add_edge(git_db_path, test_node.id, "validates", greet_node.id)
    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes)

    statuses = _staleness(git_db_path)
    assert statuses[greet_node.id] == ("VERIFIED", "VERIFIED")

    口 = Step(
        step_num=3,
        name="Edit function body, commit, discovery build",
        purpose="Discovery build preserves baselines — detects CONTENT_UPDATED via hash mismatch",
    )
    (git_project / "mod.py").write_text(
        'def greet():\n    """Say hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Change greet return value")
    _build(git_project)

    口 = Step(
        step_num=4,
        name="Assert CONTENT_UPDATED and composite inheritance",
        purpose="Code node detects change; module composite inherits worst child status",
    )
    statuses = _staleness(git_db_path)

    own, _ = statuses[greet_node.id]
    assert own == "CONTENT_UPDATED", f"greet: expected CONTENT_UPDATED, got {own}"

    mod_node = _find_node(git_db_path, "mod")
    mod_own, _ = statuses[mod_node.id]
    assert mod_own == "CONTENT_UPDATED", f"module: expected CONTENT_UPDATED (inherited), got {mod_own}"

    口 = Step(
        step_num=5,
        name="Full build to create CONTENT_ONLY history, then check LINKED_STALE",
        purpose="Full build records content change history — enables LINKED_STALE propagation to docs and tests",
    )
    _init(git_project)
    statuses = _staleness(git_db_path)

    _, doc_link = statuses[doc_section_id]
    assert doc_link == "LINKED_STALE", f"doc section: expected LINKED_STALE, got {doc_link}"

    _, test_link = statuses[test_node.id]
    assert test_link == "LINKED_STALE", f"test: expected LINKED_STALE, got {test_link}"


# ---------------------------------------------------------------------------
# E2: Move function between files preserves history
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Move a function to a different file. Hash-similarity rename detection "
        "fires because the code_hash matches. History migrates to the new node ID. "
        "The rename is traceable through the node_renames table."
    ),
)
def test_e2_move_function_preserves_history(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write function in helpers.py, commit, init",
        purpose="Establish a baseline node with history",
        outputs="INITIAL history row on helpers::compute",
    )
    (git_project / "helpers.py").write_text(
        'def compute(x):\n    """Compute a value."""\n    return x * 2\n',
        encoding="utf-8",
    )
    _commit(git_project, "Add compute in helpers")
    _init(git_project)

    old_node = _find_node(git_db_path, "compute")
    old_node_id = old_node.id
    assert "helpers" in old_node_id, f"Expected helpers in node id, got {old_node_id}"

    history = db.get_history(git_db_path, old_node_id, limit=10)
    initial_rows = [r for r in history if r["change_type"] == "INITIAL"]
    assert initial_rows, "Expected an INITIAL history row"

    口 = Step(
        step_num=2,
        name="Move function to utils.py (delete from helpers.py), commit, build",
        purpose="Same function name and body in new file — hash-similarity detection fires",
    )
    (git_project / "helpers.py").unlink()
    (git_project / "utils.py").write_text(
        'def compute(x):\n    """Compute a value."""\n    return x * 2\n',
        encoding="utf-8",
    )
    _commit(git_project, "Move compute from helpers to utils")
    _init(git_project)

    口 = Step(
        step_num=3,
        name="Verify rename detected in node_renames table",
        purpose="The rename table maps old→new so provenance is traceable",
    )
    with db._connect(git_db_path) as conn:
        renames = conn.execute("SELECT old_id, new_id FROM node_renames").fetchall()
    rename_map = {r["old_id"]: r["new_id"] for r in renames}
    assert any("helpers" in old and "utils" in new for old, new in rename_map.items()), (
        f"Expected helpers→utils rename, got {rename_map}"
    )

    # Old node should no longer be in active nodes
    all_nodes = db.all_nodes(git_db_path)
    old_still_exists = [n for n in all_nodes if n.id == old_node_id]
    assert not old_still_exists, f"Old node {old_node_id} should be gone after rename detection"

    口 = Step(
        step_num=4,
        name="Verify new node exists with correct edges and history migrated",
        purpose="History rows should have moved from old node ID to new node ID",
    )
    new_node = _find_node(git_db_path, "compute")
    assert "utils" in new_node.id, f"Expected utils in node id, got {new_node.id}"

    # New node should have composes edge from utils module
    with db._connect(git_db_path) as conn:
        composes = conn.execute(
            "SELECT from_id FROM edges WHERE edge_type = 'composes' AND to_id = ?",
            (new_node.id,),
        ).fetchall()
    assert composes, f"Expected composes edge to {new_node.id}"
    assert any("utils" in e["from_id"] for e in composes), (
        f"Expected composes from utils module, got {[e['from_id'] for e in composes]}"
    )

    new_history = db.get_history(git_db_path, new_node.id, limit=10)
    new_types = [r["change_type"] for r in new_history]
    assert "INITIAL" in new_types, f"Expected migrated INITIAL row on new node, got {new_types}"


# ---------------------------------------------------------------------------
# E3: Mixed Python + TS codebase indexed
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Index a project with both Python and TypeScript files. Verify "
        "Python functions produce nodes with call edges and TS files produce "
        "nodes with ESM import edges."
    ),
)
def test_e3_mixed_python_ts_indexed(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write axiom-graph.toml with js_paths, Python file, and two TS files",
        purpose="Set up a realistic mixed-language project",
        outputs="axiom-graph.toml + helper.py + utils.ts + main.ts on disk",
    )
    (git_project / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["*.ts"]\n',
        encoding="utf-8",
    )
    (git_project / "helper.py").write_text(
        "def compute(x):\n"
        '    """Compute a value."""\n'
        "    return x * 2\n"
        "\n"
        "\n"
        "def transform(x):\n"
        '    """Transform using compute."""\n'
        "    return compute(x) + 1\n",
        encoding="utf-8",
    )
    (git_project / "utils.ts").write_text(
        'export function esc(s: string): string {\n    return s.replace(/</g, "&lt;");\n}\n',
        encoding="utf-8",
    )
    (git_project / "main.ts").write_text(
        'import { esc } from "./utils.js";\n'
        "\n"
        "export function render(html: string): string {\n"
        "    return esc(html);\n"
        "}\n",
        encoding="utf-8",
    )
    _commit(git_project, "Add Python and TS files")

    口 = Step(
        step_num=2,
        name="Run full init build",
        purpose="Index all files — Python scanner + JS/TS scanner",
    )
    _init(git_project)

    口 = Step(
        step_num=3,
        name="Verify Python nodes and edges",
        purpose="compute and transform functions should exist with composes edges",
    )
    nodes = db.all_nodes(git_db_path)
    node_titles = {n.title for n in nodes}

    assert "compute" in node_titles, f"compute not found in {node_titles}"
    assert "transform" in node_titles, f"transform not found in {node_titles}"

    py_modules = [n for n in nodes if "helper" in n.id and n.node_type == "composite_process"]
    assert py_modules, "Expected a composite_process node for helper.py"

    with db._connect(git_db_path) as conn:
        composes = conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'composes'").fetchall()
    composes_from_helper = [e for e in composes if "helper" in e["from_id"]]
    assert len(composes_from_helper) >= 2, (
        f"Expected >=2 composes edges from helper module, got {len(composes_from_helper)}"
    )

    口 = Step(
        step_num=4,
        name="Verify TypeScript nodes and ESM import edges",
        purpose="TS modules and functions should exist with depends_on edge",
    )
    ts_func_nodes = [n for n in nodes if n.title in ("esc", "render")]
    assert len(ts_func_nodes) >= 2, f"Expected esc and render TS nodes, found {[n.title for n in ts_func_nodes]}"

    ts_modules = [n for n in nodes if n.node_type == "composite_process" and ".ts" in (n.location or "")]
    assert len(ts_modules) >= 2, f"Expected >=2 TS composite nodes, got {len(ts_modules)}"

    with db._connect(git_db_path) as conn:
        depends = conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'depends_on'").fetchall()
    ts_depends = [e for e in depends if "main" in e["from_id"] and "utils" in e["to_id"]]
    assert ts_depends, f"Expected depends_on edge main→utils, got {[(e['from_id'], e['to_id']) for e in depends]}"


# ---------------------------------------------------------------------------
# E4: Multi-commit feature branch report
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Make several commits changing different functions, run report "
        "with --since-sha, verify all changes are grouped by node."
    ),
)
def test_e4_multicommit_branch_report(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write two functions, commit, init, checkpoint",
        purpose="Establish baseline with a checkpoint as the report reference point",
        outputs="Checkpoint SHA for --since-sha",
    )
    (git_project / "mod.py").write_text(
        "def func_a():\n"
        '    """Function A."""\n'
        '    return "a"\n'
        "\n"
        "\n"
        "def func_b():\n"
        '    """Function B."""\n'
        '    return "b"\n',
        encoding="utf-8",
    )
    _write_doc(
        git_project / "docs",
        "api.json",
        "API Reference",
        [{"id": "api-section", "heading": "API", "content": "Documents func_a and func_b."}],
    )
    _commit(git_project, "Add func_a, func_b, and API doc")
    _init(git_project)
    checkpoint_sha = _checkpoint(git_db_path, git_project)

    口 = Step(
        step_num=2,
        name="Two commits changing different functions, full build after each",
        purpose="Full builds create CONTENT_ONLY history rows that the report queries",
    )
    (git_project / "mod.py").write_text(
        "def func_a():\n"
        '    """Function A."""\n'
        '    return "a_changed"\n'
        "\n"
        "\n"
        "def func_b():\n"
        '    """Function B."""\n'
        '    return "b"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Change func_a")
    _init(git_project)

    (git_project / "mod.py").write_text(
        "def func_a():\n"
        '    """Function A."""\n'
        '    return "a_changed"\n'
        "\n"
        "\n"
        "def func_b():\n"
        '    """Function B."""\n'
        '    return "b_changed"\n',
        encoding="utf-8",
    )
    _commit(git_project, "Change func_b")
    _init(git_project)

    _write_doc(
        git_project / "docs",
        "api.json",
        "API Reference",
        [{"id": "api-section", "heading": "API", "content": "Updated docs for func_a and func_b changes."}],
    )
    _commit(git_project, "Update API doc")
    _init(git_project)

    口 = Step(
        step_num=3,
        name="Query report via get_history_since",
        purpose="Retrieve all history rows since checkpoint — same data as axiom-graph report",
    )
    rows = db.get_history_since(git_db_path, since_sha=checkpoint_sha)
    assert rows, "Expected history rows after checkpoint"

    口 = Step(
        step_num=4,
        name="Verify both functions appear with CONTENT_ONLY changes",
        purpose="Multi-commit changes should all be captured, grouped by node",
    )
    content_types = {"CONTENT_ONLY", "CONTENT_AND_DESC"}
    content_changes: dict[str, list[str]] = {}
    for row in rows:
        if row["change_type"] in content_types:
            content_changes.setdefault(row["node_id"], []).append(row["change_type"])

    func_a = _find_node(git_db_path, "func_a")
    func_b = _find_node(git_db_path, "func_b")

    assert func_a.id in content_changes, f"func_a not in report: {list(content_changes.keys())}"
    assert func_b.id in content_changes, f"func_b not in report: {list(content_changes.keys())}"
    assert "CONTENT_ONLY" in content_changes[func_a.id]
    assert "CONTENT_ONLY" in content_changes[func_b.id]

    api_section_id = "proj::docs.api::api-section"
    assert api_section_id in content_changes, f"doc section not in report: {list(content_changes.keys())}"


# ---------------------------------------------------------------------------
# E6: Spec-code alignment verification
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Update a spec section without changing code — spec shows CONTENT_UPDATED, "
        "code stays VERIFIED. Then change the code — spec shows LINKED_STALE."
    ),
)
def test_e6_spec_code_alignment(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write function and spec doc, commit, init, add edge",
        purpose="Establish clean baseline with spec→code link",
        outputs="Clean baseline with documents edge",
    )
    (git_project / "mod.py").write_text(
        'def process(data):\n    """Process the data."""\n    return data.upper()\n',
        encoding="utf-8",
    )
    _write_doc(
        git_project / "docs",
        "spec.json",
        "Spec",
        [
            {
                "id": "process-section",
                "heading": "Process",
                "content": "Documents the process function.",
                "links": [{"node_id": "proj::mod::process"}],
            }
        ],
    )
    _commit(git_project, "Add process with spec")
    _init(git_project)

    process_node = _find_node(git_db_path, "process")
    spec_section_id = "proj::docs.spec::process-section"
    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes)

    口 = Step(
        step_num=2,
        name="Edit spec prose only (not the code), commit, discovery build",
        purpose="Spec prose changed but code didn't — code should stay VERIFIED",
    )
    _write_doc(
        git_project / "docs",
        "spec.json",
        "Spec",
        [
            {
                "id": "process-section",
                "heading": "Process",
                "content": "Updated description of process with more detail.",
                "links": [{"node_id": "proj::mod::process"}],
            }
        ],
    )
    _commit(git_project, "Update spec prose")
    _build(git_project)

    口 = Step(
        step_num=3,
        name="Assert spec CONTENT_UPDATED, code VERIFIED",
        purpose="Spec drifted ahead of code — spec changed, code didn't",
    )
    statuses = _staleness(git_db_path)
    spec_own, _ = statuses[spec_section_id]
    assert spec_own == "CONTENT_UPDATED", f"spec own_status: expected CONTENT_UPDATED, got {spec_own}"

    code_own, _ = statuses[process_node.id]
    assert code_own == "VERIFIED", f"code own_status: expected VERIFIED, got {code_own}"

    口 = Step(
        step_num=4,
        name="Full build to reset spec baseline, then edit code and rebuild",
        purpose="Reset spec updated_at first, then code change creates CONTENT_ONLY after it",
    )
    # Reset spec baseline so its updated_at is current
    _init(git_project)

    # Now edit code — the next full build's CONTENT_ONLY row will be
    # after the spec's updated_at from the reset above.
    (git_project / "mod.py").write_text(
        'def process(data):\n    """Process the data."""\n    return data.lower()\n',
        encoding="utf-8",
    )
    _commit(git_project, "Change process implementation")
    _init(git_project)

    口 = Step(
        step_num=5,
        name="Assert spec section is LINKED_STALE",
        purpose="Spec describes code that changed — needs review",
    )
    statuses = _staleness(git_db_path)
    _, spec_link = statuses[spec_section_id]
    assert spec_link == "LINKED_STALE", f"spec link_status: expected LINKED_STALE, got {spec_link}"


# ---------------------------------------------------------------------------
# E7: Consumer doc transitive staleness
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Consumer doc links to dev spec, dev spec links to code. Change the "
        "code — both dev spec AND consumer doc flag LINKED_STALE via "
        "transitive propagation (ADR-016)."
    ),
)
def test_e7_consumer_doc_transitive_staleness(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Write code, dev spec, and consumer guide; commit, init",
        purpose="Set up three-layer provenance chain: code → dev spec → consumer",
        outputs="All nodes indexed and VERIFIED",
    )
    (git_project / "mod.py").write_text(
        'def handle(request):\n    """Handle the request."""\n    return {"status": "ok"}\n',
        encoding="utf-8",
    )
    _write_doc(
        git_project / "docs",
        "dev-spec.json",
        "Dev Spec",
        [
            {
                "id": "handler-section",
                "heading": "Handler",
                "content": "Documents the handle function.",
                "links": [{"node_id": "proj::mod::handle"}],
            }
        ],
    )
    _write_doc(
        git_project / "docs",
        "consumer-guide.json",
        "Consumer Guide",
        [
            {
                "id": "api-section",
                "heading": "API",
                "content": "User-facing guide referencing the handler spec.",
                "links": [{"node_id": "proj::docs.dev-spec::handler-section"}],
            }
        ],
        tags=["consumer"],
    )
    _commit(git_project, "Add handler with dev spec and consumer guide")
    _init(git_project)

    口 = Step(
        step_num=2,
        name="Re-baseline (provenance links declared in the DocJSON link arrays)",
        purpose="Establish a clean staleness baseline; the dev-spec→handle and consumer→dev-spec documents edges are derived from the DocJSON links arrays",
        outputs="dev-spec→handle (documents), consumer→dev-spec (documents), all VERIFIED",
    )
    dev_spec_id = "proj::docs.dev-spec::handler-section"
    consumer_id = "proj::docs.consumer-guide::api-section"

    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes, transitive_tags=["consumer"])

    statuses = _staleness(git_db_path)
    assert statuses[dev_spec_id] == ("VERIFIED", "VERIFIED"), f"dev-spec baseline not clean: {statuses[dev_spec_id]}"
    assert statuses[consumer_id] == ("VERIFIED", "VERIFIED"), f"consumer baseline not clean: {statuses[consumer_id]}"

    口 = Step(
        step_num=3,
        name="Edit function body, commit, full build with transitive_tags",
        purpose="Full build creates CONTENT_ONLY history — cascades through dev spec to consumer",
    )
    (git_project / "mod.py").write_text(
        'def handle(request):\n    """Handle the request."""\n    return {"status": "error"}\n',
        encoding="utf-8",
    )
    _commit(git_project, "Change handle return value")
    _init(git_project, transitive_tags=["consumer"])

    口 = Step(
        step_num=4,
        name="Assert dev spec is LINKED_STALE (direct)",
        purpose="Dev spec documents code that changed — direct LINKED_STALE",
    )
    statuses = _staleness(git_db_path)
    _, dev_link = statuses[dev_spec_id]
    assert dev_link == "LINKED_STALE", f"dev-spec link_status: expected LINKED_STALE, got {dev_link}"

    口 = Step(
        step_num=5,
        name="Assert consumer doc is LINKED_STALE (transitive)",
        purpose="Consumer links to stale dev spec — transitive propagation via ADR-016",
    )
    _, consumer_link = statuses[consumer_id]
    assert consumer_link == "LINKED_STALE", f"consumer link_status: expected LINKED_STALE, got {consumer_link}"

    口 = Step(
        step_num=6,
        name="Mark dev spec clean, re-run staleness",
        purpose="Verification on the direct node should cascade — consumer also resolves",
    )
    dev_spec_node = [n for n in db.all_nodes(git_db_path) if n.id == dev_spec_id][0]
    mark_node_clean(
        git_db_path,
        git_project,
        dev_spec_node,
        reason="Reviewed code change — dev spec still accurate",
        verified_by="agent:test",
    )
    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes, transitive_tags=["consumer"])

    口 = Step(
        step_num=7,
        name="Assert both dev spec and consumer resolve to VERIFIED",
        purpose="mark_clean on direct node clears transitive chain",
    )
    statuses = _staleness(git_db_path)
    _, dev_link = statuses[dev_spec_id]
    assert dev_link == "VERIFIED", f"dev-spec after mark_clean: expected VERIFIED, got {dev_link}"

    _, consumer_link = statuses[consumer_id]
    assert consumer_link == "VERIFIED", f"consumer after mark_clean: expected VERIFIED, got {consumer_link}"


# ---------------------------------------------------------------------------
# E8: Agent task flow end-to-end
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Simulate an agent workflow: search for code, navigate the graph, "
        "read source, implement changes, check staleness, mark clean, "
        "and confirm clean baseline."
    ),
)
def test_e8_agent_task_flow(git_project: Path, git_db_path: Path):
    口 = Step(
        step_num=1,
        name="Set up project with code and doc, link them",
        purpose="Create a realistic starting state an agent would encounter",
        outputs="Indexed project with doc→code edge, all VERIFIED",
    )
    (git_project / "mod.py").write_text(
        'def handler(request):\n    """Handle incoming request."""\n    return {"result": request}\n',
        encoding="utf-8",
    )
    _write_doc(
        git_project / "docs",
        "api.json",
        "API Reference",
        [
            {
                "id": "handler-ref",
                "heading": "Handler",
                "content": "Documents the handler endpoint.",
                "links": [{"node_id": "proj::mod::handler"}],
            }
        ],
    )
    _commit(git_project, "Add handler and API doc")
    _init(git_project)

    handler_node = _find_node(git_db_path, "handler")
    doc_section_id = "proj::docs.api::handler-ref"
    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes)

    口 = Step(
        step_num=2,
        name="Agent searches for the function by name",
        purpose="Simulate axiom_graph_search — find the node by querying the DB",
    )
    search_results = [n for n in db.all_nodes(git_db_path) if "handler" in n.title]
    assert search_results, "Agent search should find the handler node"
    found_node = search_results[0]
    assert found_node.id == handler_node.id

    口 = Step(
        step_num=3,
        name="Agent navigates graph to find linked doc",
        purpose="Simulate axiom_graph_graph — follow inbound edges to find docs",
    )
    with db._connect(git_db_path) as conn:
        edges = conn.execute(
            "SELECT from_id, edge_type, to_id FROM edges WHERE to_id = ?",
            (handler_node.id,),
        ).fetchall()
    doc_edges = [e for e in edges if e["edge_type"] == "documents"]
    assert doc_edges, "Agent should find a documents edge pointing to handler"
    assert doc_edges[0]["from_id"] == doc_section_id

    # Simulate axiom_graph_source: read code via node location
    assert handler_node.location, "handler node should have a location"
    source_path = git_project / handler_node.location
    source_text = source_path.read_text(encoding="utf-8")
    assert "def handler" in source_text, "Source should contain handler function"

    口 = Step(
        step_num=4,
        name="Agent implements changes, commits, full build, checks staleness",
        purpose="Full build creates CONTENT_ONLY history — doc goes LINKED_STALE",
    )
    (git_project / "mod.py").write_text(
        'def handler(request):\n    """Handle incoming request."""\n    return {"result": request, "status": "ok"}\n',
        encoding="utf-8",
    )
    _commit(git_project, "Improve handler response")
    _init(git_project)

    statuses = _staleness(git_db_path)
    _, doc_link = statuses[doc_section_id]
    assert doc_link == "LINKED_STALE", f"Agent check: doc expected LINKED_STALE, got {doc_link}"

    口 = Step(
        step_num=5,
        name="Agent marks stale doc clean with a reason",
        purpose="mark_clean resets baseline — the agent-verified resolution path",
    )
    doc_node = [n for n in db.all_nodes(git_db_path) if n.id == doc_section_id][0]
    mark_node_clean(
        git_db_path,
        git_project,
        doc_node,
        reason="Reviewed handler change — doc still accurate",
        verified_by="agent:test",
    )

    口 = Step(
        step_num=6,
        name="Confirm doc is now clean",
        purpose="Complete the agent loop — mark_clean resets baseline so staleness recomputes as VERIFIED",
    )
    nodes = db.all_nodes(git_db_path)
    record_staleness(git_db_path, git_project, nodes)
    statuses = _staleness(git_db_path)

    _, doc_link = statuses[doc_section_id]
    assert doc_link == "VERIFIED", f"After mark_clean: expected VERIFIED, got {doc_link}"
