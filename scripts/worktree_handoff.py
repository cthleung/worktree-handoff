#!/usr/bin/env python3
"""worktree-handoff: carry uncommitted work between computers through a hidden git ref.

Commits carry finished work. This carries the UNFINISHED work: tracked files you've edited,
new untracked files and deletions, exactly as they sit in the working tree. It never
commits to your branch, never stages anything and never touches .git/index, so "what
changed since my last commit" in your editor stays exactly as it was.

  worktree-handoff end     bundle the working tree and push it (SessionEnd)
  worktree-handoff tick    the same, at most every tick_minutes (Stop, after each reply)
  worktree-handoff start   pull, fetch the bundle, unwrap it into the tree (SessionStart)
  worktree-handoff status  what's here, what's on the remote, and what `start` would do
  worktree-handoff unwrap [--force]   apply the bundle now (--force: over local edits,
                           after a backup ref)
  worktree-handoff drop    delete this branch's bundle from the remote

How a bundle is made: a scratch index (never .git/index) gets `git add -A`, which keeps
.gitignore'd files out. The secrets guard then drops new files that look like keys or
credentials, and any file whose new lines contain one. The tree is written, a commit is
made on top of HEAD naming the computer, and that commit is force-pushed to
refs/worktree-handoff/<branch>. It lives outside refs/heads, so it never shows up as a
branch, and a normal fetch doesn't download it.

How it's unwrapped: the other computer fetches the ref, checks the bundle came from
elsewhere and hasn't been applied yet, checks its own tree is clean (or still holds the
bundle it last sent, which is safe to replace after a backup), pulls with --ff-only, then
applies the bundle's diff with `git apply`. If the branch moved on since the bundle was
made it applies with --3way. Anything it can't do safely is logged as a conflict and left
alone, and the log line carries an `agent` field telling the coding agent what to do.

Config, all optional: .worktree-handoff.json at the repo root (it travels with the repo)
  {"enabled": true, "exclude": [".specstory"], "remote": "origin", "tick_minutes": 20,
   "pull": true, "allow": ["path/the/secrets/guard/should/let/through"]}
Opt out on one computer only: git config worktree-handoff.enabled false
Turn it off everywhere: WORKTREE_HANDOFF=off in the environment.

Logging: JSON lines in <git dir>/worktree-handoff/handoff.log (WORKTREE_HANDOFF_LOG
overrides). On SessionStart, stdout goes into the agent's context, so only the events the
agent should act on are printed there; `status` prints everything.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REF_PREFIX = "refs/worktree-handoff/"
BACKUP_PREFIX = "refs/worktree-handoff-backup/"
CONFIG_FILE = ".worktree-handoff.json"
DEFAULTS = {"enabled": True, "exclude": [], "remote": "origin", "tick_minutes": 20,
            "pull": True, "allow": []}
NET_TIMEOUT = 60        # seconds for any one fetch, pull or push
LOCK_STALE_MIN = 10     # a lock with no owner we can check is cleared after this
LOCK_WAIT_SEC = 60      # how long a run waits for another one to finish
DENIED_BACKOFF_H = 24   # after the remote refuses a push, wait this long before trying again
MAX_NEW_FILE_MB = 50    # new untracked files bigger than this stay out of the bundle

# Events printed to stdout on a normal run. Everything else goes to the log file only, so
# a session in a repo where nothing happened gets no noise in its context.
LOUD_EVENTS = {
    "handoff.unwrapped", "handoff.conflict", "unwrap.conflict_markers", "unwrap.blocked",
    "unwrap.failed", "resume.own_bundle_tree_clean", "bundle.already_in_main",
    "tree.reverted_to_head", "bundle.other_branches", "git.failed", "skip.in_progress",
}

# ---------------------------------------------------------------- the secrets guard

SECRET_NAMES = [
    ".env", ".env.*", "*.env", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc",
    ".netrc", "*.tfvars", "*.tfstate", "credentials", "credentials.*", "*credentials*.json",
    "*secret*", "*.ovpn", ".htpasswd", "service-account*.json",
]
SECRET_CONTENT = [
    re.compile(p) for p in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bAKIA[0-9A-Z]{16}\b",                 # AWS access key id
        r"\bgh[pousr]_[A-Za-z0-9]{36,}",         # GitHub tokens
        r"\bgithub_pat_[A-Za-z0-9_]{50,}",
        r"\bsk-ant-[A-Za-z0-9_-]{20,}",          # Anthropic
        r"\bsk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{32,}",  # OpenAI, OpenRouter
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",       # Slack
        r"\bAIza[0-9A-Za-z_-]{35}\b",            # Google API key
        r"\bglpat-[A-Za-z0-9_-]{20,}",           # GitLab
    )
]


def secret_hit(text: str) -> str | None:
    for pat in SECRET_CONTENT:
        m = pat.search(text)
        if m:
            return pat.pattern
    return None


def secret_name(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1].lower()
    for pat in SECRET_NAMES:
        if fnmatch.fnmatch(name, pat):
            return pat
    return None


# ---------------------------------------------------------------- small helpers


def env(name, default=None):
    return os.environ.get(f"WORKTREE_HANDOFF_{name}", default)


_HOST = None


def host_name() -> str:
    """A name for this computer that never changes. The network hostname can't be trusted
    (a Mac on a phone hotspot or a new router gets renamed), and the handoff tells its own
    bundles from the other computer's by this name. So it's picked once from the OS's own
    setting and kept in global git config (worktree-handoff.host); set that to rename."""
    global _HOST
    if _HOST:
        return _HOST
    name = env("HOST")
    if not name:
        out = subprocess.run(["git", "config", "--global", "--get", "worktree-handoff.host"],
                             capture_output=True, text=True)
        name = out.stdout.strip()
    if not name:
        name = _os_computer_name()
        subprocess.run(["git", "config", "--global", "worktree-handoff.host", name],
                       capture_output=True)
    _HOST = name
    return name


def _os_computer_name() -> str:
    if sys.platform == "darwin":
        out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    if os.name == "nt" and os.environ.get("COMPUTERNAME"):
        return os.environ["COMPUTERNAME"]
    return (platform.node() or "unknown").split(".")[0]


def _lock_owner(lock: Path):
    try:
        return int((lock / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) would terminate the process on Windows, so ask the kernel.
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def summarize(changes: list[dict]) -> dict:
    by: dict[str, list[str]] = {"A": [], "M": [], "D": []}
    for c in changes:
        by.setdefault(c["status"], []).append(c["path"])
    return {"added": by["A"], "modified": by["M"], "deleted": by["D"], "count": len(changes)}


# ---------------------------------------------------------------- logging


class Log:
    """structlog's shape (bind / info / warning / error) without needing structlog. Every
    event goes to the log file as one JSON line. stdout gets the loud events (or all of
    them when verbose) as readable key=value lines. When structlog is installed it renders
    the stdout lines; the file format is the same either way."""

    def __init__(self, path: Path, verbose: bool, context=None):
        self.path, self.verbose, self.context = path, verbose, dict(context or {})
        try:
            import structlog
            self._render = structlog.dev.ConsoleRenderer(colors=False)
        except ImportError:
            self._render = None

    def bind(self, **kw):
        return Log(self.path, self.verbose, {**self.context, **kw})

    def _emit(self, level, event, kw):
        rec = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level,
               "event": event, **self.context, **kw}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        if not (self.verbose or level != "info" or event in LOUD_EVENTS):
            return
        if self._render:
            line = self._render(None, level, {**rec})
        else:
            rest = " ".join(f"{k}={_short(v)}" for k, v in rec.items()
                            if k not in ("timestamp", "level", "event"))
            line = f"{rec['timestamp']} [{level:<7}] {event:<30} {rest}".rstrip()
        print(f"worktree-handoff: {line}", flush=True)

    def info(self, event, **kw): self._emit("info", event, kw)
    def warning(self, event, **kw): self._emit("warning", event, kw)
    def error(self, event, **kw): self._emit("error", event, kw)


def _short(v):
    s = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else str(v)
    return s if len(s) <= 400 else s[:400] + "..."


# ---------------------------------------------------------------- git plumbing


class GitError(Exception):
    pass


def find_root() -> Path | None:
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start,
                             capture_output=True, text=True)
    except OSError:
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


class Git:
    def __init__(self, root: Path):
        self.root = root
        self.git_dir = Path(self.run("rev-parse", "--absolute-git-dir"))
        self.state_dir = self.git_dir / "worktree-handoff"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()
        self.remote = self.config["remote"]
        self.excludes = [f":(exclude){p}" for p in self.config["exclude"]]

    def _load_config(self) -> dict:
        cfg = dict(DEFAULTS)
        f = self.root / CONFIG_FILE
        if f.exists():
            with contextlib.suppress(OSError, ValueError):
                cfg.update({k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items()
                            if k in DEFAULTS})
        local = self.run("config", "--get", "worktree-handoff.enabled", check=False)
        if local.lower() in ("false", "no", "off", "0"):
            cfg["enabled"] = False
        if env("TICK_MIN") is not None:
            cfg["tick_minutes"] = float(env("TICK_MIN"))
        return cfg

    def run(self, *args, check=True, timeout=None, env=None, input=None) -> str:
        full_env = {**os.environ, **(env or {})}
        try:
            out = subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                                 text=True, timeout=timeout, env=full_env, input=input)
        except subprocess.TimeoutExpired as e:
            raise GitError(f"git {' '.join(args[:2])} timed out after {timeout}s") from e
        if check and out.returncode:
            raise GitError((out.stderr or out.stdout).strip() or f"git {args[0]} failed")
        return out.stdout.strip()

    def try_run(self, *args, **kw) -> tuple[bool, str]:
        try:
            return True, self.run(*args, **kw)
        except GitError as e:
            return False, str(e)

    # where we are
    def branch(self) -> str:
        return self.run("branch", "--show-current", check=False)

    def has_remote(self) -> bool:
        return self.remote in self.run("remote", check=False).split()

    def in_progress(self) -> str | None:
        for name, what in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"),
                           ("rebase-apply", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                           ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
            if (self.git_dir / name).exists():
                return what
        return None

    @property
    def ref(self) -> str:
        return REF_PREFIX + self.branch()

    # refs and trees
    def rev(self, ref) -> str | None:
        ok, out = self.try_run("rev-parse", "--verify", "--quiet", ref)
        return out if ok and out else None

    def head(self) -> str:
        return self.run("rev-parse", "HEAD")

    def head_tree(self) -> str:
        return self.run("rev-parse", "HEAD^{tree}")

    def worktree_tree(self) -> tuple[str, list[dict]]:
        """Tree hash of the working tree as it is right now (tracked edits plus untracked,
        .gitignore and the config's excludes respected, secrets and huge new files left out),
        without touching .git/index. Returns the tree and the files the guard held back."""
        index = self.state_dir / "index"
        index.unlink(missing_ok=True)
        e = {"GIT_INDEX_FILE": str(index)}
        self.run("read-tree", "HEAD", env=e)
        self.run("add", "-A", "--", ".", *self.excludes, env=e)
        held = self._guard(e)
        new = [h["path"] for h in held if h["status"] == "A"]
        mods = [h["path"] for h in held if h["status"] == "M"]
        if new:
            self.run("rm", "--cached", "-q", "--", *new, env=e)
        if mods:  # the bundle keeps the committed version of these
            self.run("reset", "-q", "HEAD", "--", *mods, env=e)
        tree = self.run("write-tree", env=e)
        index.unlink(missing_ok=True)
        return tree, held

    def _guard(self, e) -> list[dict]:
        allow = set(self.config["allow"])
        out = self.run("diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD", env=e)
        parts = out.split("\0")
        held = []
        # -z output ends with a NUL, so the last field is empty and the 2 slices can differ by 1
        for status, path in zip(parts[0::2], parts[1::2], strict=False):
            status = status[:1]
            if status not in ("A", "M") or path in allow:
                continue
            full = self.root / path
            if status == "A":
                why = secret_name(path)
                if why:
                    held.append({"path": path, "status": "A", "why": f"name looks like a secret ({why})"})
                    continue
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                if size > MAX_NEW_FILE_MB * 1024 * 1024:
                    held.append({"path": path, "status": "A", "why": f"bigger than {MAX_NEW_FILE_MB} MB"})
                    continue
                if size > 1024 * 1024:
                    continue
                try:
                    data = full.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:8000]:
                    continue
                hit = secret_hit(data.decode("utf-8", "replace"))
            else:
                added = self.run("diff", "--cached", "-U0", "HEAD", "--", path, env=e, check=False)
                hit = secret_hit("\n".join(line for line in added.splitlines()
                                           if line.startswith("+") and not line.startswith("+++")))
            if hit:
                held.append({"path": path, "status": status, "why": f"content matches {hit}"})
        return held

    def changes(self, tree_a, tree_b) -> list[dict]:
        out = self.run("diff", "--name-status", "--no-renames", "-z", tree_a, tree_b)
        parts = out.split("\0")
        # -z output ends with a NUL, so the slices can differ by 1 (see _guard)
        return [{"status": s[:1], "path": p}
                for s, p in zip(parts[0::2], parts[1::2], strict=False) if s]

    def shortstat(self, tree_a, tree_b) -> dict:
        return {"shortstat": self.run("diff", "--shortstat", tree_a, tree_b).strip()}

    def commit_tree(self, tree, parent, message) -> str:
        return self.run("commit-tree", tree, "-p", parent, "-m", message)

    # patches travel as raw bytes: a stripped or re-encoded patch is a corrupt patch
    def patch(self, tree_a, tree_b, exclude=()) -> bytes:
        args = ["git", "diff", "--binary", "--no-renames", tree_a, tree_b, "--", "."]
        args += [f":(exclude){p}" for p in exclude]
        out = subprocess.run(args, cwd=self.root, capture_output=True)
        if out.returncode:
            raise GitError(out.stderr.decode("utf-8", "replace").strip())
        return out.stdout

    def apply(self, patch: bytes, *flags) -> tuple[bool, str]:
        out = subprocess.run(["git", "apply", "--whitespace=nowarn", *flags],
                             cwd=self.root, capture_output=True, input=patch)
        msg = (out.stderr or out.stdout).decode("utf-8", "replace").strip()
        return out.returncode == 0, msg

    def blobs(self, tree) -> dict[str, str]:
        """path -> blob hash for every file in a tree."""
        out = self.run("ls-tree", "-r", "-z", tree)
        rows = {}
        for entry in out.split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                rows[path] = meta.split(" ")[2]
        return rows

    def revert_to_head(self, cur_tree) -> str:
        """Put the working tree back to HEAD, keeping a backup ref of what it held."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.commit_tree(cur_tree, self.head(),
                                  f"worktree-handoff backup of {host_name()} at {stamp}")
        name = f"{BACKUP_PREFIX}{stamp}"
        self.run("update-ref", name, backup)
        ok, msg = self.apply(self.patch(cur_tree, self.head_tree()))
        if not ok:
            raise GitError(f"could not revert the tree to HEAD: {msg}")
        return name

    def read_bundle(self, ref) -> dict | None:
        commit = self.rev(ref)
        if not commit:
            return None
        raw = self.run("log", "-1", "--format=%H%n%T%n%P%n%ct%n%B", commit)
        commit, tree, parent, ctime, *body = raw.split("\n", 4)
        first = (body[0] if body else "").splitlines()[0] if body and body[0] else ""
        # "wip from HOST at TIME"
        host = first.split(" from ", 1)[1].split(" at ", 1)[0] if " from " in first else "?"
        when = first.split(" at ", 1)[1] if " at " in first else "?"
        return {"commit": commit, "tree": tree, "parent": parent, "time": int(ctime),
                "host": host, "when": when, "ref": ref}

    # state, per branch
    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8")).get(self.branch(), {})
        except (OSError, ValueError, AttributeError):
            return {}

    def save_state(self, **changes):
        try:
            everything = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            everything = {}
        everything[self.branch()] = {**everything.get(self.branch(), {}), **changes}
        self.state_file.write_text(json.dumps(everything, indent=2), encoding="utf-8")

    # lock
    def lock(self, log) -> bool:
        """Take the lock, waiting up to LOCK_WAIT_SEC for another run to finish. The lock
        folder holds the owner's pid, so a lock left by a killed run is cleared at once.
        (Resuming a chat in VS Code starts, kills and restarts it within seconds, which
        can kill a hook mid-run.)"""
        lock = self.state_dir / "lock"
        wait = float(env("LOCK_WAIT", LOCK_WAIT_SEC))
        deadline = time.time() + wait
        waited = False
        while True:
            try:
                lock.mkdir()
                (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
                if waited:
                    log.info("lock.waited", detail="the other run finished; carrying on")
                return True
            except FileExistsError:
                pass
            owner = _lock_owner(lock)
            if owner is not None and not pid_alive(owner):
                shutil.rmtree(lock, ignore_errors=True)
                log.info("lock.cleared", dead_pid=owner, detail="the run holding it was killed")
                continue
            if owner is None and lock.is_dir() and time.time() - lock.stat().st_mtime > LOCK_STALE_MIN * 60:
                shutil.rmtree(lock, ignore_errors=True)
                log.info("lock.cleared", detail=f"no owner recorded and older than {LOCK_STALE_MIN} min")
                continue
            if time.time() >= deadline:
                return False
            if not waited:
                log.info("lock.waiting", owner_pid=owner, up_to_sec=wait)
                waited = True
            time.sleep(0.5)

    def unlock(self):
        shutil.rmtree(self.state_dir / "lock", ignore_errors=True)

    def tick_due(self) -> bool:
        """True when the last tick was more than tick_minutes ago, and marks this one."""
        stamp = self.state_dir / "last_tick"
        if stamp.exists() and time.time() - stamp.stat().st_mtime < float(self.config["tick_minutes"]) * 60:
            return False
        stamp.touch()
        os.utime(stamp)
        return True


# ---------------------------------------------------------------- pushing


DENIED = re.compile(r"denied|403|not allowed|protected|permission|forbidden|unauthori", re.I)


def push(g: Git, log, refspec: str, what: str) -> bool:
    state = g.load_state()
    denied_at = state.get("push_denied_at", 0)
    if denied_at and time.time() - denied_at < DENIED_BACKOFF_H * 3600:
        log.info("push.skipped", detail=f"{g.remote} refused a push earlier; trying again "
                 f"{DENIED_BACKOFF_H}h after that", refused_at=datetime.fromtimestamp(denied_at)
                 .strftime("%Y-%m-%d %H:%M"))
        return False
    ok, msg = g.try_run("push", "-q", "--force", g.remote, refspec, timeout=NET_TIMEOUT)
    if ok:
        if denied_at:
            g.save_state(push_denied_at=0)
        return True
    if DENIED.search(msg):
        g.save_state(push_denied_at=int(time.time()))
        log.warning("push.denied", what=what, error=msg.splitlines()[-1] if msg else "",
                    detail=f"no push access to {g.remote}? pausing pushes for {DENIED_BACKOFF_H}h. "
                           "Opt this repo out with: git config worktree-handoff.enabled false")
    else:
        log.warning("push.failed", what=what, error=msg.splitlines()[-1] if msg else "",
                    retry="the next end, tick or start on this computer pushes it again")
    return False


# ---------------------------------------------------------------- end / tick


def cmd_end(g: Git, log) -> int:
    """Bundle the working tree and push it."""
    head, head_tree = g.head(), g.head_tree()
    tree, held = g.worktree_tree()
    state = g.load_state()
    log = log.bind(head=head[:10], branch=g.branch())
    if held:
        log.warning("bundle.secrets_skipped", files=held,
                    detail="left out of the bundle; add a path to \"allow\" in "
                           f"{CONFIG_FILE} if it's safe to carry")

    if tree == head_tree:
        log.info("tree.clean", detail="nothing uncommitted to carry")
        remote = g.read_bundle(g.ref)
        if remote and remote["host"] == host_name():
            ok, msg = g.try_run("push", "-q", g.remote, f":{g.ref}", timeout=NET_TIMEOUT)
            g.run("update-ref", "-d", g.ref)
            g.save_state(sent=None, sent_tree=None, pushed=True)
            log.info("bundle.dropped", reason="our work is all committed now, so our old "
                     "bundle would be stale on the other computer", remote_deleted=ok)
        return 0

    changes = g.changes(head_tree, tree)
    files = summarize(changes)
    if state.get("sent_tree") == tree and state.get("pushed"):
        log.info("bundle.unchanged", detail="same tree as the last bundle we pushed",
                 bundle=(state.get("sent") or "")[:10], count=files["count"])
        return 0

    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"wip from {host_name()} at {when}", "", f"parent {head[:10]} on {g.branch()}",
             f"{files['count']} files", ""]
    lines += [f"{c['status']} {c['path']}" for c in changes]
    commit = g.commit_tree(tree, head, "\n".join(lines))
    g.run("update-ref", g.ref, commit)
    log.info("bundle.made", bundle=commit[:10], tree=tree[:10], **files, **g.shortstat(head_tree, tree))

    ok = push(g, log, f"{g.ref}:{g.ref}", "bundle")
    g.save_state(sent=commit, sent_tree=tree, sent_time=int(time.time()), pushed=ok)
    if ok:
        log.info("bundle.pushed", bundle=commit[:10], remote=g.remote, ref=g.ref)
    return 0


# ---------------------------------------------------------------- start


def _pull(g: Git, log) -> bool:
    """ff-only pull of the branch's upstream, retried once if another process holds git's
    index lock at the same moment."""
    if not g.config["pull"]:
        return True
    if not g.rev("@{upstream}"):
        log.info("pull.no_upstream", detail="this branch tracks nothing, so there's nothing to pull")
        return True
    for attempt in (1, 2):
        ok, msg = g.try_run("pull", "--ff-only", "-q", timeout=NET_TIMEOUT)
        if ok:
            log.info("branch.pulled", head=g.head()[:10])
            return True
        if "index.lock" in msg and attempt == 1:
            time.sleep(3)
            continue
        log.warning("pull.skipped", error=msg.splitlines()[-1] if msg else "",
                    hint="offline, a diverged branch, or local edits in a file the pull "
                         "touches; `git pull` by hand shows which")
        return False
    return False


def _fetch(g: Git, log) -> dict | None:
    ok, msg = g.try_run("fetch", "-q", "--prune", g.remote,
                        f"+{REF_PREFIX}*:{REF_PREFIX}*", timeout=NET_TIMEOUT)
    if not ok:
        log.warning("bundle.fetch_failed", error=msg.splitlines()[-1] if msg else "",
                    using="whatever ref is already local")
    remote = g.read_bundle(g.ref)
    if remote:
        log.info("bundle.found", bundle=remote["commit"][:10], from_host=remote["host"],
                 bundled_at=remote["when"], parent=remote["parent"][:10])
    else:
        log.info("bundle.none", detail=f"no {g.ref} on {g.remote}")
    _other_branches(g, log)
    return remote


def _other_branches(g: Git, log):
    """Bundles another computer left on a branch this computer isn't on."""
    out = g.run("for-each-ref", "--format=%(refname)", REF_PREFIX, check=False)
    others = []
    for ref in out.splitlines():
        if ref == g.ref:
            continue
        b = g.read_bundle(ref)
        if b and b["host"] != host_name():
            others.append({"branch": ref[len(REF_PREFIX):], "from_host": b["host"], "bundled_at": b["when"]})
    if others:
        log.info("bundle.other_branches", bundles=others, current_branch=g.branch(),
                 agent="Another computer left uncommitted work on a different branch. Mention "
                       "it to the user; switching to that branch and running "
                       "`worktree-handoff start` brings it over.")


def decide(g: Git, log) -> dict:
    """Work out what `start` should do. Returns a plan dict; `start` carries it out and
    `status` only shows it."""
    me = host_name()
    head, head_tree = g.head(), g.head_tree()
    cur, held = g.worktree_tree()
    state = g.load_state()
    dirty = g.changes(head_tree, cur)
    log = log.bind(head=head[:10], branch=g.branch())
    plan: dict[str, Any] = {"action": "nothing", "cur": cur, "head": head, "head_tree": head_tree,
            "dirty": summarize(dirty), "state": state, "held": held}

    log.info("tree.dirty" if dirty else "tree.clean", **(plan["dirty"] if dirty else {}))
    if held:
        log.info("tree.secrets_held", files=held)

    remote = _fetch(g, log)
    plan["remote"] = remote
    if not remote:
        plan["action"] = "push_ours" if state.get("sent") and not state.get("pushed") else "pull_only"
        return plan

    if remote["host"] == me:
        plan["action"] = "resume_own" if dirty else "own_bundle_but_clean"
        if not state.get("pushed") and state.get("sent") and state["sent"] != remote["commit"]:
            plan["action"] = "push_ours"
        return plan

    if state.get("applied") == remote["commit"]:
        plan["action"] = "already_applied"
        return plan
    if cur == remote["tree"]:
        plan["action"] = "already_identical"
        return plan

    ours_stale = bool(dirty) and state.get("sent_tree") == cur
    if dirty and not ours_stale:
        plan["action"] = "conflict"
        plan["why"] = ("this computer has uncommitted edits that are not the bundle it last "
                       "sent, and the remote bundle is from another computer")
        return plan
    if ours_stale and state.get("sent_time", 0) > remote["time"]:
        plan["action"] = "conflict"
        plan["why"] = ("our last bundle is newer than the remote one, so the other computer "
                       "worked without unwrapping ours; both sides have edits")
        return plan

    plan["action"] = "unwrap"
    plan["revert_first"] = ours_stale
    return plan


def cmd_start(g: Git, log) -> int:
    plan = decide(g, log)
    # decide() always sets "remote"; it's None only for pull_only and push_ours
    action, remote, state = plan["action"], plan["remote"], plan["state"]
    cur = plan["cur"]

    if action == "pull_only":
        _pull(g, log)
        return 0

    if action == "push_ours":
        if state.get("sent") and not g.rev(g.ref):  # a fetch --prune can drop an unpushed ref
            g.run("update-ref", g.ref, state["sent"])
        ok = push(g, log, f"{g.ref}:{g.ref}", "bundle")
        g.save_state(pushed=ok)
        if ok:
            log.info("bundle.pushed", bundle=state["sent"][:10], note="sent late; the last push failed")
        _pull(g, log)
        return 0

    if action == "resume_own":
        log.info("resume.own_work", detail="the remote bundle is this computer's and the "
                 "tree still holds it; nothing to unwrap", **plan["dirty"])
        if _pull(g, log) or state.get("sent_tree") != cur:
            return 0
        # The pull refused because our own uncommitted files overlap what the branch now
        # has (the other computer committed our bundle). The tree is exactly our bundle,
        # so it's safe to lift it off, pull, and put back only what the branch lacks.
        backup = g.revert_to_head(cur)
        log.info("tree.reverted_to_head", backup_ref=backup,
                 reason="our own bundle overlaps what the branch now has; lifting it off to pull")
        _pull(g, log)
        _apply_bundle(g, log, remote, note="re-applying this computer's own bundle after the pull")
        return 0

    if action == "own_bundle_but_clean":
        log.warning("resume.own_bundle_tree_clean", bundle=remote["commit"][:10],
                    bundled_at=remote["when"],
                    detail="the remote bundle is this computer's but the tree is clean. Either "
                           "that work got committed (then `worktree-handoff drop`), or it was "
                           "discarded here (then `worktree-handoff unwrap --force` brings it back).",
                    agent="Ask the user which before editing anything.")
        _pull(g, log)
        return 0

    if action in ("already_applied", "already_identical"):
        g.save_state(applied=remote["commit"])
        log.info("bundle.already_here", bundle=remote["commit"][:10], from_host=remote["host"],
                 how="recorded as applied" if action == "already_applied" else "tree matches it")
        _pull(g, log)
        return 0

    if action == "conflict":
        bundle_changes = g.changes(remote["parent"], remote["tree"])
        overlap = sorted(set(p for s in ("added", "modified", "deleted") for p in plan["dirty"][s])
                         & set(c["path"] for c in bundle_changes))
        log.error("handoff.conflict", why=plan["why"], local=plan["dirty"],
                  bundle=remote["commit"][:10], from_host=remote["host"],
                  bundled_at=remote["when"], bundle_files=summarize(bundle_changes), overlap=overlap,
                  agent="Nothing was changed. Tell the user before editing: show them the "
                        "local files, the bundle's files and the overlap, and offer "
                        "`worktree-handoff unwrap --force` (backs up the local tree to "
                        f"{BACKUP_PREFIX}<time> first) or keeping local and `worktree-handoff drop`.")
        return 0

    return _unwrap(g, log, plan)


def _unwrap(g: Git, log, plan, force=False) -> int:
    remote, cur, head_tree = plan["remote"], plan["cur"], plan["head_tree"]
    log = log.bind(bundle=remote["commit"][:10], from_host=remote["host"])

    if cur != head_tree:
        backup = g.revert_to_head(cur)
        log.info("tree.reverted_to_head", backup_ref=backup,
                 reason="forced over local edits" if force else
                        "the tree still held the bundle this computer sent, which the other "
                        "computer has since carried on from",
                 **plan["dirty"])

    _pull(g, log)
    return _apply_bundle(g, log, remote)


def _apply_bundle(g: Git, log, remote, note="") -> int:
    """Apply a bundle's changes onto a working tree that is clean at HEAD. Files the
    bundle has in the same state as HEAD already (work that got committed in the
    meantime) are left out of the patch. If the branch moved on since the bundle's parent
    the patch goes on with --3way, and real conflicts are left as markers and reported."""
    head, head_tree = g.head(), g.head_tree()
    head_blobs, bundle_blobs = g.blobs(head_tree), g.blobs(remote["tree"])
    bundle_changes = g.changes(remote["parent"], remote["tree"])
    already = [c["path"] for c in bundle_changes if bundle_blobs.get(c["path"]) == head_blobs.get(c["path"])]
    todo = [c for c in bundle_changes if c["path"] not in already]
    files = summarize(todo)

    if not todo:
        g.save_state(applied=remote["commit"], sent_tree=None)
        log.info("bundle.already_in_main", bundled_at=remote["when"], files=already,
                 detail="every file in the bundle already matches HEAD, so there's nothing "
                        "uncommitted to bring back")
        return 0

    patch = g.patch(remote["parent"], remote["tree"], exclude=already)
    if remote["parent"] == head:
        how = "plain apply (bundle parent is HEAD)"
        ok, msg = g.apply(patch, "--check")
        if not ok:
            log.error("unwrap.blocked", error=msg, agent="Nothing was changed. Tell the user.")
            return 0
        ok, msg = g.apply(patch)
    else:
        how = f"3-way apply (the branch moved from {remote['parent'][:10]} to {head[:10]})"
        ok, msg = g.apply(patch, "--3way")
        if not ok:
            unmerged = g.run("diff", "--name-only", "--diff-filter=U").splitlines()
            g.run("reset", "-q")  # --3way stages what it merged; keep the index as it was
            g.save_state(applied=remote["commit"], sent_tree=None)
            log.error("unwrap.conflict_markers", how=how, note=note,
                      error=msg.splitlines()[-1] if msg else "", files_with_markers=unmerged,
                      skipped_already_committed=already, **files,
                      agent="The bundle is in the tree but these files have <<<<<<< markers. "
                            "Tell the user and resolve them together before other edits.")
            return 0
        g.run("reset", "-q")
    if not ok:
        log.error("unwrap.failed", how=how, error=msg, agent="Tell the user.")
        return 0

    g.save_state(applied=remote["commit"], sent_tree=None)
    log.info("handoff.unwrapped", how=how, note=note, bundled_at=remote["when"], **files,
             skipped_already_committed=already, **g.shortstat(remote["parent"], remote["tree"]),
             agent="These files are now uncommitted edits in the working tree, carried over "
                   "from the other computer. Tell the user which files came over.")
    return 0


# ---------------------------------------------------------------- the by-hand commands


def cmd_unwrap(g: Git, log, force: bool) -> int:
    plan = decide(g, log)
    if plan["action"] == "unwrap" or (force and plan.get("remote")):
        return _unwrap(g, log, plan, force=force)
    log.info("unwrap.skipped", action=plan["action"], why=plan.get("why", ""))
    return 0


def cmd_status(g: Git, log) -> int:
    log.info("config", **g.config, config_file=str(g.root / CONFIG_FILE),
             log_file=str(log.path), ref=g.ref)
    plan = decide(g, log)
    remote = plan.get("remote")
    if remote:
        log.info("bundle.contents", **summarize(g.changes(remote["parent"], remote["tree"])))
    log.info("status.would_do", action=plan["action"], why=plan.get("why", ""),
             state={k: (v[:10] if isinstance(v, str) and len(v) == 40 else v)
                    for k, v in plan["state"].items()})
    return 0


def cmd_drop(g: Git, log) -> int:
    ok, msg = g.try_run("push", "-q", g.remote, f":{g.ref}", timeout=NET_TIMEOUT)
    if g.rev(g.ref):
        g.run("update-ref", "-d", g.ref)
    g.save_state(sent=None, sent_tree=None, pushed=True)
    log.info("bundle.dropped", ref=g.ref, remote_deleted=ok, note="" if ok else msg)
    return 0


# ---------------------------------------------------------------- main


COMMANDS = ("end", "tick", "start", "status", "unwrap", "drop")


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command, flags = argv[0], argv[1:]
    if command not in COMMANDS:
        print(f"worktree-handoff: unknown command {command!r}; try --help", file=sys.stderr)
        return 2
    by_hand = command in ("status", "unwrap", "drop")
    if os.environ.get("WORKTREE_HANDOFF", "").lower() in ("off", "0", "false"):
        if by_hand:
            print("worktree-handoff: turned off by WORKTREE_HANDOFF=off")
        return 0

    root = find_root()
    if root is None:
        if by_hand:
            print("worktree-handoff: not inside a git repo")
        return 0
    try:
        g = Git(root)
    except GitError:
        return 0
    if not g.config["enabled"]:
        if by_hand:
            print(f"worktree-handoff: off for this repo "
                  f"({CONFIG_FILE} or git config worktree-handoff.enabled)")
        return 0
    if command == "tick" and not g.tick_due():
        return 0

    log_path = Path(env("LOG") or g.state_dir / "handoff.log")
    log = Log(log_path, verbose=by_hand or bool(env("VERBOSE")),
              context={"command": command, "host": host_name(), "pid": os.getpid()})

    if not g.branch():
        log.info("skip.detached", detail="HEAD is detached, so there's no branch to hand off")
        return 0
    if not g.rev("HEAD"):
        log.info("skip.no_commits", detail="the branch has no commits yet")
        return 0
    if not g.has_remote():
        log.info("skip.no_remote", detail=f"no remote called {g.remote!r}")
        return 0
    busy = g.in_progress()
    if busy:
        (log.warning if command == "start" or by_hand else log.info)(
            "skip.in_progress", operation=busy,
            detail=f"a {busy} is in progress; the handoff waits until it's finished")
        return 0

    # A run stopped with SIGTERM or SIGHUP (the editor closing or restarting a chat) exits
    # through the finally below, so it unlocks on the way out.
    def _stopped(signum, _frame):
        log.warning("run.stopped", signal=signal.Signals(signum).name,
                    detail="stopped from outside before finishing; the next run picks up")
        sys.exit(128 + signum)
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        # nohup sets SIGHUP to ignored so a background push outlives the editor; keep that.
        if sig is not None and signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, _stopped)

    if not g.lock(log):
        log.info("lock.busy", detail="another handoff ran past the wait; this one steps aside")
        return 0
    try:
        log.info("run.begin", repo=str(g.root), branch=g.branch())
        if command in ("end", "tick"):
            return cmd_end(g, log)
        if command == "start":
            return cmd_start(g, log)
        if command == "status":
            return cmd_status(g, log)
        if command == "unwrap":
            return cmd_unwrap(g, log, force="--force" in flags)
        return cmd_drop(g, log)
    except GitError as e:
        log.error("git.failed", error=str(e), agent="Tell the user; nothing else was changed.")
        return 0
    finally:
        g.unlock()
        log.info("run.end")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
