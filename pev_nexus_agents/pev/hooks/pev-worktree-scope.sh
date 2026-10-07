#!/bin/bash
# pev-worktree-scope.sh — PreToolUse(Write|Edit) hook for PEV subagents.
# Enforces that Write/Edit calls target only files inside the worktree, or
# inside this session's Claude Code scratchpad directory (and nowhere else
# in the temp root).
#
# Active ONLY when agent_type starts with "pev:" (i.e., a PEV subagent is
# executing the tool call — not the orchestrator or other plugins' agents).
# Reads worktree_path from .pev-state.json in the subagent's cwd.

INPUT=$(cat)

# Fail closed for PEV agents when jq is missing (see lib/pev-hook-common.sh).
. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
pev_require_jq pretool

# Gate: PEV subagents only
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
case "$AGENT_TYPE" in
  pev:*) ;;
  *) exit 0 ;;
esac

# Resolve .pev-state.json (lives at cwd root — set by EnterWorktree).
# Claude Code passes cwd as a Windows path on Windows (C:\...\foo); normalize
# to POSIX so file tests and path concatenation work in git-bash.
PROJECT_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-}"
if command -v cygpath >/dev/null 2>&1; then
  PROJECT_ROOT=$(cygpath -u "$PROJECT_ROOT")
fi
STATE_FILE="$PROJECT_ROOT/.pev-state.json"

# No state file → not in a PEV cycle → allow everything
if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi

# Read worktree path from state
WORKTREE_PATH=$(cat "$STATE_FILE" | jq -r '.worktree_path // empty' 2>/dev/null)

# No worktree_path in state → no constraint to enforce → allow
if [ -z "$WORKTREE_PATH" ]; then
  exit 0
fi

# Normalize to POSIX format (Windows paths: C:/... → /c/...)
normalize() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$1"
  else
    echo "$1"
  fi
}

# Resolve to absolute path (handles trailing slashes, symlinks)
WORKTREE_PATH=$(normalize "$WORKTREE_PATH")
WORKTREE_PATH=$(cd "$WORKTREE_PATH" 2>/dev/null && pwd -P)
if [ -z "$WORKTREE_PATH" ]; then
  echo "BLOCKED: worktree_path in pev-state.json does not exist on disk" >&2
  exit 2
fi

# Extract file_path from tool input
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Normalize to POSIX format
FILE_PATH=$(normalize "$FILE_PATH")

# Resolve file_path to absolute (it may already be absolute)
case "$FILE_PATH" in
  /*) ;; # already absolute
  *)  FILE_PATH="$(normalize "$(echo "$INPUT" | jq -r '.cwd // empty')")/$FILE_PATH" ;;
esac

# Normalize: resolve .. and symlinks in the directory portion
FILE_DIR=$(dirname "$FILE_PATH")
FILE_BASE=$(basename "$FILE_PATH")
if [ -d "$FILE_DIR" ]; then
  FILE_PATH="$(cd "$FILE_DIR" && pwd -P)/$FILE_BASE"
else
  CHECK_DIR="$FILE_DIR"
  while [ ! -d "$CHECK_DIR" ] && [ "$CHECK_DIR" != "/" ]; do
    CHECK_DIR=$(dirname "$CHECK_DIR")
  done
  if [ -d "$CHECK_DIR" ]; then
    RESOLVED=$(cd "$CHECK_DIR" && pwd -P)
    REMAINDER="${FILE_DIR#$CHECK_DIR}"
    FILE_PATH="${RESOLVED}${REMAINDER}/$FILE_BASE"
  fi
fi

# The session scratchpad is the one place outside the worktree a PEV agent
# may write: Claude Code tells agents to keep temporary files there. Its
# shape is <temp root>/claude[-<uid>]/<project slug>/<session_id>/scratchpad,
# so only this session's scratchpad matches, never the temp root or another
# session's directory.
in_session_scratchpad() {
  local sid root rest
  sid=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null | tr -d '\r')
  [[ "$sid" =~ ^[A-Za-z0-9_-]+$ ]] || return 1
  for root in "${TMPDIR:-}" "${TEMP:-}" "${TMP:-}" /tmp; do
    [ -n "$root" ] || continue
    root=$(normalize "$root")
    root=$(cd "$root" 2>/dev/null && pwd -P) || continue
    [ -n "$root" ] || continue
    rest="${FILE_PATH#"$root"/}"
    [ "$rest" = "$FILE_PATH" ] && continue
    case "/$rest/" in */../*|*/./*) return 1 ;; esac
    [[ "$rest" =~ ^claude(-[0-9]+)?/[^/]+/${sid}/scratchpad/[^/] ]] && return 0
  done
  return 1
}

# Check: file_path must start with worktree_path
case "$FILE_PATH" in
  "$WORKTREE_PATH"/*) exit 0 ;;
  "$WORKTREE_PATH")   exit 0 ;;
esac
in_session_scratchpad && exit 0
echo "BLOCKED: Write/Edit target '$FILE_PATH' is outside the worktree '$WORKTREE_PATH'" >&2
exit 2
