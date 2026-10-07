"""Behavioural tests for cloning a DocJSON document with section overrides.

A clone copies an indexed doc to a new one: everything verbatim except the
tool-write stamps, with the content of named sections replaced and named
sections dropped.  A template mismatch, or a destination that already
exists, is refused before anything is written.  All calls enter through
``axiom_graph.docjson.api``.
"""

from __future__ import annotations

import json
import sqlite3
import re
from pathlib import Path

import pytest

from axiom_annotations import Step, workflow

from axiom_graph.docjson import api
from axiom_graph.docjson.api import axiom_graph_clone_doc, axiom_graph_read_doc, axiom_graph_write_doc
from axiom_graph.index import db, doc_stamps
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary

CODE_ID = "proj::src.mod::foo"
SOURCE_ID = "proj::docs/template"

TEMPLATE = {
    "title": "Cycle Template",
    "tags": ["template", "pev"],
    "meta": {"template_version": 3, "owner": "pev"},
    "sections": [
        {
            "id": "overview",
            "heading": "Overview",
            "content": "Fill me.",
            "links": [{"node_id": CODE_ID}, {"node_id": CODE_ID}],  # linked twice: one edge
        },
        {
            "id": "builder",
            "heading": "Builder",
            "content": "Builder notes.",
            "tags": ["phase"],
            "sections": [
                {
                    "id": "friction",
                    "heading": "Friction",
                    "content": "Nested friction.",
                    "links": [{"node_id": CODE_ID}],
                    "sections": [{"id": "deep", "heading": "Deep", "content": "Deep child."}],
                },
                {"id": "plan", "heading": "Plan", "content": "Plan here."},
            ],
        },
        {"id": "friction", "heading": "Top Friction", "content": "Top-level friction."},
        {"id": "scratch", "heading": "Scratch", "content": "Drop me.", "sections": [{"id": "x", "heading": "X"}]},
    ],
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with one code node, an indexed template doc and a second doc ``other``."""
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def foo():\n    return 0\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "template.docjson").write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")
    (docs / "other.docjson").write_text(
        json.dumps({"title": "Other", "sections": [{"id": "a", "heading": "A"}]}), encoding="utf-8"
    )
    build_index(_db_path(str(tmp_path)), tmp_path)
    return tmp_path


def _without_stamps(value):
    """*value* with every tool-write stamp key removed, recursively."""
    if isinstance(value, dict):
        return {k: _without_stamps(v) for k, v in value.items() if k != doc_stamps.STAMP_KEY}
    if isinstance(value, list):
        return [_without_stamps(v) for v in value]
    return value


def _doc_ids(dbp: Path) -> set[str]:
    with db._connect(dbp) as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM nodes WHERE subtype = 'docjson_doc'")}


@workflow(
    purpose="An agent scaffolds a doc from an indexed template in one call: the copy is readable at once under the "
    "reported id, overridden sections change content only, omitted subtrees are gone, meta, title and tags carry "
    "over, everything else matches the source ignoring stamps, and the source is untouched"
)
def test_clone_scaffolds_a_doc_from_a_template(project: Path, monkeypatch) -> None:
    dbp = _db_path(str(project))
    real_connect, real_read, real_save = sqlite3.connect, Path.read_text, api.save_and_reindex
    counts: dict[str, int] = {"connections": 0, "saves": 0}
    reads: dict[str, int] = {}

    def connect(*args, **kwargs):
        counts["connections"] += 1
        return real_connect(*args, **kwargs)

    def read_text(self, *args, **kwargs):
        if self.suffix == ".docjson":
            reads[self.name] = reads.get(self.name, 0) + 1
        return real_read(self, *args, **kwargs)

    def save(*args, **kwargs):
        counts["saves"] += 1
        return real_save(*args, **kwargs)

    source_file = project / "docs" / "template.docjson"
    source_bytes = source_file.read_bytes()
    source_hash = db.get_node(dbp, SOURCE_ID).code_hash

    口 = Step(
        step_num=1,
        name="Clone with overrides",
        purpose="Replace a nested section whose leaf name a top-level section shares, and omit a subtree",
    )
    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", connect)
        m.setattr(Path, "read_text", read_text)
        m.setattr(api, "save_and_reindex", save)
        res = axiom_graph_clone_doc(
            str(project),
            SOURCE_ID,
            "cycles/run-1",
            set_sections={"builder.friction": "Filled in."},
            omit_sections=["scratch"],
        )
    assert res.startswith("Wrote"), res
    # One connection and one save (write + re-index) for the call; the source is read once.
    assert counts == {"connections": 1, "saves": 1}, counts
    assert reads["template.docjson"] == 1, reads
    reported = re.search(r"doc id\s*:\s*(\S+)", res).group(1)
    assert reported == "proj::docs/cycles/run-1"

    口 = Step(step_num=2, name="Read the copy back", purpose="read_doc resolves the reported id with no rebuild")
    text = axiom_graph_read_doc(str(project), doc_id=reported)
    assert "Filled in." in text and "Top-level friction." in text, text

    口 = Step(
        step_num=3,
        name="Compare with the source",
        purpose="Only the overridden content and the omitted subtree differ, ignoring stamps",
    )
    clone = json.loads((project / "docs" / "cycles" / "run-1.docjson").read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(TEMPLATE))
    expected["sections"][1]["sections"][0]["content"] = "Filled in."
    del expected["sections"][3]
    assert _without_stamps(clone) == expected
    statuses = compute_check_summary(dbp, project).statuses
    for dot in ("overview", "builder", "builder.friction", "builder.friction.deep", "builder.plan", "friction"):
        assert statuses[f"{reported}::{dot}"][:2] == ("VERIFIED", "VERIFIED"), dot
    assert db.get_node(dbp, f"{reported}::scratch") is None
    with db._connect(dbp) as conn:
        linked = sorted(
            r["node_id"]
            for r in conn.execute(
                "SELECT node_id FROM node_history WHERE change_type = 'LINK_ADDED' AND node_id LIKE ?",
                (f"{reported}%",),
            )
        )
    assert linked == [f"{reported}::builder.friction", f"{reported}::overview"]

    口 = Step(step_num=4, name="Clone with title and tags", purpose="Given title and tags replace the source's")
    res = axiom_graph_clone_doc(str(project), SOURCE_ID, "cycles/run-2", title="Run Two", tags=["cycle"])
    assert res.startswith("Wrote"), res
    second = json.loads((project / "docs" / "cycles" / "run-2.docjson").read_text(encoding="utf-8"))
    assert (second["title"], second["tags"], second["meta"]) == ("Run Two", ["cycle"], TEMPLATE["meta"])

    口 = Step(step_num=5, name="Source untouched", purpose="The source file and its index entry are unchanged")
    assert source_file.read_bytes() == source_bytes
    assert db.get_node(dbp, SOURCE_ID).code_hash == source_hash


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {"new_id": "fresh", "set_sections": {"nope": "x", "builder.nope": "y", "overview": "ok"}},
            ["unknown in set_sections: nope, builder.nope"],
            id="unknown-set-ids",
        ),
        pytest.param(
            {"new_id": "fresh", "omit_sections": ["nope", "builder.plan.nope"]},
            ["unknown in omit_sections: nope, builder.plan.nope"],
            id="unknown-omit-ids",
        ),
        pytest.param(
            {"new_id": "fresh", "omit_sections": ["builder"], "set_sections": {"builder.plan": "x", "scratch": "y"}},
            ["unknown in set_sections: builder.plan"],
            id="set-inside-omitted",
        ),
        pytest.param(
            {"new_id": "fresh", "omit_sections": ["scratch"], "set_sections": {"scratch": "y"}},
            ["in both set_sections and omit_sections: scratch"],
            id="set-and-omit-same-id",
        ),
        pytest.param({"new_id": "other"}, ["already exists", "proj::docs/other"], id="existing-destination"),
        pytest.param({"new_id": "template"}, ["is the source doc"], id="destination-is-source"),
        pytest.param({"new_id": "proj::docs/fresh"}, ["write_doc"], id="node-id-new-id"),
        pytest.param({"new_id": "fresh", "docs_root": "nowhere"}, ["write_doc"], id="unknown-docs-root"),
    ],
)
def test_a_refused_clone_writes_nothing(project: Path, kwargs: dict, expected: list[str]) -> None:
    """Every refusal is one ERROR naming its cause, and no file or doc node appears; input errors match write_doc's."""
    dbp = _db_path(str(project))
    files_before = sorted(p.relative_to(project).as_posix() for p in (project / "docs").rglob("*"))
    docs_before = _doc_ids(dbp)

    res = axiom_graph_clone_doc(str(project), SOURCE_ID, **kwargs)

    assert res.startswith("ERROR:") and res.count("ERROR:") == 1, res
    if expected == ["write_doc"]:
        assert res == axiom_graph_write_doc(
            str(project),
            {"id": kwargs["new_id"], "title": "T", "sections": []},
            docs_root=kwargs.get("docs_root"),
        )
    else:
        for fragment in expected:
            assert fragment in res, res
    assert sorted(p.relative_to(project).as_posix() for p in (project / "docs").rglob("*")) == files_before
    assert _doc_ids(dbp) == docs_before


def test_clone_of_a_source_with_a_malformed_link_is_refused(project: Path) -> None:
    """A source whose links hold an entry without a usable node_id is indexed, but cloning it is refused."""
    dbp = _db_path(str(project))
    bad = {"title": "Bad", "sections": [{"id": "a", "heading": "A", "links": [{"target": CODE_ID}]}]}
    (project / "docs" / "bad.docjson").write_text(json.dumps(bad), encoding="utf-8")
    build_index(dbp, project)
    assert db.get_node(dbp, "proj::docs/bad::a") is not None

    res = axiom_graph_clone_doc(str(project), "proj::docs/bad", "copy")

    assert res.startswith("ERROR:") and "'a'" in res and '"target"' in res, res
    assert not (project / "docs" / "copy.docjson").exists()
