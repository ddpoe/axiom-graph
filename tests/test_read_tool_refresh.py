"""Read tools: what they refresh before answering, what that costs, and that the answer is the truth.

``refresh_before_read`` decides: ``"changed-files"`` (the default) brings the
shown statuses up to date first, ``"off"`` answers from the stored statuses
and says how far behind they are, ``"check"`` runs ``check`` first.  A
whole-repo listing (``drift_query``) gets the incremental check; a read that
names nodes gets the cone of what it names, which never moves the journal
watermark.  Work is counted, not timed.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli
from axiom_graph.config import AxiomGraphConfig, ConfigError
from axiom_graph.index import db
from axiom_graph.index.staleness import _get_linked_stale_ids, resolve_root_offenders
from axiom_graph.lifecycle.api import compute_check_summary, refresh_before_read
from axiom_graph.query.api import compute_drift_query
from tests.fixtures.full_recompute import assert_matches_full_recompute, full_recompute_rows, stored_rows

_F_ID = "demo::pkg.mod::f"
_SECTION_ID = "demo::docs/guide::f"
_OTHER_SECTION_ID = "demo::docs/guide::other"
_TEST_F_ID = "demo::tests.test_mod::test_f"


def _toml(root: Path, mode: str | None) -> None:
    extra = f'\n[axiom_graph.staleness]\nrefresh_before_read = "{mode}"\n' if mode else ""
    (root / "axiom-graph.toml").write_text(f'[axiom_graph]\nproject_id = "demo"\n{extra}', encoding="utf-8")


def _project(root: Path, unrelated: int = 1) -> Path:
    """S documents ``f``, T validates it, a second section documents an unrelated function; init, then check."""
    _toml(root, None)
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    for i in range(unrelated):
        (pkg / f"other{i}.py").write_text(f"def h{i}():\n    return {i}\n", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mod.py").write_text(
        "from pkg.mod import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    docs_dir = root / "docs"
    docs_dir.mkdir()
    doc = {
        "title": "Guide",
        "tags": [],
        "sections": [
            {"id": "f", "heading": "f", "content": "f returns one.", "links": [{"node_id": _F_ID}]},
            {"id": "other", "heading": "Other", "content": "h0.", "links": [{"node_id": "demo::pkg.other0::h0"}]},
        ],
    }
    (docs_dir / "guide.docjson").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(root)])
    assert result.exit_code == 0, result.output
    db_path = root / ".axiom_graph" / "graph.db"
    compute_check_summary(db_path, root)
    return db_path


def _edit_f(root: Path) -> None:
    (root / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")


def _watermark(db_path: Path) -> str | None:
    with db._connect(db_path) as conn:
        return db.get_index_meta_conn(conn, db.STALENESS_WATERMARK_META_KEY)


@workflow(
    purpose="drift_query refreshes by config: changed-files lists the edit and its dependents as a full recompute "
    "stores them, off lists the stored snapshot and says the index is behind, check equals a full check"
)
def test_drift_query_refreshes_by_config(tmp_path: Path) -> None:
    口 = Step(step_num=1, name="Index, check, edit", purpose="a settled index, then f edited without a build")
    db_path = _project(tmp_path)
    _edit_f(tmp_path)

    口 = Step(step_num=2, name="off", purpose="the stored snapshot, plus one line saying how far behind it is")
    _toml(tmp_path, "off")
    before = stored_rows(db_path)
    out = compute_drift_query(db_path, tmp_path)
    assert out == "(no matches)\n\n[index is behind for 1 file — run `check`]"
    assert stored_rows(db_path) == before

    口 = Step(step_num=3, name="changed-files", purpose="the edit and its dependents, as check --full stores them")
    _toml(tmp_path, "changed-files")
    out = compute_drift_query(db_path, tmp_path)
    assert f"id={_F_ID}  CONTENT_UPDATED/VERIFIED" in out
    assert f"id={_SECTION_ID}  VERIFIED/LINKED_STALE" in out and f"id={_TEST_F_ID}  VERIFIED/LINKED_STALE" in out
    assert "index is behind" not in out
    assert stored_rows(db_path) == full_recompute_rows(db_path, tmp_path)

    口 = Step(step_num=4, name="check", purpose="a check first: the same listing a full check leaves")
    _toml(tmp_path, "check")
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 3\n", encoding="utf-8")
    assert compute_drift_query(db_path, tmp_path) == out
    assert_matches_full_recompute(db_path, tmp_path)


def test_drift_query_attribution_is_the_whole_stale_maps_for_the_page(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    _edit_f(tmp_path)
    out = compute_drift_query(db_path, tmp_path, filter="LINKED_STALE")
    config = AxiomGraphConfig.load(tmp_path)
    whole = _get_linked_stale_ids(
        db_path, transitive_tags=config.staleness.transitive_tags, frozen_tags=config.staleness.frozen_tags
    )
    roots = resolve_root_offenders(whole)
    for nid in (_SECTION_ID, _TEST_F_ID):
        line = next(ln for ln in out.splitlines() if ln.startswith(f"id={nid} "))
        assert f"via={','.join(whole[nid])}" in line
        assert ("root=" in line) == (sorted(roots[nid]) != sorted(whole[nid]))


def test_drift_query_opens_one_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _project(tmp_path)
    _edit_f(tmp_path)
    opened = {"n": 0}
    real = sqlite3.connect

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    compute_drift_query(db_path, tmp_path)
    assert opened["n"] == 1


def test_a_node_naming_read_looks_only_at_what_it_shows_and_leaves_the_watermark(tmp_path: Path) -> None:
    observed: list[set[str]] = []
    for size in (1, 5):
        root = tmp_path / f"p{size}"
        root.mkdir()
        db_path = _project(root, unrelated=size + 1)
        _edit_f(root)
        # other0 is read (the sibling section links it, and the doc's roll-up
        # reads the sibling); edits to the rest must not be looked at.
        for i in range(1, size + 1):
            (root / "pkg" / f"other{i}.py").write_text(f"def h{i}():\n    return {i + 100}\n", encoding="utf-8")
        mark = _watermark(db_path)
        rr = refresh_before_read(db_path, root, [_SECTION_ID])
        assert rr.refresh.mode == "cone"
        assert rr.tag(_SECTION_ID) == "  [LINKED_STALE]"
        assert rr.refresh.files_hashed == {"pkg/mod.py"}
        observed.append(set(rr.refresh.observed))
        assert _watermark(db_path) == mark
        shown = full_recompute_rows(db_path, root, [_SECTION_ID, _F_ID, _TEST_F_ID])
        assert stored_rows(db_path, [_SECTION_ID, _F_ID, _TEST_F_ID]) == shown
    assert observed[0] == observed[1]
    assert {"docs/guide.docjson", "pkg/mod.py", "pkg/other0.py"} <= observed[0]
    assert not any(loc.startswith("pkg/other") and loc != "pkg/other0.py" for loc in observed[0])


def test_off_counts_a_touched_file_only_until_a_pass_reads_it(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    target = tmp_path / "pkg" / "other0.py"
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 5))
    assert refresh_before_read(db_path, tmp_path, mode="off").behind == 1
    compute_check_summary(db_path, tmp_path)
    assert refresh_before_read(db_path, tmp_path, mode="off").behind == 0


def test_refresh_before_read_rejects_an_unknown_mode(tmp_path: Path) -> None:
    _toml(tmp_path, "sometimes")
    with pytest.raises(ConfigError, match="refresh_before_read"):
        AxiomGraphConfig.load(tmp_path)


def test_a_read_under_off_refreshes_nothing_although_the_index_has_file_records(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM file_state").fetchone()[0] > 0
    assert refresh_before_read(db_path, tmp_path, [_SECTION_ID], mode="off").refresh is None


# ---------------------------------------------------------------------------
# Node-naming reads: graph, search, source, read_doc
# ---------------------------------------------------------------------------

_DOC_ID = "demo::docs/guide"
_OTHER_F_ID = "demo::pkg.other0::h0"


def _own_and_link(db_path: Path) -> dict[str, tuple[str, str]]:
    """Every node's stored own and link status (columns every schema version has)."""
    with closing(sqlite3.connect(db_path)) as conn:
        return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, own_status, link_status FROM nodes")}


@workflow(
    purpose="On an index below schema v5 (a package upgrade before its first build) each read tool answers from "
    "the stored statuses: it refreshes nothing, writes nothing, raises no error, and shows the stored tag even "
    "where a refresh would have changed it"
)
@pytest.mark.parametrize("tool", ["search", "graph", "source", "read_doc", "drift_query"])
def test_each_read_on_an_index_below_v5_answers_from_the_stored_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    from axiom_graph.db import migrations  # noqa: PLC0415
    from axiom_graph.docjson.api import axiom_graph_read_doc  # noqa: PLC0415
    from axiom_graph.query.api import fetch_graph, fetch_source, search_nodes  # noqa: PLC0415
    from tests.test_schema_migration import _downgrade_to_v4  # noqa: PLC0415

    db_path = _project(tmp_path)
    # f is stored CONTENT_UPDATED, then put back at its indexed body: a refresh would realign it to VERIFIED.
    _edit_f(tmp_path)
    compute_check_summary(db_path, tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    with db._connect(db_path) as conn:
        _downgrade_to_v4(conn)
    before = _own_and_link(db_path)
    assert before[_F_ID][0] == "CONTENT_UPDATED"
    reads = _RecordReads(monkeypatch)
    calls = {
        "search": lambda: search_nodes(db_path, "f", scope="code", root=tmp_path),
        "graph": lambda: fetch_graph(db_path, _F_ID, direction="in", with_locations=True, root=tmp_path).rendered,
        "source": lambda: fetch_source(db_path, tmp_path, _F_ID).text,
        "read_doc": lambda: axiom_graph_read_doc(str(tmp_path), section_ids=[_SECTION_ID]),
        "drift_query": lambda: compute_drift_query(db_path, tmp_path),
    }

    out = calls[tool]()

    assert not out.startswith("ERROR"), out
    assert reads.calls and all(rr.refresh is None and rr.behind == 0 for rr in reads.calls)
    assert _F_ID in out and "CONTENT_UPDATED" in out
    assert _own_and_link(db_path) == before
    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == 4


def _full_tag(db_path: Path, root: Path, node_id: str) -> str:
    """The ``[STATUS]`` tag a read must show for *node_id*: what check --full stores, in the outline's format."""
    own, link = full_recompute_rows(db_path, root, [node_id])[node_id][:2]
    flags = [st for st in (own, link) if st and st != "VERIFIED"]
    return f"  [{', '.join(flags)}]" if flags else ""


def _line_naming(text: str, needle: str) -> str:
    return next(ln for ln in text.splitlines() if needle in ln)


class _RecordReads:
    """Wrap ``refresh_before_read`` (the one read helper) and keep what each read refreshed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import axiom_graph.lifecycle.api as lifecycle_api  # noqa: PLC0415

        self.calls: list = []
        real = lifecycle_api.refresh_before_read

        def recording(*args, **kwargs):
            rr = real(*args, **kwargs)
            self.calls.append(rr)
            return rr

        monkeypatch.setattr(lifecycle_api, "refresh_before_read", recording)

    def last(self):
        return self.calls[-1]


@workflow(
    purpose="graph, search, source and read_doc tag every node they name that is not VERIFIED, as check --full "
    "stores it, after refreshing only the edited file their statuses read: unrelated edits are never looked at, "
    "the journal watermark does not move, and VERIFIED neighbours render unchanged"
)
def test_node_naming_reads_tag_what_they_show_from_a_refresh_of_only_that(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from axiom_graph.docjson.api import axiom_graph_read_doc  # noqa: PLC0415
    from axiom_graph.query.api import fetch_graph, fetch_source, search_nodes  # noqa: PLC0415

    口 = Step(step_num=1, name="Index and check", purpose="S documents f, T validates f; five unrelated modules")
    db_path = _project(tmp_path, unrelated=5)
    reads = _RecordReads(monkeypatch)
    shown = [_F_ID, _SECTION_ID, _TEST_F_ID, _OTHER_SECTION_ID, _DOC_ID]

    def _edit_all(n: int) -> str | None:
        """Edit f (a new body each time) and every unrelated module but other0; return the watermark."""
        (tmp_path / "pkg" / "mod.py").write_text(f"def f():\n    return {n}\n", encoding="utf-8")
        for i in range(1, 5):
            (tmp_path / "pkg" / f"other{i}.py").write_text(f"def h{i}():\n    return {n * 10 + i}\n", encoding="utf-8")
        return _watermark(db_path)

    def _looked_only_at(mark: str | None, hashed: set[str]) -> None:
        rr = reads.last()
        assert rr.refresh.mode == ("cone" if hashed else "idle")
        assert rr.refresh.files_hashed == hashed
        assert not any(f"pkg/other{i}.py" in rr.refresh.observed for i in range(1, 5))
        assert _watermark(db_path) == mark

    口 = Step(step_num=2, name="graph", purpose="graph(f, in): f, S and T tagged as check --full stores them")
    mark = _edit_all(2)
    out = fetch_graph(db_path, _F_ID, direction="in", with_locations=True, root=tmp_path).rendered
    _looked_only_at(mark, {"pkg/mod.py"})
    assert _line_naming(out, f"] {_F_ID}").endswith("  [CONTENT_UPDATED]")
    assert _full_tag(db_path, tmp_path, _F_ID) == "  [CONTENT_UPDATED]"
    for nid in (_SECTION_ID, _TEST_F_ID):
        assert _line_naming(out, nid).endswith("  [LINKED_STALE]")
        assert _full_tag(db_path, tmp_path, nid) == "  [LINKED_STALE]"

    口 = Step(step_num=3, name="search", purpose="search tags f; a VERIFIED match's line is unchanged")
    mark = _edit_all(3)
    out = search_nodes(db_path, "h0", scope="code", root=tmp_path)
    _looked_only_at(mark, set())  # h0's status reads pkg/other0.py only, which did not change
    assert _line_naming(out, _OTHER_F_ID) == _line_naming(search_nodes(db_path, "h0", scope="code"), _OTHER_F_ID)
    out = search_nodes(db_path, "f", scope="code", root=tmp_path)
    _looked_only_at(mark, {"pkg/mod.py"})
    assert _line_naming(out, f"{_F_ID} ").endswith("  [CONTENT_UPDATED]")

    口 = Step(step_num=4, name="source", purpose="source's header line carries f's tag")
    mark = _edit_all(4)
    out = fetch_source(db_path, tmp_path, _F_ID).text
    _looked_only_at(mark, {"pkg/mod.py"})
    assert out.splitlines()[0] == f"# {_F_ID}  @ pkg/mod.py#L1-L2  [CONTENT_UPDATED]"

    口 = Step(step_num=5, name="read_doc", purpose="the section, its linked node and the doc envelope are tagged")
    mark = _edit_all(5)
    out = axiom_graph_read_doc(str(tmp_path), section_ids=[_SECTION_ID])
    _looked_only_at(mark, {"pkg/mod.py"})
    assert _line_naming(out, f"<!-- id: {_SECTION_ID} -->").endswith("  [LINKED_STALE]")
    assert _line_naming(out, f"- `{_F_ID}`").endswith("  [CONTENT_UPDATED]")
    assert _line_naming(out, f"<!-- doc: {_DOC_ID}").endswith(f"-->{_full_tag(db_path, tmp_path, _DOC_ID)}")

    口 = Step(step_num=6, name="Truth", purpose="every shown row equals what check --full stores")
    assert stored_rows(db_path, shown) == full_recompute_rows(db_path, tmp_path, shown)


def test_verified_reads_render_byte_identical_to_untagged(tmp_path: Path) -> None:
    from axiom_graph.query.api import fetch_graph, search_nodes  # noqa: PLC0415

    db_path = _project(tmp_path)
    assert fetch_graph(db_path, _F_ID, direction="in", with_locations=True, root=tmp_path).rendered == (
        fetch_graph(db_path, _F_ID, direction="in", with_locations=True).rendered
    )
    assert search_nodes(db_path, "f", root=tmp_path) == search_nodes(db_path, "f")


@workflow(
    purpose="An outline under changed-files tags the section its edit made LINKED_STALE; under off it shows the "
    "stored statuses and says how far behind the index is"
)
def test_outline_tags_by_config(tmp_path: Path) -> None:
    from axiom_graph.docjson.api import axiom_graph_read_doc  # noqa: PLC0415

    口 = Step(step_num=1, name="Edit", purpose="a settled index, then f edited without a build")
    db_path = _project(tmp_path)
    _edit_f(tmp_path)

    口 = Step(step_num=2, name="off", purpose="stored statuses (none stale yet) and the behind line")
    _toml(tmp_path, "off")
    out = axiom_graph_read_doc(str(tmp_path), doc_id=_DOC_ID, outline=True)
    assert "[LINKED_STALE]" not in out and "[CONTENT_UPDATED]" not in out
    assert out.endswith("\n\n[index is behind for 1 file — run `check`]")

    口 = Step(step_num=3, name="changed-files", purpose="S tagged LINKED_STALE, its sibling untagged, as --full")
    _toml(tmp_path, "changed-files")
    out = axiom_graph_read_doc(str(tmp_path), doc_id=_DOC_ID, outline=True)
    assert _line_naming(out, f"- {_SECTION_ID} ").endswith("  [LINKED_STALE]")
    assert _line_naming(out, f"- {_OTHER_SECTION_ID} ").endswith(" chars)")
    assert _line_naming(out, f"{_DOC_ID}  Guide").endswith(_full_tag(db_path, tmp_path, _DOC_ID) or " chars)")
    assert "index is behind" not in out
    ids = [_SECTION_ID, _OTHER_SECTION_ID, _DOC_ID]
    assert stored_rows(db_path, ids) == full_recompute_rows(db_path, tmp_path, ids)

    口 = Step(step_num=4, name="Sizes", purpose="a section's outline size is what a tagged read of it spends")
    size = int(_line_naming(out, f"- {_SECTION_ID} ").split("(")[1].split(" chars")[0].replace(",", ""))
    read = axiom_graph_read_doc(str(tmp_path), section_ids=[_SECTION_ID])
    assert len(read) - read.index(f"## f  <!-- id: {_SECTION_ID}") == size


@pytest.mark.parametrize("tool", ["graph", "search", "source", "read_doc", "outline"])
def test_each_read_tool_opens_one_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    from axiom_graph.docjson.api import axiom_graph_read_doc  # noqa: PLC0415
    from axiom_graph.query.api import fetch_graph, fetch_source, search_nodes  # noqa: PLC0415

    db_path = _project(tmp_path)
    _edit_f(tmp_path)
    calls = {
        "graph": lambda: fetch_graph(db_path, _F_ID, direction="in", with_locations=True, root=tmp_path),
        "search": lambda: search_nodes(db_path, "f", root=tmp_path),
        "source": lambda: fetch_source(db_path, tmp_path, _F_ID),
        "read_doc": lambda: axiom_graph_read_doc(str(tmp_path), section_ids=[_SECTION_ID]),
        "outline": lambda: axiom_graph_read_doc(str(tmp_path), doc_id=_DOC_ID, outline=True),
    }
    opened = {"n": 0}
    real = sqlite3.connect

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    calls[tool]()
    assert opened["n"] == 1


_MOD_ID = "demo::pkg.mod"
_GRAPH_BATCH = [_F_ID, _OTHER_F_ID, _MOD_ID, "demo::pkg.mod::ghost", _SECTION_ID]
_SOURCE_BATCH = [_F_ID, _OTHER_F_ID, "demo::pkg.mod::ghost", _MOD_ID, _TEST_F_ID]


def _edit_f_and_add_g(root: Path) -> None:
    """Edit f and add a function the index lacks, so f's file carries a structural line and other0's does not."""
    (root / "pkg" / "mod.py").write_text("def f():\n    return 2\n\n\ndef g():\n    return 3\n", encoding="utf-8")


def _batch_call(tool: str, root: Path, ids: list[str]) -> str:
    from axiom_graph.query.mcp_tools import axiom_graph_graph, axiom_graph_source  # noqa: PLC0415

    if tool == "graph":
        return axiom_graph_graph(str(root), node_id="ignored", direction="in", node_ids=ids)
    return axiom_graph_source(str(root), node_id="ignored", node_ids=ids)


@pytest.mark.parametrize("tool", ["graph", "source"])
def test_a_batch_read_opens_one_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    _project(tmp_path)
    _edit_f_and_add_g(tmp_path)
    opened = {"n": 0}
    real = sqlite3.connect

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    _batch_call(tool, tmp_path, _GRAPH_BATCH if tool == "graph" else _SOURCE_BATCH)
    assert opened["n"] == 1


@workflow(
    purpose="A batch graph or source read gives, entry by entry, the text the single reads give from the same "
    "index: tags, structural lines narrowed to each entry's files, and the per-id not-found error"
)
@pytest.mark.parametrize("tool", ["graph", "source"])
def test_a_batch_read_equals_the_single_reads_joined(tmp_path: Path, tool: str) -> None:
    from axiom_graph.query.mcp_tools import axiom_graph_graph, axiom_graph_source  # noqa: PLC0415

    口 = Step(step_num=1, name="Two equal projects", purpose="the same index on two copies, f edited and g added")
    batch_root, single_root = tmp_path / "batch", tmp_path / "single"
    for root in (batch_root, single_root):
        root.mkdir()
        _project(root)
        _edit_f_and_add_g(root)
    ids = _GRAPH_BATCH if tool == "graph" else _SOURCE_BATCH

    口 = Step(step_num=2, name="Batch vs singles", purpose="one batch call on one copy, one call per id on the other")
    batch = _batch_call(tool, batch_root, ids)
    if tool == "graph":
        singles = [axiom_graph_graph(str(single_root), node_id=nid, direction="in") for nid in ids]
    else:
        singles = [axiom_graph_source(str(single_root), node_id=nid) for nid in ids]
    assert batch == "\n\n---\n\n".join(singles)

    口 = Step(
        step_num=3,
        name="What the entries show",
        purpose="f's entry is tagged; in graph it names its file's new function (source's rescan already took it in)",
    )
    assert "[CONTENT_UPDATED]" in singles[0]
    assert ("pkg/mod.py has 1 new function — run build" in singles[0]) == (tool == "graph")
    assert "run build" not in singles[1]
    assert singles[3 if tool == "graph" else 2] == "ERROR: Node 'demo::pkg.mod::ghost' not found."


@pytest.mark.parametrize("tool", ["graph", "source"])
def test_a_batch_read_without_an_index_raises_one_error(tmp_path: Path, tool: str) -> None:
    with pytest.raises(FileNotFoundError, match="No index at"):
        _batch_call(tool, tmp_path, [_F_ID, _OTHER_F_ID])


def _viz_client(root: Path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from axiom_graph.viz import server  # noqa: PLC0415

    server._PROJECT_ROOT = root
    server._DB_PATH = root / ".axiom_graph" / "graph.db"
    server._DFLOW_DB_PATH = None
    server._TEST_PATHS = []
    server._EXCLUDE_DIRS = []
    return fastapi_testclient.TestClient(server.app)


def test_viz_neighbourhood_stores_what_check_full_stores(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    client = _viz_client(tmp_path)
    _edit_f(tmp_path)
    resp = client.get(f"/api/nodes/{_F_ID}/neighborhood", params={"direction": "in"})
    assert resp.status_code == 200
    staleness = resp.json()["staleness"]
    assert staleness[_F_ID]["own_status"] == "CONTENT_UPDATED"
    assert staleness[_SECTION_ID]["link_status"] == "LINKED_STALE"
    assert_matches_full_recompute(db_path, tmp_path)


def test_viz_check_and_all_show_what_check_full_stores(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    client = _viz_client(tmp_path)
    _edit_f(tmp_path)
    statuses = client.get("/api/check").json()["statuses"]
    assert statuses[_SECTION_ID] == {"own_status": "VERIFIED", "link_status": "LINKED_STALE"}
    assert_matches_full_recompute(db_path, tmp_path)
    _toml(tmp_path, "off")
    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 3\n", encoding="utf-8")
    assert client.get("/api/all").json()["behind"] == 1


@pytest.mark.parametrize("route", ["/api/all", "/api/check", f"/api/nodes/{_F_ID}/neighborhood"])
def test_each_viz_status_route_opens_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    _project(tmp_path)
    client = _viz_client(tmp_path)
    _edit_f(tmp_path)
    opened = {"n": 0}
    real = sqlite3.connect

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting)
    assert client.get(route).status_code == 200
    assert opened["n"] == 1


@workflow(
    purpose="Under refresh_before_read = check, a read naming nodes runs the check's refresh without re-seeding the "
    "named nodes: the second identical read recomputes nothing and the stored rows match check --full"
)
def test_a_repeated_check_mode_read_recomputes_nothing(tmp_path: Path) -> None:
    db_path = _project(tmp_path)
    _edit_f(tmp_path)
    first = refresh_before_read(db_path, tmp_path, [_SECTION_ID], mode="check")
    assert first.tag(_SECTION_ID) == "  [LINKED_STALE]"
    second = refresh_before_read(db_path, tmp_path, [_SECTION_ID], mode="check")
    assert second.refresh.scope_size == 0, second.refresh
    assert second.refresh.rows_written == 0
    assert not second.refresh.files_hashed
    assert second.statuses == first.statuses
    assert_matches_full_recompute(db_path, tmp_path)
