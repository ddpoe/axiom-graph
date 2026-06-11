"""Tests for the ADR-019 layering lint rules.

Asserts that:
- Each rule fires on a synthetic violation.
- The post-refactor tree (axiom_graph + tests) passes clean.

@workflow purpose ties these tests to ADR-019 user story US-2.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the worktree's `tools/` is importable regardless of pytest cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# `tools/` is internal dev tooling, held back from the published/shipped tree, so these
# architecture-layering checks skip when it isn't present (e.g. the public mirror CI).
pytest.importorskip("tools.check_layering", reason="internal layering-lint tooling not shipped")

from tools import check_layering  # noqa: E402


# ---------------------------------------------------------------------------
# Rule 1 — presentation may not import the DB
# ---------------------------------------------------------------------------


def test_rule1_fires_on_mcp_tools_importing_db(tmp_path: Path) -> None:
    """A synthetic *_mcp_tools.py importing axiom_graph.index.db trips Rule 1."""
    fake_repo = tmp_path
    bad = fake_repo / "axiom_graph" / "fake_domain" / "mcp_tools.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        "from axiom_graph.index import db\nimport sqlite3\n",
        encoding="utf-8",
    )
    violations = check_layering.check_paths([bad], fake_repo)
    text = "\n".join(violations)
    assert "Rule1" in text
    assert "sqlite3" in text or "db" in text


# ---------------------------------------------------------------------------
# Rule 2 — presentation may only import its domain's api (and config)
# ---------------------------------------------------------------------------


def test_rule2_fires_when_domain_mcp_tools_imports_other_domain(tmp_path: Path) -> None:
    """An mcp_tools file importing another domain's api trips Rule 2."""
    fake_repo = tmp_path
    bad = fake_repo / "axiom_graph" / "domain_a" / "mcp_tools.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        # Allowed: own-domain api + config.
        "from axiom_graph.domain_a.api import foo\n"
        "from axiom_graph.config import AxiomGraphConfig\n"
        # Disallowed: another domain's api.
        "from axiom_graph.domain_b.api import bar\n"
        # Disallowed: reaching into mcp.doc (old-style).
        "from axiom_graph.mcp.doc import baz\n",
        encoding="utf-8",
    )
    violations = check_layering.check_paths([bad], fake_repo)
    text = "\n".join(violations)
    assert "Rule2" in text
    assert "domain_b" in text
    assert "axiom_graph.mcp.doc" in text


def test_rule2_allows_timed_tool_helper(tmp_path: Path) -> None:
    """Importing _timed_tool from mcp._helpers is allowed in mcp_tools.py."""
    fake_repo = tmp_path
    ok = fake_repo / "axiom_graph" / "domain_x" / "mcp_tools.py"
    ok.parent.mkdir(parents=True)
    ok.write_text(
        "from axiom_graph.domain_x.api import foo\nfrom axiom_graph.mcp._helpers import _timed_tool\n",
        encoding="utf-8",
    )
    violations = check_layering.check_paths([ok], fake_repo)
    assert all("Rule2" not in v for v in violations), violations


# ---------------------------------------------------------------------------
# Rule 3 — behavioural tests must use api, not direct db seeding
# ---------------------------------------------------------------------------


def test_rule3_fires_on_discovery_only_false_in_test(tmp_path: Path) -> None:
    """A test file calling db.upsert_node(... discovery_only=False) trips Rule 3."""
    fake_repo = tmp_path
    bad = fake_repo / "tests" / "test_violator.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        "from axiom_graph.index import db\n"
        "def test_x(tmp_path):\n"
        "    db.upsert_node(tmp_path, None, discovery_only=False)\n",
        encoding="utf-8",
    )
    violations = check_layering.check_paths([bad], fake_repo)
    text = "\n".join(violations)
    assert "Rule3" in text
    assert "discovery_only=False" in text


def test_rule3_skips_test_db_files(tmp_path: Path) -> None:
    """tests/test_db_*.py is allowed to use discovery_only=False (DB internals)."""
    fake_repo = tmp_path
    ok = fake_repo / "tests" / "test_db_internals.py"
    ok.parent.mkdir(parents=True)
    ok.write_text(
        "from axiom_graph.index import db\n"
        "def test_x(tmp_path):\n"
        "    db.upsert_node(tmp_path, None, discovery_only=False)\n",
        encoding="utf-8",
    )
    violations = check_layering.check_paths([ok], fake_repo)
    assert all("Rule3" not in v for v in violations)


# ---------------------------------------------------------------------------
# Post-refactor tree passes clean
# ---------------------------------------------------------------------------


def test_post_refactor_tree_passes_clean() -> None:
    """The committed post-refactor axiom_graph + tests tree has no violations."""
    paths = [_REPO_ROOT / "axiom_graph", _REPO_ROOT / "tests"]
    violations = check_layering.check_paths(paths, _REPO_ROOT)
    if violations:
        msg = "Layering violations on post-refactor tree:\n" + "\n".join(violations)
        pytest.fail(msg)
