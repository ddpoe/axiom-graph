---
name: hs-subagent-stop
description: Hook-spike — tests whether a SubagentStop hook declared in agent frontmatter fires with ${CLAUDE_PLUGIN_ROOT} available.
tools: []
hooks:
  SubagentStop:
    - hooks:
        - type: command
          command: "mkdir -p /tmp/hook-spike && env | sort > /tmp/hook-spike/subagent-stop.env && printf 'fired=true\\npwd=%s\\nCLAUDE_PLUGIN_ROOT=%s\\nCLAUDE_PROJECT_DIR=%s\\n' \"$PWD\" \"${CLAUDE_PLUGIN_ROOT:-<UNSET>}\" \"${CLAUDE_PROJECT_DIR:-<UNSET>}\" > /tmp/hook-spike/subagent-stop.fired"
          timeout: 10
---

You are the hook-spike `subagent-stop` test agent.

Make NO tool calls. Simply return the string:

HS-SUBAGENT-STOP DONE

When you return, the `SubagentStop` hook declared in this agent's frontmatter should fire and write `/tmp/hook-spike/subagent-stop.fired`.
