"""The efficiency analyzer stages its DocJSON report for axiom_graph_write_doc.

``--docjson`` never writes into the docs tree: a file written there by hand is a
raw DocJSON edit the index flags and never verifies.  The script stages a
``write_doc`` payload (the document plus an ``id`` path slug) and the
orchestrator writes it with ``axiom_graph_write_doc(doc_file=...)``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev" / "scripts" / "analyze_pev_session.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="PEV plugin source not present")


@pytest.fixture(scope="module")
def analyzer():
    spec = importlib.util.spec_from_file_location("analyze_pev_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _analysis(analyzer, cycle_id="pev-2026-10-01-example"):
    return analyzer.SessionAnalysis(file_path="session.jsonl", cycle_id=cycle_id)


def test_payload_carries_the_doc_slug_and_lands_outside_the_docs_tree(analyzer, tmp_path, monkeypatch):
    monkeypatch.setattr(analyzer, "DEFAULT_STAGING_DIR", tmp_path / "staging")
    monkeypatch.chdir(tmp_path)

    path, slug = analyzer.write_docjson(_analysis(analyzer))

    assert slug == "pev/cycles/pev-2026-10-01-example/efficiency"
    assert Path(path) == tmp_path / "staging" / "pev-2026-10-01-example-efficiency.json"
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["id"] == slug
    assert payload["title"] == "PEV Efficiency: pev-2026-10-01-example"
    assert payload["tags"] == ["pev-efficiency"]
    assert payload["sections"]
    assert not (tmp_path / "docs").exists()


def test_session_index_and_doc_folder_shape_the_slug(analyzer, tmp_path):
    path, slug = analyzer.write_docjson(
        _analysis(analyzer), output_dir=str(tmp_path), session_index=2, doc_folder="/reports/eff/"
    )

    assert slug == "reports/eff/efficiency-s2"
    assert Path(path) == tmp_path / "pev-2026-10-01-example-efficiency-s2.json"
    assert json.loads(Path(path).read_text(encoding="utf-8"))["id"] == slug


def test_missing_output_dir_is_refused_without_yes(analyzer, tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    missing = tmp_path / "not-there"

    with pytest.raises(analyzer.OutputDirMissing):
        analyzer.write_docjson(_analysis(analyzer), output_dir=str(missing))

    path, _ = analyzer.write_docjson(_analysis(analyzer), output_dir=str(missing), assume_yes=True)
    assert Path(path).parent == missing
