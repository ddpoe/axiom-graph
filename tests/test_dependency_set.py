"""Tests for the shared dependency-set and module-digest primitive."""

from __future__ import annotations

import sqlite3

from axiom_graph.index.dependency_set import (
    ANNOTATES,
    DELEGATES,
    DOCUMENTS,
    VALIDATES,
    DependencyGraph,
    NodeKind,
    delegates_closure,
    dependency_set,
    digest_members,
    live_value,
    load_dependency_graph,
    module_digest,
    pair_hashes,
    pair_matches,
)

SECTION = NodeKind("atomic_process", "docjson_section", "docjson")
FUNC = NodeKind("atomic_process", "function", "module_scanner")
TEST = NodeKind("atomic_process", "test", "module_scanner")
MODULE = NodeKind("composite_process", "module", "module_scanner")
WORKFLOW = NodeKind("composite_process", "workflow", "module_scanner")
TASK = NodeKind("composite_process", "task", "module_scanner")
AUTOSTEP = NodeKind("atomic_process", "autostep", "module_scanner")
STEP = NodeKind("atomic_process", "step", "module_scanner")
DOC = NodeKind("composite_process", "docjson_doc", "docjson")


def _graph() -> DependencyGraph:
    """A small graph exercising every link kind and filter."""
    g = DependencyGraph(
        kinds={
            "sec": SECTION,
            "sec2": SECTION,
            "f": FUNC,
            "g": FUNC,
            "t": TEST,
            "mod": MODULE,
            "doc": DOC,
            "w": WORKFLOW,
            "w::step-1": AUTOSTEP,
            "w::step-2": STEP,
            "task_fn": FUNC,
            "task_fn@workflow": TASK,
            "task_fn@workflow::step-1": AUTOSTEP,
            "deep_fn": FUNC,
        }
    )
    g.documents_out = {"sec": ["f", "sec2", "mod", "doc", "gone"], "t": ["f"]}
    g.validates_out = {"t": ["f", "sec2"], "mod": ["g"]}
    g.annotates_out = {"w": ["f"], "task_fn@workflow": ["task_fn"]}
    g.annotates_rev = {"f": ["w"], "task_fn": ["task_fn@workflow"]}
    g.composes_out = {
        "mod": ["f", "g", "w"],
        "w": ["w::step-1", "w::step-2"],
        "task_fn@workflow": ["task_fn@workflow::step-1"],
    }
    g.delegates_out = {"w::step-1": "task_fn", "task_fn@workflow::step-1": "deep_fn"}
    return g


def test_documents_targets_skip_atomic_docjson_and_missing_rows() -> None:
    """A doc section depends on code, module and doc-envelope targets, never on sections or absent rows."""
    deps = dependency_set(_graph(), "sec")
    assert deps == {"f": {DOCUMENTS}, "mod": {DOCUMENTS}, "doc": {DOCUMENTS}}


def test_only_atomic_tests_carry_validates_dependencies() -> None:
    """A test's validates targets count; a module's validates edges and a test's documents edges do not."""
    g = _graph()
    assert dependency_set(g, "t") == {"f": {VALIDATES}}
    assert dependency_set(g, "mod") == {}


def test_envelope_depends_on_annotated_target_and_transitive_delegates() -> None:
    """An envelope pairs its annotated function and every task its delegates_to closure reaches."""
    deps = dependency_set(_graph(), "w")
    assert deps == {"f": {ANNOTATES}, "task_fn": {DELEGATES}, "deep_fn": {DELEGATES}}


def test_delegates_closure_is_cycle_safe() -> None:
    """A delegation cycle terminates and reports each task once."""
    composes = {"a": ["a::s"], "b": ["b::s"]}
    delegates = {"a::s": "fb", "b::s": "fa"}
    annotates_rev = {"fa": ["a"], "fb": ["b"]}
    subtypes = {"a::s": "autostep", "b::s": "autostep"}
    assert sorted(delegates_closure("a", composes, delegates, annotates_rev, subtypes.get)) == ["fa", "fb"]


def test_digest_members_are_atomic_descendants_without_steps() -> None:
    """A module's digest covers its functions and tests, not envelopes or step views."""
    assert sorted(digest_members(_graph(), "mod")) == ["f", "g"]


def test_module_digest_ignores_ids_and_order() -> None:
    """The digest depends only on the multiset of member hashes."""
    assert module_digest(["b", "a"]) == module_digest(["a", "b"])
    assert module_digest(["a", "b"]) != module_digest(["a", "c"])
    assert module_digest(["a", "b"]) != module_digest(["a"])


def test_live_value_skips_missing_members_and_missing_targets() -> None:
    """A NOT_FOUND member leaves the digest; a NOT_FOUND or hashless target has no live value."""
    g = _graph()
    hashes = {"f": ("hf", "df"), "g": ("hg", None), "doc": ("hd", "hd")}
    missing = {"g"}
    value = live_value(g, "mod", hashes.get, missing.__contains__)
    assert value == (module_digest(["hf"]), None)
    assert live_value(g, "g", hashes.get, missing.__contains__) is None
    assert live_value(g, "t", hashes.get, missing.__contains__) is None
    assert live_value(g, "doc", hashes.get, missing.__contains__) == ("hd", "hd")


def test_module_live_value_ignores_its_inherited_status() -> None:
    """A module that reads NOT_FOUND through one member still digests the rest; with no member left it has none."""
    g = _graph()
    hashes = {"f": ("hf", "df"), "g": ("hg", None)}
    assert live_value(g, "mod", hashes.get, {"mod", "g"}.__contains__) == (module_digest(["hf"]), None)
    assert live_value(g, "mod", hashes.get, {"mod", "f", "g"}.__contains__) is None


def test_pairs_compare_desc_only_for_annotates_links() -> None:
    """A docstring change breaks an annotates pair but not a documents or validates pair."""
    assert pair_hashes(frozenset({DOCUMENTS}), ("c", "d")) == ("c", None)
    assert pair_hashes(frozenset({ANNOTATES}), ("c", "d")) == ("c", "d")
    assert pair_matches(frozenset({DOCUMENTS}), ("c", None), ("c", "d2"))
    assert not pair_matches(frozenset({ANNOTATES}), ("c", "d"), ("c", "d2"))
    assert not pair_matches(frozenset({ANNOTATES}), ("c", "d"), ("c", None))
    assert not pair_matches(frozenset({VALIDATES}), ("c", None), ("c2", None))


def test_load_dependency_graph_reads_nodes_and_links() -> None:
    """The loader fills every adjacency the dependency filters read."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE nodes (id TEXT, node_type TEXT, subtype TEXT, source TEXT)")
    conn.execute("CREATE TABLE edges (edge_type TEXT, from_id TEXT, to_id TEXT)")
    conn.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?)",
        [("sec", *SECTION.__dict__.values()), ("f", *FUNC.__dict__.values()), ("t", *TEST.__dict__.values())],
    )
    conn.executemany(
        "INSERT INTO edges VALUES (?, ?, ?)",
        [("documents", "sec", "f"), ("validates", "t", "f"), ("depends_on", "t", "f")],
    )
    g = load_dependency_graph(conn)
    assert dependency_set(g, "sec") == {"f": {DOCUMENTS}}
    assert dependency_set(g, "t") == {"f": {VALIDATES}}
