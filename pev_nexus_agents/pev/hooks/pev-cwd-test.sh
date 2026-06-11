#!/bin/bash
# Temporary test: output a visible message so the agent sees the hook fired
INPUT=$(cat)
CWD=$(echo "$INPUT" | jq -r '.cwd // "UNKNOWN"' 2>/dev/null)
echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"PEV-CWD-TEST HOOK FIRED. cwd=${CWD}\"}}"
