#!/usr/bin/env bash
# Thin launcher kept at the path settings.json already points to.
# The logic lives in bash_guard.py (parsed decisions, tests, log) — see its docstring.
# No python3 => no opinion, so Claude Code's normal permission system takes over.
command -v python3 >/dev/null 2>&1 || exit 0
exec python3 "$(dirname "$0")/bash_guard.py"
