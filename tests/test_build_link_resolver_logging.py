"""The build's link resolver reports each retarget at DEBUG and one summary line at INFO.

A scanner sees one file at a time, so a test that calls a function through
a package re-export names a node that was never minted, and every rescan of
that test file re-emits the guess.  The resolver writes the retargeted link
to the index each time; the per-link detail stays out of the INFO output so
the build summary stays readable.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from axiom_annotations import workflow

from axiom_graph.index import builder

_LOGGER = "axiom_graph.index.builder"


def _write(path: Path, text: str) -> None:
    """Write *text* to *path*, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _resolver_records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """Return the resolver's messages logged at exactly *level*."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == level and r.getMessage().startswith("link resolver")
    ]


@workflow(
    purpose="A re-exported validates target is stored after one build; per-link lines are DEBUG and a no-change rebuild logs nothing at INFO"
)
def test_reexported_validates_target_logs_detail_at_debug(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.impl import real\n")
    _write(tmp_path / "pkg" / "impl.py", "def real():\n    return 1\n")
    _write(tmp_path / "tests" / "test_real.py", "from pkg import real\n\n\ndef test_real():\n    assert real() == 1\n")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        builder.build(tmp_path, project_id="p", discovery_only=True)

    assert _resolver_records(caplog, logging.DEBUG) == [
        "link resolver: validates p::tests.test_real::test_real -> p::pkg.impl::real (was p::pkg::real)"
    ]
    assert _resolver_records(caplog, logging.INFO) == [
        "link resolver: retargeted 0 delegate and 1 validates link(s) through the re-export closure"
    ]
    conn = sqlite3.connect(tmp_path / ".axiom_graph" / "graph.db")
    stored = conn.execute(
        "SELECT to_id FROM edges WHERE edge_type = 'validates' AND from_id = 'p::tests.test_real::test_real'"
    ).fetchall()
    conn.close()
    assert stored == [("p::pkg.impl::real",)]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        builder.build(tmp_path, project_id="p", discovery_only=True)

    assert _resolver_records(caplog, logging.INFO) == []
