"""Tests for axiom_graph.scanners.source_roots: the ordered Python import roots."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.scanners import module_scanner
from axiom_graph.scanners.source_roots import declared_dependencies, resolve_source_roots, roots_fingerprint


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rels(root: Path, roots) -> list[str]:
    return [p.relative_to(root).as_posix() or "." for p in roots]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param({}, ["."], id="flat"),
        pytest.param({"src/demo/__init__.py": ""}, [".", "src"], id="auto-src"),
        pytest.param({"src/notes.txt": ""}, ["."], id="src-without-python"),
        pytest.param(
            {"lib/pkg/mod.py": "", "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["lib", "."]\n'},
            ["lib", "."],
            id="pytest-pyproject",
        ),
        pytest.param(
            {"lib/m.py": "", "pytest.ini": "[pytest]\npythonpath = lib\n"},
            ["lib", "."],
            id="pytest-ini",
        ),
        pytest.param(
            {"lib/m.py": "", "tox.ini": "[pytest]\npythonpath = lib\n"},
            ["lib", "."],
            id="pytest-tox",
        ),
        pytest.param(
            {"lib/m.py": "", "setup.cfg": "[tool:pytest]\npythonpath = lib\n"},
            ["lib", "."],
            id="pytest-setup-cfg",
        ),
        pytest.param(
            {"code/demo/__init__.py": "", "pyproject.toml": '[tool.setuptools]\npackage-dir = {"" = "code"}\n'},
            ["code", "."],
            id="setuptools-package-dir",
        ),
        pytest.param(
            {
                "code/demo/__init__.py": "",
                "pyproject.toml": '[tool.setuptools.packages.find]\nwhere = ["code"]\n',
            },
            ["code", "."],
            id="setuptools-find-where",
        ),
        pytest.param(
            {"code/demo/__init__.py": "", "setup.cfg": "[options]\npackage_dir =\n    =code\n"},
            ["code", "."],
            id="setup-cfg-package-dir",
        ),
        pytest.param(
            {
                "code/demo/__init__.py": "",
                "pyproject.toml": '[tool.poetry]\npackages = [{include = "demo", from = "code"}]\n',
            },
            ["code", "."],
            id="poetry-from",
        ),
        pytest.param(
            {
                "code/demo/__init__.py": "",
                "pyproject.toml": '[tool.hatch.build.targets.wheel]\npackages = ["code/demo"]\n',
            },
            ["code", "."],
            id="hatch-packages",
        ),
    ],
)
def test_roots_are_read_from_project_config(tmp_path, files, expected):
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    assert _rels(tmp_path, resolve_source_roots(tmp_path)) == expected


def test_explicit_roots_come_first_then_pytest_then_packaging_then_root_then_src(tmp_path):
    _write(tmp_path, "src/demo/__init__.py")
    _write(tmp_path, "lib/a.py")
    _write(tmp_path, "tests_src/b.py")
    _write(tmp_path, "code/demo/__init__.py")
    _write(
        tmp_path,
        "pyproject.toml",
        '[tool.pytest.ini_options]\npythonpath = ["tests_src"]\n[tool.setuptools]\npackage-dir = {"" = "code"}\n',
    )
    roots = resolve_source_roots(tmp_path, explicit=["lib"])
    assert _rels(tmp_path, roots) == ["lib", "tests_src", "code", ".", "src"]


def test_the_first_pytest_config_file_wins(tmp_path):
    _write(tmp_path, "a/m.py")
    _write(tmp_path, "b/m.py")
    _write(tmp_path, "pytest.ini", "[pytest]\n")
    _write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["b"]\n')
    # pytest.ini is chosen even without pythonpath, so pyproject's is never read.
    assert _rels(tmp_path, resolve_source_roots(tmp_path)) == ["."]


def test_unusable_and_duplicate_entries_are_dropped(tmp_path):
    _write(tmp_path, "src/demo/__init__.py")
    _write(tmp_path, ".venv/lib/x.py")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    roots = resolve_source_roots(
        tmp_path,
        explicit=["missing", str(outside), "../", ".venv/lib", "src", "./src", "."],
        skip_dirs={".venv"},
    )
    assert _rels(tmp_path, roots) == ["src", "."]


def test_a_bare_string_source_roots_is_one_root(tmp_path):
    from axiom_graph.config import AxiomGraphConfig

    _write(tmp_path, "lib/m.py")
    _write(
        tmp_path, "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\nsource_roots = "lib"\n'
    )
    config = AxiomGraphConfig.load(tmp_path)
    assert config.scan.source_roots == ["lib"]
    assert _rels(tmp_path, resolve_source_roots(tmp_path, config.scan.source_roots)) == ["lib", "."]


@pytest.mark.parametrize(
    ("pyproject", "expected"),
    [
        pytest.param("", set(), id="none"),
        pytest.param(
            '[project]\ndependencies = ["Requests>=2", "zope.interface ; python_version>\'3\'", "my-lib[extra]"]\n',
            {"requests", "zope_interface", "my_lib"},
            id="project-dependencies",
        ),
        pytest.param(
            '[project.optional-dependencies]\nviz = ["Fast.API==1"]\n',
            {"fast_api"},
            id="project-optional",
        ),
        pytest.param(
            '[tool.poetry.dependencies]\npython = ">=3.10"\nAxiom-Annotations = ">=0.1"\n',
            {"axiom_annotations"},
            id="poetry-dependencies",
        ),
        pytest.param(
            '[tool.poetry.group.dev.dependencies]\npytest = "*"\n[tool.poetry.dev-dependencies]\nruff = "*"\n',
            {"pytest", "ruff"},
            id="poetry-groups",
        ),
    ],
)
def test_declared_dependencies_are_read_from_pyproject(tmp_path, pyproject, expected):
    _write(tmp_path, "pyproject.toml", pyproject)
    assert declared_dependencies(tmp_path) == expected


def test_fingerprint_is_stable_and_tracks_the_roots(tmp_path):
    _write(tmp_path, "src/demo/__init__.py")
    first = roots_fingerprint(tmp_path, resolve_source_roots(tmp_path))
    assert first == roots_fingerprint(tmp_path, resolve_source_roots(tmp_path))
    _write(tmp_path, "lib/m.py")
    assert roots_fingerprint(tmp_path, resolve_source_roots(tmp_path, explicit=["lib"])) != first


# ---------------------------------------------------------------------------
# Build-level behaviour: imports resolved through the roots
# ---------------------------------------------------------------------------

_TOML = '[axiom_graph]\nproject_id = "proj"\n'
_PYTEST_SRC = '[tool.pytest.ini_options]\npythonpath = ["src"]\n'
_LIB_ROOTS = _TOML + '\n[axiom_graph.scan]\nsource_roots = ["lib"]\n'


def _project(root: Path, files: dict[str, str]) -> Path:
    files = dict(files)
    _write(root, "axiom-graph.toml", files.pop("axiom-graph.toml", _TOML))
    for rel, text in files.items():
        _write(root, rel, text)
    return root / ".axiom_graph" / "graph.db"


def _node(db_path: Path, location: str, title: str) -> str:
    """Return the id of the node the build minted for *title* in *location*."""
    matches = [n.id for n in db.all_nodes(db_path) if n.location == location and n.title == title]
    assert len(matches) == 1, (location, title, matches)
    return matches[0]


def _edges(db_path: Path, edge_type: str) -> set[tuple[str, str]]:
    return {(e.from_id, e.to_id) for e in db.all_edges(db_path) if e.edge_type == edge_type}


def _demo_package(base: str, pkg: str = "demo", *, namespace: bool = False, relative: bool = False) -> dict:
    """A ``demo`` package under *base*, imported as *pkg*, with an AutoStep delegating across modules."""
    crunch_import = "from .tasks import crunch\n" if relative else f"from {pkg}.tasks import crunch\n"
    files = {
        f"{base}demo/stats.py": "def mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef total(xs):\n    return sum(xs)\n",
        f"{base}demo/tasks.py": (
            'from axiom_annotations import task\n\n\n@task(purpose="Crunch numbers")\ndef crunch(xs):\n'
            "    return sum(xs)\n"
        ),
        f"{base}demo/flow.py": (
            "from axiom_annotations import AutoStep, workflow\n"
            + crunch_import
            + '\n\n@workflow(purpose="Run the pipeline")\ndef pipeline(xs):\n'
            '    口 = AutoStep(step_num=1, name="Crunch")\n    crunch(xs)\n'
        ),
    }
    if not namespace:
        files[f"{base}demo/__init__.py"] = "from .stats import total\n"
    return files


def _stats_tests(pkg: str = "demo", *, reexport: bool = True) -> str:
    """A test module calling into *pkg* through every import shape."""
    lines = [f"import {pkg}.stats", f"from {pkg}.stats import mean", f"from {pkg} import stats as stats_mod"]
    if reexport:
        lines.append(f"from {pkg} import total")
    lines += [
        "",
        "",
        "def _helper(xs):\n    return mean(xs)\n\n",
        "def test_mean():\n    assert mean([1, 2]) == 1.5\n\n",
        f"def test_dotted():\n    assert {pkg}.stats.total([1]) == 1\n\n",
        "def test_submodule():\n    assert stats_mod.mean([2]) == 2\n\n",
        f"def test_local_import():\n    from {pkg}.stats import total as t\n\n    assert t([1]) == 1\n\n",
        "def test_helper():\n    assert _helper([4]) == 4\n",
    ]
    if reexport:
        lines.append("\ndef test_reexport():\n    assert total([1]) == 1\n")
    return "\n".join(lines)


# layout -> (base dir of the demo package, extra files, import name, namespace, relative)
_LAYOUTS = {
    "flat": ("", {}, "demo", False, False),
    "src-pytest-pythonpath": ("src/", {"pyproject.toml": _PYTEST_SRC}, "demo", False, False),
    "src-no-config": ("src/", {}, "demo", False, False),
    "setuptools-package-dir": (
        "code/",
        {"pyproject.toml": '[tool.setuptools]\npackage-dir = {"" = "code"}\n'},
        "demo",
        False,
        False,
    ),
    "poetry-packages-from": (
        "lib/",
        {"pyproject.toml": '[tool.poetry]\npackages = [{include = "demo", from = "lib"}]\n'},
        "demo",
        False,
        False,
    ),
    "explicit-source-roots": ("lib/", {"axiom-graph.toml": _LIB_ROOTS}, "demo", False, False),
    "namespace-package": ("", {}, "demo", True, False),
    "relative-imports-in-src": ("src/", {"pyproject.toml": _PYTEST_SRC}, "demo", False, True),
    "legacy-src-prefix": ("src/", {}, "src.demo", False, False),
}


@workflow(
    purpose="Every Python layout (flat, src via pytest pythonpath, src with no config, setuptools, poetry, explicit "
    "source_roots, namespace package, relative imports, the legacy src. prefix) gets the validates, depends_on and "
    "delegates_to edges a flat layout gets, for every import shape"
)
@pytest.mark.parametrize("layout", list(_LAYOUTS))
def test_every_layout_links_tests_and_steps_to_the_code_they_call(tmp_path, layout):
    base, extra, pkg, namespace, relative = _LAYOUTS[layout]
    files = {**_demo_package(base, pkg, namespace=namespace, relative=relative), **extra}
    files["tests/test_stats.py"] = _stats_tests(pkg, reexport=not namespace)
    db_path = _project(tmp_path, files)
    builder.build(tmp_path, discovery_only=False)

    stats_py = f"{base}demo/stats.py"
    mean = _node(db_path, stats_py, "mean")
    total = _node(db_path, stats_py, "total")
    test_py = "tests/test_stats.py"
    expected = {
        (_node(db_path, test_py, "test_mean"), mean),
        (_node(db_path, test_py, "test_dotted"), total),
        (_node(db_path, test_py, "test_submodule"), mean),
        (_node(db_path, test_py, "test_local_import"), total),
        (_node(db_path, test_py, "test_helper"), mean),
    }
    if not namespace:
        expected.add((_node(db_path, test_py, "test_reexport"), total))
    assert expected <= _edges(db_path, "validates")

    depends_on = _edges(db_path, "depends_on")
    assert (_node(db_path, test_py, "test_stats"), _node(db_path, stats_py, "stats")) in depends_on
    assert not any(to.endswith(("::external::demo", "::external::src")) for _frm, to in depends_on)
    crunch = _node(db_path, f"{base}demo/tasks.py", "crunch")
    assert any(to.startswith(crunch) for _frm, to in _edges(db_path, "delegates_to"))


_SHADOWED = {
    "demo/__init__.py": "",
    "demo/stats.py": "def mean(xs):\n    return 0\n",
    "src/demo/__init__.py": "",
    "src/demo/stats.py": "def mean(xs):\n    return 1\n",
    "tests/test_stats.py": "from demo.stats import mean\n\n\ndef test_mean():\n    assert mean([1])\n",
}


@workflow(
    purpose="When a root package and a src/ package share a name, explicit source_roots win, then pytest "
    "pythonpath, then the project root, and every build picks the same one"
)
@pytest.mark.parametrize(
    ("config", "winner"),
    [
        pytest.param({}, "demo/stats.py", id="no-config-root-wins"),
        pytest.param({"pyproject.toml": _PYTEST_SRC}, "src/demo/stats.py", id="pytest-pythonpath-wins"),
        pytest.param(
            {"pyproject.toml": _PYTEST_SRC, "axiom-graph.toml": _TOML + '\n[axiom_graph.scan]\nsource_roots = ["."]\n'},
            "demo/stats.py",
            id="explicit-wins",
        ),
    ],
)
def test_import_root_precedence_is_deterministic(tmp_path, config, winner):
    db_path = _project(tmp_path, {**_SHADOWED, **config})
    for discovery_only in (False, True, True):
        builder.build(tmp_path, discovery_only=discovery_only)
        test_mean = _node(db_path, "tests/test_stats.py", "test_mean")
        linked = {to for frm, to in _edges(db_path, "validates") if frm == test_mean}
        assert linked == {_node(db_path, winner, "mean")}


def _source_roots_warnings(result: dict) -> list[str]:
    return [w for w in result["warnings"] if "source_roots" in w]


_UNRECOGNISED = {
    "lib/pkg/__init__.py": "",
    "lib/pkg/mod.py": "def f():\n    return 1\n",
    "tests/test_mod.py": "import requests\nfrom pkg.mod import f\n\n\ndef test_f():\n    assert f()\n",
}


@workflow(
    purpose="An import of the project's own code that resolves to no file is reported in one build line naming "
    "source_roots; a third-party import never is; the line goes away once source_roots fixes the import, though "
    "the old external stub stays in the index"
)
def test_unresolved_project_imports_are_reported_until_source_roots_fix_them(tmp_path):
    from axiom_graph.lifecycle.api import compute_check_summary

    third_party = tmp_path / "third_party"
    _project(third_party, {"app.py": "import requests\n\n\ndef get():\n    return requests.get('x')\n"})
    assert _source_roots_warnings(builder.build(third_party, discovery_only=False)) == []

    project = tmp_path / "project"
    db_path = _project(project, _UNRECOGNISED)
    lines = _source_roots_warnings(builder.build(project, discovery_only=False))
    assert len(lines) == 1
    assert lines[0].startswith("1 ")
    assert "(pkg)" in lines[0]
    # An incremental build that parses nothing still reports it.
    rebuilt = builder.build(project)
    assert rebuilt["files_scanned"] == 0
    assert _source_roots_warnings(rebuilt) == lines

    _write(project, "axiom-graph.toml", _LIB_ROOTS)
    assert _source_roots_warnings(builder.build(project)) == []
    assert _source_roots_warnings(builder.build(project)) == []
    test_f = _node(db_path, "tests/test_mod.py", "test_f")
    assert (test_f, _node(db_path, "lib/pkg/mod.py", "f")) in _edges(db_path, "validates")
    # Any stub left behind reads neither stale nor NOT_FOUND (today a build purges the stubs outright).
    statuses = compute_check_summary(db_path, project).statuses
    for stub in [n.id for n in db.all_nodes(db_path) if n.id.endswith("::external::pkg")]:
        assert statuses[stub][:2] == ("VERIFIED", "VERIFIED")


def test_the_external_import_record_leaves_out_standard_library_names(tmp_path):
    db_path = _project(
        tmp_path, {**_UNRECOGNISED, "tests/test_io.py": "import json\nimport os.path\n\n\ndef test_io():\n    pass\n"}
    )
    _write(tmp_path, "tools/json/__init__.py")
    builder.build(tmp_path, discovery_only=False)
    record = json.loads(db.get_index_meta(db_path, builder.EXTERNAL_IMPORTS_META_KEY))
    assert record == {"tests/test_mod.py": ["pkg", "requests"]}


def test_a_vendored_copy_of_a_declared_dependency_is_not_reported(tmp_path):
    _project(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "app"\ndependencies = ["Vendored-Lib>=1"]\n',
            "third_party/vendored_lib/__init__.py": "def f():\n    return 1\n",
            "app.py": "from vendored_lib import f\n\n\ndef run():\n    return f()\n",
        },
    )
    assert _source_roots_warnings(builder.build(tmp_path, discovery_only=False)) == []


def test_the_mcp_build_output_shows_the_unresolved_import_line_without_verbose(tmp_path):
    from axiom_graph.lifecycle.mcp_tools import axiom_graph_build

    _project(tmp_path, _UNRECOGNISED)
    builder.build(tmp_path, discovery_only=False)
    lines = [line for line in axiom_graph_build(str(tmp_path)).splitlines() if "source_roots" in line]
    assert len(lines) == 1


@workflow(
    purpose="Changing the import roots of an existing index makes the next build parse every file once and gain "
    "the edges; a build with unchanged roots parses nothing extra"
)
def test_changing_import_roots_reparses_every_file_once(tmp_path):
    db_path = _project(tmp_path, _UNRECOGNISED)
    builder.build(tmp_path, discovery_only=False)
    test_f = _node(db_path, "tests/test_mod.py", "test_f")
    assert not any(frm == test_f for frm, _to in _edges(db_path, "validates"))
    assert builder.build(tmp_path)["files_scanned"] == 0

    _write(tmp_path, "axiom-graph.toml", _LIB_ROOTS)
    assert builder.build(tmp_path)["files_scanned"] == 3
    assert (test_f, _node(db_path, "lib/pkg/mod.py", "f")) in _edges(db_path, "validates")
    assert builder.build(tmp_path)["files_scanned"] == 0


def _legacy_build(monkeypatch, root: Path, **kwargs) -> None:
    """Build *root* as older scanners did: imports looked up under the project root only."""
    with monkeypatch.context() as patch:
        patch.setattr(builder, "resolve_source_roots", lambda project_root, *_args: (project_root,))
        builder.build(root, **kwargs)
    db_path = root / ".axiom_graph" / "graph.db"
    db.set_index_meta(db_path, builder.SCAN_SCHEME_META_KEY, "1")
    with db._connect(db_path) as conn:
        db.delete_index_meta_conn(conn, builder.SOURCE_ROOTS_META_KEY)


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


@workflow(
    purpose="An index built before import roots existed gains the src-layout edges on its first build after the "
    "upgrade, and the new edges flag nothing stale"
)
def test_upgrade_build_gains_src_layout_edges_and_flags_nothing(tmp_path, monkeypatch):
    from axiom_graph.lifecycle.api import compute_check_summary
    from axiom_graph.lifecycle.mcp_tools import axiom_graph_mark_clean

    files = {**_demo_package("src/"), "pyproject.toml": _PYTEST_SRC, "tests/test_stats.py": _stats_tests()}
    db_path = _project(tmp_path, files)
    _legacy_build(monkeypatch, tmp_path, discovery_only=False)
    test_mean = _node(db_path, "tests/test_stats.py", "test_mean")
    assert not any(frm == test_mean for frm, _to in _edges(db_path, "validates"))
    project_ids = [n.id for n in db.all_nodes(db_path) if n.location != "external"]
    axiom_graph_mark_clean(str(tmp_path), "", "reviewed", node_ids=project_ids)
    # Code the tests and workflows call is edited and accepted while the index still lacks those links.
    _edit(tmp_path, "src/demo/stats.py", "/ len(xs)", "/ max(len(xs), 1)")
    _edit(tmp_path, "src/demo/tasks.py", "return sum(xs)", "return sum(xs) + 0")
    _legacy_build(monkeypatch, tmp_path)
    statuses = compute_check_summary(db_path, tmp_path).statuses
    flagged = [nid for nid, status in statuses.items() if status[:2] != ("VERIFIED", "VERIFIED")]
    assert flagged
    assert test_mean not in flagged
    axiom_graph_mark_clean(str(tmp_path), "", "reviewed", node_ids=flagged)

    assert builder.build(tmp_path)["files_scanned"] == 5
    assert db.get_index_meta(db_path, builder.SCAN_SCHEME_META_KEY) == builder.SCAN_SCHEME
    assert (test_mean, _node(db_path, "src/demo/stats.py", "mean")) in _edges(db_path, "validates")
    statuses = compute_check_summary(db_path, tmp_path).statuses
    assert {statuses[nid][:2] for nid in project_ids if nid in statuses} == {("VERIFIED", "VERIFIED")}


_HELPER_AND_STATS_TESTS = (
    "from src.demo.helpers import helper\nfrom demo.stats import mean\n\n\n"
    "def test_mean():\n    assert helper(mean([1, 3])) == 2\n"
)


@workflow(
    purpose="Settling the links an upgrade build finds never hides a real edit: an unbuilt edit to code the test "
    "was already linked to, or an edit to the code it is newly linked to made in the same build, still flags the "
    "test LINKED_STALE"
)
@pytest.mark.parametrize("edited", ["already-linked", "newly-linked"])
def test_upgrade_build_still_flags_real_edits_to_linked_code(tmp_path, monkeypatch, edited):
    from axiom_graph.lifecycle.api import compute_check_summary

    db_path = _project(
        tmp_path,
        {
            "pyproject.toml": _PYTEST_SRC,
            "src/demo/__init__.py": "",
            "src/demo/stats.py": "def mean(xs):\n    return sum(xs) / len(xs)\n",
            "src/demo/helpers.py": "def helper(x):\n    return x\n",
            "tests/test_stats.py": _HELPER_AND_STATS_TESTS,
        },
    )
    _legacy_build(monkeypatch, tmp_path, discovery_only=False)
    # mean changes while the index does not yet know the test calls it.
    _edit(tmp_path, "src/demo/stats.py", "/ len(xs)", "/ max(len(xs), 1)")
    _legacy_build(monkeypatch, tmp_path)
    test_mean = _node(db_path, "tests/test_stats.py", "test_mean")
    mean = _node(db_path, "src/demo/stats.py", "mean")
    helper = _node(db_path, "src/demo/helpers.py", "helper")
    assert {to for frm, to in _edges(db_path, "validates") if frm == test_mean} == {helper}
    assert compute_check_summary(db_path, tmp_path).statuses[test_mean][1] == "VERIFIED"

    if edited == "already-linked":
        _edit(tmp_path, "src/demo/helpers.py", "return x", "return x + 0")
        target = helper
    else:
        _edit(tmp_path, "src/demo/stats.py", "max(len(xs), 1)", "max(len(xs), 2)")
        target = mean
    result = builder.build(tmp_path)

    assert result["files_scanned"] == 4
    assert (test_mean, mean) in _edges(db_path, "validates")
    # The gained link to mean is settled, yet the edit still counts.
    assert test_mean in result["scan_baselined_ids"]
    _own, link, via = compute_check_summary(db_path, tmp_path).statuses[test_mean]
    assert link == "LINKED_STALE"
    assert target in via


#: Every project-internal link a flat regular-package build of the demo fixture makes, and no other.
_FLAT_LINKS = {
    ("delegates_to", "demo.flow::pipeline::step-1", "demo.tasks::crunch"),
    ("depends_on", "demo", "demo.stats"),
    ("depends_on", "demo.flow", "demo.tasks"),
    ("depends_on", "demo.flow::pipeline", "demo.tasks"),
    ("depends_on", "tests.test_stats", "demo"),
    ("depends_on", "tests.test_stats", "demo.stats"),
    ("depends_on", "tests.test_stats::_helper", "demo.stats"),
    ("depends_on", "tests.test_stats::test_dotted", "demo.stats"),
    ("depends_on", "tests.test_stats::test_local_import", "demo.stats"),
    ("depends_on", "tests.test_stats::test_mean", "demo.stats"),
    ("depends_on", "tests.test_stats::test_reexport", "demo"),
    ("depends_on", "tests.test_stats::test_submodule", "demo.stats"),
    ("validates", "tests.test_stats::test_dotted", "demo.stats::total"),
    ("validates", "tests.test_stats::test_helper", "demo.stats::mean"),
    ("validates", "tests.test_stats::test_local_import", "demo.stats::total"),
    ("validates", "tests.test_stats::test_mean", "demo.stats::mean"),
    ("validates", "tests.test_stats::test_reexport", "demo.stats::total"),
    ("validates", "tests.test_stats::test_submodule", "demo.stats::mean"),
}


@workflow(
    purpose="Import roots change which file an import names, never a node or its hash: a flat regular-package "
    "project links exactly the code its imports name, and the root-only scans node_hashing runs hash a src-layout "
    "file as the build stored it"
)
def test_import_roots_leave_flat_output_and_node_hashes_unchanged(tmp_path):
    flat = tmp_path / "flat"
    flat_db = _project(flat, {**_demo_package(""), "tests/test_stats.py": _stats_tests()})
    builder.build(flat, discovery_only=False)
    links = {
        (e.edge_type, e.from_id.removeprefix("proj::"), e.to_id.removeprefix("proj::"))
        for e in db.all_edges(flat_db)
        if e.edge_type in ("validates", "depends_on", "delegates_to") and "::external::" not in e.to_id
    }
    assert links == _FLAT_LINKS

    src = tmp_path / "src_layout"
    files = {**_demo_package("src/"), "pyproject.toml": _PYTEST_SRC, "tests/test_stats.py": _stats_tests()}
    db_path = _project(src, files)
    builder.build(src, discovery_only=False)
    stored = {n.id: n.code_hash for n in db.all_nodes(db_path)}
    for rel in [r for r in files if r.endswith(".py")]:
        for node in module_scanner.scan_module(src / rel, src, "proj")[0]:
            if node.location != "external":
                assert stored[node.id] == node.code_hash


def _rule(finding) -> str:
    return finding["rule_id"] if isinstance(finding, dict) else finding.rule_id


@workflow(
    purpose="check's annotation overlay resolves imports with the build's roots: after an edit, a src-layout "
    "AutoStep delegating to a decorated task in another package module reports no B4 finding, as the build does"
)
def test_check_overlay_resolves_imports_like_the_build(tmp_path):
    from axiom_graph.lifecycle.api import read_annotation_findings

    db_path = _project(tmp_path, {**_demo_package("src/"), "pyproject.toml": _PYTEST_SRC})
    result = builder.build(tmp_path, discovery_only=False)
    assert [f for f in result["annotation_findings"] if _rule(f) == "B4"] == []

    flow = tmp_path / "src" / "demo" / "flow.py"
    flow.write_text(flow.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    stat = flow.stat()
    os.utime(flow, (stat.st_atime, stat.st_mtime + 5))
    findings = read_annotation_findings(db_path, tmp_path)
    assert findings.files_rescanned == 1
    assert [f for f in findings.findings if _rule(f) == "B4"] == []


@workflow(
    purpose="The README quick start works in a src layout: editing a function makes check flag the test that "
    "calls it LINKED_STALE via that function"
)
def test_src_layout_quick_start_flags_the_calling_test(tmp_path):
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli
    from axiom_graph.lifecycle.api import compute_check_summary

    口 = Step(
        step_num=1, name="Write a src-layout project", purpose="src/demo/stats.py and a test importing demo.stats"
    )
    db_path = _project(
        tmp_path,
        {
            "pyproject.toml": _PYTEST_SRC,
            "src/demo/__init__.py": "",
            "src/demo/stats.py": "def mean(xs):\n    return sum(xs) / len(xs)\n",
            "tests/test_stats.py": "from demo.stats import mean\n\n\ndef test_mean():\n    assert mean([1, 3]) == 2\n",
        },
    )

    口 = Step(step_num=2, name="Run axiom-graph init", purpose="Index the project from scratch")
    result = CliRunner().invoke(cli, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    mean = _node(db_path, "src/demo/stats.py", "mean")
    assert mean.endswith("::src.demo.stats::mean")
    test_mean = _node(db_path, "tests/test_stats.py", "test_mean")

    口 = Step(step_num=3, name="Edit the body of mean", purpose="A real code change to the function the test calls")
    stats = tmp_path / "src" / "demo" / "stats.py"
    stats.write_text("def mean(xs):\n    return sum(xs) / max(len(xs), 1)\n", encoding="utf-8")

    口 = Step(step_num=4, name="Run check", purpose="test_mean reads LINKED_STALE via the mean the build minted")
    _own, link, via = compute_check_summary(db_path, tmp_path).statuses[test_mean]
    assert link == "LINKED_STALE"
    assert mean in via
