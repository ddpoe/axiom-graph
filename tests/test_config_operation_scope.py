"""An operation scope reads each project's config once and keeps nothing after it ends."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom_graph.config import AxiomGraphConfig, config_scope, write_toml_project_id


def _write_config(root: Path, frozen: str) -> None:
    """Write an ``axiom-graph.toml`` whose only frozen tag is *frozen*."""
    (root / "axiom-graph.toml").write_text(
        f'[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.staleness]\nfrozen_tags = ["{frozen}"]\n',
        encoding="utf-8",
    )


def _count_parses(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Install a counter of the times ``axiom-graph.toml`` is parsed; return its list of roots."""
    real = AxiomGraphConfig._read.__func__
    parsed: list[Path] = []

    def counting(cls, project_root):
        parsed.append(Path(project_root))
        return real(cls, project_root)

    monkeypatch.setattr(AxiomGraphConfig, "_read", classmethod(counting))
    return parsed


def test_loads_in_one_scope_share_one_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every load of a root inside a scope, nested scopes included, returns the first one's config."""
    _write_config(tmp_path, "a")
    (tmp_path / "sub").mkdir()
    parsed = _count_parses(monkeypatch)
    with config_scope():
        first = AxiomGraphConfig.load(tmp_path)
        with config_scope():
            again = AxiomGraphConfig.load(tmp_path / "sub" / "..")
    assert again is first
    assert len(parsed) == 1


def test_outside_a_scope_every_load_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no scope open, each load reads the file again."""
    _write_config(tmp_path, "a")
    parsed = _count_parses(monkeypatch)
    AxiomGraphConfig.load(tmp_path)
    AxiomGraphConfig.load(tmp_path)
    assert len(parsed) == 2


def test_a_config_edited_between_two_scopes_is_seen_by_the_second(tmp_path: Path) -> None:
    """The scope ends with its operation, so the next one reads the edited file."""
    _write_config(tmp_path, "a")
    with config_scope():
        assert AxiomGraphConfig.load(tmp_path).staleness.frozen_tags == ["a"]
    _write_config(tmp_path, "b")
    with config_scope():
        assert AxiomGraphConfig.load(tmp_path).staleness.frozen_tags == ["b"]


def test_a_scope_that_raises_keeps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception ends the scope too: the next load parses the file again."""
    _write_config(tmp_path, "a")
    parsed = _count_parses(monkeypatch)
    with pytest.raises(RuntimeError), config_scope():
        AxiomGraphConfig.load(tmp_path)
        raise RuntimeError("operation failed")
    AxiomGraphConfig.load(tmp_path)
    assert len(parsed) == 2


def test_recording_the_project_id_inside_a_scope_is_seen_by_the_next_load(tmp_path: Path) -> None:
    """Writing ``axiom-graph.toml`` inside a scope drops the config the scope kept for that root."""
    (tmp_path / "axiom-graph.toml").write_text("[axiom_graph]\n", encoding="utf-8")
    with config_scope():
        assert AxiomGraphConfig.load(tmp_path).project_id is None
        assert write_toml_project_id(tmp_path, "proj") is True
        assert AxiomGraphConfig.load(tmp_path).project_id == "proj"
