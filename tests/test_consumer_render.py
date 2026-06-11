"""Tests for consumer documentation rendering and site build pipeline."""

from __future__ import annotations

import json
import yaml
import pytest
from pathlib import Path

from axiom_graph.index import db
from axiom_graph.models import AxiomNode
from axiom_annotations import Step, workflow

from axiom_graph.docjson.render_consumer import (
    render_doc_consumer,
    load_site_nav,
    validate_site_nav,
    parse_show,
    build_site,
    _strip_docid_links,
)


class TestStripDocidLinks:
    """Inline links whose target is an internal doc-id (contains '::') are
    reduced to plain text; every other link is left untouched."""

    def test_docid_link_collapses_to_text(self):
        md = "See [Git rename](axiom_graph::docs.pev-requests.git-aware-rename-detection) for details."
        assert _strip_docid_links(md) == "See Git rename for details."

    def test_relative_md_link_untouched(self):
        md = "Read the [Staleness](staleness.md) page."
        assert _strip_docid_links(md) == md

    def test_relative_md_link_with_anchor_untouched(self):
        md = "See [propagation](staleness.md#propagation-by-edge-type)."
        assert _strip_docid_links(md) == md

    def test_external_url_untouched(self):
        md = "TOML spec at [toml.io](https://toml.io/)."
        assert _strip_docid_links(md) == md

    def test_intrapage_anchor_untouched(self):
        md = "Jump to [node types](#node-types)."
        assert _strip_docid_links(md) == md

    def test_multiple_links_mixed(self):
        md = "[A](cortex::docs.consumer.foo) and [B](viz.md) and [C](https://x.io)."
        assert _strip_docid_links(md) == "A and [B](viz.md) and [C](https://x.io)."


# ---------------------------------------------------------------------------
# Task 1: Consumer render function tests
# ---------------------------------------------------------------------------


class TestRenderDocConsumer:
    """Tests for render_doc_consumer — clean Markdown without agent annotations."""

    def test_basic_sections(self):
        """Sections render as headings with content, no ID comments."""
        sections = [
            {"id": "intro", "heading": "Introduction", "content": "Welcome to the project.", "level": 2},
            {"id": "setup", "heading": "Setup", "content": "Install via pip.", "level": 2},
        ]
        result = render_doc_consumer("My Doc", sections)
        assert "# My Doc" in result
        assert "## Introduction" in result
        assert "Welcome to the project." in result
        assert "## Setup" in result
        assert "Install via pip." in result
        # No agent annotations
        assert "<!-- id:" not in result
        assert "**Linked nodes:**" not in result

    def test_nested_sections(self):
        """Nested sections produce correct heading levels."""
        sections = [
            {
                "id": "database",
                "heading": "Database Layer",
                "content": "Overview of DB.",
                "level": 2,
                "sections": [
                    {"id": "tables", "heading": "Tables", "content": "Table definitions.", "level": 3},
                    {"id": "migrations", "heading": "Migrations", "content": "How migrations work.", "level": 3},
                ],
            },
        ]
        result = render_doc_consumer("Architecture", sections)
        assert "# Architecture" in result
        assert "## Database Layer" in result
        assert "### Tables" in result
        assert "### Migrations" in result

    def test_empty_content_section(self):
        """Section with no content renders heading followed by blank line, no doubled blanks."""
        sections = [
            {"id": "placeholder", "heading": "Placeholder", "content": "", "level": 2},
            {"id": "next", "heading": "Next Section", "content": "Has content.", "level": 2},
        ]
        result = render_doc_consumer("Doc", sections)
        assert "## Placeholder" in result
        assert "## Next Section" in result
        # Should not have triple+ blank lines (doubled blanks)
        assert "\n\n\n\n" not in result

    def test_content_with_html_comments_passthrough(self):
        """Markdown content that contains HTML comments is passed through verbatim."""
        sections = [
            {
                "id": "example",
                "heading": "Example",
                "content": "Some text\n<!-- This is a user comment -->\nMore text",
                "level": 2,
            },
        ]
        result = render_doc_consumer("Doc", sections)
        assert "<!-- This is a user comment -->" in result

    def test_level_clamping(self):
        """Levels below 2 are clamped to 2, above 6 are clamped to 6."""
        sections = [
            {"id": "low", "heading": "Low Level", "content": "Content.", "level": 1},
            {"id": "high", "heading": "High Level", "content": "Content.", "level": 7},
        ]
        result = render_doc_consumer("Doc", sections)
        # Level 1 clamped to 2
        assert "## Low Level" in result
        # Level 7 clamped to 6
        assert "###### High Level" in result

    def test_missing_level_defaults_to_2(self):
        """Sections without a level field default to level 2."""
        sections = [
            {"id": "no-level", "heading": "No Level", "content": "Content."},
        ]
        result = render_doc_consumer("Doc", sections)
        assert "## No Level" in result

    def test_content_none_treated_as_empty(self):
        """Section with content=None renders heading only."""
        sections = [
            {"id": "null-content", "heading": "Null Content", "content": None, "level": 2},
        ]
        result = render_doc_consumer("Doc", sections)
        assert "## Null Content" in result
        assert "\n\n\n\n" not in result


# ---------------------------------------------------------------------------
# Task 2: Nav loading and validation tests
# ---------------------------------------------------------------------------


class TestSlimNav:
    """Tests for slim v2 nav loading, parsing, and validation."""

    def test_load_valid_slim_nav(self, tmp_path: Path):
        """A well-formed slim nav file loads with site_name, root, and show."""
        nav_file = tmp_path / "site-nav.yml"
        nav_file.write_text(
            yaml.dump(
                {
                    "site_name": "My Docs",
                    "root": "docs/consumer",
                    "show": ["getting-started", {"features": {"show": ["staleness"]}}],
                }
            )
        )
        nav_data = load_site_nav(nav_file)
        assert nav_data["site_name"] == "My Docs"
        assert nav_data["root"] == "docs/consumer"
        assert nav_data["show"][0] == "getting-started"

    def test_load_missing_file_raises(self, tmp_path: Path):
        """Loading a non-existent nav file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_site_nav(tmp_path / "missing.yml")

    def test_parse_show_derives_paths_and_ids(self):
        """parse_show derives output paths and doc_ids from path stems.

        A leaf under a folder mirrors its source path 1:1 and carries the
        nested doc-id (root prefix consumed by the ``::docs.`` head)."""
        entries = parse_show(
            ["getting-started", {"features": {"show": ["staleness"]}}],
            project_id="axiom_graph",
            prefix="consumer",
        )
        # top-level leaf
        leaf = entries[0]
        assert leaf.output_path == "getting-started.md"
        assert leaf.doc_id == "axiom_graph::docs.consumer.getting-started"
        # nested folder + child
        folder = entries[1]
        assert folder.stem == "features"
        child = folder.children[0]
        assert child.output_path == "features/staleness.md"
        assert child.doc_id == "axiom_graph::docs.consumer.features.staleness"

    def test_validate_missing_required_keys(self):
        """Slim nav without site_name / root / show produces errors."""
        errors = validate_site_nav({"show": ["foo"]})
        assert any("site_name" in e for e in errors)
        assert any("root" in e for e in errors)

        errors = validate_site_nav({"site_name": "T", "root": "docs/consumer"})
        assert any("show" in e for e in errors)

    def test_validate_unresolvable_stem(self, mini_project: Path):
        """A stem that resolves to no indexed doc-id is rejected."""
        db_path = mini_project / ".axiom_graph" / "graph.db"
        nav_data = {
            "site_name": "T",
            "root": "docs/consumer",
            "show": ["nonexistent"],
            "_project_id": "test",
        }
        errors = validate_site_nav(nav_data, db_path=db_path, source_root=mini_project)
        assert any("nonexistent" in e for e in errors)

    def test_validate_ambiguous_landing(self, mini_project: Path, tmp_path: Path):
        """A folder with both index.json and landing: is rejected."""
        source_root = mini_project / "docs" / "consumer"
        feat = source_root / "features"
        feat.mkdir(parents=True)
        (feat / "index.json").write_text("{}", encoding="utf-8")
        nav_data = {
            "site_name": "T",
            "root": "docs/consumer",
            "show": [{"features": {"landing": "overview", "show": []}}],
            "_project_id": "test",
        }
        # no db check (db_path omitted) so only the landing conflict surfaces
        errors = validate_site_nav(nav_data, source_root=source_root)
        assert any("ambiguous" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# Task 3: Core site build pipeline + mkdocs.yml generation
# ---------------------------------------------------------------------------


def _setup_doc_in_db(db_path: Path, doc_id: str, title: str, sections: list[dict]) -> None:
    """Insert a document node and its sections into the test DB."""
    node = AxiomNode(
        id=doc_id,
        node_type="document",
        title=title,
        location="docs/test.json",
        source="doc_scanner",
        code_hash="abc123",
        level_0=doc_id,
        level_1=title,
    )
    db.upsert_node(db_path, node, discovery_only=False)
    with db._connect(db_path) as conn:
        # Insert into docs table first (foreign key target for doc_sections)
        doc_row = {
            "id": doc_id,
            "title": title,
            "tags": json.dumps([]),
            "file_path": "docs/test.json",
            "desc_hash": "hash",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        db.upsert_doc(conn, doc_row)
        for i, sec in enumerate(sections):
            sec_row = {
                "id": f"{doc_id}::{sec['id']}",
                "doc_id": doc_id,
                "heading": sec["heading"],
                "level": sec.get("level", 2),
                "tags": json.dumps(sec.get("tags", [])),
                "content": sec.get("content", ""),
                "desc_hash": "hash",
                "parent_id": None,
                "depth": 0,
                "position": i,
                "updated_at": "2026-01-01T00:00:00Z",
            }
            db.upsert_doc_section(conn, sec_row)


def _project(mini_project: Path, project_id: str = "test") -> Path:
    """Write an axiom-graph.toml fixing project_id so doc-id derivation is stable."""
    (mini_project / "axiom-graph.toml").write_text(f'[axiom_graph]\nproject_id = "{project_id}"\n', encoding="utf-8")
    return mini_project


def _seed_doc(
    mini_project: Path,
    rel_path: str,
    title: str,
    sections: list[dict],
    *,
    project_id: str = "test",
    root: str = "docs/consumer",
    write_source: bool = True,
) -> str:
    """Seed a consumer doc into the DB (and optionally on disk) at *rel_path*.

    *rel_path* is the path relative to the publish root, sans ``.json`` (e.g.
    ``features/staleness``).  Returns the derived doc_id.
    """
    db_path = mini_project / ".axiom_graph" / "graph.db"
    prefix = ".".join(p for p in root.split("/") if p and p != "docs")
    doc_id = f"{project_id}::docs.{prefix}.{rel_path.replace('/', '.')}"
    _setup_doc_in_db(db_path, doc_id, title, sections)
    if write_source:
        src = mini_project / root / f"{rel_path}.json"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(json.dumps({"title": title, "sections": sections}), encoding="utf-8")
    return doc_id


def _slim_nav(mini_project: Path, show: list, root: str = "docs/consumer") -> Path:
    nav_file = mini_project / "site-nav.yml"
    nav_file.write_text(
        yaml.dump({"site_name": "Test Site", "root": root, "show": show}),
        encoding="utf-8",
    )
    return nav_file


class TestBuildSiteNested:
    """build_site emits nested output mirroring the source folder tree."""

    @workflow(
        purpose="A consumer doc relocated into a features/ subfolder renders at the "
        "mirrored nested output path guide/features/staleness.md with no per-page config"
    )
    def test_nested_output_mirrors_source(self, mini_project: Path):
        口 = Step(
            step_num=1, name="Seed nested source doc", purpose="Place features/staleness.json + its node in the index"
        )
        _project(mini_project)
        _seed_doc(
            mini_project,
            "features/staleness",
            "Staleness Engine",
            [{"id": "overview", "heading": "Overview", "content": "How staleness works.", "level": 2}],
        )

        口 = Step(
            step_num=2, name="Slim nav lists the nested doc", purpose="features section folder with staleness child"
        )
        nav_file = _slim_nav(mini_project, [{"features": {"show": ["staleness"]}}])

        口 = Step(step_num=3, name="Build the site", purpose="Walk the slim nav and render nested pages")
        out = mini_project / "guide"
        result = build_site(mini_project, nav_path=nav_file, output_dir=out)

        口 = Step(
            step_num=4,
            name="Assert mirrored output path",
            purpose="The doc lands at features/staleness.md, output mirrors source 1:1",
        )
        page = out / "features" / "staleness.md"
        assert page.exists(), result.warnings
        md = page.read_text(encoding="utf-8")
        assert "# Staleness Engine" in md
        assert "## Overview" in md

    def test_unpublished_doc_not_rendered(self, mini_project: Path):
        """A doc on disk but absent from show: is not published; reorder reorders toctree."""
        _project(mini_project)
        _seed_doc(mini_project, "alpha", "Alpha", [{"id": "a", "heading": "A", "content": "x", "level": 2}])
        _seed_doc(mini_project, "beta", "Beta", [{"id": "b", "heading": "B", "content": "y", "level": 2}])
        # gamma is on disk + indexed but NOT in show:
        _seed_doc(mini_project, "gamma", "Gamma", [{"id": "g", "heading": "G", "content": "z", "level": 2}])

        nav_file = _slim_nav(mini_project, ["beta", "alpha"])  # reversed order
        out = mini_project / "guide"
        result = build_site(mini_project, nav_path=nav_file, output_dir=out)

        assert (out / "beta.md").exists()
        assert (out / "alpha.md").exists()
        assert not (out / "gamma.md").exists()
        assert result.pages_rendered == 2
        # toctree order follows show: order (beta before alpha)
        index = (out / "index.md").read_text(encoding="utf-8")
        assert index.index("beta") < index.index("alpha")

    def test_manifest_uses_nested_keys(self, mini_project: Path):
        """The render manifest is keyed by nested output path."""
        _project(mini_project)
        _seed_doc(
            mini_project,
            "features/viz",
            "Viz Dashboard",
            [{"id": "intro", "heading": "Intro", "content": "Welcome.", "level": 2}],
        )
        nav_file = _slim_nav(mini_project, [{"features": {"show": ["viz"]}}])
        out = mini_project / "guide"
        build_site(mini_project, nav_path=nav_file, output_dir=out)

        manifest = json.loads((out / ".render-manifest.json").read_text(encoding="utf-8"))
        assert manifest["features/viz.md"]["doc_id"] == "test::docs.consumer.features.viz"

    def test_warns_on_missing_doc(self, mini_project: Path):
        """A nav stem with no source on disk fails validation (unresolvable)."""
        _project(mini_project)
        nav_file = _slim_nav(mini_project, ["nonexistent"])
        out = mini_project / "guide"
        result = build_site(mini_project, nav_path=nav_file, output_dir=out)
        assert result.pages_rendered == 0
        assert any("nonexistent" in w.lower() for w in result.warnings)


class TestLandingPrecedence:
    """Section folders get landing pages via the three-branch precedence."""

    @workflow(
        purpose="Three folders (index.json / landing: / neither) render the index doc, "
        "the named doc, and a synthetic # Folder page respectively, each with a child toctree"
    )
    def test_three_landing_branches(self, mini_project: Path):
        _project(mini_project)
        # (a) index.json convention
        _seed_doc(
            mini_project,
            "alpha/index",
            "Alpha Landing",
            [{"id": "a", "heading": "A", "content": "alpha body", "level": 2}],
        )
        _seed_doc(mini_project, "alpha/one", "One", [{"id": "o", "heading": "O", "content": "1", "level": 2}])
        # (b) landing: named doc
        _seed_doc(
            mini_project,
            "beta/overview",
            "Beta Overview",
            [{"id": "b", "heading": "B", "content": "beta body", "level": 2}],
        )
        _seed_doc(mini_project, "beta/two", "Two", [{"id": "t", "heading": "T", "content": "2", "level": 2}])
        # (c) synthetic — only children, no index/landing
        _seed_doc(mini_project, "features/three", "Three", [{"id": "h", "heading": "H", "content": "3", "level": 2}])

        nav_file = _slim_nav(
            mini_project,
            [
                {"alpha": {"show": ["one"]}},
                {"beta": {"landing": "overview", "show": ["two"]}},
                {"features": {"show": ["three"]}},
            ],
        )
        out = mini_project / "guide"
        result = build_site(mini_project, nav_path=nav_file, output_dir=out)
        assert not result.warnings, result.warnings

        # (a) index.json -> Alpha Landing body
        alpha = (out / "alpha" / "index.md").read_text(encoding="utf-8")
        assert "# Alpha Landing" in alpha
        assert "alpha body" in alpha
        assert "{toctree}" in alpha

        # (b) landing: overview -> Beta Overview body
        beta = (out / "beta" / "index.md").read_text(encoding="utf-8")
        assert "# Beta Overview" in beta
        assert "beta body" in beta

        # (c) synthetic -> # Features
        feats = (out / "features" / "index.md").read_text(encoding="utf-8")
        assert "# Features" in feats
        assert "{toctree}" in feats

    def test_root_index_json_is_the_guide_landing(self, mini_project: Path):
        """A root-level index.json is the guide landing itself: its authored body
        is rendered into guide/index.md with the nav toctree appended, instead of
        being clobbered by a generated toctree -- and the toctree never
        self-references index."""
        _project(mini_project)
        _seed_doc(
            mini_project,
            "index",
            "What Is Axiom Graph",
            [{"id": "thesis", "heading": "Thesis", "content": "the thesis body", "level": 2}],
        )
        _seed_doc(
            mini_project,
            "concepts/the-mesh",
            "The Mesh",
            [{"id": "m", "heading": "M", "content": "mesh body", "level": 2}],
        )
        _seed_doc(mini_project, "viz", "Viz", [{"id": "v", "heading": "V", "content": "viz body", "level": 2}])

        nav_file = _slim_nav(
            mini_project,
            ["index", {"concepts": {"show": ["the-mesh"]}}, "viz"],
        )
        out = mini_project / "guide"
        result = build_site(mini_project, nav_path=nav_file, output_dir=out)
        assert not result.warnings, result.warnings

        index_md = (out / "index.md").read_text(encoding="utf-8")
        # authored thesis is the landing (not clobbered)
        assert "# What Is Axiom Graph" in index_md
        assert "the thesis body" in index_md
        # nav toctree appended, listing the OTHER top-level entries...
        assert "{toctree}" in index_md
        tt = index_md.split("{toctree}", 1)[1]
        assert "concepts/index" in tt
        assert "viz" in tt
        # ...but never a self-reference to index
        assert not any(line.strip() == "index" for line in tt.splitlines())

    def test_child_toctree_lists_direct_children_only(self, mini_project: Path):
        """A folder landing's toctree lists only direct children, no grandchildren,
        no features/ prefix."""
        _project(mini_project)
        _seed_doc(
            mini_project, "features/staleness", "Staleness", [{"id": "s", "heading": "S", "content": "x", "level": 2}]
        )
        _seed_doc(mini_project, "features/viz", "Viz", [{"id": "v", "heading": "V", "content": "y", "level": 2}])
        # a grandchild under a nested sub-folder
        _seed_doc(mini_project, "features/deep/leaf", "Leaf", [{"id": "l", "heading": "L", "content": "z", "level": 2}])

        nav_file = _slim_nav(
            mini_project,
            [{"features": {"show": ["staleness", "viz", {"deep": {"show": ["leaf"]}}]}}],
        )
        out = mini_project / "guide"
        build_site(mini_project, nav_path=nav_file, output_dir=out)

        landing = (out / "features" / "index.md").read_text(encoding="utf-8")
        # extract the toctree block
        tt = landing.split("{toctree}", 1)[1]
        assert "staleness" in tt
        assert "viz" in tt
        assert "deep/index" in tt  # sub-folder points at its own landing
        # direct-child stems are bare (relative to landing), no features/ prefix
        assert "features/staleness" not in tt
        # grandchild leaf is NOT listed directly
        assert "deep/leaf" not in tt


# ---------------------------------------------------------------------------
# Link + anchor verification under nested output (US-5)
# ---------------------------------------------------------------------------


class TestLinksUnderNestedOutput:
    """Relative links + anchors survive; doc-id links are stripped."""

    def test_relative_anchor_survive_docid_stripped(self, mini_project: Path):
        _project(mini_project)
        _seed_doc(
            mini_project,
            "features/staleness",
            "Staleness",
            [
                {
                    "id": "intro",
                    "heading": "Intro",
                    "level": 2,
                    "content": "See [viz](viz.md), [jump](#intro), and [req](axiom_graph::docs.foo).",
                }
            ],
        )
        nav_file = _slim_nav(mini_project, [{"features": {"show": ["staleness"]}}])
        out = mini_project / "guide"
        build_site(mini_project, nav_path=nav_file, output_dir=out)

        md = (out / "features" / "staleness.md").read_text(encoding="utf-8")
        assert "[viz](viz.md)" in md  # relative .md link preserved
        assert "[jump](#intro)" in md  # intra-page anchor preserved
        assert "axiom_graph::docs.foo" not in md  # doc-id link stripped
        assert "[req](" not in md

    @workflow(
        purpose="Cross-folder relative .md links in rendered guide pages resolve to real "
        "files on disk: a root page links down into features/, a feature page links up "
        "with ../ — both targets must exist under the nested output layout"
    )
    def test_cross_folder_links_resolve_to_real_files(self, mini_project: Path):
        import re

        口 = Step(
            step_num=1,
            name="Seed docs that link across folder boundaries",
            purpose="Root config links down to features/viz; feature staleness links up to config",
        )
        _project(mini_project)
        # root page -> feature page (must carry the features/ prefix)
        _seed_doc(
            mini_project,
            "configuration",
            "Configuration",
            [{"id": "i", "heading": "I", "level": 2, "content": "See the [dashboard](features/viz.md) for visuals."}],
        )
        # feature page -> root page (must carry the ../ prefix)
        _seed_doc(
            mini_project,
            "features/staleness",
            "Staleness",
            [{"id": "i", "heading": "I", "level": 2, "content": "Configure this in [settings](../configuration.md)."}],
        )
        _seed_doc(
            mini_project,
            "features/viz",
            "Viz",
            [{"id": "i", "heading": "I", "level": 2, "content": "Charts."}],
        )

        口 = Step(
            step_num=2,
            name="Publish all three across the nested layout",
            purpose="configuration flat; staleness + viz under features/",
        )
        nav_file = _slim_nav(
            mini_project,
            ["configuration", {"features": {"show": ["staleness", "viz"]}}],
        )
        out = mini_project / "guide"
        build_site(mini_project, nav_path=nav_file, output_dir=out)

        口 = Step(
            step_num=3,
            name="Resolve every relative .md link against its page dir",
            purpose="A cross-folder link that doesn't resolve to a real file is a broken link",
        )
        link_re = re.compile(r"\]\(([A-Za-z0-9_./-]+\.md)(?:#[A-Za-z0-9_-]+)?\)")
        checked = 0
        for page in out.rglob("*.md"):
            for m in link_re.finditer(page.read_text(encoding="utf-8")):
                target = (page.parent / m.group(1)).resolve()
                assert target.exists(), f"broken link in {page.name}: {m.group(1)}"
                checked += 1

        口 = Step(
            step_num=4,
            name="Assert the cross-folder links were actually exercised",
            purpose="Guard against a vacuous pass if no cross-folder links rendered",
        )
        assert checked >= 2
        cfg_md = (out / "configuration.md").read_text(encoding="utf-8")
        stale_md = (out / "features" / "staleness.md").read_text(encoding="utf-8")
        assert "[dashboard](features/viz.md)" in cfg_md  # root -> features/ preserved
        assert "[settings](../configuration.md)" in stale_md  # feature -> ../ preserved


# ---------------------------------------------------------------------------
# CLI + MCP pass-through (result shape unchanged)
# ---------------------------------------------------------------------------


class TestCLI:
    """The render-site CLI command produces nested output files."""

    def test_render_site_cli(self, mini_project: Path):
        from click.testing import CliRunner
        from axiom_graph.cli import main

        _project(mini_project)
        _seed_doc(
            mini_project,
            "getting-started",
            "Getting Started",
            [{"id": "welcome", "heading": "Welcome", "content": "Hello world.", "level": 2}],
        )
        nav_file = _slim_nav(mini_project, ["getting-started"])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["render-site", str(mini_project), "--nav", str(nav_file), "--output", str(mini_project / "guide")],
        )
        assert result.exit_code == 0, result.output
        assert "pages rendered : 1" in result.output
        assert (mini_project / "guide" / "getting-started.md").exists()


class TestMCPTool:
    """The axiom_graph_render_site MCP handler calls the same pipeline."""

    def test_mcp_render_site_custom_nav_and_output(self, mini_project: Path):
        from axiom_graph.lifecycle.mcp_tools import axiom_graph_render_site as mcp_render_site

        _project(mini_project)
        _seed_doc(
            mini_project,
            "core-concepts",
            "Core Concepts",
            [{"id": "intro", "heading": "Intro", "content": "Custom paths.", "level": 2}],
        )

        custom_nav = mini_project / "custom" / "my-nav.yml"
        custom_nav.parent.mkdir(parents=True, exist_ok=True)
        custom_nav.write_text(
            yaml.dump({"site_name": "Custom", "root": "docs/consumer", "show": ["core-concepts"]}),
            encoding="utf-8",
        )
        custom_output = mini_project / "custom-output"

        result = mcp_render_site(
            str(mini_project),
            nav_path=str(custom_nav),
            output_dir=str(custom_output),
        )
        assert "1 page(s)" in result
        assert str(custom_output) in result
        assert (custom_output / "core-concepts.md").exists()
        assert not (mini_project / "userdocs").exists()
