"""Static validation of @workflow / @task / Step / AutoStep annotations.

Rules:
    A1 — step_num must be a positive int/float
    A2 — Step(name, purpose) both non-empty
    A3 — AutoStep arg shape valid (name, if provided, must be non-empty str)
    B1 — no duplicate step_num within one envelope
    B2 — major step numbers form 1, 2, 3, ... (no gaps)
    B3 — non-integer step_num must be inside an enclosing for/while loop
    B4 — AutoStep must be immediately followed by a call to a @task/@workflow
         decorated function.  Produces two sub-findings: "undecorated target"
         vs "unresolved target" (target not in any scanned module).
    C1 — @workflow / @task `purpose` arg non-empty

All findings are WARNING severity.  Validators never raise.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Iterable

from axiom_annotations.validation import (
    validate_autostep_args,
    validate_step_args,
    validate_step_num,
)
from axiom_annotations.exceptions import StepValidationError


SEVERITY_WARNING = "WARNING"
SEVERITY_IMPORTANT = "IMPORTANT"


@dataclass
class ValidationFinding:
    """A single annotation rule violation."""

    rule_id: str
    severity: str  # always "WARNING"
    module: str  # rel_path e.g. "pkg/sub/file.py"
    function: str  # envelope function name
    line: int
    message: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "module": self.module,
            "function": self.function,
            "line": self.line,
            "message": self.message,
        }


@dataclass
class AutoStepRecord:
    """Per-envelope record of an AutoStep and its resolved target (if any)."""

    module: str
    function: str
    line: int
    step_num: Any
    # Target call info (next-statement analysis).
    #: Short name of the called function (e.g. "do_thing" or "self.do_thing").
    target_name: str | None = None
    #: Fully-qualified node ID resolved via name_map (if intra-project call).
    target_node_id: str | None = None
    #: Whether the line after the AutoStep was a direct call statement.
    has_next_call: bool = False


# ---------------------------------------------------------------------------
# Intra-envelope validators (run inline during scan)
# ---------------------------------------------------------------------------


def _is_minor(step_num: Any) -> bool:
    """Return True if step_num has a nonzero fractional part."""
    try:
        return isinstance(step_num, float) and (step_num != int(step_num))
    except (TypeError, ValueError):
        return False


def _major(step_num: Any) -> int | None:
    """Return the integer part of step_num, or None if not numeric."""
    if isinstance(step_num, (int, float)):
        return int(step_num)
    return None


def validate_envelope(
    *,
    rel_path: str,
    func_name: str,
    func_node: ast.FunctionDef | ast.AsyncFunctionDef | None,
    envelope_kind: str,  # "workflow" | "task"
    envelope_purpose: str | None,
    envelope_line: int,
    step_calls: list[dict],
    is_rule_enabled=lambda rid: True,
) -> list[ValidationFinding]:
    """Run A1–A3, B1–B3, C1 against one envelope.

    Args:
        rel_path: Repo-relative file path.
        func_name: Enclosing function name.
        func_node: AST node of the decorated function.  Retained for
            historical context; B3 now consumes the precomputed ``in_loop``
            flag uniformly across languages, so this argument is optional.
        envelope_kind: "workflow" or "task".
        envelope_purpose: Purpose arg on the decorator (may be empty).
        envelope_line: Line of the decorator / function def.
        step_calls: List of dicts, one per Step/AutoStep call discovered by
            the scanner.  Required keys: ``step_num_value`` (parsed value:
            int/float/str/None), ``name`` (str), ``purpose`` (str),
            ``is_auto`` (bool), ``line`` (int), ``in_loop`` (bool — required
            for minor step_num B3 enforcement; defaults to False if absent).
        is_rule_enabled: Callable(rule_id) -> bool.  Findings are filtered
            through this guard; disabled rules are dropped.

    Returns:
        List of findings.  Never raises.
    """
    findings: list[ValidationFinding] = []

    def _emit(rule_id: str, line: int, msg: str) -> None:
        if not is_rule_enabled(rule_id):
            return
        findings.append(
            ValidationFinding(
                rule_id=rule_id,
                severity=SEVERITY_WARNING,
                module=rel_path,
                function=func_name,
                line=line,
                message=msg,
            )
        )

    # ------------------------------------------------------------------
    # C1 — envelope purpose must be non-empty
    # ------------------------------------------------------------------
    if envelope_purpose is None or not str(envelope_purpose).strip():
        _emit(
            "C1",
            envelope_line,
            f"@{envelope_kind} {func_name!r} has empty or missing 'purpose'",
        )

    # ------------------------------------------------------------------
    # A1/A2/A3 — per-marker argument validation
    # ------------------------------------------------------------------
    for sc in step_calls:
        step_num = sc.get("step_num_value")
        line = sc.get("line", envelope_line)
        is_auto = bool(sc.get("is_auto"))
        name = sc.get("name")
        purpose = sc.get("purpose")
        # A1 — step_num valid
        try:
            validate_step_num(step_num)
        except StepValidationError as exc:
            _emit("A1", line, f"invalid step_num: {exc}")
            # If step_num itself is bad, skip A2/A3 (they piggyback on it)
            continue
        # A2 — Step requires non-empty name+purpose
        if not is_auto:
            try:
                validate_step_args(step_num, name, purpose)
            except StepValidationError as exc:
                _emit("A2", line, f"Step({step_num}) invalid args: {exc}")
        # A3 — AutoStep arg shape
        else:
            try:
                validate_autostep_args(step_num, name if name else None)
            except StepValidationError as exc:
                _emit("A3", line, f"AutoStep({step_num}) invalid args: {exc}")

    # ------------------------------------------------------------------
    # B1 — no duplicate step_num within the envelope
    # ------------------------------------------------------------------
    seen: dict[Any, int] = {}  # step_num -> first line
    for sc in step_calls:
        step_num = sc.get("step_num_value")
        if step_num is None:
            continue
        if step_num in seen:
            _emit(
                "B1",
                sc.get("line", envelope_line),
                f"duplicate step_num {step_num!r} in {func_name!r} (first seen at line {seen[step_num]})",
            )
        else:
            seen[step_num] = sc.get("line", envelope_line)

    # ------------------------------------------------------------------
    # B2 — major step numbers form 1..N contiguously
    # ------------------------------------------------------------------
    majors: set[int] = set()
    for sc in step_calls:
        step_num = sc.get("step_num_value")
        m = _major(step_num)
        if m is not None and m > 0:
            majors.add(m)
    if majors:
        max_major = max(majors)
        missing = [i for i in range(1, max_major + 1) if i not in majors]
        if missing:
            _emit(
                "B2",
                envelope_line,
                f"major step sequence has gaps in {func_name!r}: missing {missing}, have {sorted(majors)}",
            )

    # ------------------------------------------------------------------
    # B3 — minor step_num (fractional) must be inside for/while
    # ------------------------------------------------------------------
    # Each scanner (Python AST, tree-sitter JS/TS, ...) is responsible for
    # computing ``in_loop`` from its own parser's loop ancestry and putting
    # it on the step_call dict.  B3 here just consumes the flag.
    for sc in step_calls:
        step_num = sc.get("step_num_value")
        if not _is_minor(step_num):
            continue
        inside_loop = bool(sc.get("in_loop", False))
        if not inside_loop:
            _emit(
                "B3",
                sc.get("line", envelope_line),
                f"minor step_num {step_num!r} in {func_name!r} is not inside "
                f"a for/while loop — minor step numbers require loop enclosure",
            )

    return findings


# ---------------------------------------------------------------------------
# B4 — cross-envelope AutoStep target resolution (deferred pass)
# ---------------------------------------------------------------------------


def validate_autostep_targets(
    autosteps: Iterable[AutoStepRecord],
    *,
    envelope_node_ids: set[str],
    is_rule_enabled=lambda rid: True,
) -> list[ValidationFinding]:
    """Resolve AutoStep targets against envelope registry (B4, deferred pass).

    Emits two sub-findings:
      * "undecorated target" — target function found in an indexed module but
        has no envelope (not @task/@workflow decorated).
      * "unresolved target" — target name couldn't be resolved to any indexed
        function (external module, dynamic dispatch, etc.).

    Args:
        autosteps: Records produced during scan.
        envelope_node_ids: Set of node IDs for which an envelope was emitted
            (i.e. `@workflow` or `@task` decorated functions).
        is_rule_enabled: rule filter callback.

    Returns:
        List of findings.
    """
    findings: list[ValidationFinding] = []

    if not is_rule_enabled("B4"):
        return findings

    for rec in autosteps:
        if not rec.has_next_call:
            findings.append(
                ValidationFinding(
                    rule_id="B4",
                    severity=SEVERITY_WARNING,
                    module=rec.module,
                    function=rec.function,
                    line=rec.line,
                    message=(
                        f"AutoStep({rec.step_num}) is not followed by a direct call statement (unresolved target)"
                    ),
                )
            )
            continue

        if rec.target_node_id is None:
            # No intra-project resolution — external or dynamic call.
            findings.append(
                ValidationFinding(
                    rule_id="B4",
                    severity=SEVERITY_WARNING,
                    module=rec.module,
                    function=rec.function,
                    line=rec.line,
                    message=(
                        f"AutoStep({rec.step_num}) target "
                        f"{rec.target_name!r} is unresolved "
                        f"(not a known intra-project function)"
                    ),
                )
            )
            continue

        if rec.target_node_id not in envelope_node_ids:
            findings.append(
                ValidationFinding(
                    rule_id="B4",
                    severity=SEVERITY_WARNING,
                    module=rec.module,
                    function=rec.function,
                    line=rec.line,
                    message=(
                        f"AutoStep({rec.step_num}) target "
                        f"{rec.target_name!r} is undecorated "
                        f"(found in index but has no @task/@workflow envelope)"
                    ),
                )
            )

    return findings


__all__ = [
    "ValidationFinding",
    "AutoStepRecord",
    "validate_envelope",
    "validate_autostep_targets",
    "SEVERITY_WARNING",
    "SEVERITY_IMPORTANT",
]
