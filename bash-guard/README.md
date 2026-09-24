# bash-guard — Claude Code PreToolUse hook (Bash, Read, Write, Edit, NotebookEdit)

Stops the endless "Allow?" clicks for routine work, and still stops for anything
destructive, outward-facing or outside your project.

**v2 (2026-09-24)** replaced the old "grep the whole command for scary words" script.
v1 asked about `clang-format -i x.cpp` (contains "format"), `git commit -m "drop menu"`,
python heredocs containing the word "delete", and `rm` of a scratchpad file. v2 **parses**
the command (quotes, pipes, `;`/`&&`/`||`, `$(...)`, backticks, heredocs) and decides per
command *word*.

## Files

| File | What |
|---|---|
| `bash-guard.sh` | 4-line launcher — the path you register in `settings.json`. No `python3` => no opinion (normal prompts). |
| `bash_guard.py` | The hook. Needs only Python 3 (no `jq`). |
| `test_bash_guard.py` | 112 regression cases (self-contained: temp config + temp log, never touches yours). |
| `bash-guard.json.example` | Optional config — copy to `~/.claude/hooks/bash-guard.json`. |

## How to use it

1. Put the four files in `~/.claude/hooks/` and `chmod +x bash-guard.sh bash_guard.py`.
2. Register it in `~/.claude/settings.json` (merge into existing `hooks`, never replace the file):
   ```json
   "hooks": {
     "PreToolUse": [
       {
         "matcher": "Bash|Read|Write|Edit|MultiEdit|NotebookEdit",
         "hooks": [
           { "type": "command", "command": "/home/YOURUSER/.claude/hooks/bash-guard.sh", "timeout": 10 }
         ]
       }
     ]
   }
   ```
   (`"matcher": "Bash"` alone also works — then only shell commands are judged.)
3. `python3 ~/.claude/hooks/test_bash_guard.py` — should print `112/112 passed`.
4. **Prove it fires.** An unwired hook and a wired-but-broken one look identical from
   outside. Run any harmless command through Claude, then `tail ~/.claude/hooks/bash-guard.log`
   — a fresh `allow` line means it is live. Hook changes made mid-session load only after
   `/hooks` or a restart.
5. **Toggle** without touching settings.json:
   ```bash
   echo off > ~/.claude/bash-auto-approve.conf   # disable (normal permission prompts)
   echo on  > ~/.claude/bash-auto-approve.conf   # enable (default)
   ```
6. **Tune**: read `~/.claude/hooks/bash-guard.log` (time, decision, tool, reason, cwd, command)
   to see what still asks and why, then edit `bash-guard.json` (below).

## What it decides

**Allowed silently** — anything that is not on the ask list: builds, git status/add/commit/diff/
stash/revert, grep/sed/clang-format, running your own binaries, `rm` of files strictly inside
`/tmp/claude-<uid>/…/…` (the session scratchpad) or a `build*` / `node_modules` / `__pycache__`
directory of the project (literal paths only), Read/Write/Edit inside the project, the scratchpad
and `~/.claude/projects/*/memory`.

**Asks (with the reason shown in the prompt):**

| Area | Asks on |
|---|---|
| Deleting | `rm`/`rmdir`/`shred`/`unlink` outside the safe dirs or with `$VAR`/`~`/`..` targets, `find -delete`, `find -exec rm`, `xargs rm` |
| System | `sudo`/`su`/`doas`, `kill`/`pkill`, `chmod`/`chown`, `dd`, `mkfs*`, `mount`, `shutdown`/`reboot`, `systemctl` state changes, package managers (`pacman`, `apt`, `pip install` into system Python, `npm -g`, …) |
| git | `push`, `reset --hard`, `checkout -- .`/`checkout .`, `restore` (working tree), `clean -f`, `branch -D`, `stash drop/clear`, `rebase`, `remote set-url`, `config --global`, commits on a configured frozen branch |
| Outward | `curl`/`wget` with `-X POST/PUT/DELETE`, `-d`, `-F`, `-T`; `ssh`/`scp`/`nc`; `gh` write commands; `rsync --delete`/remote |
| Containers | `docker`/`podman` `stop`, `rm`, `kill`, `exec`, `prune`, `down`, … (`ps`/`run`/`logs` are fine) |
| SQL | `psql`/`mysql`/`mariadb`/`sqlite3` with DROP/DELETE/UPDATE/ALTER/… (also inside heredocs) unless the command matches a `scratch_db_markers` regex or is a scratch sqlite file; `DROP DATABASE`, `GRANT`, `SHUTDOWN` always ask |
| Code | `python -c`/heredoc, `node -e`, `perl`, `ruby` that call delete/`subprocess`/network APIs or mention sensitive paths |
| Shell tricks | `eval`, `bash < file`, `cmd \| sh`, `curl … \| bash`, nested `bash -c '…'` is analysed recursively |
| Secrets & config | reading `.env`, `~/.ssh`, `~/.aws/credentials`, …; writing `/etc`, `/usr`, shell rc files, `~/.claude/settings*`, the hook itself, `.git/` internals |

**Never overridden:** plan mode (`permission_mode == "plan"`) and MCP tools. Any internal error
gives *no opinion* (normal prompt) — a bug never turns into a silent allow. Writes/edits outside
the safe dirs also get *no opinion*.

## Config — `~/.claude/hooks/bash-guard.json` (all keys optional)

| Key | Meaning |
|---|---|
| `safe_dirs` | extra directories where Write/Edit are auto-allowed (the current project / git top-level is always included) |
| `extra_allow_commands` / `extra_ask_commands` | command names to always allow / always ask |
| `scratch_db_markers` | regexes marking a command as aimed at a throwaway DB server (writes allowed) |
| `frozen_branches` | `[{"branch": "master", "if_tag": "upstream/1.0"}]` — commit/merge/pull on it asks (`if_tag` optional: only in a repo carrying that tag) |

Env overrides (used by the tests): `BASH_GUARD_CONF`, `BASH_GUARD_LOG`.

## Habits that keep it quiet

Use absolute literal paths in `rm`/`sqlite3` (a `$VAR` cannot be resolved by a hook, so it asks);
avoid `subprocess` in python heredocs; keep scratch-DB work on the server your markers cover.

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

1. Reads the toggle file. `off` => exit silently (normal permissions).
2. Reads the hook JSON from stdin; plan mode => exit silently.
3. **Bash:** strips heredoc bodies, lexes the command into segments (`;` `&&` `||` `|` newlines,
   subshells), collects `$(...)`/backticks/`bash -c` strings and redirect targets, and analyses
   each recursively. Per segment it skips env assignments and wrappers (`env`, `timeout`,
   `nohup`, `xargs`, …) to find the real command, then applies that command's rule.
4. **Read/Write/Edit/NotebookEdit:** allow inside safe dirs, ask for secrets/sensitive paths,
   otherwise no opinion.
5. Prints `allow` / `ask` (with reason) or nothing, and appends the decision to the log.

## PART 4: Toggle on/off

```bash
echo off > ~/.claude/bash-auto-approve.conf   # DISABLE auto-approve
echo on  > ~/.claude/bash-auto-approve.conf   # RE-ENABLE (also default)
```

When disabled: script exits 0 with no output → no-op → Claude Code's normal permission system (allow/deny/ask rules + mode default) takes over exactly as if this hook didn't exist.

## PART 5: Danger rules (what gets flagged)

See **What it decides** above; the rules live in `bash_guard.py` (`ALWAYS_ASK`, `check_git`, `check_rm`, `check_sql`, …) and are pinned by `test_bash_guard.py`.

## PART 6: Limitations — read before trusting this blindly

1. **A parser is still not a sandbox.** It understands common shell syntax, not every shell
   feature. Not caught: variable indirection (`c=rm; $c -rf x`), aliases/functions defined earlier,
   and scripts you run (`./cleanup.sh`, `python3 script.py`) — their contents are not inspected.
   (`base64 -d | sh` *is* caught, as a pipe into a shell.)
2. **Python/JS code checks are keyword-based** (`os.remove`, `shutil.rmtree`, `subprocess`,
   `fs.rm`, …). Obfuscated code slips through.
3. **Does not replace `settings.json` rules.** Allow/deny/ask lists are checked first. Put hard
   blocks in `deny`, e.g. `"Bash(rm -rf /)"`, `"Bash(sudo *)"`.
4. **MCP tools are not judged** (a database MCP with an `execute_query` tool stays under your
   normal permission rules on purpose).
5. **Unparseable commands** (unbalanced quotes) fall back to a blunt word check.

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
