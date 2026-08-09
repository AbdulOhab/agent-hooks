# claude-hooks

Personal collection of [Claude Code](https://claude.com/claude-code) hooks. Every hook script I write lives here, one `.sh` file plus a matching `.md` doc per hook.

## Hooks

| Hook | Event | Description |
|---|---|---|
| [bash-guard.sh](bash-guard.sh) | `PreToolUse` (Bash) | Auto-approves safe Bash commands, flags destructive ones for review. Docs: [BASH_GUARD.md](BASH_GUARD.md) |

## Adding a new hook

1. Add the script, e.g. `my-hook.sh`, and make it executable (`chmod +x my-hook.sh`).
2. Write its docs in a matching markdown file, e.g. `MY_HOOK.md` — what it does, how to use it, limitations.
3. Register it in the relevant `settings.json` under `"hooks"`.
4. Add a row to the table above.
