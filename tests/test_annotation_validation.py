"""Tier-2 tests for axiom-graph.scanners.annotation_validation.

One test per rule class. A1/A2/A3 combined into a single happy+violation
test per pitch guidance.
"""

from __future__ import annotations

import ast
import textwrap

from axiom_annotations import workflow as _wf  # noqa: F401  (import sanity)
from axiom_graph.workflows.validation import (
    AutoStepRecord,
    validate_autostep_targets,
    validate_envelope,
)


def _build_fixture(src: str):
    src = textwrap.dedent(src)
    mod = ast.parse(src)
    func = mod.body[0]
    return func


def _calls_inside_loops(func_node) -> set[int]:
    """Return ids of Call nodes nested under any for/while loop in *func_node*."""
    inside: set[int] = set()
    for node in ast.walk(func_node):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            for descendant in ast.walk(node):
                if isinstance(descendant, ast.Call):
                    inside.add(id(descendant))
    return inside


def _step_calls_from(func_node):
    """Extract Step/AutoStep call dicts mirroring scanner output (incl. in_loop)."""
    in_loop_ids = _calls_inside_loops(func_node)
    out = []
    for stmt in ast.walk(func_node):
        if not isinstance(stmt, ast.Call):
            continue
        fn = stmt.func
        if not (isinstance(fn, ast.Name) and fn.id in ("Step", "AutoStep")):
            continue
        kwargs = {}
        for kw in stmt.keywords:
            if kw.arg is None:
                continue
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, TypeError):
                kwargs[kw.arg] = None
        out.append(
            {
                "step_num_value": kwargs.get("step_num"),
                "name": kwargs.get("name"),
                "purpose": kwargs.get("purpose"),
                "is_auto": fn.id == "AutoStep",
                "line": stmt.lineno,
                "in_loop": id(stmt) in in_loop_ids,
            }
        )
    return out


# -------------------------------------------------------------------------
# B1 — duplicate step_num
# -------------------------------------------------------------------------


def test_b1_duplicate_step_num_emits_finding():
    src = """
    def do_work():
        Step(step_num=1, name='a', purpose='p')
        Step(step_num=1, name='b', purpose='q')
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="do_work",
        func_node=fn,
        envelope_kind="task",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    b1 = [f for f in findings if f.rule_id == "B1"]
    assert len(b1) == 1
    assert b1[0].module == "x.py"
    assert b1[0].function == "do_work"


# -------------------------------------------------------------------------
# B2 — gap in major step sequence
# -------------------------------------------------------------------------


def test_b2_major_step_gap_emits_finding():
    src = """
    def f():
        Step(step_num=1, name='a', purpose='p')
        Step(step_num=3, name='b', purpose='q')
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="workflow",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    b2 = [f for f in findings if f.rule_id == "B2"]
    assert len(b2) == 1
    assert "missing" in b2[0].message.lower()


def test_b2_contiguous_majors_ok_even_with_minors():
    # 1, 2, 2.1, 2.2, 3 is valid
    src = """
    def f():
        Step(step_num=1, name='a', purpose='p')
        Step(step_num=2, name='b', purpose='q')
        for _ in []:
            Step(step_num=2.1, name='c', purpose='r')
            Step(step_num=2.2, name='d', purpose='s')
        Step(step_num=3, name='e', purpose='t')
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="workflow",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    assert [f for f in findings if f.rule_id == "B2"] == []


# -------------------------------------------------------------------------
# B3 — minor step not inside loop
# -------------------------------------------------------------------------


def test_b3_minor_step_outside_loop_emits_finding():
    src = """
    def f():
        Step(step_num=1.1, name='a', purpose='p')
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="task",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    b3 = [f for f in findings if f.rule_id == "B3"]
    assert len(b3) == 1


def test_b3_minor_step_inside_loop_ok():
    src = """
    def f():
        for _ in []:
            Step(step_num=1.1, name='a', purpose='p')
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="task",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    assert [f for f in findings if f.rule_id == "B3"] == []


def test_b3_in_loop_flag_path_outside_loop_emits_finding():
    """Language-agnostic path: ``in_loop`` flag drives B3 when no AST."""
    findings = validate_envelope(
        rel_path="x.ts",
        func_name="f",
        func_node=None,
        envelope_kind="task",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=[
            {
                "step_num_value": 1.1,
                "name": "a",
                "purpose": "p",
                "is_auto": False,
                "line": 3,
                "in_loop": False,
            }
        ],
    )
    b3 = [f for f in findings if f.rule_id == "B3"]
    assert len(b3) == 1


def test_b3_in_loop_flag_path_inside_loop_ok():
    """Language-agnostic path: ``in_loop=True`` suppresses B3."""
    findings = validate_envelope(
        rel_path="x.ts",
        func_name="f",
        func_node=None,
        envelope_kind="task",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=[
            {
                "step_num_value": 1.1,
                "name": "a",
                "purpose": "p",
                "is_auto": False,
                "line": 3,
                "in_loop": True,
            }
        ],
    )
    assert [f for f in findings if f.rule_id == "B3"] == []


# -------------------------------------------------------------------------
# C1 — empty purpose
# -------------------------------------------------------------------------


def test_c1_empty_purpose_emits_finding():
    src = """
    def f():
        pass
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="task",
        envelope_purpose="",
        envelope_line=1,
        step_calls=[],
    )
    c1 = [f for f in findings if f.rule_id == "C1"]
    assert len(c1) == 1


def test_c1_valid_purpose_no_finding():
    src = """
    def f():
        pass
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="task",
        envelope_purpose="does something",
        envelope_line=1,
        step_calls=[],
    )
    assert [f for f in findings if f.rule_id == "C1"] == []


# -------------------------------------------------------------------------
# A-group happy path + violation (combined per pitch guidance)
# -------------------------------------------------------------------------


def test_a_group_valid_and_invalid_cases():
    # Happy path — valid Step + AutoStep
    src = """
    def f():
        Step(step_num=1, name='a', purpose='p')
        AutoStep(step_num=2)
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="workflow",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    assert [f for f in findings if f.rule_id.startswith("A")] == []

    # A1 violation — step_num is a string
    src_bad = """
    def f():
        Step(step_num='oops', name='a', purpose='p')
    """
    fn = _build_fixture(src_bad)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="workflow",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    a1 = [f for f in findings if f.rule_id == "A1"]
    assert len(a1) == 1

    # A2 violation — Step with empty name
    src_bad2 = """
    def f():
        Step(step_num=1, name='', purpose='p')
    """
    fn = _build_fixture(src_bad2)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="workflow",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
    )
    assert [f for f in findings if f.rule_id == "A2"]


# -------------------------------------------------------------------------
# B4 — AutoStep target resolution
# -------------------------------------------------------------------------


def test_b4_undecorated_target():
    """AutoStep followed by call to indexed-but-undecorated function."""
    rec = AutoStepRecord(
        module="x.py",
        function="f",
        line=10,
        step_num=1,
        target_name="helper",
        target_node_id="proj::mod::helper",
        has_next_call=True,
    )
    findings = validate_autostep_targets(
        [rec],
        envelope_node_ids=set(),  # helper has no envelope
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "B4"
    assert "undecorated" in findings[0].message.lower()


def test_b4_decorated_target_ok():
    rec = AutoStepRecord(
        module="x.py",
        function="f",
        line=10,
        step_num=1,
        target_name="helper",
        target_node_id="proj::mod::helper@task",
        has_next_call=True,
    )
    findings = validate_autostep_targets(
        [rec],
        envelope_node_ids={"proj::mod::helper@task"},
    )
    assert findings == []


def test_b4_unresolved_target():
    """AutoStep followed by call to external/dynamic target."""
    rec = AutoStepRecord(
        module="x.py",
        function="f",
        line=10,
        step_num=1,
        target_name="dynamic",
        target_node_id=None,  # not in name_map
        has_next_call=True,
    )
    findings = validate_autostep_targets(
        [rec],
        envelope_node_ids=set(),
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "B4"
    assert "unresolved" in findings[0].message.lower()


# -------------------------------------------------------------------------
# Config gating
# -------------------------------------------------------------------------


def test_per_rule_disable_suppresses_only_that_rule():
    # B3 would fire here, but disable it
    src = """
    def f():
        Step(step_num=1.1, name='a', purpose='p')
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="task",
        envelope_purpose="valid",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
        is_rule_enabled=lambda rid: rid != "B3",
    )
    assert [f for f in findings if f.rule_id == "B3"] == []


def test_master_disable_suppresses_all():
    src = """
    def f():
        Step(step_num=1.1, name='a', purpose='p')
    """
    fn = _build_fixture(src)
    findings = validate_envelope(
        rel_path="x.py",
        func_name="f",
        func_node=fn,
        envelope_kind="task",
        envelope_purpose="",
        envelope_line=1,
        step_calls=_step_calls_from(fn),
        is_rule_enabled=lambda rid: False,
    )
    assert findings == []


def test_config_unknown_rule_key_raises():
    """ValidationConfig loads rejects unknown rule ids in axiom-graph.toml."""
    import tempfile
    from pathlib import Path
    from axiom_graph.config import AxiomGraphConfig, ConfigError

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "axiom-graph.toml").write_text("[axiom_graph.validation.rules]\nZZ = true\n")
        try:
            AxiomGraphConfig.load(root)
        except ConfigError:
            pass
        else:
            raise AssertionError("expected ConfigError for unknown rule id")
