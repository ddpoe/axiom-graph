#!/bin/bash
# pev-bash-scope.sh — PreToolUse(Bash) hook for PEV subagents.
# Blocks Bash commands that cd into a directory outside the worktree.
# Prevents the anti-pattern of running pytest from the main repo with
# worktree test paths (imports the wrong code).
#
# Active ONLY when agent_type starts with "pev:" (a PEV subagent).
# Reads worktree_path from .pev-state.json in the subagent's cwd.

INPUT=$(cat)

# Gate: PEV subagents only
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty' 2>/dev/null)
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

# No state file → not in a PEV cycle → allow
if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi

WORKTREE_PATH=$(cat "$STATE_FILE" | jq -r '.worktree_path // empty' 2>/dev/null)

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

WORKTREE_PATH=$(normalize "$WORKTREE_PATH")
WORKTREE_PATH=$(cd "$WORKTREE_PATH" 2>/dev/null && pwd -P)
if [ -z "$WORKTREE_PATH" ]; then
  exit 0
fi

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

if [ -z "$COMMAND" ]; then
  exit 0
fi

# Extract the cd target from the command start.
# Matches: "cd /some/path && ...", "cd /some/path;", "cd /some/path"
# Does NOT match: "git -C /path" (not a cd — ignore)
# Use sed (POSIX) instead of grep -oP — PCRE requires UTF-8 locale which isn't
# always available in hook execution environments.
CD_TARGET=$(echo "$COMMAND" | sed -n 's/^[[:space:]]*cd[[:space:]]\+\([^[:space:];&]\+\).*/\1/p')

if [ -z "$CD_TARGET" ]; then
  exit 0
fi

CD_TARGET=$(normalize "$CD_TARGET")
case "$CD_TARGET" in
  /*) ;;
  *)  CD_TARGET="$(normalize "$(echo "$INPUT" | jq -r '.cwd // empty')")/$CD_TARGET" ;;
esac

if [ -d "$CD_TARGET" ]; then
  CD_TARGET=$(cd "$CD_TARGET" && pwd -P)
else
  # Directory doesn't exist — allow (will fail naturally)
  exit 0
fi

# Allow: cd into worktree or any subdirectory of worktree
case "$CD_TARGET" in
  "$WORKTREE_PATH"|"$WORKTREE_PATH"/*)
    exit 0
    ;;
  *)
    echo "BLOCKED: 'cd ${CD_TARGET}' is outside the worktree '${WORKTREE_PATH}'. Your cwd is already the worktree — run commands directly without cd." >&2
    exit 2
    ;;
esac
