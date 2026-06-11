#!/bin/bash
# pev-axiom-graph-scope.sh — PreToolUse hook for axiom-graph MCP tools.
# Enforces that axiom-graph calls use the worktree's project_root, not the
# main repo.
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

# Claude Code passes cwd as a Windows path on Windows (C:\...\foo); normalize
# to POSIX so file tests and path concatenation work in git-bash.
PROJECT_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-}"
if command -v cygpath >/dev/null 2>&1; then
  PROJECT_ROOT=$(cygpath -u "$PROJECT_ROOT")
fi
STATE_FILE="$PROJECT_ROOT/.pev-state.json"

if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi

WORKTREE_PATH=$(cat "$STATE_FILE" | jq -r '.worktree_path // empty' 2>/dev/null)

if [ -z "$WORKTREE_PATH" ]; then
  exit 0
fi

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
  echo "BLOCKED: worktree_path in pev-state.json does not exist on disk" >&2
  exit 2
fi

TOOL_PROJECT_ROOT=$(echo "$INPUT" | jq -r '.tool_input.project_root // empty' 2>/dev/null)

if [ -z "$TOOL_PROJECT_ROOT" ]; then
  echo "BLOCKED: axiom-graph tool call missing project_root parameter" >&2
  exit 2
fi

TOOL_PROJECT_ROOT=$(normalize "$TOOL_PROJECT_ROOT")
if [ -d "$TOOL_PROJECT_ROOT" ]; then
  TOOL_PROJECT_ROOT=$(cd "$TOOL_PROJECT_ROOT" && pwd -P)
else
  echo "BLOCKED: project_root '$TOOL_PROJECT_ROOT' does not exist on disk" >&2
  exit 2
fi

if [ "$TOOL_PROJECT_ROOT" = "$WORKTREE_PATH" ]; then
  exit 0
fi

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
echo "BLOCKED: axiom-graph project_root '$TOOL_PROJECT_ROOT' does not match worktree '$WORKTREE_PATH' (tool: $TOOL_NAME)" >&2
exit 2
