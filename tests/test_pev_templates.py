"""The PEV plugin's cloneable templates are valid DocJSON and carry the tags the cycle layout relies on.

The orchestrator clones each cycle doc from ``.pev/templates/cycle/`` (seeded from
the plugin's ``templates/``). Every cycle template must carry the frozen
``pev-cycle`` tag so cloned docs stay out of ``check`` counts, and no template may
carry a status tag: a seeded template is an indexed project doc, so a status tag
on it would make tag searches (resume, request listings) find the template as a
live cycle. The manifest's status tag is set by the ``clone_doc`` call instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from axiom_annotations import workflow

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev" / "templates"

pytestmark = pytest.mark.skipif(not TEMPLATES_DIR.is_dir(), reason="PEV plugin source not present")

CYCLE_DOCS = ("manifest", "architect", "decisions", "builder", "review", "audit", "friction")
SIBLINGS = ("instance", "request", "audit-annotations", "audit-consumer-docs", "audit-dev-docs")
STATUS_TAGS = {
    "in-progress",
    "completed",
    "incomplete",
    "not-started",
    "backlog",
    "superseded",
    "archived",
    "pev-audit-active",
}
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_DEPTH = 2


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _walk(sections: list[dict], depth: int = 0):
    for section in sections:
        yield section, depth
        yield from _walk(section.get("sections") or [], depth + 1)


def _docjson_errors(data: dict, slugs: bool) -> list[str]:
    errors = [f"missing '{key}'" for key in ("title", "sections") if key not in data]
    for section, depth in _walk(data.get("sections", [])):
        for key in ("id", "heading"):
            if key not in section:
                errors.append(f"section missing '{key}': {section}")
        sid = section.get("id", "")
        if slugs and not SLUG.match(sid):
            errors.append(f"section id {sid!r} is not a dot-free slug")
        if depth > MAX_DEPTH:
            errors.append(f"section {sid!r} is nested deeper than {MAX_DEPTH}")
    return errors


def _cloneable_templates() -> list[Path]:
    return sorted(p for p in TEMPLATES_DIR.rglob("*.docjson") if "template_version" in _load(p).get("meta", {}))


@workflow(
    purpose=(
        "Every DocJSON file the plugin ships under templates/ parses and validates, the seven cycle docs and the "
        "instance, request and three audit templates are all present and versioned, and the retired single-file "
        "cycle template is gone"
    )
)
def test_every_template_is_valid_docjson_and_the_cloneable_set_is_complete() -> None:
    cloneable = set(_cloneable_templates())
    for path in sorted(TEMPLATES_DIR.rglob("*.docjson")):
        assert _docjson_errors(_load(path), slugs=path in cloneable) == [], path

    expected = {TEMPLATES_DIR / "cycle" / f"{name}.docjson" for name in CYCLE_DOCS}
    expected |= {TEMPLATES_DIR / f"{name}.docjson" for name in SIBLINGS}
    assert cloneable == expected
    for path in expected:
        assert re.match(r"^\d+\.\d+\.\d+$", _load(path)["meta"]["template_version"]), path

    assert not (TEMPLATES_DIR / "cycle-manifest-template.docjson").exists()


@workflow(
    purpose=(
        "Every cycle template carries the frozen pev-cycle tag, and no cloneable template carries a status tag, "
        "so a seeded template never shows up as a live cycle, instance, request or audit"
    )
)
def test_cycle_templates_are_frozen_and_no_template_carries_a_status_tag() -> None:
    for name in CYCLE_DOCS:
        tags = _load(TEMPLATES_DIR / "cycle" / f"{name}.docjson").get("tags", [])
        assert "pev-cycle" in tags, name
    for path in _cloneable_templates():
        tags = set(_load(path).get("tags", []))
        assert not tags & STATUS_TAGS, path
        assert not any(tag.startswith("pev-audit-") for tag in tags), path


def test_cycle_templates_stay_within_depth_one_and_seed_no_runtime_entries() -> None:
    """Runtime entries (d-N, inc-N, pass-N, round-N, friction entries) are added at run time, never pre-seeded."""
    for name in CYCLE_DOCS:
        data = _load(TEMPLATES_DIR / "cycle" / f"{name}.docjson")
        for section, depth in _walk(data["sections"]):
            assert depth <= 1, (name, section["id"])
            assert not re.match(r"^(d|inc|pass|task|round)-\d+", section["id"]), (name, section["id"])


def test_manifest_template_holds_the_orchestrators_baseline_and_fix_list() -> None:
    """The merge target's node ids and each approved fix round get their own orchestrator-owned sections.

    Both are empty placeholders in the template: entry ids, pre-merge ids and fix rounds are written per run.
    """
    data = _load(TEMPLATES_DIR / "cycle" / "manifest.docjson")
    sections = {section["id"]: section for section in data["sections"]}
    for sid in ("status", "request", "scope", "change-set", "baseline", "fix-list"):
        assert sid in sections, sid
    assert data["meta"]["template_version"] != "1.0.0"

    for sid in ("baseline", "fix-list"):
        content = sections[sid]["content"]
        assert content.startswith("**Filled by:** Orchestrator"), sid
        assert not sections[sid].get("sections"), sid
        # A node id in the template is a placeholder ({project_id}::...) or a cycle doc's section, never a real id.
        for token in re.findall(r"\S*::\S*", content):
            doc = token.split("::", 1)[0].strip("`(")
            assert doc.endswith("}") or doc.rsplit("/", 1)[-1] in CYCLE_DOCS, (sid, token)

    baseline = sections["baseline"]["content"]
    for line in ("Entry stale ids (worktree):", "Pre-merge (main):", "Pre-merge stale ids (main):", "Final check:"):
        assert line in baseline, line
    assert "drift_query" in baseline
    assert "round-N" in sections["fix-list"]["content"]
    assert "Resume at:" in sections["status"]["content"]


def test_builder_template_holds_only_the_build_plan() -> None:
    """Continuations resume from inc-N.task-M checkpoints, so the template reserves no handoff section."""
    data = _load(TEMPLATES_DIR / "cycle" / "builder.docjson")
    assert [section["id"] for section in data["sections"]] == ["build-plan"]
    assert "handoff" not in data["sections"][0]["content"]


def test_instance_template_matches_the_reviewed_flow() -> None:
    """The checkin holds the plan and a section for each independent reviewer, and no self-review."""
    data = _load(TEMPLATES_DIR / "instance.docjson")
    ids = [section["id"] for section in data["sections"]]
    for sid in ("problem", "user-stories", "acceptance", "plan", "changes", "doc-updates", "review", "doc-review"):
        assert sid in ids, sid
    assert "user-story" not in ids
    assert "self-review" not in ids
    assert ids.index("plan") < ids.index("changes") < ids.index("review") < ids.index("doc-review")

    doc_review = next(section for section in data["sections"] if section["id"] == "doc-review")
    assert [child["id"] for child in doc_review.get("sections", [])] == ["progress", "findings"]

    meta = next(section for section in data["sections"] if section["id"] == "meta")
    assert "Commit:" not in meta["content"]
    assert data["meta"]["template_version"] not in ("1.0.0", "1.1.0")

    # Each task runs in its own worktree and lands through the shared merge step, which reads these lines.
    for line in ("Worktree:", "Branch:", "Baseline SHA:", "Main branch:", "Main repo:", "Main failing tests:"):
        assert line in meta["content"], line
    assert "Resume at:" in meta["content"]

    # The entry check's stale ids are the instance's baseline; the merge step appends main's pre-merge ids.
    assert ids.index("meta") < ids.index("baseline") < ids.index("plan")
    baseline = next(section for section in data["sections"] if section["id"] == "baseline")
    assert not baseline.get("sections")
    for line in ("Entry baseline (worktree):", "Entry stale ids (worktree):", "Pre-merge stale ids (main):"):
        assert line in baseline["content"], line
    assert "drift_query" in baseline["content"]


def test_friction_template_has_one_group_per_agent_type() -> None:
    data = _load(TEMPLATES_DIR / "cycle" / "friction.docjson")
    groups = [section["id"] for section in data["sections"]]
    assert groups == ["architect", "builder", "reviewer", "auditor", "doc-review", "orchestrator"]
