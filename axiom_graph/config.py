"""AxiomGraphConfig — reads ``axiom-graph.toml`` from the project root.

All sections and keys are optional; missing values fall back to the defaults
defined on the dataclasses below.

Example ``axiom-graph.toml``::

    [axiom_graph]
    project_id = "pm_mvp"

    [axiom_graph.scan]
    exclude_dirs = [".venv", "data", "notebooks/scratch"]
    docs_extensions = [".docjson", ".json"]

    [axiom_graph.thresholds]
    max_function_lines = 80
    max_module_lines   = 600
    stale_days         = 90

    [axiom_graph.scan]
    test_paths = ["tests/"]

    [axiom_graph.validation]
    enabled = true

    [axiom_graph.validation.rules]
    A1 = true
    A2 = true
    A3 = true
    B1 = true
    B2 = true
    B3 = true
    B4 = true
    C1 = true
"""

from __future__ import annotations


class ConfigError(ValueError):
    """Raised when ``axiom-graph.toml`` contains an invalid / unknown key."""


class ProjectIdMismatchError(RuntimeError):
    """A project id disagrees with the one already recorded for the project.

    Raised when ``init --id`` names an id other than the ``project_id`` in
    ``axiom-graph.toml``, and when a build resolves an id other than the one
    the index stores.  The message names both ids and how to fix it; the CLI
    prints it as a clean ``Error:`` line.
    """


import contextlib
import contextvars
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from axiom_graph.index.doc_ids import DOCJSON_EXTENSIONS as _DOCJSON_EXTENSIONS
from axiom_graph.index.doc_ids import validate_docjson_extensions as _validate_docjson_extensions

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------


@dataclass
class ScanConfig:
    """File-scanning behaviour."""

    #: Directory names to skip **in addition to** the built-in set
    #: (.axiom_graph, __pycache__, .git, .venv, venv, node_modules, .tox,
    #: dist, build).  Values are bare directory names, not paths.
    exclude_dirs: list[str] = field(default_factory=list)

    #: Path prefixes that contain test code (e.g. ``["tests/"]``).
    #: Used by the viz server to filter test functions out of the
    #: Workflows tab — any workflow/task function whose module path starts
    #: with one of these prefixes is excluded from ``/api/workflows``.
    test_paths: list[str] = field(default_factory=list)

    #: Glob patterns for JS/TS files to scan (e.g.
    #: ``["viz/static/ts/*.ts"]``).  When empty, no JS/TS files are
    #: scanned.  Requires the ``[js]`` optional extra.
    js_paths: list[str] = field(default_factory=list)

    #: Python import roots, relative to the project root (e.g. ``["lib"]``).
    #: An absolute import such as ``from pkg.mod import f`` is looked up
    #: under each root in turn.  When set, these are tried before every
    #: detected root (pytest ``pythonpath``, packaging config, the project
    #: root, an auto-detected ``src/``).  Empty means detection only.
    source_roots: list[str] = field(default_factory=list)

    #: Directories containing documentation to scan (both Markdown and
    #: DocJSON).  Paths are relative to the project root.  Defaults to
    #: ``["docs"]`` for backward compatibility.  Non-existent entries
    #: emit a build warning ONLY when the user explicitly configured this
    #: key (see ``docs_dirs_explicit``); default-missing is silent.
    docs_dirs: list[str] = field(default_factory=lambda: ["docs"])

    #: Directories containing agent/config artifacts to scan (settings,
    #: skills, hooks, etc.).  Paths are relative to the project root.
    #: Defaults to ``[".claude"]`` for backward compatibility.
    config_dirs: list[str] = field(default_factory=lambda: [".claude"])

    #: File extensions read as DocJSON documents under ``docs_dirs``.  Only
    #: ``.docjson`` and ``.json`` are accepted; the first entry is the
    #: extension a new document is written with.  The order does not decide
    #: a collision: when ``x.json`` and ``x.docjson`` are both documents,
    #: ``.docjson`` is indexed.
    docs_extensions: list[str] = field(default_factory=lambda: list(_DOCJSON_EXTENSIONS))

    #: Configured ``docs_extensions`` entries that were not valid and were
    #: dropped.  The build reports them as a warning.
    docs_extensions_dropped: list[str] = field(default_factory=list)

    #: True when ``docs_dirs`` was explicitly set in ``axiom-graph.toml``.
    #: Used by ``build()`` to distinguish user-configured-missing (emit a
    #: build warning) from default-missing (silent — preserves byte-for-byte
    #: backward compatibility with projects that have no ``docs/`` dir).
    docs_dirs_explicit: bool = False

    #: True when ``config_dirs`` was explicitly set in ``axiom-graph.toml``.
    #: Same semantics as ``docs_dirs_explicit`` — default-missing
    #: ``.claude/`` must not produce a warning.
    config_dirs_explicit: bool = False


@dataclass
class ThresholdsConfig:
    """Size / freshness thresholds used for tagging at build time."""

    max_function_lines: int = 80
    max_module_lines: int = 600
    stale_days: int = 90


@dataclass
class StalenessConfig:
    """Staleness engine behaviour.

    Controls transitive propagation of LINKED_STALE signals through
    doc-to-doc ``documents`` edges.  Only docs whose parent document
    carries a tag listed in ``transitive_tags`` participate.

    ``frozen_tags`` is the symmetric opt-out: docs whose parent document
    carries any tag listed in ``frozen_tags`` are treated as write-once
    historical records (ADRs, plans, PEV cycle manifests, etc.).  Their
    sections are skipped at staleness-signal entry (Pass 1 doc-to-code)
    and never participate in transitive propagation (Pass 3).  A
    LINKED_STALE a section already had is kept only while a linked node has
    changed since its last verification.  BROKEN_LINK is independent of
    LINKED_STALE and still surfaces on frozen docs.
    """

    #: Doc-level tags that opt in to transitive LINKED_STALE propagation.
    #: When empty (the default), no transitive propagation occurs.
    transitive_tags: list[str] = field(default_factory=list)

    #: Doc-level tags that opt OUT of LINKED_STALE propagation.  Sections
    #: under a doc carrying any of these tags never receive LINKED_STALE
    #: signal (Pass 1 + Pass 3 skip).  Empty (the default) is a no-op.
    frozen_tags: list[str] = field(default_factory=list)

    #: What a read tool does to the stored statuses before it answers:
    #: ``"changed-files"`` (the default) refreshes what the read shows (a
    #: listing of the whole repo: every file whose bytes changed);
    #: ``"off"`` answers from the stored statuses and says how many files
    #: the index is behind; ``"check"`` runs ``check`` first.
    refresh_before_read: str = "changed-files"


#: The values ``[axiom_graph.staleness] refresh_before_read`` accepts.
REFRESH_BEFORE_READ_MODES: tuple[str, ...] = ("off", "changed-files", "check")


_VALID_RULE_IDS: frozenset[str] = frozenset({"A1", "A2", "A3", "B1", "B2", "B3", "B4", "C1"})


@dataclass
class ValidationConfig:
    """Annotation validation settings.

    ``enabled`` is the master switch (False suppresses ALL findings).
    ``rules`` maps rule IDs (A1-A3, B1-B4, C1) to booleans — False per-rule
    suppresses that rule only.  Missing rule keys default to True.  Unknown
    keys raise :class:`ConfigError`.
    """

    enabled: bool = True
    rules: dict[str, bool] = field(default_factory=lambda: {rid: True for rid in _VALID_RULE_IDS})

    def is_enabled(self, rule_id: str) -> bool:
        """Return True iff the given rule should emit findings."""
        if not self.enabled:
            return False
        return self.rules.get(rule_id, True)


_VALID_TARGET_FORMATS: frozenset[str] = frozenset({"plain", "sphinx"})


@dataclass
class SiteTarget:
    """A single declared render destination for the docs graph.

    A target describes either a single-doc render (``doc`` set) or a
    nav-driven subtree render (``nav`` set) — exactly one of the two.  The
    ``format`` selects plain GFM (``plain``) or Sphinx/MyST (``sphinx``)
    output; only ``sphinx`` may use a ``nav`` and only ``nav`` may be
    ``sphinx``.

    Attributes:
        name: Unique target identifier (used by ``--target`` / ``only=``).
        output: Output path relative to project root.  For ``nav`` targets
            this is the output directory; for ``doc`` targets the output
            file.
        format: ``"plain"`` (GitHub-friendly Markdown) or ``"sphinx"``
            (MyST/toctree scaffolding).  Defaults to ``"plain"``.
        nav: Path to a ``site-nav.yml`` relative to project root (mutually
            exclusive with ``doc``).
        doc: A single DocJSON doc-id to render to ``output`` (mutually
            exclusive with ``nav``).
        overwrite: When ``True``, an existing un-stamped file at ``output``
            is replaced; otherwise it is warn-and-skipped (path safety).
    """

    name: str
    output: str
    format: str = "plain"
    nav: str | None = None
    doc: str | None = None
    overwrite: bool = False


@dataclass
class SiteConfig:
    """Consumer documentation site settings.

    Controls the ``axiom-graph render-site`` pipeline that converts DocJSON
    docs into clean Markdown suitable for MkDocs.
    """

    #: Path to the site navigation YAML file, relative to project root.
    nav_file: str = "site-nav.yml"
    #: Output directory for the generated site, relative to project root.
    output_dir: str = "site"
    #: Declared render destinations.  Empty means "synthesize an implicit
    #: ``guide`` sphinx target from ``nav_file`` -> ``userdocs/guide``"
    #: (see :func:`axiom_graph.docjson.render_consumer.resolve_targets`).
    targets: list[SiteTarget] = field(default_factory=list)


@dataclass
class RenameConfig:
    """Scoped-similarity rename-detection settings.

    Controls the body-similarity matcher that welds a lost node to a
    newly-appeared node when their bodies are similar enough.  ``code_threshold``
    governs code nodes; ``prose_threshold`` is reserved for the deferred
    DocJSON adapter (D-4).  ``pool_cap`` caps the size of a scoped candidate
    pool before the matcher degrades to exact-``code_hash`` fallback.
    """

    #: Minimum body-similarity ratio to auto-apply a code rename.
    code_threshold: float = 0.6
    #: Minimum ratio for prose renames (deferred DocJSON adapter).
    prose_threshold: float = 0.5
    #: Max ``len(lost) + len(found)`` in a scope before exact-hash fallback.
    pool_cap: int = 50


@dataclass
class DocjsonConfig:
    """``[axiom_graph.docjson]`` settings.

    ``raw_docjson_edits`` controls what indexing does with a DocJSON section
    edited outside the doc tools (a raw DocJSON edit):

    - ``"warn"`` (default) -- record one ``RAW_DOCJSON_EDIT`` history event
      per edit and report it once.
    - ``"off"`` -- record and report nothing; such sections simply stay
      unverified under the normal staleness rules.

    Either way, tool-written sections that arrive by merge or pull are still
    verified when their stamp matches, and the doc tools still stamp.
    """

    raw_docjson_edits: str = "warn"


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


@dataclass
class AxiomGraphConfig:
    """Full axiom-graph configuration.  All fields have sensible defaults."""

    #: Override the project ID (same as ``--id`` on the CLI).
    #: ``None`` means "fall back to project directory name".
    project_id: str | None = None
    scan: ScanConfig = field(default_factory=ScanConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    staleness: StalenessConfig = field(default_factory=StalenessConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    site: SiteConfig = field(default_factory=SiteConfig)
    rename: RenameConfig = field(default_factory=RenameConfig)
    docjson: DocjsonConfig = field(default_factory=DocjsonConfig)
    #: Path to the axiom-graph index SQLite database.  Relative paths are
    #: resolved against the project root; absolute paths are honored as-is.
    #: Defaults to ``".axiom_graph/graph.db"``.
    db_path: str = ".axiom_graph/graph.db"

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, project_root: Path) -> "AxiomGraphConfig":
        """Load ``axiom-graph.toml`` from *project_root*.

        Returns a default ``AxiomGraphConfig`` if the file is absent or if
        the TOML library is not available (Python 3.10 without ``tomli``
        installed).  No ``axiom-graph.toml`` fallback.

        Inside :func:`config_scope` the first load of a project root is kept
        and every later load of it in the scope returns that config; outside
        one, every call reads the file.  A failed load is not kept.
        """
        held = _SCOPED_CONFIGS.get()
        if held is None:
            return cls._read(project_root)
        key = (cls, Path(project_root).resolve())
        if key not in held:
            held[key] = cls._read(project_root)
        return held[key]

    @classmethod
    def _read(cls, project_root: Path) -> "AxiomGraphConfig":
        """Parse ``axiom-graph.toml`` from *project_root* (:meth:`load` without the scope).

        Args:
            project_root: Project root directory.

        Returns:
            The parsed config, or the defaults when the file is absent or no
            TOML library is available.
        """
        toml_path = Path(project_root) / "axiom-graph.toml"
        if not toml_path.exists() or tomllib is None:
            return cls()

        with toml_path.open("rb") as fh:
            raw: dict = tomllib.load(fh)

        ag_section: dict = raw.get("axiom_graph", {})

        project_id: str | None = ag_section.get("project_id", None)

        scan_raw: dict = ag_section.get("scan", {})
        # docs_dirs / config_dirs: honor missing key (default) vs empty list.
        # List values are accepted verbatim; non-list values fall back to the
        # default list.  Each entry is coerced to str.  We also track whether
        # the key was explicitly set so the builder can distinguish
        # user-configured-missing (warn) from default-missing (silent).
        _raw_docs_dirs = scan_raw.get("docs_dirs")
        if isinstance(_raw_docs_dirs, list):
            docs_dirs = [str(x) for x in _raw_docs_dirs]
            docs_dirs_explicit = True
        else:
            docs_dirs = ["docs"]
            # Non-list malformed values fall back silently — treat as default.
            docs_dirs_explicit = False
        _raw_config_dirs = scan_raw.get("config_dirs")
        if isinstance(_raw_config_dirs, list):
            config_dirs = [str(x) for x in _raw_config_dirs]
            config_dirs_explicit = True
        else:
            config_dirs = [".claude"]
            config_dirs_explicit = False
        # docs_extensions: validated against the DocJSON extensions; invalid
        # entries are dropped (and reported by the build), and a missing,
        # empty or wholly invalid value means the default.
        docs_extensions, docs_extensions_dropped = _validate_docjson_extensions(scan_raw.get("docs_extensions"))
        scan = ScanConfig(
            exclude_dirs=list(scan_raw.get("exclude_dirs", [])),
            test_paths=list(scan_raw.get("test_paths", [])),
            js_paths=list(scan_raw.get("js_paths", [])),
            source_roots=_str_list(scan_raw.get("source_roots", [])),
            docs_dirs=docs_dirs,
            docs_extensions=docs_extensions,
            docs_extensions_dropped=docs_extensions_dropped,
            config_dirs=config_dirs,
            docs_dirs_explicit=docs_dirs_explicit,
            config_dirs_explicit=config_dirs_explicit,
        )

        thresh_raw: dict = ag_section.get("thresholds", {})
        thresholds = ThresholdsConfig(
            max_function_lines=int(thresh_raw.get("max_function_lines", 80)),
            max_module_lines=int(thresh_raw.get("max_module_lines", 600)),
            stale_days=int(thresh_raw.get("stale_days", 90)),
        )

        staleness_raw: dict = ag_section.get("staleness", {})
        refresh_before_read = str(staleness_raw.get("refresh_before_read", "changed-files"))
        if refresh_before_read not in REFRESH_BEFORE_READ_MODES:
            raise ConfigError(
                f"[axiom_graph.staleness] refresh_before_read must be one of {list(REFRESH_BEFORE_READ_MODES)}, "
                f"got {refresh_before_read!r}"
            )
        staleness = StalenessConfig(
            transitive_tags=list(staleness_raw.get("transitive_tags", [])),
            frozen_tags=list(staleness_raw.get("frozen_tags", [])),
            refresh_before_read=refresh_before_read,
        )

        validation_raw: dict = ag_section.get("validation", {})
        validation_enabled = bool(validation_raw.get("enabled", True))
        rules_raw: dict = validation_raw.get("rules", {})
        if not isinstance(rules_raw, dict):
            raise ConfigError(f"[axiom_graph.validation.rules] must be a table, got {type(rules_raw).__name__}")
        rules: dict[str, bool] = {rid: True for rid in _VALID_RULE_IDS}
        for key, val in rules_raw.items():
            if key not in _VALID_RULE_IDS:
                raise ConfigError(
                    f"[axiom_graph.validation.rules] unknown rule id {key!r} (valid: {sorted(_VALID_RULE_IDS)})"
                )
            rules[key] = bool(val)
        validation = ValidationConfig(enabled=validation_enabled, rules=rules)

        site_raw: dict = ag_section.get("site", {})
        targets = _parse_site_targets(site_raw.get("targets", []))
        site = SiteConfig(
            nav_file=str(site_raw.get("nav_file", "site-nav.yml")),
            output_dir=str(site_raw.get("output_dir", "site")),
            targets=targets,
        )

        rename_raw: dict = ag_section.get("rename", {})
        rename = RenameConfig(
            code_threshold=float(rename_raw.get("code_threshold", 0.6)),
            prose_threshold=float(rename_raw.get("prose_threshold", 0.5)),
            pool_cap=int(rename_raw.get("pool_cap", 50)),
        )

        docjson_raw: dict = ag_section.get("docjson", {})
        raw_edits = str(docjson_raw.get("raw_docjson_edits", "warn"))
        if raw_edits not in ("warn", "off"):
            raise ConfigError(f'[axiom_graph.docjson] raw_docjson_edits must be "warn" or "off", got {raw_edits!r}')
        docjson = DocjsonConfig(raw_docjson_edits=raw_edits)

        db_path_raw = ag_section.get("db_path", ".axiom_graph/graph.db")
        db_path = str(db_path_raw) if db_path_raw else ".axiom_graph/graph.db"

        return cls(
            project_id=project_id,
            scan=scan,
            thresholds=thresholds,
            staleness=staleness,
            validation=validation,
            site=site,
            rename=rename,
            docjson=docjson,
            db_path=db_path,
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _str_list(raw: object) -> list[str]:
    """Return a TOML list of strings as a list; a bare string is a one-item list.

    Args:
        raw: The raw TOML value.

    Returns:
        The entries as strings.
    """
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw]


def _parse_site_targets(raw: object) -> list[SiteTarget]:
    """Parse and validate ``[[axiom_graph.site.targets]]`` table entries.

    Each entry must declare exactly one of ``nav`` / ``doc``; a ``format`` in
    ``{plain, sphinx}`` (default ``plain``); a ``sphinx`` target must use a
    ``nav`` (never a ``doc``); and target names must be unique.

    Args:
        raw: The ``targets`` value from the ``[axiom_graph.site]`` table —
            expected to be a list of tables (dicts).

    Returns:
        Ordered list of validated :class:`SiteTarget` instances.  An empty or
        missing value yields ``[]`` (implicit-guide synthesis happens later).

    Raises:
        ConfigError: If any target is malformed (missing ``name``/``output``,
            both/neither ``nav``+``doc``, unknown ``format``, ``sphinx``+``doc``,
            or a duplicate name).
    """
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"[[axiom_graph.site.targets]] must be an array of tables, got {type(raw).__name__}")

    targets: list[SiteTarget] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"site target #{i} must be a table, got {type(entry).__name__}")

        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise ConfigError(f"site target #{i} is missing a non-empty string 'name'")
        output = entry.get("output")
        if not output or not isinstance(output, str):
            raise ConfigError(f"site target {name!r} is missing a non-empty string 'output'")

        fmt = str(entry.get("format", "plain"))
        if fmt not in _VALID_TARGET_FORMATS:
            raise ConfigError(
                f"site target {name!r} has unknown format {fmt!r} (valid: {sorted(_VALID_TARGET_FORMATS)})"
            )

        nav = entry.get("nav")
        doc = entry.get("doc")
        if (nav is None) == (doc is None):
            raise ConfigError(f"site target {name!r} must declare exactly one of 'nav' or 'doc'")
        if fmt == "sphinx" and doc is not None:
            raise ConfigError(f"site target {name!r} is format 'sphinx' but declares 'doc'; sphinx requires 'nav'")

        if name in seen_names:
            raise ConfigError(f"duplicate site target name {name!r}")
        seen_names.add(name)

        targets.append(
            SiteTarget(
                name=name,
                output=str(output),
                format=fmt,
                nav=str(nav) if nav is not None else None,
                doc=str(doc) if doc is not None else None,
                overwrite=bool(entry.get("overwrite", False)),
            )
        )
    return targets


#: The configs loaded inside the active :func:`config_scope`, keyed by class
#: and resolved project root; ``None`` outside a scope.
_SCOPED_CONFIGS: contextvars.ContextVar[dict[tuple[type, Path], AxiomGraphConfig] | None] = contextvars.ContextVar(
    "_SCOPED_CONFIGS", default=None
)


@contextlib.contextmanager
def config_scope() -> Iterator[None]:
    """Read each project's ``axiom-graph.toml`` at most once for one operation.

    While the scope is open, :meth:`AxiomGraphConfig.load` keeps the first
    config it loads for a project root and returns it to every later load in
    the scope.  The scope is reset when the outermost one exits, normally or
    by an exception, so nothing is kept across operations: a config edited
    between two operations is read again by the second.  A nested scope
    shares the outer one's configs.

    Yields:
        Nothing; the scope lasts for the ``with`` block.
    """
    if _SCOPED_CONFIGS.get() is not None:
        yield
        return
    token = _SCOPED_CONFIGS.set({})
    try:
        yield
    finally:
        _SCOPED_CONFIGS.reset(token)


def _forget_scoped_config(project_root: Path) -> None:
    """Drop the configs the open scope holds for *project_root* (after the file is rewritten).

    Args:
        project_root: Project root whose ``axiom-graph.toml`` changed.
    """
    held = _SCOPED_CONFIGS.get()
    if held:
        root = Path(project_root).resolve()
        for key in [k for k in held if k[1] == root]:
            del held[key]


def db_path_for(project_root: Path) -> Path:
    """Return the absolute path to the axiom-graph index DB for a project.

    Loads ``axiom-graph.toml`` from *project_root* and honors the
    ``axiom_graph.db_path`` key.  Absolute values are returned as-is; relative
    values are resolved against *project_root*.

    Args:
        project_root: Absolute (or resolvable) path to the project directory.

    Returns:
        Absolute ``Path`` to the axiom-graph SQLite database file.
    """
    root = Path(project_root).resolve()
    cfg = AxiomGraphConfig.load(root)
    raw = Path(cfg.db_path)
    return raw if raw.is_absolute() else root / raw


# ---------------------------------------------------------------------------
# Recording the project id in axiom-graph.toml
# ---------------------------------------------------------------------------

_AXIOM_GRAPH_HEADER_RE = re.compile(r"^\s*\[\s*axiom_graph\s*\]\s*(#.*)?$")
_ANY_TABLE_HEADER_RE = re.compile(r"^\s*\[")
_EMPTY_PROJECT_ID_RE = re.compile(r"""^\s*project_id\s*=\s*(""|'')\s*(#.*)?$""")


def check_toml_project_id(project_root: Path, project_id: str) -> None:
    """Refuse *project_id* when ``axiom-graph.toml`` already holds a different one.

    Args:
        project_root: The project directory holding ``axiom-graph.toml``.
        project_id: The id the caller wants to record.

    Raises:
        ProjectIdMismatchError: When the toml's ``project_id`` is set and
            differs from *project_id*.
    """
    toml_id = AxiomGraphConfig.load(Path(project_root)).project_id or None
    if toml_id is not None and toml_id != project_id:
        raise ProjectIdMismatchError(
            f"axiom-graph.toml already sets project id '{toml_id}'; --id '{project_id}' will not "
            "overwrite it. Nothing was changed. Run `axiom-graph init` without --id to keep "
            f"'{toml_id}', or edit project_id in axiom-graph.toml to change the project id."
        )


def write_toml_project_id(project_root: Path, project_id: str) -> bool:
    """Record *project_id* as ``[axiom_graph] project_id`` in ``axiom-graph.toml``.

    Creates the file when it is absent.  Otherwise an empty
    ``project_id = ""`` line is replaced, or the key is inserted under the
    existing ``[axiom_graph]`` table, or a new ``[axiom_graph]`` table is
    inserted before the file's first table; every other line, comment and
    line ending is kept as it was.

    Args:
        project_root: The project directory holding ``axiom-graph.toml``.
        project_id: The id to record.

    Returns:
        ``True`` when the file was written, ``False`` when it already held
        *project_id*.

    Raises:
        ProjectIdMismatchError: When the toml already holds a different id.
        ConfigError: When the edited file would not parse back to
            *project_id* (the file is left unchanged).
    """
    check_toml_project_id(project_root, project_id)
    toml_path = Path(project_root) / "axiom-graph.toml"
    if not toml_path.exists():
        with toml_path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(f"[axiom_graph]\nproject_id = {json.dumps(project_id)}\n")
        _forget_scoped_config(project_root)
        return True
    current_id = AxiomGraphConfig.load(Path(project_root)).project_id
    if current_id == project_id:
        return False

    with toml_path.open(encoding="utf-8", newline="") as fh:
        text = fh.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    key_line = f"project_id = {json.dumps(project_id)}{nl}"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl

    header_at = next((i for i, ln in enumerate(lines) if _AXIOM_GRAPH_HEADER_RE.match(ln.rstrip("\r\n"))), None)
    empty_at = (
        next((i for i, ln in enumerate(lines) if _EMPTY_PROJECT_ID_RE.match(ln.rstrip("\r\n"))), None)
        if current_id == ""
        else None
    )
    if empty_at is not None:
        lines[empty_at] = key_line
    elif header_at is not None:
        lines.insert(header_at + 1, key_line)
    else:
        first_table = next((i for i, ln in enumerate(lines) if _ANY_TABLE_HEADER_RE.match(ln)), len(lines))
        block = f"[axiom_graph]{nl}{key_line}"
        if first_table < len(lines):
            block += nl
        elif lines and lines[-1].strip():
            block = nl + block
        lines.insert(first_table, block)
    new_text = "".join(lines)

    if tomllib is not None:
        try:
            parsed = tomllib.loads(new_text)
        except Exception as exc:  # tomllib.TOMLDecodeError
            raise ConfigError(f"could not add project_id to axiom-graph.toml: {exc}") from exc
        if parsed.get("axiom_graph", {}).get("project_id") != project_id:
            raise ConfigError("could not add project_id to axiom-graph.toml; add it under [axiom_graph] by hand")
    with toml_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    _forget_scoped_config(project_root)
    return True


# ---------------------------------------------------------------------------
# Comparing axiom-graph.toml with the defaults
# ---------------------------------------------------------------------------

#: Stands in for the default of a toml key that is not an axiom-graph setting.
NO_DEFAULT = object()


def _flatten_toml(table: dict, prefix: str) -> Iterator[tuple[str, object]]:
    """Yield ``(dotted key, value)`` for every leaf of a parsed TOML table; arrays are leaves."""
    for key, value in table.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            yield from _flatten_toml(value, dotted)
        else:
            yield dotted, value


def toml_settings_off_default(project_root: Path) -> list[tuple[str, object, object]]:
    """List the settings in ``axiom-graph.toml`` that resetting it to the defaults would change.

    Every key other than ``[axiom_graph] project_id`` whose value differs from
    the built-in default, in file order.  A key with no default (not an
    axiom-graph setting, or outside ``[axiom_graph]``) is listed with
    :data:`NO_DEFAULT`.

    Args:
        project_root: The project directory holding ``axiom-graph.toml``.

    Returns:
        ``(dotted key, value, default)`` triples; empty when the file is
        absent, holds only defaults, or no TOML library is available.
    """
    toml_path = Path(project_root) / "axiom-graph.toml"
    if tomllib is None or not toml_path.exists():
        return []
    with toml_path.open("rb") as fh:
        raw: dict = tomllib.load(fh)
    defaults = asdict(AxiomGraphConfig())
    changes: list[tuple[str, object, object]] = []
    for key, value in _flatten_toml(raw, ""):
        if key == "axiom_graph.project_id":
            continue
        default: object = NO_DEFAULT
        if key.startswith("axiom_graph."):
            default = defaults
            for part in key.split(".")[1:]:
                if not isinstance(default, dict) or part not in default:
                    default = NO_DEFAULT
                    break
                default = default[part]
        if value != default:
            changes.append((key, value, default))
    return changes


def reset_toml_to_defaults(project_root: Path, project_id: str) -> None:
    """Rewrite ``axiom-graph.toml`` to hold only ``[axiom_graph] project_id``.

    Every other setting goes back to its built-in default.

    Args:
        project_root: The project directory holding ``axiom-graph.toml``.
        project_id: The id to keep.
    """
    toml_path = Path(project_root) / "axiom-graph.toml"
    with toml_path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"[axiom_graph]\nproject_id = {json.dumps(project_id)}\n")
    _forget_scoped_config(project_root)
