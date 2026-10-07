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

import pytest
from axiom_annotations import workflow, Step

from axiom_graph.index.mark_clean import (
    VERIFICATION_OP_MARK_CLEAN,
    VERIFICATION_OP_REVERIFY,
)
from axiom_graph.index.staleness import (
    _get_linked_stale_ids,
    already_reverified_offenders,
    composes_ancestors,
    expand_composes_subtree,
    resolve_root_offenders,
)
from axiom_graph.lifecycle.api import (
    REVERIFY_MARKED_CLEAN_NOTE,
    REVERIFY_SKIP_HINT,
    build_index,
    compute_check_summary,
    fetch_history,
    mark_clean_nodes,
    reverify_node,
    reverify_nodes,
)
from axiom_graph.lifecycle.mcp_tools import axiom_graph_reverify
from axiom_graph.query.api import compute_drift_query


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


TWO_OFFENDER_SECTION = "proj::docs/both::combined"
TWO_OFFENDER_ENVELOPE = "proj::docs/both"
OFFENDER_A = "proj::src.xmod::foo"
OFFENDER_B = "proj::src.ymod::bar"


def _build_two_offender_project(project: Path) -> Path:
    """Build a project whose one doc section is stale via two code offenders.

    Returns the project's DB path.  The section
    :data:`TWO_OFFENDER_SECTION` documents both :data:`OFFENDER_A` and
    :data:`OFFENDER_B`; both drift, so the section roots at each of them.
    """
    db_p = project / ".axiom_graph" / "graph.db"
    code_x = _write_code(project, "xmod", "def foo():\n    return 0\n")
    code_y = _write_code(project, "ymod", "def bar():\n    return 0\n")
    _write_doc(
        project,
        "both.json",
        {
            "title": "Both",
            "sections": [
                {
                    "id": "combined",
                    "heading": "Combined",
                    "content": "Documents foo and bar together.",
                    "links": [{"node_id": OFFENDER_A}, {"node_id": OFFENDER_B}],
                },
            ],
        },
    )
    build_index(db_p, project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    code_y.write_text("def bar():\n    return 1\n", encoding="utf-8")
    build_index(db_p, project, project_id="proj", discovery_only=False)
    assert TWO_OFFENDER_SECTION in _get_linked_stale_ids(db_p)
    return db_p


def _build_staggered_offender_project(project: Path, *, verify_between: bool) -> Path:
    """Build the two-offender section, but drift its offenders one at a time.

    :data:`OFFENDER_A` drifts first; when *verify_between* is set the
    section is then marked clean (its prose re-read against that change);
    finally :data:`OFFENDER_B` drifts.  Returns the project's DB path.
    """
    db_p = project / ".axiom_graph" / "graph.db"
    code_x = _write_code(project, "xmod", "def foo():\n    return 0\n")
    code_y = _write_code(project, "ymod", "def bar():\n    return 0\n")
    _write_doc(
        project,
        "both.json",
        {
            "title": "Both",
            "sections": [
                {
                    "id": "combined",
                    "heading": "Combined",
                    "content": "Documents foo and bar together.",
                    "links": [{"node_id": OFFENDER_A}, {"node_id": OFFENDER_B}],
                },
            ],
        },
    )
    build_index(db_p, project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    build_index(db_p, project, project_id="proj", discovery_only=False)
    if verify_between:
        time.sleep(0.05)
        mark_clean_nodes(db_p, project, [TWO_OFFENDER_SECTION], "Re-read against foo.", verified_by="agent")
    time.sleep(0.05)
    code_y.write_text("def bar():\n    return 1\n", encoding="utf-8")
    build_index(db_p, project, project_id="proj", discovery_only=False)
    return db_p


def _drift_query_row(db_path: Path, project: Path, node_id: str) -> str:
    """The ``drift_query`` full-format row for *node_id*."""
    out = compute_drift_query(db_path, project, filter="LINKED_STALE", format="full")
    rows = [line for line in out.splitlines() if line.startswith(f"id={node_id} ")]
    assert len(rows) == 1, out
    return rows[0]


def _dependent_state(db_path: Path, project: Path) -> dict[str, tuple[str, str]]:
    """The (own, link) status of the two-offender dependent and its envelope."""
    cs = compute_check_summary(db_path, project)
    return {nid: cs.statuses[nid][:2] for nid in (TWO_OFFENDER_SECTION, TWO_OFFENDER_ENVELOPE)}


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

    x_section = "proj::docs/xdoc::overview"
    y_section = "proj::docs/ydoc::intro"
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
        verbose=True,
        project_root=str(mini_project),
        node_id="proj::src.xmod",
        reason="foo change is internal; docs still accurate.",
    )
    assert "ERROR" not in out
    cleared_ids = _cleared_block_ids(out)
    assert x_section in cleared_ids
    assert y_section not in cleared_ids
    assert not any(nid.startswith("proj::docs/ydoc") for nid in cleared_ids)

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

    section_id = "proj::docs/both::combined"
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
        verbose=True,
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
                    "links": [{"node_id": "proj::docs/base::impl"}],
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
                    "links": [{"node_id": "proj::docs/mid::sec"}],
                },
            ],
        },
    )
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    base_sec = "proj::docs/base::impl"
    mid_sec = "proj::docs/mid::sec"
    top_sec = "proj::docs/top::sec"
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
        "section caused by code node X resolves within reverify(X) — the "
        "operation ends with a recompute, so no second command is needed. "
        "The report lists only the section as cleared and counts the envelope "
        "as settled by inheritance"
    ),
)
def test_reverify_settles_the_envelope_in_same_call_without_listing_it_cleared(mini_project: Path, db_path: Path):
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

    envelope_id = "proj::docs/spec"
    section_id = "proj::docs/spec::overview"
    persisted = compute_check_summary(db_path, mini_project)
    assert persisted.statuses[envelope_id][1] == "LINKED_STALE"

    口 = Step(
        step_num=2,
        name="Single reverify of the code node through the MCP surface",
        purpose=(
            "The report lists the section, and only the section, as cleared and counts "
            "the envelope under settled by inheritance, with before/after counts"
        ),
    )
    time.sleep(0.05)
    out = axiom_graph_reverify(
        verbose=True,
        project_root=str(mini_project),
        node_id="proj::src.mod::foo",
        reason="Behavior change documented elsewhere; this doc unaffected.",
    )
    assert "ERROR" not in out
    assert _cleared_block_ids(out) == [section_id]
    assert f"- {envelope_id}" not in out.splitlines()
    assert "Settled by inheritance (not listed as cleared): 1" in out.splitlines()
    assert "LINKED_STALE before: 2 -> after: 0 (as check counts it, frozen docs excluded)" in out.splitlines()

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
    section_id = "proj::docs/spec::overview"

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


# ---------------------------------------------------------------------------
# Tier 3: reverifies compose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [(OFFENDER_A, OFFENDER_B), (OFFENDER_B, OFFENDER_A)],
    ids=["a-then-b", "b-then-a"],
)
@workflow(
    purpose=(
        "Reverifies compose: a doc section documenting two changed functions is "
        "cleared by reverifying each of them in turn, in either order. Each "
        "reverify contributes its own offender and the section clears when the "
        "last one lands, reaching the same end state as marking that section "
        "clean directly"
    ),
)
def test_reverifies_compose_to_clear_a_multi_offender_dependent(
    mini_project: Path,
    db_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    first: str,
    second: str,
):
    口 = Step(
        step_num=1,
        name="One doc section documenting two functions, both changed",
        purpose="The section roots at two offenders, so no single reverify covers it",
    )
    _build_two_offender_project(mini_project)

    口 = Step(
        step_num=2,
        name="Reverify the first offender",
        purpose=(
            "Partial composition, honestly reported: the section stays stale and "
            "the skip names the offender still outstanding"
        ),
    )
    time.sleep(0.05)
    result_1 = reverify_node(db_path, mini_project, first, "First reviewed.", verified_by="agent")
    assert result_1.skipped == {TWO_OFFENDER_SECTION: [second]}
    assert TWO_OFFENDER_SECTION not in result_1.cleared
    assert compute_check_summary(db_path, mini_project).statuses[TWO_OFFENDER_SECTION][1] == "LINKED_STALE"

    口 = Step(
        step_num=3,
        name="Reverify the second offender",
        purpose="The composition completes, so the section clears and is reported cleared",
    )
    time.sleep(0.05)
    result_2 = reverify_node(db_path, mini_project, second, "Second reviewed.", verified_by="agent")
    assert TWO_OFFENDER_SECTION in result_2.cleared
    assert result_2.skipped == {}
    # The first offender contributed a term; it was not itself touched by this call.
    assert first not in result_2.verified
    assert first not in result_2.cleared

    口 = Step(
        step_num=4,
        name="Repeat the last reverify",
        purpose="Nothing is left to compose, so the call is a clean no-op",
    )
    time.sleep(0.05)
    result_3 = reverify_node(db_path, mini_project, second, "Second reviewed again.", verified_by="agent")
    assert result_3.cleared == []
    assert result_3.skipped == {}
    assert result_3.verified == [second]

    口 = Step(
        step_num=5,
        name="Compare against marking the section clean directly",
        purpose=(
            "The composed end state equals the direct one — the same fixture, "
            "cleared in a single mark_clean of the dependent"
        ),
    )
    direct_project = tmp_path_factory.mktemp("direct")
    direct_db = _build_two_offender_project(direct_project)
    time.sleep(0.05)
    mark_clean_nodes(
        direct_db,
        direct_project,
        [TWO_OFFENDER_SECTION],
        "Section re-read against both functions.",
        verified_by="agent",
    )
    assert _dependent_state(db_path, mini_project) == _dependent_state(direct_db, direct_project)


# ---------------------------------------------------------------------------
# Tier 2: the composition ranges over reverifies
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "The composition ranges over reverifies: while an offender behind a "
        "dependent has not been reverified the dependent stays LINKED_STALE and "
        "that offender is reported outstanding, and reverifying it completes the "
        "composition so the dependent clears"
    ),
)
def test_reverify_composition_counts_reverified_offenders(mini_project: Path, db_path: Path):
    _build_two_offender_project(mini_project)

    # Offender A carries a verification recorded by mark_clean.
    mark_clean_nodes(db_path, mini_project, [OFFENDER_A], "foo re-read.", verified_by="agent")

    time.sleep(0.05)
    partial = reverify_node(db_path, mini_project, OFFENDER_B, "bar verified.", verified_by="agent")
    assert partial.skipped == {TWO_OFFENDER_SECTION: [OFFENDER_A]}
    assert TWO_OFFENDER_SECTION not in partial.cleared
    assert compute_check_summary(db_path, mini_project).statuses[TWO_OFFENDER_SECTION][1] == "LINKED_STALE"

    time.sleep(0.05)
    completed = reverify_node(db_path, mini_project, OFFENDER_A, "foo verified.", verified_by="agent")
    assert TWO_OFFENDER_SECTION in completed.cleared
    assert completed.skipped == {}
    assert compute_check_summary(db_path, mini_project).statuses[TWO_OFFENDER_SECTION][1] == "VERIFIED"


# ---------------------------------------------------------------------------
# Tier 2: the already-reverified rule is an operation + ordering question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latest_change_ids", "verification_ops", "expected"),
    [
        ({"a": 10}, {"a": [(20, VERIFICATION_OP_REVERIFY)]}, {"a"}),
        ({"a": 30}, {"a": [(20, VERIFICATION_OP_REVERIFY)]}, set()),
        ({"a": 10}, {"a": [(20, VERIFICATION_OP_MARK_CLEAN)]}, set()),
        ({"a": 10}, {}, set()),
        ({}, {"a": [(20, VERIFICATION_OP_REVERIFY)]}, set()),
        ({"a": 10}, {"a": [(20, VERIFICATION_OP_REVERIFY), (30, VERIFICATION_OP_MARK_CLEAN)]}, {"a"}),
    ],
    ids=[
        "reverified-after-its-last-change",
        "changed-again-after-the-reverify",
        "verification-recorded-by-another-operation",
        "no-verification-at-all",
        "no-change-row-to-order-against",
        "reverify-row-followed-by-another-verification",
    ],
)
@workflow(
    purpose=(
        "An offender counts as already-reverified only when a verification "
        "recorded as written by reverify post-dates its last content-bearing "
        "change; every ambiguous case defaults to not-already-reverified"
    ),
)
def test_already_reverified_is_an_operation_and_ordering_question(
    latest_change_ids: dict[str, int],
    verification_ops: dict[str, list[tuple[int, str]]],
    expected: set[str],
):
    assert (
        already_reverified_offenders(
            ["a"],
            latest_change_ids=latest_change_ids,
            verification_ops=verification_ops,
        )
        == expected
    )


# ---------------------------------------------------------------------------
# Tier 2: the reverify record persists in append-only history
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "A reverify's record of an offender survives later verification activity "
        "on that same offender, so a reverify performed months earlier still "
        "counts as a term when the remaining offender is finally reverified"
    ),
)
def test_reverify_record_persists_through_later_verifications(mini_project: Path, db_path: Path):
    _build_two_offender_project(mini_project)

    time.sleep(0.05)
    first = reverify_node(db_path, mini_project, OFFENDER_A, "foo verified.", verified_by="agent")
    assert first.skipped == {TWO_OFFENDER_SECTION: [OFFENDER_B]}

    # An ordinary later verification of the same offender.
    time.sleep(0.05)
    mark_clean_nodes(db_path, mini_project, [OFFENDER_A], "audit pass.", verified_by="agent")

    time.sleep(0.05)
    second = reverify_node(db_path, mini_project, OFFENDER_B, "bar verified.", verified_by="agent")
    assert TWO_OFFENDER_SECTION in second.cleared
    assert second.skipped == {}


# ---------------------------------------------------------------------------
# Tier 2: the skip report names the clearing action
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "The rendered skip block still lists every skipped node with the "
        "offenders still outstanding behind it, and adds exactly one line naming "
        "the action that clears them without repeating those offender IDs"
    ),
)
def test_reverify_skip_report_names_the_clearing_action(mini_project: Path, db_path: Path):
    _build_two_offender_project(mini_project)

    time.sleep(0.05)
    out = axiom_graph_reverify(
        verbose=True,
        project_root=str(mini_project),
        node_id=OFFENDER_A,
        reason="foo verified.",
    )
    lines = out.splitlines()

    assert "Skipped — also stale via other offenders (1):" in out
    node_line = f"- {TWO_OFFENDER_SECTION} (other offenders: {OFFENDER_B})"
    assert node_line in lines

    # Exactly one hint line, immediately after the per-node lines.
    assert out.count(REVERIFY_SKIP_HINT) == 1
    assert lines[lines.index(node_line) + 1] == REVERIFY_SKIP_HINT

    # The hint names the action; it does not restate the offender list above it.
    assert TWO_OFFENDER_SECTION not in REVERIFY_SKIP_HINT
    assert OFFENDER_B not in REVERIFY_SKIP_HINT

    # No further block was added: the reason line closes the report.
    assert lines[lines.index(REVERIFY_SKIP_HINT) + 1 :] == ["Reason: foo verified."]


# ---------------------------------------------------------------------------
# Tier 2: the emitted verification payload names the reason and the operation
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Every verification emits a history payload naming both the reason and "
        "the operation that wrote it — mark_clean and reverify, with a filled "
        "reason and with a blank one — so a consumer reading the whole payload, "
        "such as the report command's JSON output, sees a payload on each "
        "verification row rather than a missing one"
    ),
)
def test_verification_payload_names_reason_and_operation(mini_project: Path, db_path: Path):
    from click.testing import CliRunner

    from axiom_graph.cli import main

    _write_code(
        mini_project,
        "mod",
        "def foo():\n    return 0\n\n\ndef bar():\n    return 0\n\n\n"
        "def baz():\n    return 0\n\n\ndef qux():\n    return 0\n",
    )
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    reasoned_mark_clean = "proj::src.mod::foo"
    blank_mark_clean = "proj::src.mod::bar"
    reasoned_reverify = "proj::src.mod::baz"
    blank_reverify = "proj::src.mod::qux"

    mark_clean_nodes(db_path, mini_project, [reasoned_mark_clean], "Re-read.", verified_by="agent")
    mark_clean_nodes(db_path, mini_project, [blank_mark_clean], "", verified_by="agent")
    reverify_node(db_path, mini_project, reasoned_reverify, "Re-read.", verified_by="agent")
    reverify_node(db_path, mini_project, blank_reverify, "", verified_by="agent")

    # Read back through the consumer that serialises the whole payload rather
    # than looking up one key inside it.  Keys and operation values are spelled
    # as literals here because this is the emitted wire shape: a rename on the
    # writing side is a change to what consumers receive.
    result = CliRunner().invoke(main, ["report", "--format", "json", str(mini_project)])
    assert result.exit_code == 0

    rows_by_node: dict[str, list[dict]] = {}
    for row in json.loads(result.output)["verifications"]:
        rows_by_node.setdefault(row["node_id"], []).append(row)

    expected_payloads = {
        reasoned_mark_clean: {"reason": "Re-read.", "verification_op": "mark_clean"},
        blank_mark_clean: {"reason": "", "verification_op": "mark_clean"},
        reasoned_reverify: {"reason": "Re-read.", "verification_op": "reverify"},
        blank_reverify: {"reason": "", "verification_op": "reverify"},
    }
    for node_id, expected_meta in expected_payloads.items():
        rows = rows_by_node.get(node_id, [])
        assert len(rows) == 1, f"expected exactly one verification row for {node_id}, got {len(rows)}"
        assert "meta" in rows[0], f"verification row for {node_id} carries no payload"
        assert rows[0]["meta"] == expected_meta


def test_reverify_unknown_source_reports_not_found(mini_project: Path, db_path: Path):
    """An unknown source node yields not_found, and the MCP surface an ERROR string."""
    _write_code(mini_project, "mod", "def foo():\n    return 0\n")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    result = reverify_node(db_path, mini_project, "proj::nope", "x", verified_by="agent")
    assert result.not_found is True

    out = axiom_graph_reverify(verbose=True, project_root=str(mini_project), node_id="proj::nope", reason="x")
    assert out == "ERROR: Node 'proj::nope' not found."


# ---------------------------------------------------------------------------
# Tier 3: a verification settles the offenders it saw
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "A doc section verified after one linked function changed stays settled "
        "about that change: when a second linked function changes later, check, "
        "drift_query and reverify all name only the newer offender, and "
        "reverifying it clears the section"
    ),
)
def test_verified_dependent_reports_only_offenders_newer_than_its_verification(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="foo changes, the section is re-read and marked clean, then bar changes",
        purpose="The section's verification post-dates foo's change but not bar's",
    )
    _build_staggered_offender_project(mini_project, verify_between=True)

    口 = Step(
        step_num=2,
        name="Every surface names bar alone",
        purpose="foo's change was already accounted for by the verification, so it is not an offender",
        outputs="via list [bar] in the stale map, check and drift_query",
    )
    assert _get_linked_stale_ids(db_path)[TWO_OFFENDER_SECTION] == [OFFENDER_B]
    _own, link, via = compute_check_summary(db_path, mini_project).statuses[TWO_OFFENDER_SECTION]
    assert link == "LINKED_STALE"
    assert via == [OFFENDER_B]
    row = _drift_query_row(db_path, mini_project, TWO_OFFENDER_SECTION)
    assert OFFENDER_B in row
    assert OFFENDER_A not in row

    口 = Step(
        step_num=3,
        name="Reverify bar",
        purpose="bar is the only outstanding offender, so the section clears in this one call",
    )
    time.sleep(0.05)
    result = reverify_node(db_path, mini_project, OFFENDER_B, "bar change is internal.", verified_by="agent")
    assert TWO_OFFENDER_SECTION in result.cleared
    assert result.skipped == {}
    assert compute_check_summary(db_path, mini_project).statuses[TWO_OFFENDER_SECTION][1] == "VERIFIED"


@workflow(
    purpose=(
        "A doc section that was never verified keeps every linked function that "
        "changed as an offender, however the changes were spaced out"
    ),
)
def test_unverified_dependent_keeps_every_offender(mini_project: Path, db_path: Path):
    _build_staggered_offender_project(mini_project, verify_between=False)

    assert sorted(_get_linked_stale_ids(db_path)[TWO_OFFENDER_SECTION]) == [OFFENDER_A, OFFENDER_B]
    _own, _link, via = compute_check_summary(db_path, mini_project).statuses[TWO_OFFENDER_SECTION]
    assert sorted(via) == [OFFENDER_A, OFFENDER_B]


# ---------------------------------------------------------------------------
# Tier 3: reverify names the real blocker
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "An agent marks one offender clean and reverifies the other: the "
        "dependent is still skipped, and the reverify output says the first "
        "offender was marked clean, not reverified, and to reverify it — instead "
        "of claiming nothing was rooted at the reverified node"
    ),
)
def test_reverify_names_an_offender_that_was_only_marked_clean(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="A section stale via foo and bar; foo is marked clean",
        purpose="mark_clean is a single-node claim, so it does not settle foo for the section",
    )
    _build_two_offender_project(mini_project)
    time.sleep(0.05)
    mark_clean_nodes(db_path, mini_project, [OFFENDER_A], "foo re-read.", verified_by="agent")

    口 = Step(
        step_num=2,
        name="Reverify bar through the MCP surface",
        purpose="The section is skipped; the output must explain that foo blocks it and which verb clears it",
    )
    time.sleep(0.05)
    out = axiom_graph_reverify(verbose=True, project_root=str(mini_project), node_id=OFFENDER_B, reason="bar verified.")
    assert f"- {TWO_OFFENDER_SECTION} (other offenders: {OFFENDER_A})" in out.splitlines()
    assert REVERIFY_MARKED_CLEAN_NOTE.format(offender=OFFENDER_A) in out
    assert "Nothing to clear — no LINKED_STALE rooted at this node" not in out

    口 = Step(
        step_num=3,
        name="mark_clean semantics are unchanged",
        purpose="The section is still LINKED_STALE; only a reverify of foo clears it",
    )
    assert compute_check_summary(db_path, mini_project).statuses[TWO_OFFENDER_SECTION][1] == "LINKED_STALE"
    time.sleep(0.05)
    result = reverify_node(db_path, mini_project, OFFENDER_A, "foo verified.", verified_by="agent")
    assert TWO_OFFENDER_SECTION in result.cleared


# ---------------------------------------------------------------------------
# Tier 3: reverifying a module counts for its functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_first", [True, False], ids=["module-then-bar", "bar-then-module"])
@workflow(
    purpose=(
        "Reverifying a module counts as reverifying each of its functions: a "
        "section stale via a function of that module and an unrelated function "
        "clears once both the module and the unrelated function are reverified, "
        "in either order — and a later change to the module's function re-opens it"
    ),
)
def test_reverifying_a_module_counts_for_its_functions(mini_project: Path, db_path: Path, module_first: bool):
    口 = Step(
        step_num=1,
        name="A section documents foo (in module xmod) and bar (elsewhere); both change",
        purpose="The section roots at foo and bar",
    )
    _build_two_offender_project(mini_project)
    module_id = "proj::src.xmod"

    口 = Step(
        step_num=2,
        name="Reverify the first of module xmod and bar",
        purpose="The section is skipped, naming only the offender not yet covered",
    )
    first, second = (module_id, OFFENDER_B) if module_first else (OFFENDER_B, module_id)
    time.sleep(0.05)
    partial = reverify_node(db_path, mini_project, first, "First reviewed.", verified_by="agent")
    assert partial.skipped == {TWO_OFFENDER_SECTION: [OFFENDER_B if module_first else OFFENDER_A]}

    口 = Step(
        step_num=3,
        name="Reverify the other one",
        purpose="foo counts as reverified through its module, so the section clears",
    )
    time.sleep(0.05)
    completed = reverify_node(db_path, mini_project, second, "Second reviewed.", verified_by="agent")
    assert TWO_OFFENDER_SECTION in completed.cleared
    assert completed.skipped == {}

    口 = Step(
        step_num=4,
        name="Change foo and bar again, then reverify bar",
        purpose="The module's reverify predates foo's new change, so foo is outstanding again",
    )
    time.sleep(0.05)
    (mini_project / "src" / "xmod.py").write_text("def foo():\n    return 2\n", encoding="utf-8")
    (mini_project / "src" / "ymod.py").write_text("def bar():\n    return 2\n", encoding="utf-8")
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    reopened = reverify_node(db_path, mini_project, OFFENDER_B, "bar re-reviewed.", verified_by="agent")
    assert reopened.skipped == {TWO_OFFENDER_SECTION: [OFFENDER_A]}


class TestComposesAncestors:
    """The upward composes walk behind composite reverify settlement."""

    def test_walks_the_full_chain(self):
        children_map = {"doc": ["sec"], "sec": ["child"], "mod": ["f"]}
        assert composes_ancestors(["child", "f", "doc"], children_map) == {
            "child": {"sec", "doc"},
            "f": {"mod"},
            "doc": set(),
        }

    def test_cycles_terminate_and_exclude_the_node_itself(self):
        children_map = {"a": ["b"], "b": ["c"], "c": ["a"]}
        assert composes_ancestors(["a"], children_map) == {"a": {"b", "c"}}

    def test_an_ancestor_reverify_settles_only_changes_it_postdates(self):
        ops = {"mod": [(20, VERIFICATION_OP_REVERIFY)]}
        ancestors = {"f": {"mod"}}
        settled = already_reverified_offenders(
            ["f"], latest_change_ids={"f": 10}, verification_ops=ops, ancestors=ancestors
        )
        reopened = already_reverified_offenders(
            ["f"], latest_change_ids={"f": 30}, verification_ops=ops, ancestors=ancestors
        )
        no_change_row = already_reverified_offenders(
            ["f"], latest_change_ids={}, verification_ops=ops, ancestors=ancestors
        )
        assert (settled, reopened, no_change_row) == ({"f"}, set(), set())


# ---------------------------------------------------------------------------
# Batch reverify: several sources asserted in one call
# ---------------------------------------------------------------------------

OFFENDER_C = "proj::src.zmod::baz"
BATCH_ONLY_FOO = "proj::docs/batch::only-foo"
BATCH_ONLY_BAR = "proj::docs/batch::only-bar"
BATCH_SHARED = "proj::docs/batch::shared"
BATCH_FOO_AND_BAZ = "proj::docs/batch::foo-and-baz"
BATCH_DEPENDENTS = (BATCH_ONLY_FOO, BATCH_ONLY_BAR, BATCH_SHARED, BATCH_FOO_AND_BAZ)


def _build_batch_project(project: Path) -> Path:
    """Build three changed functions and four doc sections that depend on them.

    ``only-foo`` documents :data:`OFFENDER_A`, ``only-bar`` documents
    :data:`OFFENDER_B`, ``shared`` documents both, and ``foo-and-baz``
    documents :data:`OFFENDER_A` and :data:`OFFENDER_C`.  All three
    functions change after the first build.  Returns the project's DB path.
    """
    db_p = project / ".axiom_graph" / "graph.db"
    codes = {
        name: _write_code(project, mod, f"def {name}():\n    return 0\n")
        for mod, name in (("xmod", "foo"), ("ymod", "bar"), ("zmod", "baz"))
    }

    def _section(sid: str, *links: str) -> dict:
        return {
            "id": sid,
            "heading": sid.title(),
            "content": f"Documents {', '.join(links)}.",
            "links": [{"node_id": link} for link in links],
        }

    _write_doc(
        project,
        "batch.json",
        {
            "title": "Batch",
            "sections": [
                _section("only-foo", OFFENDER_A),
                _section("only-bar", OFFENDER_B),
                _section("shared", OFFENDER_A, OFFENDER_B),
                _section("foo-and-baz", OFFENDER_A, OFFENDER_C),
            ],
        },
    )
    build_index(db_p, project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    for name, path in codes.items():
        path.write_text(f"def {name}():\n    return 1\n", encoding="utf-8")
    build_index(db_p, project, project_id="proj", discovery_only=False)
    stale = _get_linked_stale_ids(db_p)
    assert all(dep in stale for dep in BATCH_DEPENDENTS)
    return db_p


def _batch_state(db_path: Path, project: Path) -> dict[str, tuple[str, str]]:
    """The (own, link) status of every batch-fixture dependent and the doc envelope."""
    cs = compute_check_summary(db_path, project)
    return {nid: cs.statuses[nid][:2] for nid in (*BATCH_DEPENDENTS, "proj::docs/batch")}


@workflow(
    purpose=(
        "A maintainer who reviewed two changed functions reverifies both in one "
        "batch call. The batch ends in the same state as two single reverifies in "
        "either order, and the section documenting both functions is reported "
        "cleared in that one call rather than skipped"
    ),
)
def test_batch_reverify_equals_the_sequence(
    mini_project: Path, db_path: Path, tmp_path_factory: pytest.TempPathFactory
):
    口 = Step(
        step_num=1,
        name="Reverify foo and bar together",
        purpose="Each function's own dependents and the shared section clear in one call",
    )
    _build_batch_project(mini_project)
    time.sleep(0.05)
    batch = reverify_nodes(db_path, mini_project, [OFFENDER_A, OFFENDER_B], "Both reviewed.", verified_by="agent")
    assert batch.sources == [OFFENDER_A, OFFENDER_B]
    assert batch.not_found == []
    assert {BATCH_ONLY_FOO, BATCH_ONLY_BAR, BATCH_SHARED} <= set(batch.cleared)
    assert BATCH_SHARED not in batch.skipped

    口 = Step(
        step_num=2,
        name="Each cleared dependent names the source(s) that held it",
        purpose="Provenance stays per source: one source, or the list of batch sources",
    )
    expected_tags = {
        BATCH_ONLY_FOO: f"[reverify:{OFFENDER_A}]",
        BATCH_ONLY_BAR: f"[reverify:{OFFENDER_B}]",
        BATCH_SHARED: f"[reverify:{OFFENDER_A}, {OFFENDER_B}]",
    }
    for dep, tag in expected_tags.items():
        hist = fetch_history(db_path, dep, max_results=20)
        reasons = [json.loads(r.meta)["reason"] for r in hist.rows if r.change_type == "AGENT_VERIFIED" and r.meta]
        assert any(m.startswith(tag) for m in reasons), (dep, reasons)

    口 = Step(
        step_num=3,
        name="Run the same review as two single reverifies, in both orders",
        purpose="The batch is the same assertion as the sequence, so the end states match",
    )
    batch_state = _batch_state(db_path, mini_project)
    for order in ((OFFENDER_A, OFFENDER_B), (OFFENDER_B, OFFENDER_A)):
        seq_project = tmp_path_factory.mktemp("sequence")
        seq_db = _build_batch_project(seq_project)
        for source in order:
            time.sleep(0.05)
            reverify_node(seq_db, seq_project, source, "Reviewed.", verified_by="agent")
        assert _batch_state(seq_db, seq_project) == batch_state, order


@workflow(
    purpose=(
        "Batch reverify keeps the skip rule: a section documenting a batch source "
        "and a function outside the batch stays LINKED_STALE and is reported "
        "skipped, naming only the function outside the batch"
    ),
)
def test_batch_reverify_skips_dependents_held_outside_the_batch(mini_project: Path, db_path: Path):
    _build_batch_project(mini_project)
    time.sleep(0.05)
    batch = reverify_nodes(db_path, mini_project, [OFFENDER_A, OFFENDER_B], "Both reviewed.", verified_by="agent")
    assert batch.skipped == {BATCH_FOO_AND_BAZ: [OFFENDER_C]}
    assert BATCH_FOO_AND_BAZ not in batch.cleared
    assert compute_check_summary(db_path, mini_project).statuses[BATCH_FOO_AND_BAZ][1] == "LINKED_STALE"


@workflow(
    purpose=(
        "A batch naming an unknown node still reverifies the sources it can find, "
        "clears their dependents, and lists the unknown id under not-found"
    ),
)
def test_batch_reverify_reports_not_found_per_source(mini_project: Path, db_path: Path):
    _build_batch_project(mini_project)
    time.sleep(0.05)
    batch = reverify_nodes(
        db_path, mini_project, [OFFENDER_A, "proj::nope", OFFENDER_B], "Both reviewed.", verified_by="agent"
    )
    assert batch.not_found == ["proj::nope"]
    assert batch.sources == [OFFENDER_A, OFFENDER_B]
    assert {BATCH_ONLY_FOO, BATCH_ONLY_BAR, BATCH_SHARED} <= set(batch.cleared)


def test_batch_reverify_recomputes_staleness_once(mini_project: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Three sources, one entry refresh and one final staleness recompute, never one per source."""
    import axiom_graph.lifecycle.api as lifecycle_api

    _build_batch_project(mini_project)
    calls: list[int] = []
    real = lifecycle_api.compute_check_summary

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(lifecycle_api, "compute_check_summary", _counting)
    time.sleep(0.05)
    reverify_nodes(db_path, mini_project, [OFFENDER_A, OFFENDER_B, OFFENDER_C], "All reviewed.", verified_by="agent")
    assert len(calls) == 2


def test_batch_reverify_mcp_report(mini_project: Path, db_path: Path):
    """The MCP tool's node_ids form prints one report covering every source."""
    from axiom_graph.mcp.server import axiom_graph_reverify as server_reverify

    _build_batch_project(mini_project)
    time.sleep(0.05)
    out = server_reverify(
        verbose=True,
        project_root=str(mini_project),
        reason="Both reviewed.",
        node_ids=[OFFENDER_A, "proj::nope", OFFENDER_B],
    )
    assert out.count("LINKED_STALE before:") == 1
    assert f"- {OFFENDER_A}" in out
    assert f"- {OFFENDER_B}" in out
    assert set(_cleared_block_ids(out)) >= {BATCH_ONLY_FOO, BATCH_ONLY_BAR, BATCH_SHARED}
    assert f"- {BATCH_FOO_AND_BAZ} (other offenders: {OFFENDER_C})" in out
    assert "Not found (1):\n- proj::nope" in out
    assert out.rstrip().endswith("Reason: Both reviewed.")


@workflow(
    purpose=(
        "A maintainer marks baz clean, then batch-reverifies foo and bar: the "
        "section documenting foo and baz stays skipped, and both the batch result "
        "and the MCP batch report name baz as marked clean, not reverified"
    ),
)
def test_batch_reverify_names_an_offender_that_was_only_marked_clean(mini_project: Path, db_path: Path):
    from axiom_graph.mcp.server import axiom_graph_reverify as server_reverify

    _build_batch_project(mini_project)
    time.sleep(0.05)
    mark_clean_nodes(db_path, mini_project, [OFFENDER_C], "baz re-read.", verified_by="agent")
    time.sleep(0.05)
    batch = reverify_nodes(db_path, mini_project, [OFFENDER_A, OFFENDER_B], "Both reviewed.", verified_by="agent")
    assert batch.skipped == {BATCH_FOO_AND_BAZ: [OFFENDER_C]}
    assert batch.marked_clean_offenders == [OFFENDER_C]

    time.sleep(0.05)
    out = server_reverify(
        verbose=True, project_root=str(mini_project), reason="Both reviewed.", node_ids=[OFFENDER_A, OFFENDER_B]
    )
    assert f"- {BATCH_FOO_AND_BAZ} (other offenders: {OFFENDER_C})" in out.splitlines()
    assert REVERIFY_MARKED_CLEAN_NOTE.format(offender=OFFENDER_C) in out
    assert "Nothing to clear — no LINKED_STALE rooted at this node" not in out
