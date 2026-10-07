"""Tests for ``axiom_graph_info``: one project's facts, read without touching the index."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from axiom_annotations import Step, workflow

import axiom_graph
from axiom_graph.config import db_path_for
from axiom_graph.db.migrations import CURRENT_SCHEMA_VERSION
from axiom_graph.lifecycle.api import build_index
from axiom_graph.mcp.server import mcp
from axiom_graph.docjson.api import axiom_graph_write_doc
from axiom_graph.index.builder import PROJECT_ID_META_KEY
from axiom_graph.project.api import DEFAULT_POLICY_SECTIONS, POLICY_RENDER_LIMIT, project_facts

_DEFAULT_PHRASE = DEFAULT_POLICY_SECTIONS[1][2][:40]

_MODULE = 'def greet(name):\n    """Say hello."""\n    return f"hello {name}"\n'

_NESTED_TOML = """\
[axiom_graph]
project_id = "demo"

[axiom_graph.scan]
docs_dirs = ["documentation", ".pev"]
docs_extensions = [".json", ".docjson"]
config_dirs = [".agents"]
exclude_dirs = ["generated"]
js_paths = ["web/*.ts"]

[axiom_graph.staleness]
frozen_tags = ["adr"]
transitive_tags = ["spec"]
"""


def _info(root: Path) -> str:
    blocks = list(asyncio.run(mcp.call_tool("axiom_graph_info", {"project_root": str(root)})))
    return blocks[0].text


def _line(text: str, label: str) -> str:
    """Return the one output line that starts with *label*."""
    matches = [line for line in text.splitlines() if line.startswith(label)]
    assert len(matches) == 1, f"{label!r}: {matches}"
    return matches[0]


def _gloss_after(text: str, label: str) -> str:
    """Return the line right after the one starting with *label* (its gloss)."""
    lines = text.splitlines()
    index = lines.index(_line(text, label))
    return lines[index + 1]


def _project(root: Path, toml: str | None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "hello.py").write_text(_MODULE, encoding="utf-8")
    if toml is not None:
        (root / "axiom-graph.toml").write_text(toml, encoding="utf-8")
    return root


# ===================================================================
# Tier 3 -- user story
# ===================================================================


@workflow(purpose="An agent reads every project fact in one call, taken from the project's own nested toml")
def test_info_reports_every_fact_from_the_projects_own_config(tmp_path):
    口 = Step(
        step_num=1,
        name="Build a project with customised settings",
        purpose="A toml that sets every scan and staleness key info reports, then a real build",
    )
    root = _project(tmp_path / "proj", _NESTED_TOML)
    for d in ("documentation", ".pev", ".agents", "web"):
        (root / d).mkdir()
    build_index(db_path_for(root), root)

    口 = Step(step_num=2, name="Ask for the facts", purpose="Call the registered axiom_graph_info tool")
    text = _info(root)

    口 = Step(
        step_num=3,
        name="Check each line",
        purpose="Every fact is on its own line and carries the toml's value, not the default",
    )
    assert text.splitlines()[0].startswith(f"axiom-graph {axiom_graph.__version__}")
    assert f"index schema {CURRENT_SCHEMA_VERSION}" in text.splitlines()[0]
    assert "|" not in text
    assert _line(text, "project id").split()[2] == "demo"
    assert str(root.resolve()) in _line(text, "project root")
    assert _line(text, "docs_dirs").split(None, 1)[1] == "documentation, .pev"
    extensions = _line(text, "docs_extensions")
    assert ".json, .docjson" in extensions
    assert "(new docs: .json)" in extensions
    assert _line(text, "config_dirs").split(None, 1)[1] == ".agents"
    assert _line(text, "frozen_tags").split(None, 1)[1] == "adr"
    assert "LINKED_STALE" in _gloss_after(text, "frozen_tags")
    assert _line(text, "transitive_tags").split(None, 1)[1] == "spec"
    assert "LINKED_STALE" in _gloss_after(text, "transitive_tags")
    db_line = _line(text, "db")
    assert str(db_path_for(root)) in db_line
    assert " nodes" in db_line and " docs" in db_line
    scanned = _line(text, "scanned")
    for part in ("js_paths web/*.ts", "docs_dirs documentation, .pev", "config_dirs .agents"):
        assert part in scanned, part
    excluded = _line(text, "excluded")
    assert "generated" in excluded
    assert ".git" in excluded


@workflow(purpose="info ends with the shipped default policy until a doc tagged agent-policy replaces it")
def test_info_shows_the_default_policy_until_a_policy_doc_exists(tmp_path):
    口 = Step(
        step_num=1,
        name="Build a project with no policy doc",
        purpose="A plain build never writes a policy doc",
    )
    root = _project(tmp_path / "proj", None)
    (root / "docs").mkdir()
    build_index(db_path_for(root), root, project_id="p")

    口 = Step(
        step_num=2,
        name="Read the fallback",
        purpose="info shows the shipped default, labelled as the default, with how to make it yours",
    )
    text = _info(root)
    source = _line(text, "agent policy")
    assert "shipped default" in source
    assert "axiom-graph init --policy" in source
    assert _DEFAULT_PHRASE in text

    口 = Step(
        step_num=3,
        name="Write the project's own policy",
        purpose="A doc tagged agent-policy, written through the doc write path",
    )
    result = axiom_graph_write_doc(
        str(root),
        {
            "id": "house-rules",
            "title": "House rules",
            "tags": ["agent-policy"],
            "sections": [{"id": "tabs", "heading": "Indentation", "content": "Use tabs in every file."}],
        },
    )
    assert not result.startswith("ERROR"), result

    口 = Step(
        step_num=4,
        name="Read the project's policy",
        purpose="info shows that doc under its id and none of the default text",
    )
    text = _info(root)
    assert _line(text, "agent policy").split()[2] == "p::docs/house-rules"
    assert "Use tabs in every file." in text
    assert "shipped default" not in text
    assert _DEFAULT_PHRASE not in text


# ===================================================================
# Tier 2 -- subsystem
# ===================================================================


@workflow(purpose="info reports the id the index stores, not the folder name, when there is no toml")
def test_info_reports_the_stored_id_when_the_toml_is_absent(tmp_path):
    root = _project(tmp_path / "folder-name", None)
    build_index(db_path_for(root), root, project_id="stored-id")

    line = _line(_info(root), "project id")

    assert line.split()[2] == "stored-id"
    assert "folder-name" not in line
    assert "no index" not in line


@workflow(purpose="Before the first build, info answers from config and never creates the index")
def test_info_before_the_first_build_creates_no_index(tmp_path):
    root = _project(
        tmp_path / "proj", '[axiom_graph]\nproject_id = "early"\n\n[axiom_graph.scan]\ndocs_dirs = ["notes"]\n'
    )

    text = _info(root)

    assert _line(text, "project id").split()[2] == "early"
    assert "(no index yet; run build)" in _line(text, "project id")
    assert "index schema" not in text
    assert " nodes" not in text
    assert _line(text, "docs_dirs").split(None, 1)[1] == "notes"
    assert "picked up after the first build" in _line(text, "agent policy")
    assert not (root / ".axiom_graph").exists()


# ===================================================================
# Tier 1 -- internal logic
# ===================================================================


def test_unbuilt_project_takes_the_folder_name_without_a_toml(tmp_path):
    root = _project(tmp_path / "plain", None)

    facts = project_facts(root)

    assert facts.project_id == "plain"
    assert not facts.indexed
    assert facts.node_count is None


def test_an_index_that_stores_no_id_falls_back_to_the_toml_id_and_says_so(tmp_path):
    root = _project(tmp_path / "folder-name", '[axiom_graph]\nproject_id = "from-toml"\n')
    build_index(db_path_for(root), root)
    conn = sqlite3.connect(db_path_for(root))
    conn.execute("DELETE FROM index_meta WHERE key = ?", (PROJECT_ID_META_KEY,))
    conn.execute("DELETE FROM nodes")
    conn.commit()
    conn.close()

    facts = project_facts(root)
    line = _line(_info(root), "project id")

    assert facts.indexed and not facts.id_from_index
    assert facts.project_id == "from-toml"
    assert line.split()[2] == "from-toml"
    assert "the index stores no id" in line


def test_info_flags_a_toml_id_that_disagrees_with_the_stored_id(tmp_path):
    root = _project(tmp_path / "proj", None)
    build_index(db_path_for(root), root, project_id="built")
    (root / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "edited"\n', encoding="utf-8")

    line = _line(_info(root), "project id")

    assert line.split()[2] == "built"
    assert "edited" in line
    assert "build" in line


def test_info_flags_an_index_schema_the_package_does_not_match(tmp_path):
    root = _project(tmp_path / "proj", None)
    build_index(db_path_for(root), root, project_id="p")
    conn = sqlite3.connect(db_path_for(root))
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    header = _info(root).splitlines()[0]

    assert f"index schema {CURRENT_SCHEMA_VERSION + 1}" in header
    assert f"package expects {CURRENT_SCHEMA_VERSION}" in header
    with sqlite3.connect(db_path_for(root)) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION + 1


def _policy_doc(slug: str, sections: list[dict]) -> dict:
    return {"id": slug, "title": slug, "tags": ["agent-policy"], "sections": sections}


def test_several_policy_docs_show_the_lowest_id_and_name_the_rest(tmp_path):
    root = _project(tmp_path / "proj", None)
    (root / "docs").mkdir()
    build_index(db_path_for(root), root, project_id="p")
    for slug in ("b-rules", "a-rules"):
        section = {"id": "only", "heading": "Only", "content": f"from {slug}"}
        assert not axiom_graph_write_doc(str(root), _policy_doc(slug, [section])).startswith("ERROR")

    text = _info(root)

    assert _line(text, "agent policy").split()[2] == "p::docs/a-rules"
    assert "from a-rules" in text
    assert "from b-rules" not in text
    assert "p::docs/b-rules" in text


def test_a_long_policy_doc_is_cut_with_a_pointer_to_read_doc(tmp_path):
    root = _project(tmp_path / "proj", None)
    (root / "docs").mkdir()
    build_index(db_path_for(root), root, project_id="p")
    sections = [{"id": f"s-{i}", "heading": f"Rule {i}", "content": "x" * 900} for i in range(10)]
    assert not axiom_graph_write_doc(str(root), _policy_doc("long", sections)).startswith("ERROR")

    policy = project_facts(root).policy
    text = _info(root)

    assert policy.truncated
    assert sum(len(s.heading) + len(s.content) for s in policy.sections) <= POLICY_RENDER_LIMIT
    assert 'read_doc(doc_id="p::docs/long")' in text
