"""Staleness status constants -- single source of truth for all status names."""

# own_status values (content vs verified baseline, hash-based)
VERIFIED = "VERIFIED"
CONTENT_UPDATED = "CONTENT_UPDATED"
DESC_UPDATED = "DESC_UPDATED"
# RENAMED: this node's identity moved (a scoped-similarity rename was applied).
# More significant than an in-place content edit (identity changed) but less
# alarming than NOT_FOUND (history + edges were migrated, not orphaned).
# Cleared by mark_clean like any other own-dimension status. Slotted between
# CONTENT_UPDATED and NOT_FOUND in the severity ladder.
RENAMED = "RENAMED"
NOT_FOUND = "NOT_FOUND"

# link_status values (dependency health, timestamp-based)
LINKED_STALE = "LINKED_STALE"
BROKEN_LINK = "BROKEN_LINK"

# History transition events -- own dimension
BECAME_CONTENT_UPDATED = "BECAME_CONTENT_UPDATED"
BECAME_DESC_UPDATED = "BECAME_DESC_UPDATED"
BECAME_NOT_FOUND = "BECAME_NOT_FOUND"
BECAME_RENAMED = "BECAME_RENAMED"
BECAME_VERIFIED = "BECAME_VERIFIED"

# History transition events -- link dimension
BECAME_LINKED_STALE = "BECAME_LINKED_STALE"
BECAME_BROKEN_LINK = "BECAME_BROKEN_LINK"
LINK_BECAME_VERIFIED = "LINK_BECAME_VERIFIED"

# History change_type (NOT an own_status): records that a lost node fell back
# to exact-hash matching in a degraded scope (pool cap exceeded / no git) and
# found no exact match, so its resulting NOT_FOUND may be an undetected rename.
# Rides the existing node_history.change_type + meta columns (no new schema).
RENAME_SCORING_SKIPPED = "RENAME_SCORING_SKIPPED"

# Severity orderings (own dimension: lower index = less severe)
OWN_SEVERITY = {VERIFIED: 0, DESC_UPDATED: 1, CONTENT_UPDATED: 2, RENAMED: 3, NOT_FOUND: 4}

# Severity orderings (link dimension)
LINK_SEVERITY = {VERIFIED: 0, LINKED_STALE: 1, BROKEN_LINK: 2}

# Sets for quick membership checks
OWN_PROBLEM_STATUSES = {CONTENT_UPDATED, DESC_UPDATED, RENAMED, NOT_FOUND}
LINK_PROBLEM_STATUSES = {LINKED_STALE, BROKEN_LINK}
