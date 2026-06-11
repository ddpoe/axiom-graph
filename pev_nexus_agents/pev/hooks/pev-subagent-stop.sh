#!/bin/bash
# pev-subagent-stop.sh — SubagentStop hook for every PEV subagent.
# Runs when a PEV subagent returns. Two jobs:
#   1. Clean up the subagent's counter file (/tmp/pev-counter-<agent_id>.txt).
#   2. For the Builder: rebuild the axiom-graph index on the worktree DB so
#      the Reviewer starts with a fresh index.
#
# Active ONLY when agent_type starts with "pev:". No-op for other
# subagents.

INPUT=$(cat)

AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty' 2>/dev/null)
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null)

case "$AGENT_TYPE" in
  pev:*) ;;
  *) exit 0 ;;
esac

# 1. Clean up counter file (best-effort)
if [ -n "$AGENT_ID" ]; then
  rm -f "/tmp/pev-counter-${AGENT_ID}.txt" 2>/dev/null
else
  rm -f "/tmp/pev-counter-${AGENT_TYPE//:/-}.txt" 2>/dev/null
fi

# 2. Builder-only: refresh axiom-graph index on the worktree DB.
if [ "$AGENT_TYPE" = "pev:pev-builder" ]; then
  PROJECT_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
  [ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-}"
  if command -v cygpath >/dev/null 2>&1; then
    PROJECT_ROOT=$(cygpath -u "$PROJECT_ROOT")
  fi
  STATE_FILE="$PROJECT_ROOT/.pev-state.json"

  if [ -f "$STATE_FILE" ]; then
    WORKTREE_PATH=$(cat "$STATE_FILE" | jq -r '.worktree_path // empty' 2>/dev/null)
    if [ -n "$WORKTREE_PATH" ]; then
      if command -v cygpath >/dev/null 2>&1; then
        WORKTREE_PATH=$(cygpath -u "$WORKTREE_PATH")
      fi
      if command -v axiom-graph >/dev/null 2>&1; then
        axiom-graph build --project-root "$WORKTREE_PATH" >&2 || \
          echo "WARN: axiom-graph build failed on $WORKTREE_PATH; Reviewer may see stale index" >&2
      else
        echo "WARN: axiom-graph CLI not found on PATH; skipping post-Builder rebuild" >&2
      fi
    fi
  fi
fi

exit 0
