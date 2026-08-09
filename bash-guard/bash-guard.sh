#!/usr/bin/env bash
# bash-guard.sh — Claude Code PreToolUse hook for the Bash tool
# Full docs, usage, and setup instructions: see README.md
# Toggle: echo off|on > ~/.claude/bash-auto-approve.conf

set -euo pipefail

TOGGLE_FILE="$HOME/.claude/bash-auto-approve.conf"
STATE="on"
if [[ -f "$TOGGLE_FILE" ]]; then
  STATE="$(tr -d '[:space:]' < "$TOGGLE_FILE")"
fi

if [[ "$STATE" != "on" ]]; then
  # Disabled: don't emit a decision, Claude Code falls back to its normal prompt.
  exit 0
fi

input="$(cat)"
command="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

if [[ -z "$command" ]]; then
  exit 0
fi

# Heuristic, not exhaustive: whole-word destructive commands/flags.
danger_regex='\bsudo\b|\brm\b|\brmdir\b|\bdel\b|\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b|\bmkfs\b|\bdd\b|\bkill(all)?\b|\bchmod\b|\bchown\b|\btruncate\b|\bformat\b|\bdrop\b|\bdelete\b|--force\b|--hard\b|>[[:space:]]*/dev/sd|curl[^|]*\|[[:space:]]*(sh|bash)|wget[^|]*\|[[:space:]]*(sh|bash)'

if printf '%s' "$command" | grep -Eiq -- "$danger_regex"; then
  jq -n --arg cmd "$command" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "ask",
      permissionDecisionReason: ("Potentially destructive command detected, review before approving: " + $cmd)
    }
  }'
else
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      permissionDecisionReason: "Auto-approved by bash-guard: no dangerous pattern matched"
    }
  }'
fi
