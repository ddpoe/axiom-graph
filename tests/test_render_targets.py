"""Tests for configurable render targets.

Covers two subsystems:

- ``[[axiom_graph.site.targets]]`` parsing + validation (config.py)
- the multi-target render pipeline (render_consumer.py): ``fmt`` switch on
  ``build_site``, ``render_doc_to_file``, path-safety, the hybrid manifest, and
  the ``render_targets`` orchestrator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from axiom_annotations import Step, workflow

from axiom_graph.config import AxiomGraphConfig, ConfigError
from axiom_graph.docjson.render_consumer import (
    build_site,
    render_doc_consumer,
    render_doc_to_file,
    render_targets,
    resolve_targets,
)

# Reuse the consumer-render fixtures for DB seeding.
from tests.test_consumer_render import _project, _seed_doc, _slim_nav


# ---------------------------------------------------------------------------
# Task 1: config parsing + validation
# ---------------------------------------------------------------------------


def _write_toml(root: Path, body: str) -> None:
    (root / "axiom-graph.toml").write_text(body, encoding="utf-8")


class TestSiteTargetConfig:
    """``[[axiom_graph.site.targets]]`` parsing + validation."""

    def test_valid_three_targets_parse(self, tmp_path: Path):
        _write_toml(
            tmp_path,
            """
[axiom_graph]
project_id = "demo"

[[axiom_graph.site.targets]]
name = "guide"
output = "userdocs/guide"
format = "sphinx"
nav = "site-nav.yml"

[[axiom_graph.site.targets]]
name = "readme"
output = "README.md"
format = "plain"
doc = "demo::docs.consumer.readme"
overwrite = true

[[axiom_graph.site.targets]]
name = "plugin-pev"
output = "pev_nexus_agents/pev/docs"
format = "plain"
nav = "docs/consumer/plugins/pev/nav.yml"
""",
        )
        cfg = AxiomGraphConfig.load(tmp_path)
        assert [t.name for t in cfg.site.targets] == ["guide", "readme", "plugin-pev"]
        guide, readme, plugin = cfg.site.targets
        assert guide.format == "sphinx" and guide.nav == "site-nav.yml" and guide.doc is None
        assert readme.format == "plain" and readme.doc == "demo::docs.consumer.readme" and readme.overwrite is True
        assert plugin.nav == "docs/consumer/plugins/pev/nav.yml" and plugin.overwrite is False

    def test_both_nav_and_doc_rejected(self, tmp_path: Path):
        _write_toml(
            tmp_path,
            """
[[axiom_graph.site.targets]]
name = "bad"
output = "out"
nav = "site-nav.yml"
doc = "x::y"
""",
        )
        with pytest.raises(ConfigError, match="exactly one of 'nav' or 'doc'"):
            AxiomGraphConfig.load(tmp_path)

    def test_neither_nav_nor_doc_rejected(self, tmp_path: Path):
        _write_toml(
            tmp_path,
            """
[[axiom_graph.site.targets]]
name = "bad"
output = "out"
""",
        )
        with pytest.raises(ConfigError, match="exactly one of 'nav' or 'doc'"):
            AxiomGraphConfig.load(tmp_path)

    def test_unknown_format_rejected(self, tmp_path: Path):
        _write_toml(
            tmp_path,
            """
[[axiom_graph.site.targets]]
name = "bad"
output = "out"
format = "pdf"
doc = "x::y"
""",
        )
        with pytest.raises(ConfigError, match="unknown format"):
            AxiomGraphConfig.load(tmp_path)

    def test_sphinx_with_doc_rejected(self, tmp_path: Path):
        _write_toml(
            tmp_path,
            """
[[axiom_graph.site.targets]]
name = "bad"
output = "out"
format = "sphinx"
doc = "x::y"
""",
        )
        with pytest.raises(ConfigError, match="sphinx requires 'nav'"):
            AxiomGraphConfig.load(tmp_path)

    def test_duplicate_names_rejected(self, tmp_path: Path):
        _write_toml(
            tmp_path,
            """
[[axiom_graph.site.targets]]
name = "dup"
output = "a"
doc = "x::a"

[[axiom_graph.site.targets]]
name = "dup"
output = "b"
doc = "x::b"
""",
        )
        with pytest.raises(ConfigError, match="duplicate site target name"):
            AxiomGraphConfig.load(tmp_path)

    def test_absent_targets_yields_empty_list(self, tmp_path: Path):
        _write_toml(tmp_path, '[axiom_graph]\nproject_id = "demo"\n')
        cfg = AxiomGraphConfig.load(tmp_path)
        assert cfg.site.targets == []


# ---------------------------------------------------------------------------
# Task 2: implicit-target synthesis
# ---------------------------------------------------------------------------


class TestResolveTargets:
    """``resolve_targets`` synthesises an implicit guide or honors explicit ones."""

    def test_empty_targets_synthesise_guide_to_userdocs_guide(self, mini_project: Path):
        _project(mini_project)  # writes axiom-graph.toml with no targets
        targets = resolve_targets(mini_project)
        assert len(targets) == 1
        guide = targets[0]
        assert guide.name == "guide"
        # Critical edge case: implicit guide resolves to userdocs/guide, NOT "site".
        assert guide.output == "userdocs/guide"
        assert guide.format == "sphinx"
        assert guide.nav == "site-nav.yml"

    def test_explicit_targets_are_authoritative_no_implicit_guide(self, mini_project: Path):
        (mini_project / "axiom-graph.toml").write_text(
            """
[axiom_graph]
project_id = "test"

[[axiom_graph.site.targets]]
name = "readme"
output = "README.md"
doc = "test::docs.consumer.readme"
""",
            encoding="utf-8",
        )
        targets = resolve_targets(mini_project)
        assert [t.name for t in targets] == ["readme"]  # no implicit guide added


# ---------------------------------------------------------------------------
# Task 3: fmt switch on build_site (plain link lists + contentless folder)
# ---------------------------------------------------------------------------


class TestPlainBuildSite:
    """``build_site(fmt='plain')`` emits link lists and skips contentless stubs."""

    @workflow(
        purpose="Rendering a nav subtree with fmt='plain' emits Markdown link lists "
        "instead of {toctree}, and a contentless section folder writes no index stub — "
        "its children render inline as a nested heading + link list on the parent"
    )
    def test_plain_linklists_and_contentless_folder(self, mini_project: Path):
        口 = Step(step_num=1, name="Seed a folder with a landing + a contentless folder", purpose="two folder shapes")
        _project(mini_project)
        # folder WITH a landing (index.json convention)
        _seed_doc(
            mini_project, "guides/index", "Guides", [{"id": "g", "heading": "G", "content": "guides", "level": 2}]
        )
        _seed_doc(mini_project, "guides/intro", "Intro", [{"id": "i", "heading": "I", "content": "intro", "level": 2}])
        # contentless folder (no index.json, no landing) with one child
        _seed_doc(mini_project, "extras/tips", "Tips", [{"id": "t", "heading": "T", "content": "tips", "level": 2}])

        口 = Step(step_num=2, name="Nav lists both folders", purpose="guides has landing; extras is contentless")
        nav_file = _slim_nav(
            mini_project,
            [{"guides": {"show": ["intro"]}}, {"extras": {"show": ["tips"]}}],
        )

        口 = Step(step_num=3, name="Build plain", purpose="fmt='plain' -> GFM link lists")
        out = mini_project / "out"
        result = build_site(mini_project, nav_path=nav_file, output_dir=out, fmt="plain")
        assert not result.warnings, result.warnings

        口 = Step(step_num=4, name="Assert link lists, no toctree, no contentless stub", purpose="GFM shape")
        guides_landing = (out / "guides" / "index.md").read_text(encoding="utf-8")
        assert "{toctree}" not in guides_landing
        assert "- [Intro](intro.md)" in guides_landing
        # contentless folder: no stub file written
        assert not (out / "extras" / "index.md").exists()
        # child still rendered to its own file
        assert (out / "extras" / "tips.md").exists()
        # top-level index expands contentless folder inline with prefixed links
        top = (out / "index.md").read_text(encoding="utf-8")
        assert "{toctree}" not in top
        assert "- [Guides](guides/index.md)" in top
        assert "## Extras" in top  # inline nested heading
        assert "- [Tips](extras/tips.md)" in top  # child link, folder-prefixed

    def test_sphinx_mode_still_emits_toctree(self, mini_project: Path):
        """Default fmt='sphinx' is unchanged: contentless folders get a synthetic stub + toctree."""
        _project(mini_project)
        _seed_doc(mini_project, "extras/tips", "Tips", [{"id": "t", "heading": "T", "content": "tips", "level": 2}])
        nav_file = _slim_nav(mini_project, [{"extras": {"show": ["tips"]}}])
        out = mini_project / "out"
        build_site(mini_project, nav_path=nav_file, output_dir=out, fmt="sphinx")
        stub = (out / "extras" / "index.md").read_text(encoding="utf-8")
        assert "# Extras" in stub
        assert "{toctree}" in stub


# ---------------------------------------------------------------------------
# Task 4: render_doc_to_file
# ---------------------------------------------------------------------------


class TestRenderDocToFile:
    """Single-doc -> single-file renderer."""

    def test_renders_with_stamp_strips_docid_no_toctree(self, mini_project: Path):
        _project(mini_project)
        doc_id = _seed_doc(
            mini_project,
            "readme",
            "axiom-graph",
            [
                {
                    "id": "intro",
                    "heading": "Intro",
                    "level": 2,
                    "content": "See [internal](test::docs.foo) and [staleness](staleness.md).",
                }
            ],
        )
        out = mini_project / "README.md"
        result = render_doc_to_file(mini_project, doc_id, out, fmt="plain", overwrite=True)
        assert result.pages_rendered == 1
        text = out.read_text(encoding="utf-8")
        assert text.startswith("<!-- generated from")  # provenance stamp
        assert "{toctree}" not in text
        assert "test::docs.foo" not in text  # internal doc-id link stripped
        assert "internal" in text
        assert "[staleness](staleness.md)" in text  # relative link preserved

    def test_missing_doc_warns_and_skips_no_file(self, mini_project: Path):
        """A doc-id not in the index warns and skips — no crash, no file."""
        _project(mini_project)
        out = mini_project / "README.md"
        result = render_doc_to_file(mini_project, "test::docs.consumer.nope", out)
        assert result.pages_rendered == 0
        assert any("not found" in w.lower() for w in result.warnings)
        assert not out.exists()


# ---------------------------------------------------------------------------
# Task 5: path-safety + hybrid manifest
# ---------------------------------------------------------------------------


class TestPathSafetyAndManifest:
    """Overwrite guard + hybrid manifest conventions."""

    def test_overwrite_guard_unstamped_skip_then_allow(self, mini_project: Path):
        _project(mini_project)
        doc_id = _seed_doc(
            mini_project, "readme", "axiom-graph", [{"id": "i", "heading": "I", "content": "body", "level": 2}]
        )
        out = mini_project / "README.md"
        out.write_text("# Hand authored\n", encoding="utf-8")  # un-stamped

        # default: warn-and-skip, file unchanged
        r1 = render_doc_to_file(mini_project, doc_id, out)
        assert r1.pages_rendered == 0
        assert any("overwrite" in w.lower() for w in r1.warnings)
        assert out.read_text(encoding="utf-8") == "# Hand authored\n"

        # overwrite=True: replaced
        r2 = render_doc_to_file(mini_project, doc_id, out, overwrite=True)
        assert r2.pages_rendered == 1
        assert out.read_text(encoding="utf-8").startswith("<!-- generated from")

        # subsequent run sees the stamp -> regenerates cleanly without overwrite
        r3 = render_doc_to_file(mini_project, doc_id, out)
        assert r3.pages_rendered == 1

    def test_never_writes_outside_root(self, mini_project: Path):
        _project(mini_project)
        with pytest.raises(ValueError, match="escapes project root"):
            render_doc_to_file(mini_project, "test::docs.consumer.readme", "../escape.md")

    def test_hybrid_manifest_central_for_single_file(self, mini_project: Path):
        """Single-file targets land in the central .axiom_graph manifest; subtree targets co-located."""
        _project_with_targets(
            mini_project,
            [
                {"name": "readme", "output": "README.md", "doc": "test::docs.consumer.readme", "overwrite": True},
                {"name": "guide", "output": "guide", "format": "sphinx", "nav": "site-nav.yml"},
            ],
        )
        _seed_doc(mini_project, "readme", "axiom-graph", [{"id": "i", "heading": "I", "content": "b", "level": 2}])
        _seed_doc(mini_project, "intro", "Intro", [{"id": "x", "heading": "X", "content": "y", "level": 2}])
        _slim_nav(mini_project, ["intro"])

        render_targets(mini_project)

        central = json.loads((mini_project / ".axiom_graph" / "render-manifest.json").read_text(encoding="utf-8"))
        assert "README.md" in central
        assert central["README.md"]["doc_id"] == "test::docs.consumer.readme"
        assert central["README.md"]["target"] == "readme"
        assert central["README.md"]["fmt"] == "plain"
        # subtree target keeps its co-located manifest
        co = json.loads((mini_project / "guide" / ".render-manifest.json").read_text(encoding="utf-8"))
        assert "intro.md" in co
        # subtree target NOT in central manifest
        assert "guide/intro.md" not in central

    def test_corrupt_central_manifest_warns_then_resets(self, mini_project: Path, caplog):
        """A corrupt central manifest is logged (ADR-014) then reset, not silently dropped."""
        _project_with_targets(
            mini_project,
            [{"name": "readme", "output": "README.md", "doc": "test::docs.consumer.readme", "overwrite": True}],
        )
        _seed_doc(mini_project, "readme", "axiom-graph", [{"id": "i", "heading": "I", "content": "b", "level": 2}])

        # Pre-seed a corrupt central manifest.
        manifest_path = mini_project / ".axiom_graph" / "render-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("{ not valid json", encoding="utf-8")

        with caplog.at_level("WARNING", logger="axiom_graph.docjson.render_consumer"):
            render_targets(mini_project)

        # Warning emitted, naming the manifest path (ADR-014 logging contract).
        assert any("render-manifest.json" in rec.message and rec.levelname == "WARNING" for rec in caplog.records)
        # Recovery unchanged: corrupt file reset to {} then this run's entry written.
        central = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "README.md" in central
        assert central["README.md"]["doc_id"] == "test::docs.consumer.readme"


# ---------------------------------------------------------------------------
# Task 6: multi-target orchestrator + subset
# ---------------------------------------------------------------------------


def _project_with_targets(mini_project: Path, targets: list[dict]) -> Path:
    """Write axiom-graph.toml with project_id + the given site targets."""
    lines = ['[axiom_graph]\nproject_id = "test"\n']
    for t in targets:
        lines.append("\n[[axiom_graph.site.targets]]")
        for k, v in t.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {str(v).lower()}")
            else:
                lines.append(f'{k} = "{v}"')
    (mini_project / "axiom-graph.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mini_project


class TestRenderTargets:
    """The render_targets orchestrator renders all targets or a named subset."""

    @workflow(
        purpose="One render_targets run with three declared targets (sphinx guide, plain "
        "README doc, plain plugin subtree) lands all three outputs in their declared "
        "locations with the correct flavor"
    )
    def test_three_targets_all_render(self, mini_project: Path):
        口 = Step(step_num=1, name="Declare three targets", purpose="sphinx guide + plain readme + plain subtree")
        _project_with_targets(
            mini_project,
            [
                {"name": "guide", "output": "userdocs/guide", "format": "sphinx", "nav": "site-nav.yml"},
                {
                    "name": "readme",
                    "output": "README.md",
                    "format": "plain",
                    "doc": "test::docs.consumer.readme",
                    "overwrite": True,
                },
                {
                    "name": "plugin",
                    "output": "pev_nexus_agents/pev/docs",
                    "format": "plain",
                    "nav": "docs/consumer/plugins/pev/nav.yml",
                },
            ],
        )

        口 = Step(step_num=2, name="Seed docs for each target", purpose="guide nav doc, readme doc, plugin subtree doc")
        _seed_doc(mini_project, "intro", "Intro", [{"id": "x", "heading": "X", "content": "guide", "level": 2}])
        _slim_nav(mini_project, ["intro"])
        _seed_doc(mini_project, "readme", "axiom-graph", [{"id": "i", "heading": "I", "content": "rdme", "level": 2}])
        # plugin subtree (rel_path is relative to the plugin publish root)
        _seed_doc(
            mini_project,
            "readme",
            "pev",
            [{"id": "p", "heading": "P", "content": "plug", "level": 2}],
            root="docs/consumer/plugins/pev",
        )
        plugin_nav = mini_project / "docs" / "consumer" / "plugins" / "pev" / "nav.yml"
        plugin_nav.write_text(
            yaml.dump({"site_name": "pev", "root": "docs/consumer/plugins/pev", "show": ["readme"]}),
            encoding="utf-8",
        )

        口 = Step(step_num=3, name="Render all targets", purpose="no filter -> all three")
        results = render_targets(mini_project)
        by_name = {r.name: r for r in results}
        assert set(by_name) == {"guide", "readme", "plugin"}

        口 = Step(step_num=4, name="Assert each output lands with the right flavor", purpose="locations + flavor")
        # sphinx guide: toctree present
        guide_idx = (mini_project / "userdocs" / "guide" / "intro.md").read_text(encoding="utf-8")
        assert guide_idx  # rendered
        # plain README: stamp, no toctree
        readme = (mini_project / "README.md").read_text(encoding="utf-8")
        assert readme.startswith("<!-- generated from")
        assert "{toctree}" not in readme
        # plain plugin subtree
        assert (mini_project / "pev_nexus_agents" / "pev" / "docs" / "readme.md").exists()
        plug_idx = (mini_project / "pev_nexus_agents" / "pev" / "docs" / "index.md").read_text(encoding="utf-8")
        assert "{toctree}" not in plug_idx

    @workflow(
        purpose="render_targets(only=['readme']) renders only the named target; the other "
        "declared targets are skipped and their outputs untouched"
    )
    def test_subset_only_renders_named(self, mini_project: Path):
        口 = Step(step_num=1, name="Declare guide + readme", purpose="two targets")
        _project_with_targets(
            mini_project,
            [
                {"name": "guide", "output": "userdocs/guide", "format": "sphinx", "nav": "site-nav.yml"},
                {
                    "name": "readme",
                    "output": "README.md",
                    "format": "plain",
                    "doc": "test::docs.consumer.readme",
                    "overwrite": True,
                },
            ],
        )
        _seed_doc(mini_project, "intro", "Intro", [{"id": "x", "heading": "X", "content": "g", "level": 2}])
        _slim_nav(mini_project, ["intro"])
        _seed_doc(mini_project, "readme", "axiom-graph", [{"id": "i", "heading": "I", "content": "r", "level": 2}])

        口 = Step(step_num=2, name="Render only readme", purpose="only=['readme']")
        results = render_targets(mini_project, only=["readme"], run_sphinx_build=True)
        by_name = {r.name: r for r in results}

        口 = Step(step_num=3, name="Assert only README rendered, guide skipped", purpose="subset + no sphinx fire")
        assert by_name["readme"].pages_rendered == 1
        assert by_name["guide"].skipped is True
        assert (mini_project / "README.md").exists()
        # guide output never produced (sphinx-build wouldn't fire for plain either)
        assert not (mini_project / "userdocs" / "guide" / "intro.md").exists()


# ---------------------------------------------------------------------------
# Task 7: CLI + MCP wiring
# ---------------------------------------------------------------------------


class TestCLIAndMCPTargets:
    """The --target CLI flag and MCP targets param drive the subset render."""

    def test_cli_target_flag_renders_subset(self, mini_project: Path):
        from click.testing import CliRunner

        from axiom_graph.cli import main

        _project_with_targets(
            mini_project,
            [
                {"name": "guide", "output": "userdocs/guide", "format": "sphinx", "nav": "site-nav.yml"},
                {
                    "name": "readme",
                    "output": "README.md",
                    "format": "plain",
                    "doc": "test::docs.consumer.readme",
                    "overwrite": True,
                },
            ],
        )
        _seed_doc(mini_project, "intro", "Intro", [{"id": "x", "heading": "X", "content": "g", "level": 2}])
        _slim_nav(mini_project, ["intro"])
        _seed_doc(mini_project, "readme", "axiom-graph", [{"id": "i", "heading": "I", "content": "r", "level": 2}])

        runner = CliRunner()
        result = runner.invoke(main, ["render-site", str(mini_project), "--target", "readme"])
        assert result.exit_code == 0, result.output
        assert "[readme]" in result.output
        assert "[guide] skipped" in result.output
        assert (mini_project / "README.md").exists()
        assert not (mini_project / "userdocs" / "guide" / "intro.md").exists()

    def test_cli_nav_output_override_bypasses_targets(self, mini_project: Path):
        from click.testing import CliRunner

        from axiom_graph.cli import main

        # targets configured, but --nav/--output must bypass the target list
        _project_with_targets(
            mini_project,
            [{"name": "readme", "output": "README.md", "format": "plain", "doc": "test::docs.consumer.readme"}],
        )
        _seed_doc(mini_project, "getting-started", "GS", [{"id": "w", "heading": "W", "content": "hi", "level": 2}])
        nav_file = _slim_nav(mini_project, ["getting-started"])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["render-site", str(mini_project), "--nav", str(nav_file), "--output", str(mini_project / "adhoc")],
        )
        assert result.exit_code == 0, result.output
        assert "pages rendered : 1" in result.output
        assert (mini_project / "adhoc" / "getting-started.md").exists()
        assert not (mini_project / "README.md").exists()  # target list bypassed

    def test_mcp_targets_param_renders_subset(self, mini_project: Path):
        from axiom_graph.lifecycle.mcp_tools import axiom_graph_render_site as mcp_render_site

        _project_with_targets(
            mini_project,
            [
                {"name": "guide", "output": "userdocs/guide", "format": "sphinx", "nav": "site-nav.yml"},
                {
                    "name": "readme",
                    "output": "README.md",
                    "format": "plain",
                    "doc": "test::docs.consumer.readme",
                    "overwrite": True,
                },
            ],
        )
        _seed_doc(mini_project, "intro", "Intro", [{"id": "x", "heading": "X", "content": "g", "level": 2}])
        _slim_nav(mini_project, ["intro"])
        _seed_doc(mini_project, "readme", "axiom-graph", [{"id": "i", "heading": "I", "content": "r", "level": 2}])

        out = mcp_render_site(str(mini_project), targets=["readme"])
        assert "[readme]" in out
        assert "[guide] skipped" in out
        assert (mini_project / "README.md").exists()


# ---------------------------------------------------------------------------
# Headingless lead section (README port fidelity)
# ---------------------------------------------------------------------------


class TestHeadinglessLeadSection:
    """A section with an empty heading renders content-only (no heading line).

    Lets a ported README / landing doc place its lead content (badges,
    tagline, intro paragraph) directly under the doc title without a
    duplicate ``## Title`` heading. Regression for the doubled-title defect
    found when dogfooding ``render-site`` against the real README.
    """

    def test_empty_heading_section_renders_content_without_heading_line(self):
        md = render_doc_consumer(
            "axiom-graph",
            [
                {"id": "intro", "heading": "", "content": "badges\n\ntagline", "level": 2},
                {"id": "quick-start", "heading": "Quick Start", "content": "do it", "level": 2},
            ],
        )
        lines = md.splitlines()
        # Title is the sole H1; not duplicated as an H2; no empty heading line.
        assert lines.count("# axiom-graph") == 1
        assert "## axiom-graph" not in lines
        assert "## " not in lines
        assert not any(ln.strip() == "##" for ln in lines)
        # Lead content lands directly under the title; real headings preserved.
        assert "badges" in md and "tagline" in md
        assert "## Quick Start" in md
        assert md.index("badges") < md.index("## Quick Start")

    def test_nonempty_heading_still_renders(self):
        md = render_doc_consumer(
            "Doc",
            [{"id": "s", "heading": "Real Heading", "content": "body", "level": 2}],
        )
        assert "## Real Heading" in md
        assert "body" in md
