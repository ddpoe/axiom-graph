"""Annotation findings store: per-file scanner findings and AutoStep records.

The ``annotation_findings`` table (schema v4) holds what the build learned
about each file's annotations, so ``check`` need not re-parse an unchanged
tree and both commands can tell a new finding from one already reported.
Only the build writes it.  Row kinds:

- ``finding``: one raw scanner finding of a file (rules A1-C1), stored
  unfiltered by the ``[validation]`` config.
- ``autostep``: one AutoStep record of a file, kept raw so the B4 rule is
  resolved against the whole index when read.
- ``b4``: one B4 finding as the last build resolved it.
- ``dotted``: one dotted DocJSON filename the last build saw.

Findings and AutoStep records travel as plain dicts here: a finding in the
``ValidationFinding.to_dict`` shape, a record with the ``AutoStepRecord``
fields.  The domain types live above the DB layer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from axiom_graph.db._core import _connect

#: Row kind of a raw per-file scanner finding.
FINDING_KIND = "finding"
#: Row kind of a raw per-file AutoStep record.
AUTOSTEP_KIND = "autostep"
#: Row kind of a B4 finding as the last build resolved it.
B4_KIND = "b4"
#: Row kind of a dotted DocJSON filename the last build saw.
DOTTED_KIND = "dotted"

_FINDING_COLUMNS = ("rule_id", "severity", "function", "line", "message")


@dataclass
class StoredAnnotations:
    """Everything the annotation findings store holds.

    Attributes:
        findings: File path -> its raw scanner findings, as
            ``ValidationFinding.to_dict`` dicts.
        autosteps: File path -> its AutoStep records, as dicts of the
            ``AutoStepRecord`` fields.
        b4: B4 findings as the last build resolved them.
        dotted: Dotted DocJSON filenames the last build saw.
    """

    findings: dict[str, list[dict]] = field(default_factory=dict)
    autosteps: dict[str, list[dict]] = field(default_factory=dict)
    b4: list[dict] = field(default_factory=list)
    dotted: list[str] = field(default_factory=list)

    @property
    def files(self) -> set[str]:
        """Return every file that holds per-file rows."""
        return set(self.findings) | set(self.autosteps)


def _finding_row(kind: str, finding: dict) -> tuple:
    return (
        kind,
        finding["module"],
        finding.get("rule_id"),
        finding.get("severity"),
        finding.get("function"),
        finding.get("line"),
        finding.get("message"),
        None,
    )


def _finding_dict(row: sqlite3.Row) -> dict:
    return {
        "rule_id": row["rule_id"],
        "severity": row["severity"],
        "module": row["file"],
        "function": row["function"],
        "line": row["line"],
        "message": row["message"],
    }


def _insert_rows(conn: sqlite3.Connection, rows: Iterable[tuple]) -> None:
    conn.executemany(
        "INSERT INTO annotation_findings (kind, file, rule_id, severity, function, line, message, record) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        list(rows),
    )


def read_annotation_store_conn(conn: sqlite3.Connection) -> StoredAnnotations:
    """Read the whole annotation findings store.

    An index that predates the store (no table yet) reads as empty.

    Args:
        conn: Open connection to the axiom-graph DB.

    Returns:
        The stored rows grouped by kind, in insertion order.
    """
    store = StoredAnnotations()
    try:
        rows = conn.execute(
            "SELECT kind, file, rule_id, severity, function, line, message, record FROM annotation_findings ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return store
    for row in rows:
        kind = row["kind"]
        if kind == FINDING_KIND:
            store.findings.setdefault(row["file"], []).append(_finding_dict(row))
        elif kind == AUTOSTEP_KIND:
            store.autosteps.setdefault(row["file"], []).append(json.loads(row["record"]))
        elif kind == B4_KIND:
            store.b4.append(_finding_dict(row))
        elif kind == DOTTED_KIND:
            store.dotted.append(row["file"])
    return store


def read_annotation_store(db_path: Path) -> StoredAnnotations:
    """Read the whole annotation findings store from *db_path*.

    Args:
        db_path: Path to the axiom-graph DB.

    Returns:
        See :func:`read_annotation_store_conn`.
    """
    with _connect(db_path) as conn:
        return read_annotation_store_conn(conn)


def replace_file_annotations_conn(
    conn: sqlite3.Connection,
    file: str,
    findings: Iterable[dict],
    autosteps: Iterable[dict],
) -> None:
    """Replace the stored findings and AutoStep records of one scanned file.

    A file scanned with nothing to report is stored as no rows, which
    clears whatever it held before.

    Args:
        conn: Open connection to the axiom-graph DB.
        file: Project-relative POSIX path of the file.
        findings: Its raw scanner findings, as ``ValidationFinding.to_dict``
            dicts.
        autosteps: Its AutoStep records, as dicts of the ``AutoStepRecord``
            fields.
    """
    conn.execute(
        "DELETE FROM annotation_findings WHERE file = ? AND kind IN (?, ?)",
        (file, FINDING_KIND, AUTOSTEP_KIND),
    )
    rows = [_finding_row(FINDING_KIND, {**finding, "module": file}) for finding in findings]
    rows.extend(
        (AUTOSTEP_KIND, file, None, None, None, None, None, json.dumps(record, sort_keys=True)) for record in autosteps
    )
    _insert_rows(conn, rows)


def delete_file_annotations_conn(conn: sqlite3.Connection, files: Iterable[str]) -> int:
    """Drop the stored findings and AutoStep records of *files*.

    Args:
        conn: Open connection to the axiom-graph DB.
        files: Project-relative POSIX paths no longer walked.

    Returns:
        Number of rows deleted.
    """
    deleted = 0
    for file in files:
        deleted += conn.execute(
            "DELETE FROM annotation_findings WHERE file = ? AND kind IN (?, ?)",
            (file, FINDING_KIND, AUTOSTEP_KIND),
        ).rowcount
    return deleted


def replace_b4_findings_conn(conn: sqlite3.Connection, findings: Iterable[dict]) -> None:
    """Replace the stored set of resolved B4 findings.

    Args:
        conn: Open connection to the axiom-graph DB.
        findings: B4 findings as ``ValidationFinding.to_dict`` dicts.
    """
    conn.execute("DELETE FROM annotation_findings WHERE kind = ?", (B4_KIND,))
    _insert_rows(conn, (_finding_row(B4_KIND, finding) for finding in findings))


def replace_dotted_filenames_conn(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
    """Replace the stored set of dotted DocJSON filenames.

    Args:
        conn: Open connection to the axiom-graph DB.
        paths: Project-relative paths of the dotted DocJSON files on disk.
    """
    conn.execute("DELETE FROM annotation_findings WHERE kind = ?", (DOTTED_KIND,))
    _insert_rows(conn, ((DOTTED_KIND, path, None, None, None, None, None, None) for path in paths))


__all__ = [
    "FINDING_KIND",
    "AUTOSTEP_KIND",
    "B4_KIND",
    "DOTTED_KIND",
    "StoredAnnotations",
    "read_annotation_store",
    "read_annotation_store_conn",
    "replace_file_annotations_conn",
    "delete_file_annotations_conn",
    "replace_b4_findings_conn",
    "replace_dotted_filenames_conn",
]
