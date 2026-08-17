"""What ``axiom-graph init`` tells you before it throws your index away.

``init`` deletes the database and rebuilds it from scratch, so accepting its
prompt costs every verification record and the whole change history.  A prompt
that admits less than that is how someone loses their history by accident.
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli
from axiom_graph.index import db


def _existing_index(project_root: Path) -> Path:
    """Give *project_root* an index database, as a project already in use has."""
    ag_dir = project_root / ".axiom_graph"
    ag_dir.mkdir(exist_ok=True)
    db_path = ag_dir / "graph.db"
    db.init_db(db_path)
    return db_path


@workflow(
    purpose="Re-initialising an existing index asks for confirmation in terms of what is actually lost",
)
def test_init_prompt_admits_the_database_and_its_history_are_deleted(tmp_path):
    db_path = _existing_index(tmp_path)

    result = CliRunner().invoke(cli, ["init", str(tmp_path)], input="n\n")

    prompt = result.output.lower()
    assert "delete" in prompt, f"the prompt must say the database is deleted: {result.output}"
    assert "history" in prompt, f"the prompt must say the change history is lost: {result.output}"
    assert "verification" in prompt, f"the prompt must say verification records are lost: {result.output}"

    assert result.exit_code != 0, "declining the prompt aborts"
    assert db_path.exists(), "declining the prompt leaves the index alone"
