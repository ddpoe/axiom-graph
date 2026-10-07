"""A doc write parses the project config once, whatever reads it.

The pre-write refresh, the offender read, the still-LINKED_STALE note, the
re-index and the closing refresh all load the config inside the write's
config scope, so ``axiom-graph.toml`` is parsed once per call however many
of those steps run, and nothing is kept between calls.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from axiom_graph.config import AxiomGraphConfig
from axiom_graph.docjson.api import axiom_graph_patch_section, axiom_graph_update_section
from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes

_SECTION = "proj::docs/spec::s"


def _linked_stale_section_project(root: Path) -> Path:
    """A section documents ``f``; it is verified, then ``f`` changes, so the section is LINKED_STALE."""
    (root / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "docs").mkdir()
    doc = {
        "title": "Spec",
        "sections": [{"id": "s", "heading": "S", "content": "S.", "links": [{"node_id": "proj::mod::f"}]}],
    }
    (root / "docs" / "spec.json").write_text(json.dumps(doc), encoding="utf-8")
    dbp = _db_path(str(root))
    build_index(dbp, root)
    mark_clean_nodes(dbp, root, [_SECTION], reason="reviewed", verified_by="human")
    time.sleep(0.02)
    (root / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    build_index(dbp, root)
    compute_check_summary(dbp, root)
    with db._connect(dbp) as conn:
        assert conn.execute("SELECT link_status FROM nodes WHERE id = ?", (_SECTION,)).fetchone()[0] == "LINKED_STALE"
    return dbp


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Install a counter of the times ``axiom-graph.toml`` is parsed; return its list of roots."""
    real = AxiomGraphConfig._read.__func__
    parsed: list[Path] = []

    def counting(cls, project_root):
        parsed.append(Path(project_root))
        return real(cls, project_root)

    monkeypatch.setattr(AxiomGraphConfig, "_read", classmethod(counting))
    return parsed


@pytest.mark.parametrize(
    "write",
    [
        lambda root: axiom_graph_update_section(str(root), _SECTION, content="S, revised."),
        lambda root: axiom_graph_patch_section(str(root), _SECTION, new_string=" More.", anchor="$"),
    ],
    ids=["update_section", "patch_section"],
)
def test_a_doc_write_parses_the_config_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write) -> None:
    """A doc write on a LINKED_STALE section runs every config-reading step and parses the config once in all."""
    _linked_stale_section_project(tmp_path)
    parsed = _count_parses(monkeypatch)
    result = write(tmp_path)
    assert not str(result).startswith("ERROR"), result
    assert "still LINKED_STALE" in str(result), result
    assert len(parsed) == 1, parsed


def test_a_config_read_outside_a_doc_write_is_not_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two doc writes each parse the config, and a load after them parses it again: nothing is kept."""
    _linked_stale_section_project(tmp_path)
    parsed = _count_parses(monkeypatch)
    axiom_graph_update_section(str(tmp_path), _SECTION, content="First.")
    axiom_graph_update_section(str(tmp_path), _SECTION, content="Second.")
    assert len(parsed) == 2, parsed
    AxiomGraphConfig.load(tmp_path)
    assert len(parsed) == 3, parsed
