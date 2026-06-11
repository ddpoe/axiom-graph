"""Discovery-only staleness contract — regression prevention.

Tier 3 flagship + Tier 1 unit tests.

Covers:
- Init → edit body → build → check → CONTENT_UPDATED
- Init → edit docstring only → DESC_UPDATED
- Both changed → CONTENT_UPDATED (not CLEAN)
- discovery_only upsert does not update mtime
- discovery_only upsert does not update hashes
- Line shift (add function above) → level_3_location updates, staleness stays CLEAN
"""

from __future__ import annotations

import time
from pathlib import Path

from axiom_annotations import workflow, Step


def _bump_mtime():
    """Ensure filesystem mtime advances past the staleness fast-path threshold."""
    time.sleep(0.05)


from axiom_graph.index import builder, db
from axiom_graph.index.staleness import compute_staleness


# ---------------------------------------------------------------------------
# Tier 3 — Init → edit body → build → check → CONTENT_UPDATED
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify the discovery_only contract end-to-end: a code body edit "
        "after initial build is detected as CONTENT_UPDATED by axiom-graph check."
    ),
)
def test_init_edit_build_detects_content_stale(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="Write initial Python file",
        purpose="Create a .py file with a function to establish the hash baseline",
        outputs="mod.py with greet() returning 'hello'",
    )
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )

    口 = Step(
        step_num=2,
        name="Run initial full build",
        purpose="Scan and upsert with discovery_only=False to set code_hash/desc_hash baseline",
        outputs="Node in DB with known code_hash and desc_hash",
    )
    result = builder.build(mini_project, project_id="proj", discovery_only=False)
    assert result["nodes_written"] > 0

    口 = Step(
        step_num=3,
        name="Edit function body only",
        purpose="Change the return value but keep the docstring identical — triggers CONTENT_UPDATED",
    )
    _bump_mtime()
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )

    口 = Step(
        step_num=4,
        name="Run discovery-only build",
        purpose="Re-scan with discovery_only=True — new nodes discovered but existing hashes preserved",
    )
    builder.build(mini_project, project_id="proj", discovery_only=True)

    口 = Step(
        step_num=5,
        name="Compute staleness and assert CONTENT_UPDATED",
        purpose="The stored code_hash (from step 2) differs from the current file — CONTENT_UPDATED",
        outputs="greet() node status == CONTENT_UPDATED",
    )
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    func_nodes = [n for n in nodes if n.title == "greet"]
    assert len(func_nodes) == 1
    assert statuses[func_nodes[0].id][0] == "CONTENT_UPDATED"


# ---------------------------------------------------------------------------
# Tier 1 — Init → edit docstring only → DESC_UPDATED
# ---------------------------------------------------------------------------


def test_init_edit_build_detects_desc_stale(mini_project: Path, db_path: Path):
    """Init → edit docstring only → DESC_UPDATED."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Change only the docstring — bump mtime so staleness engine doesn't fast-path
    _bump_mtime()
    src.write_text(
        'def greet():\n    """Say goodbye."""\n    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=True)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    func_nodes = [n for n in nodes if n.title == "greet"]
    assert len(func_nodes) == 1
    assert statuses[func_nodes[0].id][0] == "DESC_UPDATED"


# ---------------------------------------------------------------------------
# Tier 1 — Both changed → CONTENT_UPDATED (not CLEAN)
# ---------------------------------------------------------------------------


def test_init_edit_build_both_changed_content_stale(mini_project: Path, db_path: Path):
    """When both body and docstring change, emit CONTENT_UPDATED (not CLEAN)."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Change both body AND docstring
    _bump_mtime()
    src.write_text(
        'def greet():\n    """Say goodbye."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=True)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    func_nodes = [n for n in nodes if n.title == "greet"]
    assert len(func_nodes) == 1
    assert statuses[func_nodes[0].id][0] == "CONTENT_UPDATED"


# ---------------------------------------------------------------------------
# Tier 1 — discovery_only upsert does not update mtime
# ---------------------------------------------------------------------------


def test_discovery_only_upsert_does_not_update_mtime(mini_project: Path, db_path: Path):
    """Direct DB assertion: file_mtime is unchanged after discovery_only upsert."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Record the stored mtime after initial build
    stored_mtime_before = db.get_file_mtime(db_path, "mod.py")
    assert stored_mtime_before is not None

    # Edit the file (changes its OS mtime)
    _bump_mtime()
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=True)

    # Stored mtime should be unchanged — discovery_only preserves baseline
    stored_mtime_after = db.get_file_mtime(db_path, "mod.py")
    assert stored_mtime_after == stored_mtime_before


# ---------------------------------------------------------------------------
# Tier 1 — discovery_only upsert does not update hashes
# ---------------------------------------------------------------------------


def test_discovery_only_upsert_does_not_update_hashes(mini_project: Path, db_path: Path):
    """Direct DB assertion: code_hash and desc_hash are unchanged after discovery_only upsert."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Record stored hashes
    nodes_before = db.all_nodes(db_path)
    func_before = [n for n in nodes_before if n.title == "greet"][0]
    code_hash_before = func_before.code_hash
    desc_hash_before = func_before.desc_hash

    # Edit both body and docstring
    _bump_mtime()
    src.write_text(
        'def greet():\n    """Say goodbye."""\n    return "goodbye"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=True)

    # Stored hashes should be unchanged — discovery_only preserves baseline
    nodes_after = db.all_nodes(db_path)
    func_after = [n for n in nodes_after if n.title == "greet"][0]
    assert func_after.code_hash == code_hash_before
    assert func_after.desc_hash == desc_hash_before


# ---------------------------------------------------------------------------
# Tier 1 — Line shift: add function above → level_3_location updates, CLEAN
# ---------------------------------------------------------------------------


def test_line_shift_updates_location_stays_clean(mini_project: Path, db_path: Path):
    """Adding a function above an existing one shifts line numbers.

    discovery_only build should update level_3_location (structural metadata)
    while preserving code_hash/desc_hash baselines.  compute_staleness should
    report CLEAN because the function body is unchanged.
    """
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Record baseline location
    nodes_before = db.all_nodes(db_path)
    func_before = [n for n in nodes_before if n.title == "greet"][0]
    loc_before = func_before.level_3_location
    hash_before = func_before.code_hash

    # Add a new function ABOVE greet — shifts greet's line numbers
    _bump_mtime()
    src.write_text(
        "def helper():\n"
        '    """I push greet down."""\n'
        "    return 42\n"
        "\n"
        "\n"
        "def greet():\n"
        '    """Say hello."""\n'
        '    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=True)

    # level_3_location should have shifted
    nodes_after = db.all_nodes(db_path)
    func_after = [n for n in nodes_after if n.title == "greet"][0]
    assert func_after.level_3_location != loc_before, "level_3_location should update when lines shift"

    # code_hash baseline preserved
    assert func_after.code_hash == hash_before

    # Staleness should be CLEAN — content didn't change
    statuses = compute_staleness(db_path, mini_project, nodes_after)
    assert statuses[func_after.id] == ("VERIFIED", "VERIFIED", [])

    # The new function should also be discovered
    helper_nodes = [n for n in nodes_after if n.title == "helper"]
    assert len(helper_nodes) == 1


# ---------------------------------------------------------------------------
# Tier 1 — axiom_graph_source auto-rescans stale file to return correct lines
# ---------------------------------------------------------------------------


def test_axiom_graph_source_auto_rescan_on_line_shift(mini_project: Path, db_path: Path):
    """axiom_graph_source should return correct source even when lines shifted
    since the last build, by auto-rescanning the file when mtime changed."""
    from axiom_graph.index.builder import rescan_file_if_needed as _rescan_file_if_needed

    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Record baseline
    node_before = [n for n in db.all_nodes(db_path) if n.title == "greet"][0]
    loc_before = node_before.level_3_location

    # Add a function above — shifts greet's lines, do NOT run build
    _bump_mtime()
    src.write_text(
        "def helper():\n"
        '    """I push greet down."""\n'
        "    return 42\n"
        "\n"
        "\n"
        "def greet():\n"
        '    """Say hello."""\n'
        '    return "hello"\n',
        encoding="utf-8",
    )

    # The node in DB still has old level_3_location
    stale_node = db.get_node(db_path, node_before.id)
    assert stale_node.level_3_location == loc_before

    # Rescan should detect mtime change and update
    rescanned = _rescan_file_if_needed(db_path, mini_project, stale_node)
    assert rescanned, "Should have rescanned — file mtime changed"

    # Re-fetch: level_3_location should now point to the new line range
    updated_node = db.get_node(db_path, node_before.id)
    assert updated_node.level_3_location != loc_before, "level_3_location should update after auto-rescan"

    # Read the actual source at the updated location
    all_lines = src.read_text(encoding="utf-8").splitlines()
    loc = updated_node.level_3_location
    _file_part, line_part = loc.split("#L", 1)
    line_part = line_part.replace("L", "")
    start_str, end_str = line_part.split("-", 1)
    start, end = int(start_str), int(end_str)
    body = "\n".join(all_lines[start - 1 : end])
    assert "def greet()" in body
    assert 'return "hello"' in body
