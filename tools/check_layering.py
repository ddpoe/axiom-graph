"""Layering lint rules from ADR-019.

Three rules are enforced; violations exit non-zero:

Rule 1 — Presentation may not import the DB.
    In ``*/mcp_tools.py`` and ``axiom_graph/cli/*.py``: forbid
    ``from axiom_graph.index import db``, ``import sqlite3``, and
    ``from axiom_graph.db import *`` / ``from axiom_graph.index.db import *``.

Rule 2 — Presentation may only import its domain's api (and config).
    In ``axiom_graph/<domain>/mcp_tools.py``: allowed imports are the
    matching ``axiom_graph.<domain>.api`` module, ``axiom_graph.config``,
    ``axiom_graph.mcp._helpers`` (for ``_timed_tool`` only), and the
    standard library.
    In ``axiom_graph/cli/*.py``: allowed imports are any
    ``axiom_graph.<domain>.api`` module, ``axiom_graph.config``,
    ``click``, and the standard library.

Rule 3 — Behavioural tests must use api, not direct db seeding.
    In ``tests/test_*.py`` (excluding ``tests/test_db_*.py``): forbid
    ``db.upsert_*(... discovery_only=False)`` calls.

Usage:
    python tools/check_layering.py [paths...]

When invoked with no paths the checker scans ``axiom_graph`` and
``tests`` under the current working directory.

Allowlists:
    ADR-019 cycles 1, 2, and 3 migrated all four ``mcp_tools`` domains
    (docjson, workflows, lifecycle, query) into per-domain ``api.py`` +
    ``mcp_tools.py`` layers.  No ``mcp/`` aggregator module remains on
    the allowlist.  CLI files (``axiom_graph/cli/*.py``) are still
    grandfathered (``CLI_LEGACY_FILES``) -- ADR-019 calls out a future
    CLI-extraction cycle that will give each command a per-domain api
    layer to talk to; until then ``cli/*`` legitimately imports from
    ``axiom_graph.index.*``.  ``RULE3_LEGACY_TEST_FILES`` retains
    pre-ADR-019 test fixtures that still call ``db.upsert_*(...
    discovery_only=False)`` directly; new test files MUST NOT add
    themselves to that list without an explicit pev-request.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

# ADR-019 cycles 1, 2, and 3 emptied the per-cycle allowlist of
# ``mcp_tools`` modules.  ``cli/*`` files are still grandfathered while the
# future CLI-extraction cycle wires per-command api layers; until then
# ``cli/*`` legitimately imports from ``axiom_graph.index.*``.  Listed by
# POSIX-relative path under repo root.
CLI_LEGACY_FILES = frozenset(
    {
        "axiom_graph/cli/__init__.py",
        "axiom_graph/cli/__main__.py",
        "axiom_graph/cli/_core.py",
        "axiom_graph/cli/indexing.py",
        "axiom_graph/cli/inspection.py",
        "axiom_graph/cli/rendering.py",
    }
)


# Backward-compat alias for any external test that may reference the
# pre-cycle-3 name.  Kept frozen-empty so checks still pass when this
# constant is read.
CYCLE_2_3_PRESENTATION_FILES = frozenset()


# Test files predating ADR-019 that test non-docjson subsystems (staleness,
# mark-clean, semantic search, viz, etc.).  Their use of
# ``db.upsert_*(... discovery_only=False)`` is the exact fixture short-circuit
# Rule 3 will eventually outlaw, but re-pointing them at api layers is gated
# on those domain api layers existing — scheduled for cycles 2/3.  Listed by
# POSIX-relative path; new test files are NOT allowed to add themselves to
# this list without an explicit pev-request.
RULE3_LEGACY_TEST_FILES = frozenset(
    {
        "tests/test_broken_links.py",
        "tests/test_consumer_render.py",
        "tests/test_diff.py",
        "tests/test_drift_query.py",
        "tests/test_mark_clean_baseline.py",
        "tests/test_mark_clean_batch.py",
        "tests/test_mcp_tool_enhancements.py",
        "tests/test_mcp_tool_improvements.py",
        "tests/test_rename_detection.py",
        "tests/test_rename_edge_migration.py",
        "tests/test_report.py",
        "tests/test_semantic_search.py",
        "tests/test_since_filter.py",
        "tests/test_staleness_helpers.py",
        "tests/test_staleness_viz.py",
        "tests/test_two_column_staleness.py",
    }
)


# Files matched as "presentation" for Rule 1 / Rule 2.
def _is_presentation_file(rel: str) -> bool:
    """Return True for ``*/mcp_tools.py`` modules and ``axiom_graph/cli/*.py``."""
    posix = rel.replace("\\", "/")
    if posix.endswith("/mcp_tools.py"):
        return True
    if posix.startswith("axiom_graph/cli/") and posix.endswith(".py"):
        return True
    return False


def _is_cli_file(rel: str) -> bool:
    """Return True for any ``axiom_graph/cli/*.py`` module."""
    posix = rel.replace("\\", "/")
    return posix.startswith("axiom_graph/cli/") and posix.endswith(".py")


def _domain_for_mcp_tools(rel: str) -> str | None:
    """Return ``<domain>`` for ``axiom_graph/<domain>/mcp_tools.py``."""
    posix = rel.replace("\\", "/")
    m = re.match(r"^axiom_graph/([^/]+)/mcp_tools\.py$", posix)
    if not m:
        return None
    return m.group(1)


# ---------------------------------------------------------------------------
# Rule 1 — presentation may not import the DB.
# ---------------------------------------------------------------------------

_DB_FORBIDDEN_PREFIXES = (
    "axiom_graph.index.db",
    "axiom_graph.db",
)


def _check_rule1(rel: str, tree: ast.Module) -> list[str]:
    """Return Rule 1 violations for ``tree`` (presentation files only)."""
    if not _is_presentation_file(rel):
        return []
    if rel.replace("\\", "/") in CLI_LEGACY_FILES:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "sqlite3":
                    out.append(f"{rel}:{node.lineno}: Rule1: presentation file imports `sqlite3`")
                if any(name == p or name.startswith(p + ".") for p in _DB_FORBIDDEN_PREFIXES):
                    out.append(f"{rel}:{node.lineno}: Rule1: presentation file imports `{name}`")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "sqlite3":
                out.append(f"{rel}:{node.lineno}: Rule1: presentation file imports from `sqlite3`")
            if mod == "axiom_graph.index" and any(a.name == "db" for a in node.names):
                out.append(f"{rel}:{node.lineno}: Rule1: presentation file imports `db` from `axiom_graph.index`")
            if any(mod == p or mod.startswith(p + ".") for p in _DB_FORBIDDEN_PREFIXES):
                out.append(f"{rel}:{node.lineno}: Rule1: presentation file imports from `{mod}`")
    return out


# ---------------------------------------------------------------------------
# Rule 2 — presentation may only import its domain's api (and config).
# ---------------------------------------------------------------------------


# Modules whose names are part of stdlib (a small allowlist; we treat anything
# not starting with ``axiom_graph`` as 3rd-party / stdlib and accept stdlib
# implicitly via "not axiom_graph").
def _is_stdlib_or_thirdparty(mod: str) -> bool:
    if not mod:
        return True
    return not mod.startswith("axiom_graph")


def _allowed_for_mcp_tools(domain: str, mod: str) -> bool:
    """Imports allowed by Rule 2 for ``axiom_graph/<domain>/mcp_tools.py``."""
    if _is_stdlib_or_thirdparty(mod):
        return True
    if mod == f"axiom_graph.{domain}.api" or mod.startswith(f"axiom_graph.{domain}.api."):
        return True
    if mod == "axiom_graph.config":
        return True
    # Architect note: ``axiom_graph.mcp._helpers`` allowed for ``_timed_tool``
    # only.  We don't introspect names here; allowing the module is the
    # narrow exception ADR-019 explicitly carves out.
    if mod == "axiom_graph.mcp._helpers":
        return True
    # ``axiom_graph.index.paths`` is the canonical home for ``db_path`` /
    # ``require_db`` per ADR-019 cycle 3 (formerly the ``_db_path`` /
    # ``_require_db`` wrappers in ``mcp/_helpers.py``).  Allowed here so
    # wire layers can resolve project_root -> DB path without re-introducing
    # the deprecated wrappers.
    if mod == "axiom_graph.index.paths":
        return True
    # ``axiom_graph.renderers`` is presentation-side string formatting only
    # (no db.* imports, no sqlite, no index.* state).  Wire layers compose
    # api outputs into formatted text via these helpers.
    if mod == "axiom_graph.renderers" or mod.startswith("axiom_graph.renderers."):
        return True
    return False


def _allowed_for_cli(mod: str) -> bool:
    """Imports allowed by Rule 2 for ``axiom_graph/cli/*.py``."""
    if _is_stdlib_or_thirdparty(mod):
        return True
    if mod == "axiom_graph.config":
        return True
    # Any domain api: axiom_graph.<x>.api[.*]
    m = re.match(r"^axiom_graph\.([^.]+)\.api(?:\.|$)", mod)
    if m:
        return True
    return False


def _check_rule2(rel: str, tree: ast.Module) -> list[str]:
    """Return Rule 2 violations for ``tree`` (presentation files only)."""
    if rel.replace("\\", "/") in CLI_LEGACY_FILES:
        return []
    domain = _domain_for_mcp_tools(rel)
    is_cli = _is_cli_file(rel)
    if not domain and not is_cli:
        return []

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                ok = _allowed_for_mcp_tools(domain, mod) if domain else _allowed_for_cli(mod)
                if not ok:
                    out.append(
                        f"{rel}:{node.lineno}: Rule2: presentation imports `{mod}` "
                        f"(allowed: own-domain api, config, stdlib"
                        f"{', _timed_tool from mcp._helpers' if domain else ', click'})"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            ok = _allowed_for_mcp_tools(domain, mod) if domain else _allowed_for_cli(mod)
            if not ok:
                out.append(
                    f"{rel}:{node.lineno}: Rule2: presentation imports from `{mod}` "
                    f"(allowed: own-domain api, config, stdlib"
                    f"{', _timed_tool from mcp._helpers' if domain else ', click'})"
                )
    return out


# ---------------------------------------------------------------------------
# Rule 3 — behavioural tests must use api, not direct db seeding.
# ---------------------------------------------------------------------------

_DISCOVERY_ONLY_TOKEN = "discovery_only=False"


def _is_test_file(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    if not posix.startswith("tests/"):
        return False
    if not posix.split("/")[-1].startswith("test_"):
        return False
    if posix.split("/")[-1].startswith("test_db_"):
        return False
    return True


def _check_rule3(rel: str, tree: ast.Module, source: str) -> list[str]:
    """Return Rule 3 violations.

    A violation is a call that looks like ``db.upsert_*(...)`` containing
    a literal keyword ``discovery_only=False``.  We use ast for the call
    shape (any attribute call ending in ``.upsert_*`` against an identifier
    named ``db``) and require the literal ``False`` keyword.
    """
    if not _is_test_file(rel):
        return []
    if rel.replace("\\", "/") in RULE3_LEGACY_TEST_FILES:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not func.attr.startswith("upsert_"):
            continue
        # Receiver must be the bare identifier ``db`` to avoid catching
        # unrelated upserts on other objects.
        recv = func.value
        if not isinstance(recv, ast.Name) or recv.id != "db":
            continue
        for kw in node.keywords:
            if kw.arg == "discovery_only" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                out.append(
                    f"{rel}:{node.lineno}: Rule3: test calls "
                    f"`db.{func.attr}(... discovery_only=False)` (use api.* instead)"
                )
                break
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _walk_python_files(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            out.append(root)
            continue
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            out.append(p)
    return out


def check_paths(paths: list[Path], repo_root: Path) -> list[str]:
    """Run all three rules over the supplied paths.

    Args:
        paths: Files or directories to scan recursively.
        repo_root: Repo root for relative-path display in violation messages.

    Returns:
        List of violation messages (empty when clean).
    """
    violations: list[str] = []
    for f in _walk_python_files(paths):
        try:
            rel = f.relative_to(repo_root).as_posix()
        except ValueError:
            rel = f.as_posix()
        try:
            source = f.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(f))
        except (UnicodeDecodeError, SyntaxError):
            continue
        violations.extend(_check_rule1(rel, tree))
        violations.extend(_check_rule2(rel, tree))
        violations.extend(_check_rule3(rel, tree, source))
    return violations


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_root = Path.cwd()
    if argv:
        paths = [Path(a).resolve() for a in argv]
    else:
        paths = [repo_root / "axiom_graph", repo_root / "tests"]

    violations = check_paths(paths, repo_root)
    if violations:
        for v in violations:
            print(v)
        print(f"\n{len(violations)} layering violation(s) found.")
        return 1
    print("Layering check: OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
