# claude-hooks

Personal collection of [Claude Code](https://claude.com/claude-code) hooks. Every hook lives in its own folder: the script plus a `README.md` with its docs.

## Hooks

| Hook | Event | Description |
|---|---|---|
| [bash-guard/](bash-guard/) | `PreToolUse` (Bash, Read, Write, Edit) | Parses shell commands and auto-approves routine work; asks on destructive / outward / secret-touching ones. Logged, configurable, 112 tests. Docs: [bash-guard/README.md](bash-guard/README.md) |

## Adding a new hook

1. Create a folder, e.g. `my-hook/`.
2. Add the script inside it, e.g. `my-hook/my-hook.sh`, and make it executable (`chmod +x my-hook/my-hook.sh`).
3. Write its docs in `my-hook/README.md` — what it does, how to use it, limitations.
4. Register it in the relevant `settings.json` under `"hooks"`.
5. Add a row to the table above.
