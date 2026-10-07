"""Report reference resolution, condensed output, exclusion and the MCP cap.

Behavioural tests enter at ``axiom_graph.db.history`` / ``axiom_graph.lifecycle.api``;
the MCP wrapper is called directly only where its own behaviour (cap, ``verbose``
alias, ``ERROR:`` string) is under test.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from axiom_annotations import workflow
from click.testing import CliRunner

from axiom_graph.index import db
from axiom_graph.lifecycle import api
from axiom_graph.lifecycle.mcp_tools import axiom_graph_report

FULL_A = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
FULL_B = "b1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _row(db_path: Path, node_id: str, change_type: str, ts: str, git_sha: str | None = None, meta=None) -> None:
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (node_id, ts, change_type, git_sha, json.dumps(meta) if meta is not None else None),
        )


def _ts(minute: int) -> str:
    return f"2026-05-01T10:{minute:02d}:00.000000+00:00"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@workflow(
    purpose="An explicit SHA that neither the index nor git knows — or a too-short prefix — is an error "
    "in the db layer, the CLI and the MCP tool; it never yields the full table, even with an until bound"
)
def test_unresolvable_sha_is_an_error_everywhere(mini_project: Path, db_path: Path):
    _row(db_path, "p::m::f", "INITIAL", _ts(1), git_sha=FULL_A)
    _row(db_path, "p::m::f", "CONTENT_ONLY", _ts(2), git_sha=FULL_A)

    for bad in ("ffff0000", "a1b", "HEAD", "master"):
        with pytest.raises(db.UnresolvedReferenceError):
            db.get_history_since(db_path, since_sha=bad, project_root=mini_project)
        with pytest.raises(db.UnresolvedReferenceError):
            db.get_history_since(db_path, since_sha=bad, until_timestamp=_ts(59), project_root=mini_project)
        with pytest.raises(api.UnresolvedReferenceError):
            api.compute_report(db_path, since_sha=bad, project_root=mini_project)
        assert axiom_graph_report(str(mini_project), since_sha=bad).startswith("ERROR: since_sha")

    from axiom_graph.cli import main

    result = CliRunner().invoke(main, ["report", str(mini_project), "--since-sha", "ffff0000"])
    assert result.exit_code != 0
    assert "ffff0000" in result.output
    assert "CONTENT_ONLY" not in result.output
    assert "is not a hex SHA prefix" in axiom_graph_report(str(mini_project), since_sha="HEAD")


@workflow(
    purpose="Prefix matching is symmetric: a full SHA matches a legacy 12-char checkpoint, a short "
    "prefix matches full build rows, and a row with an empty or very short stored SHA matches nothing"
)
def test_symmetric_prefix_match(db_path: Path):
    _row(db_path, "p::junk", "INITIAL", _ts(0), git_sha="")
    _row(db_path, "p::junk", "INITIAL", _ts(0), git_sha="ab")
    _row(db_path, "p::m::f", "INITIAL", _ts(1), git_sha=FULL_B)
    _row(db_path, "p::m::f", "CHECKPOINT", _ts(5), git_sha=FULL_A[:12])

    res = db.resolve_since_cutoff(db_path, since_sha=FULL_A)
    assert (res.source, res.cutoff, res.sha) == (db.SOURCE_CHECKPOINT, _ts(5), FULL_A[:12])

    res = db.resolve_since_cutoff(db_path, since_sha=FULL_A[:7])
    assert res.source == db.SOURCE_CHECKPOINT

    res = db.resolve_since_cutoff(db_path, since_sha=FULL_B[:12])
    assert (res.source, res.sha) == (db.SOURCE_BUILD, FULL_B)

    res = db.resolve_since_cutoff(db_path, since_sha="abcd1234")
    assert res.source == db.SOURCE_UNRESOLVED, "an empty/short stored SHA must not match every input"


@workflow(
    purpose="history checkpoint stores the full HEAD SHA, list-refs still shows 12 characters, and the "
    "full SHA resolves back to that checkpoint"
)
def test_checkpoint_stores_full_sha(git_project: Path, git_db_path: Path):
    from axiom_graph.cli import main
    from axiom_graph.models import AxiomNode

    db.upsert_node(
        git_db_path,
        AxiomNode(
            id="p::m::f",
            node_type="atomic_process",
            subtype="function",
            title="f",
            location="m.py",
            source="ast",
            code_hash="h",
            desc_hash=None,
            level_0="f",
            level_1="f",
        ),
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_project, capture_output=True, text=True, check=True
    ).stdout.strip()

    runner = CliRunner()
    assert runner.invoke(main, ["history", "checkpoint", str(git_project)]).exit_code == 0
    refs = db.list_reference_points(git_db_path)
    assert refs[0]["git_sha"] == head and refs[0]["type"] == "checkpoint"

    listed = runner.invoke(main, ["report", str(git_project), "--list-refs"]).output
    assert f"  {head[:12]}  checkpoint" in listed
    assert head not in listed

    res = db.resolve_since_cutoff(git_db_path, since_sha=head, project_root=git_project)
    assert res.source == db.SOURCE_CHECKPOINT


# ---------------------------------------------------------------------------
# Reference header
# ---------------------------------------------------------------------------


@workflow(
    purpose="Every format starts with (or, for JSON, carries) the reference it was measured against — "
    "for checkpoint, build row, timestamp, default fallback and no reference — including on an empty window"
)
def test_reference_header_on_every_format(mini_project: Path, db_path: Path):
    no_ref = api.compute_report(db_path)
    assert no_ref.no_rows
    for detail in api.REPORT_DETAILS:
        assert api.render_report_text(no_ref, detail).startswith("reference: none — whole history")

    _row(db_path, "p::m::f", "INITIAL", _ts(1), git_sha=FULL_B)
    _row(db_path, "p::m::f", "CONTENT_ONLY", _ts(10), git_sha=FULL_B)
    default_build = api.compute_report(db_path)
    assert api.render_report_text(default_build, "summary").startswith(f"reference: {FULL_B[:12]} — latest SHA")

    _row(db_path, "p::m::f", "CHECKPOINT", _ts(5), git_sha=FULL_A)
    cases = {
        "checkpoint": (dict(since_sha=FULL_A[:8]), f"reference: {FULL_A[:12]} — checkpoint at {_ts(5)}"),
        "build": (dict(since_sha=FULL_B[:8]), f"reference: {FULL_B[:12]} — end of its first indexed build"),
        "timestamp": (dict(since_timestamp=_ts(3)), f"reference: timestamp {_ts(3)}"),
        "default-checkpoint": ({}, f"reference: {FULL_A[:12]} — latest checkpoint at {_ts(5)}"),
    }
    for name, (kwargs, header) in cases.items():
        data = api.compute_report(db_path, project_root=mini_project, **kwargs)
        for detail in api.REPORT_DETAILS:
            lines = api.render_report_text(data, detail).splitlines()
            assert lines[0].startswith(header), (name, detail, lines[0])
            assert "nodes changed" in lines[1]
        ref = api.report_to_dict(data)["reference"]
        assert ref["description"].startswith(header)
        assert ref["source"] == data.resolution.source

    empty = api.compute_report(db_path, since_timestamp=_ts(59))
    assert empty.no_rows
    assert api.render_report_text(empty, "full").startswith(f"reference: timestamp {_ts(59)}")
    assert api.report_to_dict(empty)["reference"]["cutoff"] == _ts(59)


# ---------------------------------------------------------------------------
# Condensed + exclude
# ---------------------------------------------------------------------------


def _seed_mixed(db_path: Path) -> None:
    m = 1
    for i in range(3):
        _row(db_path, f"p::mod_a::f{i}", "CONTENT_ONLY", _ts(m))
    _row(db_path, "p::mod_a::f0", "INITIAL", _ts(m))
    _row(db_path, "p::docs/x::s1", "DESC_ONLY", _ts(m))
    # Cascades from 12 distinct via containers (+ repeats from mod_a).
    for i in range(12):
        _row(db_path, f"p::docs/y::s{i}", "BECAME_LINKED_STALE", _ts(m), meta={"linked_node": f"p::via{i:02d}::fn"})
    for i in range(3):
        _row(db_path, f"p::docs/z::s{i}", "BECAME_LINKED_STALE", _ts(m), meta={"linked_node": "p::mod_a::f0"})
    _row(db_path, "p::docs/y::s0", "LINK_BECAME_VERIFIED", _ts(m), meta={"from_link": "LINKED_STALE"})
    # Links: one hand-made, three system purges onto mod_a.
    _row(db_path, "p::docs/x::s1", "LINK_ADDED", _ts(m), meta={"target": "p::mod_a::f1", "actor": "agent"})
    for i in range(3):
        _row(db_path, f"p::docs/w::s{i}", "LINK_REMOVED", _ts(m), meta={"target": "p::mod_a::f2", "actor": "system"})
    # Verification: mod_a directly (agent-only), docs/y appears only via cascade retirement.
    _row(db_path, "p::mod_a::f0", "AGENT_VERIFIED", _ts(m))
    _row(db_path, "p::mod_a::f1", "MANUAL_VERIFIED", _ts(m))


@workflow(
    purpose="Condensed output rolls rows up by container: one content line per container with per-type "
    "counts, a staleness table with the top 10 via containers then +N others, actor rows verbatim, "
    "system purges collapsed, and verification split into direct vs cascade-only containers"
)
def test_condensed_report_shape(mini_project: Path, db_path: Path):
    _seed_mixed(db_path)
    data = api.compute_report(db_path, since_timestamp=_ts(0))
    text = api.render_report_text(data, "condensed")
    lines = text.splitlines()

    assert lines[0].startswith("reference: timestamp")
    assert "  p::mod_a  3 nodes  [CONTENT_ONLY 3, INITIAL 1]" in lines
    assert "  p::docs/x  1 node  [DESC_ONLY 1]" in lines

    assert "  BECAME_LINKED_STALE  15 rows, 15 nodes" in lines
    via_start = lines.index("  BECAME_LINKED_STALE  15 rows, 15 nodes") + 2
    assert lines[via_start] == "      p::mod_a  3", "via containers are ranked by count"
    via_block = lines[via_start : via_start + 11]
    assert via_block[-1] == "      +3 others"
    assert len([v for v in via_block if v.startswith("      p::")]) == 10

    assert "  p::docs/x::s1  → p::mod_a::f1  [agent]" in lines
    assert "  [system] 3 purges  → p::mod_a" in lines
    assert not any("p::docs/w::s" in line for line in lines), "system rows are collapsed, not listed"

    assert "  2 nodes verified (1 agent-only)" in lines
    direct = lines.index("  verified directly:")
    assert lines[direct + 1] == "    p::mod_a  2 nodes  [AGENT_VERIFIED 1, MANUAL_VERIFIED 1]"
    assert "    p::docs/y  1 node" in lines[lines.index(next(x for x in lines if "cascade retirement only" in x)) :]

    assert len(text) < len(api.render_report_text(data, "full"))


@workflow(
    purpose="Raw DocJSON edits are listed one per section in the full report and rolled up to one "
    "line per container in the condensed report, and they count in the headline"
)
def test_raw_docjson_edits_full_vs_condensed(mini_project: Path, db_path: Path):
    for i in range(5):
        _row(db_path, f"p::docs/a::s{i}", "RAW_DOCJSON_EDIT", _ts(1))
    _row(db_path, "p::docs/b::s0", "RAW_DOCJSON_EDIT", _ts(1))
    data = api.compute_report(db_path, since_timestamp=_ts(0))
    assert data.summary["raw_docjson_edits"] == 6

    full = api.render_report_text(data, "full").splitlines()
    assert full[1].endswith(", 6 raw DocJSON edits")
    assert sum("RAW_DOCJSON_EDIT" in line for line in full) == 6

    condensed = api.render_report_text(data, "condensed").splitlines()
    assert "  p::docs/a  5 sections" in condensed and "  p::docs/b  1 section" in condensed
    assert not any("p::docs/a::s" in line for line in condensed), "per-section rows are rolled up"


@workflow(
    purpose="The condensed report lists the top 10 directly-verified containers by node count and folds "
    "the rest into one +N line, while the full report still lists every verification row"
)
def test_condensed_verified_directly_is_capped(mini_project: Path, db_path: Path):
    for c in range(14):
        for n in range(c + 1):
            _row(db_path, f"p::mod_{c:02d}::f{n}", "AGENT_VERIFIED", _ts(1))
    data = api.compute_report(db_path, since_timestamp=_ts(0))

    condensed = api.render_report_text(data, "condensed").splitlines()
    start = condensed.index("  verified directly:") + 1
    assert condensed[start] == "    p::mod_13  14 nodes  [AGENT_VERIFIED 14]", "ranked by node count"
    assert condensed[start + 10] == "    +4 other containers (10 nodes; the full report lists every row)"
    assert not any("p::mod_00" in line for line in condensed)

    full = api.render_report_text(data, "full").splitlines()
    assert sum("AGENT_VERIFIED" in line for line in full) == sum(range(1, 15))


@workflow(
    purpose="Excluded node globs (string or list) drop rows from every bucket and from the headline "
    "counts, keep a kept row's via pointing at an excluded node, and compose with the positive filters"
)
def test_exclude_node_pattern(mini_project: Path, db_path: Path):
    _seed_mixed(db_path)
    base = api.compute_report(db_path, since_timestamp=_ts(0))

    excluded = api.compute_report(db_path, since_timestamp=_ts(0), exclude_node_pattern="p::mod_a*")
    ids = (
        set(excluded.content_changes)
        | {r["node_id"] for r in excluded.staleness_transitions}
        | {r["node_id"] for r in excluded.link_changes}
        | {r["node_id"] for r in excluded.verifications}
    )
    assert not any(i.startswith("p::mod_a") for i in ids)
    assert excluded.summary["nodes_changed"] == base.summary["nodes_changed"] - 3
    assert excluded.summary["verified"] == 0
    vias = {json.loads(r["meta"]).get("linked_node") for r in excluded.staleness_transitions if r["meta"]}
    assert "p::mod_a::f0" in vias, "a via to an excluded node explains the cascade and stays"

    as_list = api.compute_report(db_path, since_timestamp=_ts(0), exclude_node_pattern=["p::mod_a*", "p::docs/y*"])
    assert not any(r["node_id"].startswith("p::docs/y") for r in as_list.staleness_transitions)

    composed = api.compute_report(
        db_path,
        since_timestamp=_ts(0),
        node_pattern="p::docs/*",
        change_type_pattern="BECAME_*",
        exclude_node_pattern="p::docs/z*",
    )
    assert {r["node_id"].split("::")[1] for r in composed.staleness_transitions} == {"docs/y"}
    assert not composed.content_changes and not composed.link_changes

    from axiom_graph.models import AxiomNode

    for i in range(3):
        nid = f"p::mod_a::f{i}"
        db.upsert_node(
            db_path,
            AxiomNode(
                id=nid,
                node_type="atomic_process",
                subtype="function",
                title=nid,
                location="mod_a.py",
                source="ast",
                code_hash="h",
                desc_hash=None,
                level_0=nid,
                level_1=nid,
            ),
        )
    by_type = api.compute_report(
        db_path, since_timestamp=_ts(0), node_type="atomic_process", exclude_node_pattern="p::mod_a::f0"
    )
    assert set(by_type.content_changes) == {"p::mod_a::f1", "p::mod_a::f2"}
    assert {r["node_id"] for r in by_type.verifications} == {"p::mod_a::f1"}
    assert not by_type.staleness_transitions and not by_type.link_changes

    rows = db.get_history_since(db_path, since_timestamp=_ts(0))
    assert db.filter_history_rows(rows, exclude_node_pattern=["p::*"]) == []


# ---------------------------------------------------------------------------
# MCP wrapper: cap + alias
# ---------------------------------------------------------------------------


@workflow(
    purpose="The MCP report caps output at max_chars on a line boundary with a footer, leaves output "
    "under the cap byte-identical, disables the cap with None, and maps verbose=True to detail='full' "
    "unless detail is given"
)
def test_mcp_report_cap_and_verbose_alias(mini_project: Path, db_path: Path):
    for i in range(400):
        _row(db_path, f"p::mod_{i:03d}::function_with_a_long_name", "CONTENT_ONLY", _ts(1))
    root = str(mini_project)
    kwargs = dict(since_timestamp=_ts(0))

    full_uncapped = axiom_graph_report(root, detail="full", max_chars=None, **kwargs)
    assert len(full_uncapped) > 5000
    assert axiom_graph_report(root, detail="full", max_chars=len(full_uncapped), **kwargs) == full_uncapped

    capped = axiom_graph_report(root, detail="full", max_chars=5000, **kwargs)
    assert len(capped) <= 5000
    body, footer = capped.split("\n\n[report truncated: ")
    assert full_uncapped.startswith(body + "\n"), "the cut falls on a line boundary"
    dropped = int(footer.split(" ", 1)[0])
    assert dropped == len(full_uncapped.splitlines()) - len(body.splitlines())
    assert 'detail="condensed"' in footer and "exclude_node_pattern" in footer

    condensed_footer = api.cap_report_text("line\n" * 2000, 500, detail="condensed")
    assert 'detail="condensed"' not in condensed_footer and "exclude_node_pattern" in condensed_footer

    assert axiom_graph_report(root, verbose=True, max_chars=None, **kwargs) == full_uncapped
    summary = axiom_graph_report(root, verbose=True, detail="summary", **kwargs)
    assert len(summary.splitlines()) == 2
    assert axiom_graph_report(root, **kwargs) == summary

    tiny = api.cap_report_text("x" * 1000, 300)
    assert tiny.startswith("x") and "[report truncated: 0 more line(s)" in tiny


# ---------------------------------------------------------------------------
# Viz endpoint
# ---------------------------------------------------------------------------


@workflow(
    purpose="The viz changed-since endpoint resolves a SHA git knows but the index never saw — for both "
    "the since and the until end — and still reports resolved:false with an accurate reason for a SHA "
    "git does not know"
)
def test_viz_since_resolves_unindexed_commits(git_project: Path, git_db_path: Path):
    from axiom_graph.viz.server import _apply_project, get_history_since_endpoint

    _apply_project(git_project)

    def _commit(msg: str, when: str) -> str:
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", msg], cwd=git_project, env=env, capture_output=True, check=True
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=git_project, capture_output=True, text=True, check=True
        ).stdout.strip()

    sha_1 = _commit("one", "2026-05-01T10:00:00+00:00")
    sha_2 = _commit("two", "2026-05-01T10:30:00+00:00")
    _row(git_db_path, "p::m::inside", "DELETED", _ts(15))
    _row(git_db_path, "p::m::after", "DELETED", _ts(45))

    since = get_history_since_endpoint(sha=sha_1[:9])
    assert since["resolved"] is True
    assert since["baseline_sha"] == sha_1

    until = get_history_since_endpoint(sha=sha_1[:9], until_sha=sha_2[:9])
    assert until["resolved"] is True
    assert until["until_timestamp"] == "2026-05-01T10:30:00+00:00", "the window ends at the until commit's time"
    assert [g["id"] for g in until["deleted_nodes"]] == ["p::m::inside"], "a row after the until commit is excluded"

    unknown = get_history_since_endpoint(sha="0123456789ab")
    assert unknown["resolved"] is False
    assert "not a commit in this repository" in unknown["reason"]

    unknown_until = get_history_since_endpoint(sha=sha_1[:9], until_sha="0123456789ab")
    assert unknown_until["resolved"] is False
    assert unknown_until["requested_sha"] == "0123456789ab"

    ref_name = get_history_since_endpoint(sha="HEAD")
    assert ref_name["resolved"] is False
    assert "not a hex SHA prefix" in ref_name["reason"] and "ref name" in ref_name["reason"]
