"""Python import roots: where an absolute import of project code is looked up.

``from demo.stats import mean`` names a module by its import path, not its
file path.  In a flat layout the two agree under the project root; in a
``src/`` layout the module lives at ``src/demo/stats.py``.  This module
works out the ordered list of directories an absolute import is tried
against, from configuration the project already has:

1. ``[axiom_graph.scan] source_roots`` in ``axiom-graph.toml`` (explicit);
2. pytest's ``pythonpath``, read from the first pytest config file found
   (``pytest.ini``, ``.pytest.ini``, ``pyproject.toml``, ``tox.ini``,
   ``setup.cfg``, the order pytest itself uses);
3. packaging config: setuptools ``package-dir`` / ``packages.find where``
   (``pyproject.toml`` and ``setup.cfg``), poetry ``packages[].from``,
   hatch wheel ``packages`` / ``sources``;
4. the project root;
5. ``src/``, when it holds a Python package or module.

Entries that don't exist, lie outside the project or sit under a skipped
directory are dropped, and each directory keeps its first position only.
Only declarative files are read: ``setup.py`` is executable and never
parsed.  Nothing here touches the index.

:func:`declared_dependencies` reads the same ``pyproject.toml`` for the
distributions the project depends on: an import of one is external even
when a copy of its source sits in the tree.
"""

from __future__ import annotations

import configparser
import json
import logging
import re
from collections.abc import Container, Iterable, Sequence
from pathlib import Path

from axiom_graph.config import tomllib

logger = logging.getLogger(__name__)

#: Directory auto-detected as an import root when it holds Python code.
_AUTO_SRC_DIR = "src"

#: The distribution name at the start of a PEP 508 requirement string.
_PEP508_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def resolve_source_roots(
    project_root: Path,
    explicit: Sequence[str] = (),
    skip_dirs: Container[str] = (),
) -> tuple[Path, ...]:
    """Return the ordered import roots of *project_root*.

    Args:
        project_root: Absolute project root.
        explicit: ``[axiom_graph.scan] source_roots`` entries, relative to
            the project root.  They come first, ahead of every detected root.
        skip_dirs: Directory names the build skips; a root under one of
            them is dropped.

    Returns:
        The roots, each spelled ``project_root / <relative path>`` so a file
        found under it stays relative to *project_root*.  The project root
        itself is always present.
    """
    project_root = Path(project_root)
    pyproject = _read_toml(project_root / "pyproject.toml")
    setup_cfg = _read_ini(project_root / "setup.cfg")

    candidates: list[str] = [str(e) for e in explicit]
    candidates += _pytest_pythonpath(project_root, pyproject, setup_cfg)
    candidates += _packaging_roots(pyproject, setup_cfg)
    candidates.append(".")
    if _holds_python(project_root / _AUTO_SRC_DIR, skip_dirs):
        candidates.append(_AUTO_SRC_DIR)

    roots: list[Path] = []
    seen: set[str] = set()
    for entry in candidates:
        rel = _contained_rel(project_root, entry, skip_dirs)
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        roots.append(project_root if rel == "." else project_root / rel)
    return tuple(roots)


def roots_fingerprint(project_root: Path, roots: Iterable[Path]) -> str:
    """Return a stable text fingerprint of *roots*.

    Two builds that resolve the same roots in the same order get the same
    fingerprint, wherever the project is checked out.

    Args:
        project_root: The project root the roots were resolved against.
        roots: Roots from :func:`resolve_source_roots`.

    Returns:
        A JSON list of the roots' project-relative POSIX paths (``"."`` for
        the project root).
    """
    rels = []
    for root in roots:
        rel = Path(root).relative_to(project_root).as_posix()
        rels.append(rel or ".")
    return json.dumps(rels)


def declared_dependencies(project_root: Path) -> frozenset[str]:
    """Return the distribution names the project declares as dependencies.

    Read from ``pyproject.toml``: ``[project] dependencies`` and
    ``optional-dependencies`` (PEP 508 strings), and poetry's
    ``[tool.poetry.dependencies]``, ``dev-dependencies`` and
    ``[tool.poetry.group.*.dependencies]`` tables.  Each name is normalised
    the way an import name is spelled: lowercase, ``-`` and ``.`` become
    ``_``.  Poetry's ``python`` entry is not a dependency and is dropped.

    Args:
        project_root: Absolute project root.

    Returns:
        The normalised names; empty when there is no readable pyproject.
    """
    pyproject = _read_toml(Path(project_root) / "pyproject.toml")
    specs: list[str] = list(_as_list(_get(pyproject, "project", "dependencies")))
    optional = _get(pyproject, "project", "optional-dependencies")
    if isinstance(optional, dict):
        for extra in optional.values():
            specs += _as_list(extra) if isinstance(extra, list) else []
    tables = [_get(pyproject, "tool", "poetry", "dependencies"), _get(pyproject, "tool", "poetry", "dev-dependencies")]
    groups = _get(pyproject, "tool", "poetry", "group")
    if isinstance(groups, dict):
        tables += [_get(group, "dependencies") for group in groups.values()]
    for table in tables:
        if isinstance(table, dict):
            specs += [str(name) for name in table]
    names = set()
    for spec in specs:
        match = _PEP508_NAME.match(spec)
        if match:
            names.add(re.sub(r"[-.]+", "_", match.group(1)).lower())
    names.discard("python")
    return frozenset(names)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _contained_rel(project_root: Path, entry: str, skip_dirs: Container[str]) -> str | None:
    """Return *entry* as a normalised project-relative POSIX path, or ``None`` when unusable.

    Unusable means: empty, not an existing directory, outside the project,
    or under a skipped directory.
    """
    entry = entry.strip()
    if not entry:
        return None
    path = Path(entry)
    if not path.is_absolute():
        path = project_root / path
    try:
        if not path.is_dir():
            return None
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if rel in ("", "."):
        return "."
    if any(part in skip_dirs for part in rel.split("/")):
        return None
    return rel


def _holds_python(directory: Path, skip_dirs: Container[str]) -> bool:
    """Return True when *directory* holds a ``.py`` module or a directory with one."""
    try:
        if not directory.is_dir():
            return False
        for child in directory.iterdir():
            if child.is_file() and child.suffix == ".py":
                return True
            if child.is_dir() and child.name not in skip_dirs and any(p.suffix == ".py" for p in child.iterdir()):
                return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_toml(path: Path) -> dict:
    """Return the parsed TOML file, or ``{}`` when absent or unreadable."""
    if tomllib is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:  # malformed config never fails a build
        logger.debug("source roots: could not read %s: %s", path, exc)
        return {}


def _read_ini(path: Path) -> configparser.ConfigParser | None:
    """Return the parsed INI file, or ``None`` when absent or unreadable."""
    if not path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError) as exc:
        logger.debug("source roots: could not read %s: %s", path, exc)
        return None
    return parser


def _get(table: dict, *keys: str):
    """Return the nested value at *keys* in *table*, or ``None``."""
    node = table
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _as_list(value) -> list[str]:
    """Return a TOML list, a whitespace-separated string or ``None`` as a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _pytest_pythonpath(project_root: Path, pyproject: dict, setup_cfg: configparser.ConfigParser | None) -> list[str]:
    """Return ``pythonpath`` from the first pytest config file found, in pytest's order."""
    for name in ("pytest.ini", ".pytest.ini"):
        ini = _read_ini(project_root / name)
        if ini is not None:
            # pytest.ini is chosen even without a [pytest] section.
            return ini.get("pytest", "pythonpath", fallback="").split()
    ini_options = _get(pyproject, "tool", "pytest", "ini_options")
    if isinstance(ini_options, dict):
        return _as_list(ini_options.get("pythonpath"))
    tox = _read_ini(project_root / "tox.ini")
    if tox is not None and tox.has_section("pytest"):
        return tox.get("pytest", "pythonpath", fallback="").split()
    if setup_cfg is not None and setup_cfg.has_section("tool:pytest"):
        return setup_cfg.get("tool:pytest", "pythonpath", fallback="").split()
    return []


def _packaging_roots(pyproject: dict, setup_cfg: configparser.ConfigParser | None) -> list[str]:
    """Return the import roots declared by setuptools, poetry and hatch config."""
    roots: list[str] = []

    # setuptools (pyproject.toml)
    package_dir = _get(pyproject, "tool", "setuptools", "package-dir")
    if isinstance(package_dir, dict):
        roots += _package_dir_roots(package_dir)
    roots += _as_list(_get(pyproject, "tool", "setuptools", "packages", "find", "where"))

    # setuptools (setup.cfg)
    if setup_cfg is not None:
        raw = setup_cfg.get("options", "package_dir", fallback="")
        mapping: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                mapping[key.strip()] = value.strip()
            elif line.strip():
                mapping[""] = line.strip()
        roots += _package_dir_roots(mapping)
        roots += setup_cfg.get("options.packages.find", "where", fallback="").split()

    # poetry
    packages = _get(pyproject, "tool", "poetry", "packages")
    if isinstance(packages, list):
        for pkg in packages:
            if isinstance(pkg, dict) and isinstance(pkg.get("from"), str):
                roots.append(pkg["from"])

    # hatch (wheel target)
    wheel = _get(pyproject, "tool", "hatch", "build", "targets", "wheel")
    if isinstance(wheel, dict):
        for pkg in _as_list(wheel.get("packages")):
            parent = Path(pkg).parent.as_posix()
            roots.append(parent)
        sources = wheel.get("sources")
        if isinstance(sources, list):
            roots += [str(s) for s in sources]
        elif isinstance(sources, dict):
            roots += [str(k) for k, v in sources.items() if v == ""]
    return roots


def _package_dir_roots(package_dir: dict) -> list[str]:
    """Return the import roots a setuptools ``package-dir`` mapping declares.

    ``{"" = "src"}`` makes ``src`` the root.  ``{"pkg" = "lib/pkg"}`` makes
    ``lib`` the root when the directory carries the package's own name.
    """
    roots: list[str] = []
    for key, value in package_dir.items():
        if not isinstance(value, str):
            continue
        if key == "":
            roots.append(value)
        elif "." not in key and Path(value).name == key:
            roots.append(Path(value).parent.as_posix())
    return roots


__all__ = ["declared_dependencies", "resolve_source_roots", "roots_fingerprint"]
