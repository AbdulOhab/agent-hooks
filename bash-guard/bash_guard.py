#!/usr/bin/env python3
"""bash_guard.py — Claude Code PreToolUse hook (Bash, Read, Write, Edit, NotebookEdit).

Goal: stop asking the owner to click "Allow" for routine work, while still
stopping for anything destructive, outward-facing or outside the project.

How it differs from the old grep-the-whole-string version (bash-guard.sh v1):
  * The command is PARSED (quotes, pipes, ;/&&/||, $(...), backticks, heredocs),
    and the decision is made per command WORD — so `git commit -m "drop menu"`,
    `clang-format -i x.cpp`, `grep delete f` or a python heredoc that merely
    contains the word "delete" no longer trigger a prompt.
  * Destructive *commands* still ask: rm (unless strictly inside the session
    scratchpad or a build dir), sudo, kill, chmod, dd, git push / reset --hard /
    checkout -- / clean / branch -D / rebase, docker|podman stop|rm, package
    managers, systemctl changes, curl POST/PUT/DELETE/upload, ssh/scp, gh writes,
    find -delete, writes to /etc /usr ~/.ssh shell rc + the guard itself.
  * SQL passed to psql/mysql/mariadb/sqlite3 is inspected: read-only is fine;
    DROP/DELETE/UPDATE/ALTER/... asks unless it targets the throwaway test
    server (port 3307 / port_test) or a scratch sqlite file.
  * Code passed to python/node/perl/ruby (-c/-e or heredoc) asks when it calls
    deletion / process / network APIs, otherwise runs (that is how file edits
    via `python3 - <<'EOF'` work).
  * Optional `frozen_branches` (config): committing on such a branch asks.
  * Read/Write/Edit/NotebookEdit: allowed inside the project / scratchpad /
    memory dirs, ask for secrets and for the guard's own files, otherwise no
    opinion (the normal permission system decides).
  * Plan mode (`permission_mode == "plan"`) is never overridden.
  * Any internal error => no opinion (normal prompt), never a silent allow.

Every decision is appended to ~/.claude/hooks/bash-guard.log so you can see
what still asks and why, then tune ~/.claude/hooks/bash-guard.json.

Toggle:  echo off > ~/.claude/bash-auto-approve.conf   (on = default)
Tests:   python3 ~/.claude/hooks/test_bash_guard.py
"""
import json
import os
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
HOOK_DIR = os.path.join(HOME, ".claude", "hooks")
TOGGLE = os.path.join(HOME, ".claude", "bash-auto-approve.conf")
LOG = os.environ.get("BASH_GUARD_LOG") or os.path.join(HOOK_DIR, "bash-guard.log")
CONF = os.environ.get("BASH_GUARD_CONF") or os.path.join(HOOK_DIR, "bash-guard.json")


class Ask(Exception):
    pass


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
DEFAULT_CONF = {
    # extra directories where Write/Edit/rm are considered "mine"
    "safe_dirs": [],
    # command names always allowed / always asked, on top of the built-in rules
    "extra_allow_commands": [],
    "extra_ask_commands": [],
    # regexes that mark a command as aimed at a throwaway database server,
    # where SQL writes are fine, e.g. [r"-P\s*3307\b", r"\bmy_scratch_db\b"]
    "scratch_db_markers": [],
    # branches nobody may commit on, e.g.
    # [{"branch": "master", "if_tag": "upstream/1.0"}]  (if_tag optional: only
    # enforce in a repo that carries that tag)
    "frozen_branches": [],
}


def load_conf():
    conf = dict(DEFAULT_CONF)
    try:
        with open(CONF) as f:
            conf.update(json.load(f))
    except (OSError, ValueError):
        pass
    return conf


# --------------------------------------------------------------------------
# path helpers
# --------------------------------------------------------------------------
SCRATCH_RE = re.compile(r"^/tmp/claude-\d+/")


def expand(p):
    if p.startswith("~/") or p == "~":
        return HOME + p[1:]
    if p.startswith("$HOME/"):
        return HOME + p[5:]
    return p


def norm(p, cwd):
    p = expand(p)
    if not os.path.isabs(p):
        p = os.path.join(cwd or "/", p)
    return os.path.normpath(p)


def is_scratch(path):
    """strictly inside /tmp/claude-<uid>/<...>/<...> (never the root itself)"""
    if not SCRATCH_RE.match(path + "/"):
        return False
    return len(path.split("/")) >= 5


def project_roots(cwd, conf):
    roots = [d for d in conf.get("safe_dirs", [])]
    if cwd and cwd not in (HOME, "/"):
        roots.append(cwd)
        try:
            top = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            if top and top not in (HOME, "/"):
                roots.append(top)
        except Exception:
            pass
    return [os.path.normpath(expand(r)) for r in roots]


def inside(path, roots):
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


SENSITIVE_WRITE = [
    "/etc/", "/usr/", "/boot/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/var/lib/",
    "/sys/", "/proc/", "/dev/sd", "/dev/nvme", "/dev/mapper",
    HOME + "/.ssh", HOME + "/.gnupg", HOME + "/.aws", HOME + "/.bashrc", HOME + "/.zshrc",
    HOME + "/.profile", HOME + "/.bash_profile", HOME + "/.config/fish",
    HOME + "/.claude/settings", HOME + "/.claude/hooks", HOME + "/.claude/bash-auto-approve",
    HOME + "/.claude.json", HOME + "/.config/systemd", HOME + "/.local/bin",
]
SECRET_PATH_RE = re.compile(
    r"(^|/)(\.ssh/|\.gnupg/|\.aws/credentials|\.netrc|\.pgpass|\.git-credentials|"
    r"id_rsa|id_ed25519|id_ecdsa|\.env($|\.(?!example|sample|template))|"
    r"credentials\.json|\.credentials\.json|\.config/gh/hosts\.yml|\.claude\.json$)"
)


def sensitive_write(path):
    return any(path == s.rstrip("/") or path.startswith(s if s.endswith("/") else s)
               for s in SENSITIVE_WRITE)


def secret_path(path):
    return bool(SECRET_PATH_RE.search(path))


# --------------------------------------------------------------------------
# shell lexer
# --------------------------------------------------------------------------
def match_paren(s, i):
    """s[i] == '(' -> index of the matching ')', quote-aware"""
    depth, j, n = 0, i, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                raise ParseError("quote")
            j = k + 1
            continue
        if c == '"':
            j += 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            j += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise ParseError("paren")


def lex(cmd):
    """-> (segments, subs, redirs). segments: list of word lists."""
    segs, words, cur, subs, redirs = [], [], [], [], []
    st = {"has": False, "redir": False, "skip": False}
    i, n = 0, len(cmd)

    def endword():
        if st["has"]:
            w = "".join(cur)
            if st["redir"]:
                redirs.append(w)
                st["redir"] = False
            elif st["skip"]:
                st["skip"] = False
            else:
                words.append(w)
        cur.clear()
        st["has"] = False

    def endseg():
        nonlocal words
        endword()
        if words:
            segs.append(words)
        words = []

    while i < n:
        c = cmd[i]
        if c == "\\":
            if i + 1 < n:
                if cmd[i + 1] == "\n":
                    i += 2
                    continue
                cur.append(cmd[i + 1])
                st["has"] = True
                i += 2
                continue
            i += 1
            continue
        if c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                raise ParseError("quote")
            cur.append(cmd[i + 1:j])
            st["has"] = True
            i = j + 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and cmd[j] != '"':
                if cmd[j] == "\\" and j + 1 < n:
                    buf.append(cmd[j + 1])
                    j += 2
                elif cmd.startswith("$(", j):
                    e = match_paren(cmd, j + 1)
                    subs.append(cmd[j + 2:e])
                    buf.append("$SUB")
                    j = e + 1
                elif cmd[j] == "`":
                    k = cmd.find("`", j + 1)
                    if k < 0:
                        raise ParseError("backtick")
                    subs.append(cmd[j + 1:k])
                    buf.append("$SUB")
                    j = k + 1
                else:
                    buf.append(cmd[j])
                    j += 1
            if j >= n:
                raise ParseError("dquote")
            cur.append("".join(buf))
            st["has"] = True
            i = j + 1
            continue
        if cmd.startswith("$(", i) or cmd.startswith("<(", i) or cmd.startswith(">(", i):
            e = match_paren(cmd, i + 1)
            subs.append(cmd[i + 2:e])
            cur.append("$SUB")
            st["has"] = True
            i = e + 1
            continue
        if c == "`":
            k = cmd.find("`", i + 1)
            if k < 0:
                raise ParseError("backtick")
            subs.append(cmd[i + 1:k])
            cur.append("$SUB")
            st["has"] = True
            i = k + 1
            continue
        if c == "#" and not st["has"] and (i == 0 or cmd[i - 1] in " \t\n;&|("):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in " \t":
            endword()
            i += 1
            continue
        if c in "\n;":
            endseg()
            i += 1
            continue
        if c in "()":
            endseg()
            i += 1
            continue
        if c in "&|":
            two = cmd[i:i + 2]
            # `&>file`, `>&2`, `2>&1`, `|&`
            if c == "&" and (i + 1 < n and cmd[i + 1] == ">"):
                cur.clear()
                st["has"] = False
                i += 2
                if cmd[i:i + 1] == ">":
                    i += 1
                st["redir"] = True
                continue
            if c == "&" and cur and cur[-1].endswith((">", "<")):
                cur.append(c)
                st["has"] = True
                i += 1
                continue
            endseg()
            i += 2 if two in ("&&", "||", "|&") else 1
            continue
        if c in "<>":
            # drop a leading fd number ("2>")
            if st["has"] and "".join(cur).isdigit():
                cur.clear()
                st["has"] = False
            else:
                endword()
            if cmd.startswith("<<<", i):
                i += 3
                continue
            if cmd.startswith("<<", i):
                i += 2
                if cmd[i:i + 1] == "-":
                    i += 1
                st["skip"] = True  # heredoc delimiter word
                continue
            if c == "<":
                i += 1
                st["skip"] = True  # input file: harmless
                continue
            # > , >> , >| , >&N
            j = i + 1
            if cmd[j:j + 1] in (">", "|"):
                j += 1
            if cmd[j:j + 1] == "&":
                j += 1
                # >&1 / >&- : fd dup, not a file
                m = re.match(r"\d+|-", cmd[j:])
                if m:
                    i = j + m.end()
                    continue
            i = j
            st["redir"] = True
            continue
        cur.append(c)
        st["has"] = True
        i += 1
    endseg()
    return segs, subs, redirs


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(cmd):
    """-> (command without heredoc bodies, [(line_before_marker, body), ...])"""
    lines = cmd.split("\n")
    out, docs, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC_RE.finditer(line):
            delim = m.group(2)
            body = []
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            i += 1  # closing delimiter
            docs.append((line[:m.start()], "\n".join(body)))
    return "\n".join(out), docs


# --------------------------------------------------------------------------
# command classification
# --------------------------------------------------------------------------
KEYWORDS_SKIP_SEGMENT = {"for", "case", "select", "function"}
KEYWORDS_PREFIX = {"if", "while", "until", "then", "else", "elif", "do", "time", "!", "{", "}"}
NOOP_END = {"fi", "done", "esac", "in"}
WRAPPERS = {"env", "nohup", "nice", "ionice", "time", "timeout", "stdbuf", "command",
            "builtin", "exec", "setsid", "unbuffer", "xargs", "chrt", "taskset", "flock"}
SHELLS = {"bash", "sh", "zsh", "dash", "fish", "ksh"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

ALWAYS_ASK = {
    "sudo", "su", "doas", "pkexec", "rmdir", "unlink", "shred", "dd", "fdisk", "sfdisk",
    "parted", "wipefs", "mkswap", "shutdown", "reboot", "halt", "poweroff", "init", "telinit",
    "kill", "killall", "pkill", "skill", "chmod", "chown", "chgrp", "chattr", "setfacl",
    "truncate", "mount", "umount", "swapoff", "swapon", "crontab", "at", "batch", "userdel",
    "usermod", "useradd", "groupdel", "passwd", "iptables", "ip6tables", "nft", "ufw",
    "ssh", "scp", "sftp", "nc", "ncat", "socat", "telnet", "ftp", "eval", "visudo",
    "pacman", "yay", "paru", "apt", "apt-get", "dpkg", "dnf", "yum", "zypper", "rpm",
    "flatpak", "snap", "dropdb", "dropuser", "createdb", "createuser", "pg_restore",
    "mysqladmin", "mariadb-admin", "modprobe", "rmmod", "insmod", "grub-install",
    "update-grub", "mkinitcpio", "loginctl", "machinectl", "hostnamectl", "timedatectl",
    "localectl", "nmcli", "rfkill", "passwd", "chpasswd",
}
ASK_MKFS = re.compile(r"^mkfs(\.\w+)?$|^mke2fs$|^mkntfs$")
SYSTEMCTL_SAFE = {"status", "list-units", "list-unit-files", "is-active", "is-enabled",
                  "is-failed", "show", "cat", "list-timers", "list-sockets", "list-dependencies"}
WRITERS = {"cp", "mv", "ln", "install", "tee", "touch", "mkdir", "rsync", "tar", "unzip", "curl",
           "wget", "dd", "sed", "perl", "cat", "echo", "printf", "git", "patch", "truncate"}

SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "usql", "pgcli", "mycli"}
SQL_WRITE_RE = re.compile(
    r"\b(drop|truncate|delete|alter|create|insert|update|replace|grant|revoke|rename|"
    r"load\s+data|copy|vacuum|reindex|call|execute|shutdown|set\s+global|attach|pragma\s+\w+\s*=)\b",
    re.I)
SQL_NEVER_RE = re.compile(r"\b(drop\s+(database|schema|user|role)|shutdown|grant|revoke)\b", re.I)

PY_DANGER_RE = re.compile(
    r"\bos\.(remove|unlink|rmdir|removedirs|system|popen|kill|killpg|exec\w*|spawn\w*|chmod|chown)\b|"
    r"\bshutil\.rmtree\b|\brmtree\b|\bsubprocess\b|\bPopen\b|\.unlink\(|\.rmdir\(|\bctypes\b|"
    r"\bsocket\b|\brequests\.(post|put|delete|patch)\b|\burllib\b.*data\s*=|\bsmtplib\b|"
    r"\bpty\b|\bpexpect\b|\bsh\.\w+\(|__import__\(")
JS_DANGER_RE = re.compile(
    r"child_process|\bexec(Sync|File)?\(|\bspawn(Sync)?\(|\bfs\.(rm|rmSync|unlink|unlinkSync|"
    r"rmdir|rmdirSync|rename|renameSync)\b|Deno\.(remove|run|Command)|Bun\.(spawn|\$)|"
    r"method\s*:\s*['\"](POST|PUT|DELETE|PATCH)['\"]|\beval\(|new Function\(")
GEN_DANGER_RE = re.compile(
    r"\bsystem\s*\(|\bexec\s*\(|\bpopen\b|\bunlink\b|File\.(delete|unlink)|FileUtils\.rm|"
    r"\brm_rf\b|`[^`]*`|%x\{|\bsocket\b|Net::HTTP|LWP|\bqx\b|\bkill\b|\bsysopen\b")
SENSITIVE_STR_RE = re.compile(
    r"/etc/|~/\.ssh|\.ssh/|\.bashrc|\.zshrc|\.gnupg|\.claude/settings|\.claude/hooks|"
    r"\.aws/|\.config/fish|/usr/|/boot/")

DEST_GIT = {
    "push", "clean", "rebase", "filter-branch", "filter-repo", "gc", "prune", "worktree",
    "submodule", "bisect", "am", "replace", "notes", "svn", "send-email",
}
GIT_COMMIT_LIKE = {"commit", "merge", "cherry-pick", "revert", "pull", "am", "rebase"}


def base(word):
    return os.path.basename(word) if "/" in word else word


def strip_flags(args):
    return [a for a in args if not a.startswith("-")]


def resolve_command(words):
    """skip assignments/wrappers -> (command_name, args, wrapper_names)"""
    i, wrappers = 0, []
    while i < len(words):
        w = words[i]
        if ASSIGN_RE.match(w):
            i += 1
            continue
        b = base(w)
        if b in WRAPPERS:
            wrappers.append(b)
            i += 1
            # skip the wrapper's own flags / duration / assignments
            while i < len(words):
                a = words[i]
                if a.startswith("-") or ASSIGN_RE.match(a) or (b == "timeout" and re.match(r"^\d+[smhd]?$", a)) \
                        or (b in ("nice", "ionice", "chrt") and re.match(r"^-?\d+$", a)):
                    i += 1
                    # options with a separate value
                    if b in ("xargs",) and a in ("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"):
                        i += 1
                    if b == "timeout" and a in ("-s", "-k"):
                        i += 1
                    if b == "env" and a in ("-u", "-C", "-S"):
                        i += 1
                    continue
                break
            continue
        return b, words[i + 1:], wrappers
    return "", [], wrappers


def check_rm(args, cwd, roots):
    targets = strip_flags(args)
    if not targets:
        raise Ask("rm without targets")
    for t in targets:
        if "$" in t or "`" in t or t.startswith("~"):
            raise Ask("rm target with a variable/~: " + t)
        p = norm(t.split("*")[0].rstrip("/") or "/", cwd) if "*" in t else norm(t, cwd)
        if ".." in t.split("/"):
            raise Ask("rm target with ..: " + t)
        if is_scratch(p):
            continue
        # build output directories of a project checkout
        parts = p.split("/")
        if any(inside(p, [r]) for r in roots):
            rel_parts = [os.path.relpath(p, r).split("/")[0] for r in roots if inside(p, [r])]
            if any(re.match(r"^(build[\w.-]*|cmake-build[\w.-]*|__pycache__|node_modules|\.cache)$", t)
                   for t in rel_parts):
                continue
        raise Ask("rm outside scratch/build dirs: " + t)


def check_git(args, cwd, conf):
    a = [x for x in args]
    # skip global options: -C dir, -c k=v, --no-pager, ...
    i = 0
    while i < len(a):
        if a[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif a[i].startswith("-"):
            i += 1
        else:
            break
    if i >= len(a):
        return
    sub, rest = a[i], a[i + 1:]
    flags = [x for x in rest if x.startswith("-")]
    pos = [x for x in rest if not x.startswith("-")]

    if sub == "push":
        raise Ask("git push (outward-facing; the owner pauses pushes)")
    if sub == "reset" and ("--hard" in flags or "--merge" in flags or "--keep" in flags):
        raise Ask("git reset --hard discards work")
    if sub == "clean" and any(f.startswith("-") and ("f" in f or f == "--force") for f in flags):
        raise Ask("git clean -f deletes untracked files")
    if sub == "checkout" and ("--" in rest or "." in pos or "-f" in flags or "--force" in flags
                              or "-B" in flags):
        raise Ask("git checkout discards working-tree changes")
    if sub == "restore" and "--staged" not in flags and "-S" not in flags:
        raise Ask("git restore discards working-tree changes")
    if sub == "switch" and ("-f" in flags or "--force" in flags or "--discard-changes" in flags):
        raise Ask("git switch --force discards changes")
    if sub == "branch" and any(f in ("-D", "-d", "--delete", "-M", "-m", "--move") for f in flags):
        raise Ask("git branch delete/rename")
    if sub == "tag" and any(f in ("-d", "--delete", "-f", "--force") for f in flags):
        raise Ask("git tag delete/force")
    if sub == "stash" and pos[:1] in (["drop"], ["clear"]):
        raise Ask("git stash drop/clear loses stashed work")
    if sub == "reflog" and pos[:1] in (["expire"], ["delete"]):
        raise Ask("git reflog expire/delete")
    if sub == "update-ref" and "-d" in flags:
        raise Ask("git update-ref -d")
    if sub == "remote" and pos[:1] in (["remove"], ["rm"], ["set-url"], ["add"], ["rename"]):
        raise Ask("git remote change")
    if sub == "config" and ("--global" in flags or "--system" in flags):
        raise Ask("git config --global/--system")
    if sub in DEST_GIT and sub not in ("worktree", "submodule", "bisect", "notes", "replace"):
        raise Ask("git " + sub)
    if sub == "worktree" and pos[:1] in (["remove"], ["prune"]):
        raise Ask("git worktree remove/prune")
    if any(f in ("--force", "-f", "--force-with-lease") for f in flags) and sub not in (
            "add", "commit", "stash", "diff", "log", "show", "status", "apply", "merge-base"):
        raise Ask("git " + sub + " --force")
    # frozen branches from the config (e.g. a mirror of upstream that must never
    # receive commits)
    if sub in GIT_COMMIT_LIKE and conf.get("frozen_branches"):
        try:
            def git(*a):
                return subprocess.run(["git", "-C", cwd or ".", *a], capture_output=True,
                                      text=True, timeout=3)
            current = git("symbolic-ref", "--short", "HEAD").stdout.strip()
            for fb in conf["frozen_branches"]:
                tag = fb.get("if_tag")
                if current == fb.get("branch") and (
                        not tag or git("rev-parse", "-q", "--verify", "refs/tags/" + tag).returncode == 0):
                    raise Ask("on frozen branch `%s` (config: frozen_branches)" % current)
        except (OSError, subprocess.SubprocessError):
            pass


def check_curl(name, args):
    joined = " ".join(args)
    if re.search(r"(^|\s)(-X|--request)\s*(POST|PUT|DELETE|PATCH)\b", joined, re.I) or \
       re.search(r"(^|\s)(-d|--data\S*|-F|--form\S*|-T|--upload-file|--json)(\s|=|$)", joined) or \
       re.search(r"(^|\s)-[A-Za-z]*[dFT]\b", joined):
        raise Ask(name + " sending data outward")


def check_sql(name, words, extra_text, conf):
    text = re.sub(r"\bshow\s+create\b", "show", " ".join(words) + "\n" + extra_text, flags=re.I)
    if not SQL_WRITE_RE.search(text):
        return
    scratch = any(re.search(p, text) for p in conf.get("scratch_db_markers", []))
    if name == "sqlite3":
        paths = [w for w in words[1:] if not w.startswith("-") and ("/" in w or "." in w)]
        scratch = scratch or any(is_scratch(norm(p, "/")) for p in paths[:1])
    if scratch and not SQL_NEVER_RE.search(text):
        return
    raise Ask(name + ": SQL that writes/drops on a non-scratch database")


def check_interpreter_code(name, code):
    if name.startswith("python"):
        rx = PY_DANGER_RE
    elif name in ("node", "deno", "bun", "nodejs"):
        rx = JS_DANGER_RE
    else:
        rx = GEN_DANGER_RE
    m = rx.search(code)
    if m:
        raise Ask("%s code calls %s" % (name, m.group(0).strip()))
    if SENSITIVE_STR_RE.search(code):
        raise Ask(name + " code touches a sensitive path")


def check_segment(words, ctx, depth):
    conf, cwd, roots = ctx["conf"], ctx["cwd"], ctx["roots"]
    if not words:
        return
    # leading shell keywords
    while words and words[0] in KEYWORDS_PREFIX:
        words = words[1:]
    if not words or words[0] in KEYWORDS_SKIP_SEGMENT or words[0] in NOOP_END:
        return
    name, args, wrappers = resolve_command(words)
    if not name:
        return
    if name in conf.get("extra_allow_commands", []):
        return
    if name in conf.get("extra_ask_commands", []):
        raise Ask(name + " (listed in extra_ask_commands)")
    if "xargs" in wrappers and name in ("rm", "rmdir", "unlink", "shred", "mv", "chmod", "chown"):
        raise Ask("xargs " + name)

    # nested shell
    if name in SHELLS:
        if "-c" in args or any(a.startswith("-") and "c" in a[1:] and not a.startswith("--")
                               for a in args):
            idx = next((i for i, a in enumerate(args) if a == "-c" or (a.startswith("-") and
                        not a.startswith("--") and "c" in a[1:])), None)
            if idx is not None and idx + 1 < len(args):
                analyze(args[idx + 1], ctx, depth + 1)
                return
        script = [a for a in args if not a.startswith("-")]
        if not script:
            raise Ask("shell reading commands from stdin")
        return  # running a script file: same as any other program
    if name in ("source", "."):
        return
    if name in ("python", "python3", "python2", "node", "nodejs", "deno", "bun", "perl", "ruby",
                "php", "lua", "Rscript", "awk", "gawk"):
        for i, a in enumerate(args):
            if a in ("-c", "-e", "-E", "-p", "--eval", "-r") and i + 1 < len(args):
                check_interpreter_code(name, args[i + 1])
            if a == "-" or (a == "--" and False):
                pass
        if name in ("awk", "gawk"):
            joined = " ".join(args)
            if re.search(r"\bsystem\s*\(|\|\s*getline|\"\s*\|", joined):
                raise Ask("awk running commands")
        return

    if name in ALWAYS_ASK or ASK_MKFS.match(name):
        raise Ask(name + " is on the always-ask list")
    if name == "systemctl" or name == "service":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub not in SYSTEMCTL_SAFE:
            raise Ask("systemctl " + sub)
    elif name == "journalctl" and any(a.startswith("--vacuum") or a == "--rotate" for a in args):
        raise Ask("journalctl vacuum")
    elif name in ("rm",):
        check_rm(args, cwd, roots)
    elif name == "git":
        check_git(args, cwd, conf)
    elif name == "gh":
        pos = strip_flags(args)
        safe = {"view", "list", "status", "diff", "checks", "browse", "search", "watch", "download"}
        if not (pos[:1] == ["auth"] and pos[1:2] == ["status"]) and (
                len(pos) < 2 or pos[1] not in safe) and pos[:1] not in (["status"], ["search"]):
            raise Ask("gh " + " ".join(pos[:2]))
        if pos[:1] == ["api"] and re.search(r"-X\s*(POST|PUT|PATCH|DELETE)|--method", " ".join(args)):
            raise Ask("gh api write")
    elif name in ("curl", "wget", "http", "https", "xh"):
        check_curl(name, args)
    elif name == "find":
        if "-delete" in args or any(a in ("-exec", "-execdir", "-ok") for a in args):
            j = " ".join(args)
            if "-delete" in args or re.search(r"-(exec|execdir|ok)\s+(rm|shred|mv|chmod|chown|dd|unlink)\b", j):
                raise Ask("find with -delete / -exec rm")
    elif name in ("docker", "podman", "docker-compose", "podman-compose", "nerdctl", "buildah"):
        pos = strip_flags(args)
        risky = {"stop", "rm", "rmi", "kill", "restart", "start", "exec", "prune", "down", "pause",
                 "unpause", "update", "cp", "commit", "push", "login", "logout"}
        if any(p in risky for p in pos[:3]) or (pos[:1] == ["system"] or pos[:1] == ["volume"]
                                                or pos[:1] == ["network"]) and pos[1:2] in (
                ["prune"], ["rm"]):
            raise Ask("container operation that can touch your existing containers: " + " ".join(pos[:3]))
    elif name in ("npm", "pnpm", "yarn", "bun", "pip", "pip3", "pipx", "cargo", "gem", "go", "uv",
                  "composer", "poetry"):
        pos = strip_flags(args)
        j = " ".join(args)
        if pos[:1] in (["publish"], ["uninstall"], ["remove"], ["unpublish"], ["login"]) or \
           (pos[:1] in (["install"], ["i"], ["add"]) and re.search(r"(^|\s)(-g|--global|--user|--break-system-packages)\b", j)) or \
           (name in ("pip", "pip3", "pipx", "gem", "uv") and pos[:1] in (["install"], ["uninstall"]) and "venv" not in j and "-r" not in args and False):
            raise Ask(name + " " + " ".join(pos[:2]))
        if name in ("pip", "pip3") and pos[:1] == ["install"] and not re.search(r"(^|\s)-e\b|--target|--prefix", j) \
                and not os.environ.get("VIRTUAL_ENV"):
            raise Ask("pip install into the system Python")
    elif name in SQL_CLIENTS:
        check_sql(name, [name] + args, ctx["heredoc_text"], conf)
    elif name in ("mysqldump", "pg_dump", "pg_dumpall", "mariadb-dump"):
        pass
    elif name == "rsync":
        j = " ".join(args)
        if "--delete" in j or re.search(r"\S+@\S+:|(^|\s)[\w.-]+:[/~\w]", j):
            raise Ask("rsync --delete or to a remote host")
    elif name == "openssl" and any(a in ("genrsa", "req", "ca", "x509") for a in args):
        pass

    # generic: a writer command aimed at a sensitive path (reads are fine)
    dests = []
    pos = strip_flags(args)
    if name in ("cp", "install", "ln", "rsync") and pos:
        dests = [pos[-1]]
    elif name in ("mv", "tee", "touch", "mkdir", "truncate", "patch"):
        dests = pos
    elif name in ("sed", "perl") and any(a == "-i" or a.startswith("-i") or a.startswith("--in-place")
                                         for a in args):
        dests = pos
    elif name in ("curl", "wget"):
        for k, a in enumerate(args):
            if a in ("-o", "-O", "--output", "--output-document") and k + 1 < len(args):
                dests.append(args[k + 1])
    for a in dests:
        if not (a.startswith("/") or a.startswith("~") or a.startswith("$HOME")):
            continue
        if sensitive_write(norm(a, cwd)):
            raise Ask("%s writing to sensitive path %s" % (name, a))
    # any command touching secret material
    for a in args:
        if not a.startswith("-") and "$" not in a and secret_path(norm(a, cwd)):
            if name not in ("ls", "stat", "file", "test", "[", "git"):
                raise Ask("touches secret file " + a)


def analyze(cmd, ctx, depth=0):
    if depth > 4:
        raise Ask("command nesting too deep")
    body, docs = strip_heredocs(cmd)
    heredoc_text = "\n".join(d[1] for d in docs)
    ctx = dict(ctx)
    ctx["heredoc_text"] = ctx.get("heredoc_text", "") + "\n" + heredoc_text
    segs, subs, redirs = lex(body)

    for target in redirs:
        if target in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or target.startswith("&"):
            continue
        if "$SUB" in target:
            continue
        p = norm(target, ctx["cwd"])
        if sensitive_write(p):
            raise Ask("redirect into sensitive path " + target)
    for line_before, doc in docs:
        segs_b, _, _ = lex(line_before) if line_before.strip() else ([], [], [])
        hint = segs_b[-1] if segs_b else []
        hname, hargs, _ = resolve_command(hint)
        if hname in SHELLS and not any(a == "-c" for a in hargs):
            analyze(doc, ctx, depth + 1)
        elif hname.startswith("python") or hname in ("node", "nodejs", "deno", "bun", "perl", "ruby",
                                                       "php", "lua", "Rscript"):
            check_interpreter_code(hname, doc)
        elif hname in SQL_CLIENTS:
            pass  # handled by check_sql through heredoc_text
        elif hname in ("sudo", "su", "doas"):
            raise Ask("privileged heredoc")
    for s in subs:
        analyze(s, ctx, depth + 1)
    for words in segs:
        check_segment(words, ctx, depth)
        # `curl ... | sh` style pipelines are caught by the bare-shell rule above


# --------------------------------------------------------------------------
# non-Bash tools
# --------------------------------------------------------------------------
def analyze_file_tool(tool, tin, ctx):
    path = tin.get("file_path") or tin.get("notebook_path") or tin.get("path") or ""
    if not path:
        return None
    p = norm(path, ctx["cwd"])
    if tool == "Read":
        if secret_path(p):
            raise Ask("reading secret file " + path)
        return ("allow", "read of a non-secret file")
    # Write / Edit / NotebookEdit / MultiEdit
    if sensitive_write(p) or secret_path(p):
        raise Ask("writing sensitive file " + path)
    if "/.git/" in p + "/" and not p.endswith(".gitignore"):
        raise Ask("writing inside .git")
    mem = re.match(r"^" + re.escape(HOME) + r"/\.claude/projects/[^/]+/memory(/|$)", p)
    if is_scratch(p) or mem or inside(p, ctx["roots"]):
        return ("allow", "edit inside project/scratchpad/memory")
    return None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def log(decision, reason, tool, cwd, text):
    try:
        os.makedirs(HOOK_DIR, exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:
            with open(LOG) as f:
                keep = f.readlines()[-500:]
            with open(LOG, "w") as f:
                f.writelines(keep)
        one = " ".join(text.split())[:240]
        with open(LOG, "a") as f:
            f.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), decision, tool, reason, cwd, one))
    except OSError:
        pass


def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


def main():
    try:
        with open(TOGGLE) as f:
            if f.read().strip() != "on":
                return 0
    except OSError:
        pass
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if data.get("permission_mode") == "plan":
        return 0  # never override plan mode
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    conf = load_conf()
    ctx = {"conf": conf, "cwd": cwd, "roots": project_roots(cwd, conf), "heredoc_text": ""}

    try:
        if tool == "Bash":
            cmd = tin.get("command") or ""
            if not cmd.strip():
                return 0
            try:
                analyze(cmd, ctx)
            except ParseError:
                # can't parse it reliably: fall back to a blunt whole-text check
                if re.search(r"\b(sudo|rm|dd|kill|killall|chmod|chown|mkfs|shutdown|reboot)\b|"
                             r"--force|--hard|\|\s*(sh|bash)\b", cmd):
                    raise Ask("unparseable command containing a dangerous word")
            log("allow", "ok", tool, cwd, cmd)
            emit("allow", "bash-guard: no destructive command found")
            return 0
        res = analyze_file_tool(tool, tin, ctx)
        if res is None:
            return 0  # no opinion: normal permission rules
        log(res[0], res[1], tool, cwd, json.dumps(tin)[:200])
        emit(res[0], "bash-guard: " + res[1])
        return 0
    except Ask as e:
        text = tin.get("command") or json.dumps(tin)
        log("ask", str(e), tool, cwd, text)
        emit("ask", "bash-guard: " + str(e))
        return 0
    except Exception as e:  # never turn a bug into a silent allow
        log("error", repr(e), tool, cwd, json.dumps(tin)[:200])
        return 0


if __name__ == "__main__":
    sys.exit(main())
