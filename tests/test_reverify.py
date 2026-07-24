"""Reverify: one-operation scoped clear of the LINKED_STALE a node caused.

``reverify_node`` asserts "I verified the source; my change to it does not
invalidate its dependents": it resolves each live LINKED_STALE entry back
to its root offenders, clears the nodes rooted entirely at the source via
the mark_clean machinery (with reverify-of-source provenance), skips nodes
that are also stale via other offenders, and finishes with the shared
staleness recompute so aggregates clear in the same call.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from axiom_annotations import workflow, Step

from axiom_graph.index.staleness import (
    _get_linked_stale_ids,
    expand_composes_subtree,
    resolve_root_offenders,
)
from axiom_graph.lifecycle.api import (
    build_index,
    compute_check_summary,
    fetch_history,
    reverify_node,
)
from axiom_graph.lifecycle.mcp_tools import axiom_graph_reverify


# ---------------------------------------------------------------------------
# Unit tests: root-attribution walk + subtree expansion (pure helpers)
# ---------------------------------------------------------------------------


class TestResolveRootOffenders:
    """The via-chain walk resolves stale entries to leaf root offenders."""

    def test_chain_resolves_to_leaf_root(self):
        """A -> B -> C -> X via chain resolves every entry to root X."""
        stale_map = {"c": ["x"], "b": ["c"], "a": ["b"]}
        roots = resolve_root_offenders(stale_map)
        assert roots == {"a": ["x"], "b": ["x"], "c": ["x"]}

    def test_multiple_roots_accumulate(self):
        """A node stale via two chains reports both leaf roots."""
        stale_map = {"n": ["x", "b"], "b": ["y"]}
        roots = resolve_root_offenders(stale_map)
        assert roots["n"] == ["x", "y"]
        assert roots["b"] == ["y"]

    def test_cycles_collapse_without_hanging(self):
        """Via cycles terminate; external roots still resolve, pure cycles yield none."""
        pure_cycle = {"a": ["b"], "b": ["a"]}
        assert resolve_root_offenders(pure_cycle) == {"a": [], "b": []}

        cycle_with_root = {"a": ["b", "x"], "b": ["a"]}
        roots = resolve_root_offenders(cycle_with_root)
        assert roots["a"] == ["x"]
        assert roots["b"] == ["x"]


def test_expand_composes_subtree_is_cycle_safe(tmp_path: Path):
    """Subtree expansion covers nested descendants and survives cycles."""
    children_map = {"a": ["b"], "b": ["c", "a"], "c": []}
    result = expand_composes_subtree(tmp_path, "a", children_map)
    assert result == {"b", "c"}


# ---------------------------------------------------------------------------
# Behavioural fixtures
# ---------------------------------------------------------------------------


def _write_doc(mini_project: Path, filename: str, payload: dict) -> None:
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_code(mini_project: Path, name: str, body: str) -> Path:
    src_dir = mini_project / "src"
    src_dir.mkdir(exist_ok=True)
    code_path = src_dir / f"{name}.py"
    code_path.write_text(body, encoding="utf-8")
    return code_path


def _doc_payload(section_id: str, link_node_id: str) -> dict:
    return {
        "title": section_id.title(),
        "sections": [
            {
                "id": section_id,
                "heading": section_id.title(),
                "content": f"Documents {link_node_id}.",
                "links": [{"node_id": link_node_id}],
            },
        ],
    }


def _cleared_block_ids(report: str) -> list[str]:
    """Node IDs listed under the report's ``Cleared (N):`` heading."""
    ids: list[str] = []
    in_block = False
    for line in report.splitlines():
        if line.startswith("Cleared ("):
            in_block = True
            continue
        if in_block:
            if not line.startswith("- "):
                break
            ids.append(line[2:])
    return ids


# ---------------------------------------------------------------------------
# Tier 3: scoped clear separates offenders (through the MCP surface)
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "A maintainer edits two unrelated code modules, rebuilds, and reverifies "
        "only the first: every dependent whose staleness roots at the first "
        "module clears in one call, while dependents of the second module stay "
        "LINKED_STALE — the clear is scoped to what the source caused"
    ),
)
def test_reverify_clears_only_source_rooted_dependents(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="Two code modules, each documented by its own doc",
        purpose="Set up independent staleness roots X and Y with separate dependents",
    )
    code_x = _write_code(mini_project, "xmod", "def foo():\n    return 0\n")
    code_y = _write_code(mini_project, "ymod", "def bar():\n    return 0\n")
    _write_doc(mini_project, "xdoc.json", _doc_payload("overview", "proj::src.xmod::foo"))
    _write_doc(mini_project, "ydoc.json", _doc_payload("intro", "proj::src.ymod::bar"))
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=2,
        name="Drift both modules and rebuild",
        purpose="Both docs become LINKED_STALE, each rooted at its own module",
    )
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    code_y.write_text("def bar():\n    return 1\n", encoding="utf-8")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    x_section = "proj::docs.xdoc::overview"
    y_section = "proj::docs.ydoc::intro"
    stale_map = _get_linked_stale_ids(db_path)
    assert x_section in stale_map
    assert y_section in stale_map

    口 = Step(
        step_num=3,
        name="Reverify module X through the MCP surface",
        purpose=(
            "The composite module source expands to its function leaves; the "
            "X-rooted doc clears, the Y-rooted doc must not be touched"
        ),
    )
    time.sleep(0.05)
    out = axiom_graph_reverify(
        project_root=str(mini_project),
        node_id="proj::src.xmod",
        reason="foo change is internal; docs still accurate.",
    )
    assert "ERROR" not in out
    cleared_ids = _cleared_block_ids(out)
    assert x_section in cleared_ids
    assert y_section not in cleared_ids
    assert not any(nid.startswith("proj::docs.ydoc") for nid in cleared_ids)

    口 = Step(
        step_num=4,
        name="Verify persisted outcome per node",
        purpose="X-rooted dependent VERIFIED, Y-rooted dependent still LINKED_STALE",
    )
    cs = compute_check_summary(db_path, mini_project)
    assert cs.statuses[x_section][1] == "VERIFIED"
    assert cs.statuses[y_section][1] == "LINKED_STALE"


# ---------------------------------------------------------------------------
# Tier 2: multi-offender skip
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "A doc section stale via two independent code roots is conservatively "
        "skipped by reverify of one root — it stays LINKED_STALE and is "
        "reported with the other offender, because clearing it would silently "
        "discharge the other root's staleness too"
    ),
)
def test_reverify_skips_nodes_with_other_offenders(mini_project: Path, db_path: Path):
    code_x = _write_code(mini_project, "xmod", "def foo():\n    return 0\n")
    code_y = _write_code(mini_project, "ymod", "def bar():\n    return 0\n")
    _write_doc(
        mini_project,
        "both.json",
        {
            "title": "Both",
            "sections": [
                {
                    "id": "combined",
                    "heading": "Combined",
                    "content": "Documents foo and bar together.",
                    "links": [
                        {"node_id": "proj::src.xmod::foo"},
                        {"node_id": "proj::src.ymod::bar"},
                    ],
                },
            ],
        },
    )
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    code_y.write_text("def bar():\n    return 1\n", encoding="utf-8")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    section_id = "proj::docs.both::combined"
    assert section_id in _get_linked_stale_ids(db_path)

    time.sleep(0.05)
    result = reverify_node(
        db_path,
        mini_project,
        "proj::src.xmod::foo",
        "foo verified.",
        verified_by="agent",
    )

    assert result.skipped == {section_id: ["proj::src.ymod::bar"]}
    assert section_id not in result.cleared
    assert section_id not in result.verified

    # The MCP surface renders the skip with the other offender named, so the
    # caller knows exactly which reverify would discharge the remainder.
    time.sleep(0.05)
    out = axiom_graph_reverify(
        project_root=str(mini_project),
        node_id="proj::src.xmod::foo",
        reason="foo verified.",
    )
    assert "Skipped — also stale via other offenders (1):" in out
    assert f"- {section_id} (other offenders: proj::src.ymod::bar)" in out
    assert section_id not in _cleared_block_ids(out)

    cs = compute_check_summary(db_path, mini_project)
    assert cs.statuses[section_id][1] == "LINKED_STALE"


# ---------------------------------------------------------------------------
# Tier 2: transitive doc-to-doc chain resolves to the root offender
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "With transitive tags configured, a doc-to-doc chain (top documents mid "
        "documents base documents code) goes LINKED_STALE end-to-end when the "
        "code drifts; one reverify of the code node resolves the one-hop via "
        "entries back to the root and clears the entire chain in one operation"
    ),
)
def test_reverify_clears_transitive_doc_chain(mini_project: Path, db_path: Path):
    (mini_project / "axiom-graph.toml").write_text(
        '[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n', encoding="utf-8"
    )
    code_x = _write_code(mini_project, "core", "def foo():\n    return 0\n")
    _write_doc(mini_project, "base.json", _doc_payload("impl", "proj::src.core::foo"))
    _write_doc(
        mini_project,
        "mid.json",
        {
            "title": "Mid",
            "tags": ["consumer"],
            "sections": [
                {
                    "id": "sec",
                    "heading": "Sec",
                    "content": "Consumes base.",
                    "links": [{"node_id": "proj::docs.base::impl"}],
                },
            ],
        },
    )
    _write_doc(
        mini_project,
        "top.json",
        {
            "title": "Top",
            "tags": ["consumer"],
            "sections": [
                {
                    "id": "sec",
                    "heading": "Sec",
                    "content": "Consumes mid.",
                    "links": [{"node_id": "proj::docs.mid::sec"}],
                },
            ],
        },
    )
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    base_sec = "proj::docs.base::impl"
    mid_sec = "proj::docs.mid::sec"
    top_sec = "proj::docs.top::sec"
    stale_map = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])
    assert {base_sec, mid_sec, top_sec} <= set(stale_map)
    # Sanity: the transitive entries carry one-hop vias, not the root.
    assert stale_map[top_sec] == [mid_sec]

    time.sleep(0.05)
    result = reverify_node(
        db_path,
        mini_project,
        "proj::src.core::foo",
        "Signature unchanged; docs remain accurate.",
        verified_by="agent",
    )

    assert {base_sec, mid_sec, top_sec} <= set(result.cleared)
    assert result.skipped == {}

    cs = compute_check_summary(db_path, mini_project)
    assert cs.statuses[base_sec][1] == "VERIFIED"
    assert cs.statuses[mid_sec][1] == "VERIFIED"
    assert cs.statuses[top_sec][1] == "VERIFIED"


# ---------------------------------------------------------------------------
# Tier 3: aggregates clear in the same call
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "A doc envelope that is LINKED_STALE only by inheritance from a "
        "section caused by code node X reads as cleared in reverify(X)'s own "
        "report — the operation ends with a recompute, so the maintainer sees "
        "the aggregate resolve without issuing a second command"
    ),
)
def test_reverify_report_shows_envelope_cleared_in_same_call(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="Doc with one section documenting code; drift the code",
        purpose="Section gets the own LINKED_STALE signal; the envelope inherits it",
    )
    code_x = _write_code(mini_project, "mod", "def foo():\n    return 0\n")
    _write_doc(mini_project, "spec.json", _doc_payload("overview", "proj::src.mod::foo"))
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    envelope_id = "proj::docs.spec"
    section_id = "proj::docs.spec::overview"
    persisted = compute_check_summary(db_path, mini_project)
    assert persisted.statuses[envelope_id][1] == "LINKED_STALE"

    口 = Step(
        step_num=2,
        name="Single reverify of the code node through the MCP surface",
        purpose="The report itself must show the envelope cleared, with before/after counts",
    )
    time.sleep(0.05)
    out = axiom_graph_reverify(
        project_root=str(mini_project),
        node_id="proj::src.mod::foo",
        reason="Behavior change documented elsewhere; this doc unaffected.",
    )
    assert "ERROR" not in out
    assert section_id in out
    assert envelope_id in out
    assert "LINKED_STALE before:" in out

    口 = Step(
        step_num=3,
        name="Envelope is VERIFIED on both dimensions after the call",
        purpose="No second command needed for the aggregate to resolve",
    )
    cs = compute_check_summary(db_path, mini_project)
    own, link, _via = cs.statuses[envelope_id]
    assert (own, link) == ("VERIFIED", "VERIFIED")


# ---------------------------------------------------------------------------
# Tier 2: auditable report + cascade provenance
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "The reverify result is auditable: it lists verified and cleared node "
        "IDs with before/after LINKED_STALE counts, and cascade-cleared "
        "dependents carry a history row whose reason ties them to the reverify "
        "of the source, distinguishable from individually reviewed verifications"
    ),
)
def test_reverify_report_and_history_provenance(mini_project: Path, db_path: Path):
    code_x = _write_code(mini_project, "mod", "def foo():\n    return 0\n")
    _write_doc(mini_project, "spec.json", _doc_payload("overview", "proj::src.mod::foo"))
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    source_id = "proj::src.mod::foo"
    section_id = "proj::docs.spec::overview"

    time.sleep(0.05)
    result = reverify_node(
        db_path,
        mini_project,
        source_id,
        "Docs re-read against the new implementation.",
        verified_by="agent",
    )

    assert result.verified == [source_id, section_id]
    assert section_id in result.cleared
    assert result.before_linked_stale > result.after_linked_stale
    assert result.after_linked_stale == 0

    # Cascade-cleared node's verification history carries the provenance tag.
    hist = fetch_history(db_path, section_id, max_results=20)
    verified_rows = [r for r in hist.rows if r.change_type == "AGENT_VERIFIED"]
    assert verified_rows, "expected an AGENT_VERIFIED history row for the cascade-cleared node"
    metas = [json.loads(r.meta)["reason"] for r in verified_rows if r.meta]
    assert any(m.startswith(f"[reverify:{source_id}]") for m in metas)

    # The source's own verification is the plain reason — not cascade-tagged.
    src_hist = fetch_history(db_path, source_id, max_results=20)
    src_metas = [json.loads(r.meta)["reason"] for r in src_hist.rows if r.change_type == "AGENT_VERIFIED" and r.meta]
    assert any(not m.startswith("[reverify:") for m in src_metas)


# ---------------------------------------------------------------------------
# Tier 1: idempotence + missing source
# ---------------------------------------------------------------------------


def test_reverify_nothing_to_clear_is_honest_success(mini_project: Path, db_path: Path):
    """Reverify with no caused staleness reports zero cleared, no error."""
    _write_code(mini_project, "mod", "def foo():\n    return 0\n")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    result = reverify_node(
        db_path,
        mini_project,
        "proj::src.mod::foo",
        "Nothing drifted.",
        verified_by="agent",
    )
    assert result.not_found is False
    assert result.verified == ["proj::src.mod::foo"]
    assert result.cleared == []
    assert result.skipped == {}
    assert result.before_linked_stale == result.after_linked_stale == 0


def test_reverify_unknown_source_reports_not_found(mini_project: Path, db_path: Path):
    """An unknown source node yields not_found, and the MCP surface an ERROR string."""
    _write_code(mini_project, "mod", "def foo():\n    return 0\n")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    result = reverify_node(db_path, mini_project, "proj::nope", "x", verified_by="agent")
    assert result.not_found is True

    out = axiom_graph_reverify(project_root=str(mini_project), node_id="proj::nope", reason="x")
    assert out == "ERROR: Node 'proj::nope' not found."
