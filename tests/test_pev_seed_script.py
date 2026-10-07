"""The PEV seed script copies the plugin's cloneable templates into a project, once, and never overwrites them.

``scripts/pev-seed.sh`` seeds every plugin template that carries
``meta.template_version`` into ``<project>/.pev/templates/`` (mirroring
``cycle/``), replacing the version with a ``meta.seeded_from`` stamp. A file
already present is left alone, so re-runs are no-ops and local edits survive.

Before seeding it makes sure ``.pev`` is a configured docs root, so the
seeded templates get indexed, and that PEV's run records and closed requests
(``completed``, ``superseded``, ``archived``) are frozen: a missing
``axiom-graph.toml`` is created with the project id the index already stores;
a toml with no ``[axiom_graph.scan]`` or ``[axiom_graph.staleness]`` table
(what ``axiom-graph init`` writes) gets the table appended; a scan table whose
``docs_dirs`` lacks ``.pev`` gets the exact edit printed and a non-zero exit,
unchanged; a staleness table missing PEV's frozen tags gets a printed note.

It also seeds the ``doc-topology`` and ``test-policy`` SOPs into ``.pev/``,
then builds the index and accepts the raw-edit flags of PEV's own files that
are still exactly as seeded, so the first build reports no raw DocJSON edits.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from axiom_annotations import workflow
from axiom_graph.config import AxiomGraphConfig
from pev_hook_helpers import BASH, bash_has_jq

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev"
SCRIPT = PLUGIN_DIR / "scripts" / "pev-seed.sh"
TEMPLATES_DIR = PLUGIN_DIR / "templates"

pytestmark = [
    pytest.mark.skipif(not SCRIPT.is_file(), reason="PEV plugin source not present"),
    pytest.mark.skipif(BASH is None, reason="bash not available"),
    pytest.mark.skipif(not bash_has_jq(), reason="jq not available to the script"),
]


FROZEN_TAGS = [
    "pev-cycle",
    "pev-instance",
    "pev-efficiency",
    "pev-audit-dev-docs",
    "pev-audit-consumer-docs",
    "pev-audit-annotations",
    "completed",
    "superseded",
    "archived",
]
PEV_TOML = (
    '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]\ndocs_dirs = ["docs", ".pev"]\n\n'
    f"[axiom_graph.staleness]\nfrozen_tags = {json.dumps(FROZEN_TAGS)}\n"
)


def _seed(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    return subprocess.run(
        [BASH, SCRIPT.as_posix(), "--project-root", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=project,
        timeout=600,
    )


def _plugin_templates() -> dict[str, dict]:
    found = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.docjson")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "template_version" in data.get("meta", {}):
            found[path.relative_to(TEMPLATES_DIR).as_posix()] = data
    return found


def _seeded(project: Path) -> dict[str, dict]:
    root = project / ".pev" / "templates"
    return {
        p.relative_to(root).as_posix(): json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(root.rglob("*.docjson"))
    }


@workflow(
    purpose=(
        "Seeding an empty project copies every cloneable template under .pev/templates/ with cycle/ mirrored and a "
        "meta.seeded_from stamp, a second run changes nothing, and a locally edited seeded file survives a re-run"
    )
)
def test_seed_copies_missing_templates_once_and_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "axiom-graph.toml").write_text(PEV_TOML, encoding="utf-8")
    first = _seed(tmp_path, "--no-build")
    assert first.returncode == 0, first.stderr

    plugin = _plugin_templates()
    seeded = _seeded(tmp_path)
    assert set(seeded) == set(plugin)
    assert "cycle/manifest.docjson" in seeded
    assert "test-policy.docjson" not in seeded  # SOP files carry no template_version
    for rel, source in plugin.items():
        copy = seeded[rel]
        assert copy["meta"] == {
            "seeded_from": {"source": f"pev/templates/{rel}", "version": source["meta"]["template_version"]}
        }
        assert {k: v for k, v in copy.items() if k != "meta"} == {k: v for k, v in source.items() if k != "meta"}
    assert "axiom-graph build" in first.stdout
    assert not (tmp_path / ".axiom_graph").exists()  # --no-build with no index builds nothing
    for sop in ("doc-topology", "test-policy"):
        assert (tmp_path / ".pev" / f"{sop}.docjson").read_bytes() == (TEMPLATES_DIR / f"{sop}.docjson").read_bytes()
    assert not (tmp_path / ".pev" / "review-criteria.docjson").exists()  # opt-in: its checks are examples

    snapshot = {p: p.read_bytes() for p in (tmp_path / ".pev").rglob("*.docjson")}
    second = _seed(tmp_path, "--no-build")
    assert second.returncode == 0, second.stderr
    assert "0 seeded" in second.stdout
    assert {p: p.read_bytes() for p in snapshot} == snapshot

    edited = tmp_path / ".pev" / "templates" / "cycle" / "decisions.docjson"
    edited.write_text('{"title": "Our decisions", "sections": []}\n', encoding="utf-8")
    (tmp_path / ".pev" / "templates" / "request.docjson").unlink()
    third = _seed(tmp_path, "--no-build")
    assert third.returncode == 0, third.stderr
    assert edited.read_text(encoding="utf-8") == '{"title": "Our decisions", "sections": []}\n'
    assert (tmp_path / ".pev" / "templates" / "request.docjson").is_file()
    assert "1 seeded" in third.stdout
    assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == PEV_TOML


def _axiom_graph_wrapper(tmp_path: Path) -> str:
    """Write a bash wrapper that runs this interpreter's axiom-graph CLI, for ``--axiom-graph``."""
    wrapper = tmp_path / "ag.sh"
    python = Path(sys.executable).as_posix()
    wrapper.write_text(f'#!/bin/bash\nexec "{python}" -m axiom_graph.cli "$@"\n', encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)
    return wrapper.as_posix()


@workflow(
    purpose=(
        "In a project with an index but no axiom-graph.toml, seeding creates the toml with the project id the index "
        "stores (not the folder name) and .pev as a docs root under [axiom_graph.scan], then seeds the templates"
    )
)
def test_missing_toml_is_created_with_the_stored_project_id(tmp_path: Path) -> None:
    project = tmp_path / "folder-name"
    project.mkdir()
    (project / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    built = subprocess.run(
        [sys.executable, "-m", "axiom_graph.cli", "build", str(project), "--id", "stored_id"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert built.returncode == 0, built.stderr
    assert not (project / "axiom-graph.toml").exists()

    result = _seed(project, "--axiom-graph", _axiom_graph_wrapper(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr

    config = AxiomGraphConfig.load(project)
    assert config.project_id == "stored_id"
    assert config.scan.docs_dirs == ["docs", ".pev"]
    assert config.staleness.frozen_tags == FROZEN_TAGS
    assert "stored_id" in result.stdout
    assert (project / ".pev" / "templates" / "cycle" / "manifest.docjson").is_file()


def _flagged_raw_edits(project: Path) -> list[str]:
    listed = subprocess.run(
        [sys.executable, "-m", "axiom_graph.cli", "stamps", "accept", str(project), "--list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert listed.returncode == 0, listed.stderr
    return [line.strip() for line in listed.stdout.splitlines() if line.startswith("  ") and "::" in line]


@workflow(
    purpose=(
        "Seeding builds the index and accepts PEV's own unedited files, so no seeded section is left flagged as a raw "
        "DocJSON edit, while a seeded file the user edited by hand keeps its raw-edit flags"
    )
)
def test_seed_builds_and_accepts_only_its_own_unedited_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (project / "axiom-graph.toml").write_text(PEV_TOML, encoding="utf-8")
    # Setup indexes the project before seeding; a file that appears after that is a raw edit to the build.
    built = subprocess.run(
        [sys.executable, "-m", "axiom_graph.cli", "build", str(project)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert built.returncode == 0, built.stderr
    skipped = _seed(project, "--no-build")
    assert skipped.returncode == 0, skipped.stderr
    assert "stamps accept" in skipped.stdout
    policy = project / ".pev" / "test-policy.docjson"
    doc = json.loads(policy.read_text(encoding="utf-8"))
    doc["sections"][0]["content"] += "\n\nOur own rule."
    policy.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    result = _seed(project, "--axiom-graph", _axiom_graph_wrapper(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "accepted" in result.stdout

    flagged = _flagged_raw_edits(project)
    assert flagged, "the hand-edited SOP must stay flagged"
    assert all(sid.startswith("demo::.pev/test-policy::") for sid in flagged), flagged


def test_an_existing_sop_under_any_extension_is_kept(tmp_path: Path) -> None:
    (tmp_path / "axiom-graph.toml").write_text(PEV_TOML, encoding="utf-8")
    (tmp_path / ".pev").mkdir()
    (tmp_path / ".pev" / "test-policy.json").write_text('{"title": "Ours", "sections": []}\n', encoding="utf-8")
    result = _seed(tmp_path, "--no-build", "--review-criteria")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".pev" / "test-policy.docjson").exists()
    assert (tmp_path / ".pev" / "test-policy.json").read_text(encoding="utf-8") == '{"title": "Ours", "sections": []}\n'
    assert (tmp_path / ".pev" / "review-criteria.docjson").is_file()


@pytest.mark.parametrize("trailing_newline", [True, False], ids=["init-shape", "no-trailing-newline"])
def test_toml_without_scan_or_staleness_tables_gets_them_appended(tmp_path: Path, trailing_newline: bool) -> None:
    original = '[axiom_graph]\nproject_id = "demo"' + ("\n" if trailing_newline else "")
    path = tmp_path / "axiom-graph.toml"
    path.write_bytes(original.encode("utf-8"))
    result = _seed(tmp_path, "--no-build")
    assert result.returncode == 0, result.stderr

    assert path.read_text(encoding="utf-8").startswith(original)
    config = AxiomGraphConfig.load(tmp_path)
    assert config.project_id == "demo"
    assert config.scan.docs_dirs == ["docs", ".pev"]
    assert config.staleness.frozen_tags == FROZEN_TAGS
    assert (tmp_path / ".pev" / "templates" / "cycle" / "manifest.docjson").is_file()


@pytest.mark.parametrize(
    ("staleness", "printed"),
    [
        ('[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n', "frozen_tags = ["),
        ('[axiom_graph.staleness]\nfrozen_tags = ["adr", "pev-cycle"]\n', '"pev-efficiency"'),
    ],
    ids=["no-frozen-tags", "some-frozen-tags"],
)
def test_staleness_table_missing_pev_tags_gets_a_note_and_seeding_goes_on(
    tmp_path: Path, staleness: str, printed: str
) -> None:
    toml = '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]\ndocs_dirs = ["docs", ".pev"]\n\n' + staleness
    path = tmp_path / "axiom-graph.toml"
    path.write_bytes(toml.encode("utf-8"))
    result = _seed(tmp_path, "--no-build")
    assert result.returncode == 0, result.stderr
    assert printed in result.stderr
    assert path.read_bytes() == toml.encode("utf-8")
    assert (tmp_path / ".pev" / "templates" / "request.docjson").is_file()


@pytest.mark.parametrize("extra", [[], ["--no-build"]], ids=["build", "no-build"])
def test_missing_toml_without_an_index_asks_for_a_build_first(tmp_path: Path, extra: list[str]) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    result = _seed(project, "--axiom-graph", _axiom_graph_wrapper(tmp_path), *extra)
    assert result.returncode != 0
    assert "build" in result.stderr
    assert not (project / "axiom-graph.toml").exists()
    assert not (project / ".pev").exists()


@pytest.mark.parametrize(
    ("toml", "edit"),
    [
        (
            '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]\ndocs_dirs = ["docs"]\n',
            'docs_dirs = ["docs", ".pev"]',
        ),
        (
            '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]\nexclude_dirs = ["build"]\n',
            'docs_dirs = ["docs", ".pev"]',
        ),
        (
            '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]\ndocs_dirs = [\n  "docs",\n  "guides",\n]\n',
            '".pev"',
        ),
        (
            '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]  # what to index\ndocs_dirs = ["docs"]\n',
            'change: docs_dirs = ["docs", ".pev"]',
        ),
        (
            '[axiom_graph]\nproject_id = "demo"\nscan = { docs_dirs = ["docs"] }\n',
            'inline or dotted keys:\n  scan = { docs_dirs = ["docs"] }\nThis script does not edit those. '
            'Make sure docs_dirs there includes ".pev"',
        ),
        (
            '[axiom_graph]\nproject_id = "demo"\nscan.docs_dirs = ["docs"]\n',
            'Make sure docs_dirs there includes ".pev"',
        ),
    ],
    ids=[
        "docs-dirs-without-pev",
        "scan-table-without-docs-dirs",
        "multi-line-docs-dirs",
        "header-with-a-comment",
        "inline-scan-table",
        "dotted-scan-keys",
    ],
)
def test_toml_without_pev_gets_the_edit_printed_and_is_left_unchanged(tmp_path: Path, toml: str, edit: str) -> None:
    path = tmp_path / "axiom-graph.toml"
    path.write_bytes(toml.encode("utf-8"))
    result = _seed(tmp_path)
    assert result.returncode != 0
    assert edit in result.stderr
    assert path.read_bytes() == toml.encode("utf-8")
    assert not (tmp_path / ".pev").exists()


@pytest.mark.parametrize(
    "docs_dirs",
    ["docs_dirs = ['docs', '.pev']", 'docs_dirs = [\n  "docs",\n  ".pev",  # PEV templates\n]'],
    ids=["single-quoted", "multi-line"],
)
def test_toml_that_lists_pev_is_accepted_in_any_layout(tmp_path: Path, docs_dirs: str) -> None:
    toml = f'[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]\n{docs_dirs}\n\n' + PEV_TOML.split("\n\n")[-1]
    (tmp_path / "axiom-graph.toml").write_text(toml, encoding="utf-8")
    result = _seed(tmp_path, "--no-build")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".pev" / "templates" / "request.docjson").is_file()
    assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == toml


@pytest.mark.parametrize(
    "scan",
    [
        '[axiom_graph.scan]  # what to index\ndocs_dirs = ["docs", ".pev"]\n',
        'scan = { docs_dirs = ["docs", ".pev"] }\n',
    ],
    ids=["header-with-a-comment", "inline-scan-table"],
)
def test_scan_settings_that_list_pev_in_another_shape_are_accepted_and_not_appended(tmp_path: Path, scan: str) -> None:
    toml = f'[axiom_graph]\nproject_id = "demo"\n{scan}\n' + PEV_TOML.split("\n\n")[-1]
    path = tmp_path / "axiom-graph.toml"
    path.write_text(toml, encoding="utf-8")
    result = _seed(tmp_path, "--no-build")
    assert result.returncode == 0, result.stderr
    assert path.read_text(encoding="utf-8") == toml
    assert (tmp_path / ".pev" / "templates" / "request.docjson").is_file()


def test_toml_with_only_a_scan_table_gets_the_staleness_table_appended(tmp_path: Path) -> None:
    original = '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.scan]\ndocs_dirs = ["docs", ".pev"]\n'
    path = tmp_path / "axiom-graph.toml"
    path.write_text(original, encoding="utf-8")
    result = _seed(tmp_path, "--no-build")
    assert result.returncode == 0, result.stderr

    assert path.read_text(encoding="utf-8").startswith(original)
    assert path.read_text(encoding="utf-8").count("[axiom_graph.scan]") == 1
    config = AxiomGraphConfig.load(tmp_path)
    assert config.scan.docs_dirs == ["docs", ".pev"]
    assert config.staleness.frozen_tags == FROZEN_TAGS
    assert "added to axiom-graph.toml: [axiom_graph.staleness]" in result.stdout
    assert "added to axiom-graph.toml: [axiom_graph.scan]" not in result.stdout


def _failing_cli(tmp_path: Path, fails_on: str) -> str:
    """Write a stand-in CLI for ``--axiom-graph`` that fails when its arguments contain *fails_on*."""
    fake = tmp_path / "fake-ag.sh"
    fake.write_text(
        f'#!/bin/bash\ncase "$*" in *"{fails_on}"*) echo "boom: $*" >&2; exit 3;; esac\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    fake.chmod(0o755)
    return fake.as_posix()


@pytest.mark.parametrize(
    ("fails_on", "printed"),
    [("build", "build .` failed"), ("--list", "stamps accept . --list` failed")],
    ids=["build", "list-flagged-sections"],
)
def test_a_failing_cli_step_is_reported_and_exits_non_zero(tmp_path: Path, fails_on: str, printed: str) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "axiom-graph.toml").write_text(PEV_TOML, encoding="utf-8")
    result = _seed(project, "--axiom-graph", _failing_cli(tmp_path, fails_on))
    assert result.returncode == 1, result.stdout + result.stderr
    assert printed in result.stderr
    assert "boom" in result.stderr
    assert "No section" not in result.stdout
    assert "Next: commit" not in result.stdout


@pytest.mark.parametrize("mode", ["--check", "--diff"])
def test_reserved_modes_refuse_without_writing(tmp_path: Path, mode: str) -> None:
    result = _seed(tmp_path, mode)
    assert result.returncode == 2
    assert "not implemented" in result.stderr
    assert not (tmp_path / ".pev").exists()


def test_unknown_argument_is_refused(tmp_path: Path) -> None:
    result = _seed(tmp_path, "--bogus")
    assert result.returncode == 2
    assert not (tmp_path / ".pev").exists()
