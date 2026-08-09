# bash-guard.sh — Claude Code PreToolUse hook for the Bash tool

## How to use it

1. **Make sure `jq` is installed** (the script depends on it).
2. **Place the script** somewhere stable, e.g. `~/.claude/hooks/bash-guard.sh`, and make it executable:
   ```bash
   chmod +x ~/.claude/hooks/bash-guard.sh
   ```
3. **Register it as a `PreToolUse` hook** for the `Bash` tool in `~/.claude/settings.json` (or your project's `.claude/settings.json`):
   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             {
               "type": "command",
               "command": "~/.claude/hooks/bash-guard.sh"
             }
           ]
         }
       ]
     }
   }
   ```
4. **Toggle it on/off** at any time without touching settings.json:
   ```bash
   echo off > ~/.claude/bash-auto-approve.conf   # disable auto-approve
   echo on  > ~/.claude/bash-auto-approve.conf   # re-enable (also the default)
   ```
   When disabled, the script exits silently and Claude Code's normal permission system (mode + allow/deny/ask rules) takes over as if the hook didn't exist.
5. **Pair it with explicit rules** in `settings.json` for real hard blocks — see Part 8 below. The hook is a catch-all convenience layer, not a substitute for `deny` rules.
6. **Customize the danger list** by editing `danger_regex` in the script — see Part 6 for the current patterns and how to add/remove one.

---

## PART 1: Claude Code permission system — full overview

Claude Code (CLI, VS Code extension, VSCodium extension — all share the same
engine) has a layered permission system. Every tool call Claude wants to make
(Bash, Read, Write, Edit, WebFetch, MCP tools, etc.) passes through this
system before it actually executes. Here's how the layers stack, from
outermost (checked first) to innermost:

### 1. Permission modes (the big-picture dial)

Set via `/config`, CLI flags (`--permission-mode`), or `settings.json` under
`"permissions.defaultMode"`. Controls the overall behavior:

- `"default"` → Normal: ask the user for anything not explicitly allowed. This is the out-of-box behavior.
- `"plan"` → Read-only mode: Claude can read/explore but cannot modify anything. Good for "just analyze" tasks.
- `"acceptEdits"` → Auto-accepts Write/Edit tool calls (file edits), but still prompts for Bash and other tools. Good for "just edit files, don't run commands".
- `"auto"` → Smart auto-mode: uses a classifier to decide if a command is safe. Destructive commands still prompt. Think of it as a built-in version of what this script does, but maintained by Anthropic.
- `"bypassPermissions"` → Skip ALL permission checks. Everything runs. Dangerous. There's a separate dialog you must accept before this mode activates.
- `"dontAsk"` → Similar to bypass; suppresses prompts.

Mode is the broadest control — it sets the default policy for every single tool call in the session.

### 2. Permission rules (allow / deny / ask lists)

Fine-grained rules in `settings.json` under `"permissions"`:

- `"allow": [...]` → These tool calls ALWAYS run, no prompt. e.g. `"Bash(git status)"`, `"Bash(npm *)"`, `"Read"`
- `"deny": [...]` → These tool calls are ALWAYS blocked, no prompt. e.g. `"Bash(rm -rf *)"`, `"Bash(sudo *)"`
- `"ask": [...]` → These ALWAYS prompt, even in acceptEdits/auto mode. e.g. `"Bash(git push *)"`, `"Edit(//etc/*)"`

Rule syntax:

- `"Bash(npm *)"` → prefix wildcard: matches `"npm install"`, `"npm run build"`, etc.
- `"Bash(git status)"` → exact match: only matches that literal command.
- `"Read"` → tool-only: allows ALL Read operations.
- `"Edit(.claude)"` → path-scoped: only edits inside the `.claude/` dir.
- `"Bash(curl -s *)"` → prefix with args.
- `"mcp__server__tool"` → MCP tool by name.

Evaluation order: deny is checked FIRST (deny wins), then allow, then ask.
If nothing matches, the permission MODE decides (default = ask user).

Where rules live (loaded in order, later overrides earlier):

- `~/.claude/settings.json` → user-global (all projects)
- `.claude/settings.json` (in project) → team-shared (committed to git)
- `.claude/settings.local.json` → personal per-project (gitignored)

### 3. Hooks (this script — the custom layer)

Hooks are user-defined scripts that run at specific lifecycle events.
This script is a PreToolUse hook — it runs AFTER permission rules are
checked but BEFORE the tool actually executes. It can override the
decision by outputting JSON with a `permissionDecision` field.

Hook events (the main ones):

- `PreToolUse` → Before a tool runs. CAN allow/deny/ask. ← THIS SCRIPT
- `PostToolUse` → After a tool succeeds. Can log, format, run tests.
- `PostToolUseFailure` → After a tool fails.
- `Stop` → When Claude finishes a turn.
- `UserPromptSubmit` → When you hit Enter on a prompt.
- `SessionStart` → When a new session begins.
- `PreCompact`/`PostCompact` → Before/after context compression.

Hook input: JSON on stdin. For `PreToolUse`:

```json
{
  "session_id": "abc123",
  "tool_name": "Bash",
  "tool_input": { "command": "ls -la" }
}
```

Hook output (what this script prints to stdout):

```json
{ "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow" | "deny" | "ask",
    "permissionDecisionReason": "why"
}}
```

If the hook prints nothing (or exits with no JSON), it's a no-op —
whatever the permission system already decided sticks. This is how
the toggle-off path works in this script (just exit 0 silently).

Hook types:

- `"command"` → runs a shell command (like this script). Cheapest, fastest.
- `"prompt"` → sends the input to an LLM for evaluation. Slower, smarter.
- `"agent"` → spawns a full agent with tools. Most powerful, most costly.
- `"http"` → POSTs the input to a URL. For external integrations.
- `"mcp_tool"` → calls an MCP server tool.


## PART 2: Where this script fits in the stack

`bash-guard.sh` runs at STEP 5 above. That means:

- If you already have `"Bash(rm *)"` in your deny list, the command never reaches this script — it's blocked at STEP 2.
- If you have `"Bash(git *)"` in your allow list, git commands are allowed at STEP 3 — this script never sees them.
- This script only kicks in for commands that fell through all the static rules and would otherwise be decided by mode (i.e., would normally prompt you in "default" mode).

So this script is a CATCH-ALL auto-approver: "if none of my explicit rules already decided, and the command doesn't look dangerous, just approve it so I don't have to click Allow for the 50th time."

## PART 3: What this script does (step by step)

1. Reads the toggle file (`~/.claude/bash-auto-approve.conf`). If it says "off", exits silently — Claude Code's normal permission system takes over (as if this hook doesn't exist).
2. Reads JSON from stdin, extracts `tool_input.command` via `jq`.
3. Checks the command against `danger_regex` — a list of destructive patterns (sudo, rm, delete, shutdown, dd, chmod, --force, pipe-to-shell, disk overwrite, etc).
4. If a dangerous pattern matches: outputs `permissionDecision: "ask"` with a warning reason. Claude Code shows the NORMAL permission prompt, but now with the reason displayed so the user sees WHY it flagged.
5. If nothing matches: outputs `permissionDecision: "allow"`. Command runs without any prompt at all.

## PART 4: Toggle on/off

```bash
echo off > ~/.claude/bash-auto-approve.conf   # DISABLE auto-approve
echo on  > ~/.claude/bash-auto-approve.conf   # RE-ENABLE (also default)
```

When disabled: script exits 0 with no output → no-op → Claude Code's normal permission system (allow/deny/ask rules + mode default) takes over exactly as if this hook didn't exist.

## PART 5: Danger patterns (what gets flagged)

Currently flagged (edit `danger_regex` in the script to change):

**COMMANDS:**
sudo, rm, rmdir, del, shutdown, reboot, halt, poweroff, mkfs, dd, kill, killall, chmod, chown, truncate, format, drop (SQL), delete

**FLAGS:**
`--force` (any command), `--hard` (e.g. `git reset --hard`)

**PATTERNS:**

- `> /dev/sd*` → direct disk write (overwrite a drive)
- `curl ... | sh` → pipe remote content directly into shell
- `curl ... | bash` → same
- `wget ... | sh` → same
- `wget ... | bash` → same

To ADD a pattern: append `|\byourpattern\b` to `danger_regex`.
To REMOVE a pattern: delete the corresponding `|\b...\b` segment.

## PART 6: Limitations — read before trusting this blindly

1. **Text matching, not semantic analysis.** This script greps the literal command text. It does NOT understand what the command actually does. It's a speed-bump for obvious danger, not a real security boundary.
2. **Can be evaded by obfuscation.** Examples that would slip past:
   - `c=rm; $c -rf /important/dir` (variable indirection)
   - `echo "cm0gLXJmIC8" | base64 -d | sh` (base64-encoded payload)
   - `find / -delete` (find with `-delete` not in regex)
   - `rename`, `unlink`, `shred`, etc. (lesser-known destructive tools)
3. **False positives.** Commands that contain a flagged word but are harmless:
   - `git commit -m "remove old files"` (contains "remove" — not flagged, but "rm" could appear in a message)
   - `grep "delete" file.txt` (contains "delete" as a search term)
   The regex uses `\b` word boundaries to reduce this, but it's not perfect.
4. **Does not replace settings.json rules.** Your allow/deny/ask lists in `settings.json` are checked FIRST and take precedence. This hook only decides for commands that no rule already handled. For real safety, put your hard blocks in deny:
   ```json
   "deny": ["Bash(rm -rf *)", "Bash(sudo *)"]
   ```
5. **Not exhaustive.** The danger list covers common cases but is not a complete catalog. New/obscure destructive commands may not be caught.

## PART 7: Practical recommendations

For best results, combine this script with explicit rules in `settings.json`:

`~/.claude/settings.json` (or `.claude/settings.local.json`):

```json
{
  "permissions": {
    "deny": [
      "Bash(sudo *)",
      "Bash(rm -rf /)",
      "Bash(dd if=* of=/dev/*)"
    ],
    "allow": [
      "Bash(git status)",
      "Bash(git diff *)",
      "Bash(ls *)",
      "Bash(cat *)",
      "Read"
    ],
    "ask": [
      "Bash(git push *)",
      "Bash(npm publish *)"
    ]
  }
}
```

Layered approach:

- `deny` = hard boundary (absolute "never")
- `allow` = known-safe commands (skip the hook entirely)
- `ask` = sensitive commands (always prompt)
- `hook` = catch-all for everything else (auto-approve safe, flag risky)
