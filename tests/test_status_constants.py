"""Tests for centralized status constants module.

Verifies that all staleness-related constants are defined and consistent,
severity orderings are correct, and problem status sets are accurate.
"""

from __future__ import annotations

from axiom_graph.index.status import (
    VERIFIED,
    CONTENT_UPDATED,
    DESC_UPDATED,
    RENAMED,
    NOT_FOUND,
    LINKED_STALE,
    BROKEN_LINK,
    BECAME_CONTENT_UPDATED,
    BECAME_DESC_UPDATED,
    BECAME_NOT_FOUND,
    BECAME_RENAMED,
    BECAME_VERIFIED,
    BECAME_LINKED_STALE,
    BECAME_BROKEN_LINK,
    LINK_BECAME_VERIFIED,
    OWN_SEVERITY,
    LINK_SEVERITY,
    OWN_PROBLEM_STATUSES,
    LINK_PROBLEM_STATUSES,
)


# ---------------------------------------------------------------------------
# Own-status constants
# ---------------------------------------------------------------------------


def test_own_status_values_are_strings():
    """Each own-status constant is the expected uppercase string."""
    assert VERIFIED == "VERIFIED"
    assert CONTENT_UPDATED == "CONTENT_UPDATED"
    assert DESC_UPDATED == "DESC_UPDATED"
    assert RENAMED == "RENAMED"
    assert NOT_FOUND == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Link-status constants
# ---------------------------------------------------------------------------


def test_link_status_values_are_strings():
    """Each link-status constant is the expected uppercase string."""
    assert LINKED_STALE == "LINKED_STALE"
    assert BROKEN_LINK == "BROKEN_LINK"


# ---------------------------------------------------------------------------
# Transition event constants
# ---------------------------------------------------------------------------


def test_transition_event_values():
    """Transition event constants match expected strings."""
    assert BECAME_CONTENT_UPDATED == "BECAME_CONTENT_UPDATED"
    assert BECAME_DESC_UPDATED == "BECAME_DESC_UPDATED"
    assert BECAME_NOT_FOUND == "BECAME_NOT_FOUND"
    assert BECAME_RENAMED == "BECAME_RENAMED"
    assert BECAME_VERIFIED == "BECAME_VERIFIED"
    assert BECAME_LINKED_STALE == "BECAME_LINKED_STALE"
    assert BECAME_BROKEN_LINK == "BECAME_BROKEN_LINK"
    assert LINK_BECAME_VERIFIED == "LINK_BECAME_VERIFIED"


# ---------------------------------------------------------------------------
# Severity orderings
# ---------------------------------------------------------------------------


def test_own_severity_order():
    """Own-dimension severity: VERIFIED < DESC_UPDATED < CONTENT_UPDATED < RENAMED < NOT_FOUND."""
    assert OWN_SEVERITY[VERIFIED] < OWN_SEVERITY[DESC_UPDATED]
    assert OWN_SEVERITY[DESC_UPDATED] < OWN_SEVERITY[CONTENT_UPDATED]
    assert OWN_SEVERITY[CONTENT_UPDATED] < OWN_SEVERITY[RENAMED]
    assert OWN_SEVERITY[RENAMED] < OWN_SEVERITY[NOT_FOUND]


def test_link_severity_order():
    """Link-dimension severity: VERIFIED < LINKED_STALE < BROKEN_LINK."""
    assert LINK_SEVERITY[VERIFIED] < LINK_SEVERITY[LINKED_STALE]
    assert LINK_SEVERITY[LINKED_STALE] < LINK_SEVERITY[BROKEN_LINK]


def test_severity_dicts_use_constants_as_keys():
    """Severity dicts use the same constant objects as keys."""
    assert set(OWN_SEVERITY.keys()) == {VERIFIED, DESC_UPDATED, CONTENT_UPDATED, RENAMED, NOT_FOUND}
    assert set(LINK_SEVERITY.keys()) == {VERIFIED, LINKED_STALE, BROKEN_LINK}


# ---------------------------------------------------------------------------
# Problem-status sets
# ---------------------------------------------------------------------------


def test_own_problem_statuses():
    """OWN_PROBLEM_STATUSES contains exactly the non-VERIFIED own statuses."""
    assert OWN_PROBLEM_STATUSES == {CONTENT_UPDATED, DESC_UPDATED, RENAMED, NOT_FOUND}
    assert VERIFIED not in OWN_PROBLEM_STATUSES


def test_link_problem_statuses():
    """LINK_PROBLEM_STATUSES contains exactly the non-VERIFIED link statuses."""
    assert LINK_PROBLEM_STATUSES == {LINKED_STALE, BROKEN_LINK}
    assert VERIFIED not in LINK_PROBLEM_STATUSES
