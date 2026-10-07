"""Reading the tags of many nodes at once: every node's tags, in row order, past one batch."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from axiom_graph.index import db
from axiom_graph.query.api import node_tags


def test_tags_of_more_nodes_than_one_batch_come_back_whole_and_in_row_order(tmp_path: Path) -> None:
    """More ids than one batch holds: each node's tags come back complete, in primary-key index order, as a per-node read gives them."""
    db_path = tmp_path / "graph.db"
    db.init_db(db_path)
    ids = [f"demo::m{i:04d}" for i in range(1203)]
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executemany(
            "INSERT INTO tags (node_id, tag) VALUES (?, ?)",
            [(nid, tag) for nid in ids[::2] for tag in ("zeta", "alpha")],
        )
        conn.commit()

        one_at_a_time = {
            nid: [r[0] for r in conn.execute("SELECT tag FROM tags WHERE node_id = ?", (nid,))] for nid in ids[::2]
        }

    tags = node_tags(db_path, ids)

    assert tags == one_at_a_time
    assert all(sorted(tags[nid]) == ["alpha", "zeta"] for nid in ids[::2])
    assert node_tags(db_path, []) == {}
