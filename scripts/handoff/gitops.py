"""Git plumbing: the scratch index, bundles, patches, state and the lock."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import (
    BACKUP_PREFIX,
    CONFIG_FILE,
    DEFAULTS,
    LOCK_STALE_MIN,
    LOCK_WAIT_SEC,
    MAX_NEW_FILE_MB,
    REF_PREFIX,
    env,
)
from .guard import secret_hit, secret_name
from .host import _lock_owner, host_name, pid_alive


def summarize(changes: list[dict]) -> dict:
    by: dict[str, list[str]] = {"A": [], "M": [], "D": []}
    for c in changes:
        by.setdefault(c["status"], []).append(c["path"])
    return {"added": by["A"], "modified": by["M"], "deleted": by["D"], "count": len(changes)}


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

    def worktree_tree(self, held_paths=()) -> tuple[str, list[dict]]:
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
        extra = set(held_paths) - {h["path"] for h in held}
        if extra:  # held from the staged snapshot; keep them out of this one too
            head_paths = set(self.blobs(self.head_tree()))
            new += [p for p in extra if p not in head_paths]
            mods += [p for p in extra if p in head_paths]
        if new:
            self.run("rm", "--cached", "-q", "--ignore-unmatch", "--", *new, env=e)
        if mods:  # the bundle keeps the committed version of these
            self.run("reset", "-q", "HEAD", "--", *mods, env=e)
        tree = self.run("write-tree", env=e)
        index.unlink(missing_ok=True)
        return tree, held

    def staging_tree(self) -> tuple[str, list[dict]]:
        """Build a filtered tree of the user's staged contents without changing the real index."""
        index = self.state_dir / "staging-index"
        index.unlink(missing_ok=True)
        e = {"GIT_INDEX_FILE": str(index)}
        self.run("read-tree", "HEAD", env=e)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--binary", "--no-renames", "HEAD", "--", ".", *self.excludes],
            cwd=self.root, capture_output=True,
        )
        if diff.returncode:
            raise GitError(diff.stderr.decode("utf-8", "replace").strip())
        ok, msg = self.apply(diff.stdout, "--cached", env=e)
        if not ok:
            raise GitError(msg)
        held = self._guard_index(e)
        held += self._ignored_additions(e, skip={h["path"] for h in held})
        paths = [h["path"] for h in held]
        if paths:
            self.run("reset", "-q", "HEAD", "--", *paths, env=e)
        tree = self.run("write-tree", env=e)
        index.unlink(missing_ok=True)
        return tree, held

    def worktree_snapshot(self) -> tuple[str, str, list[dict]]:
        """Return safe working-tree and index trees plus every path held by either guard."""
        index_tree, held = self.staging_tree()
        tree, worktree_held = self.worktree_tree(h["path"] for h in held)
        by_path = {h["path"]: h for h in held}
        by_path.update({h["path"]: h for h in worktree_held})
        return tree, index_tree, list(by_path.values())

    def _ignored_additions(self, e, skip=()) -> list[dict]:
        """New staged files that .gitignore covers (they got in with `git add -f`). The
        working-tree snapshot never carries them, so the staged one doesn't either."""
        out = self.run("diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD", env=e)
        parts = out.split("\0")
        added = [p for s, p in zip(parts[0::2], parts[1::2], strict=False)
                 if s[:1] == "A" and p not in skip]
        if not added:
            return []
        r = subprocess.run(["git", "check-ignore", "--no-index", "-z", "--stdin"], cwd=self.root,
                           input="\0".join(added) + "\0", capture_output=True, text=True)
        return [{"path": p, "status": "A", "why": "ignored by .gitignore"}
                for p in r.stdout.split("\0") if p]

    def _guard_index(self, e) -> list[dict]:
        """Guard staged blobs, rather than reading the possibly different working copy."""
        out = self.run("diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD", env=e)
        parts = out.split("\0")
        entries = self.run("ls-files", "--stage", "-z", env=e).split("\0")
        blobs = {}
        for entry in entries:
            if entry:
                meta, path = entry.split("\t", 1)
                mode, oid, stage = meta.split()
                if stage == "0":
                    blobs[path] = (oid, mode)
        held = []
        for status, path in zip(parts[0::2], parts[1::2], strict=False):
            status = status[:1]
            if status not in ("A", "M") or path in set(self.config["allow"]):
                continue
            why = secret_name(path)
            if why:
                held.append({"path": path, "status": status, "why": f"name looks like a secret ({why})"})
                continue
            blob = blobs.get(path)
            if not blob:
                continue
            oid, _ = blob
            if status == "A":
                size = int(self.run("cat-file", "-s", oid))
                if size > MAX_NEW_FILE_MB * 1024 * 1024:
                    held.append({"path": path, "status": status, "why": f"bigger than {MAX_NEW_FILE_MB} MB"})
                    continue
                if size > 1024 * 1024:
                    continue
                data = subprocess.run(["git", "cat-file", "blob", oid], cwd=self.root,
                                      capture_output=True).stdout
                if b"\0" in data[:8000]:
                    continue
                hit = secret_hit(data.decode("utf-8", "replace"))
            else:
                added = self.run("diff", "--cached", "-U0", "HEAD", "--", path,
                                 env=e, check=False)
                hit = secret_hit("\n".join(line for line in added.splitlines()
                                           if line.startswith("+") and not line.startswith("+++")))
            if hit:
                held.append({"path": path, "status": status, "why": f"content matches {hit}"})
        return held

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

    def staged_changes(self) -> list[dict]:
        out = self.run("diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD")
        parts = out.split("\0")
        return [{"status": s[:1], "path": p}
                for s, p in zip(parts[0::2], parts[1::2], strict=False) if s]

    def shortstat(self, tree_a, tree_b) -> dict:
        return {"shortstat": self.run("diff", "--shortstat", tree_a, tree_b).strip()}

    def commit_tree(self, tree, parent, message, parent2=None) -> str:
        args = ["commit-tree", tree, "-p", parent]
        if parent2:
            args += ["-p", parent2]
        return self.run(*args, "-m", message)

    # patches travel as raw bytes: a stripped or re-encoded patch is a corrupt patch
    def patch(self, tree_a, tree_b, exclude=(), include=()) -> bytes:
        paths = [f":(literal){p}" for p in include] if include else ["."]
        args = ["git", "diff", "--binary", "--no-renames", tree_a, tree_b, "--", *paths]
        args += [f":(exclude){p}" for p in exclude]
        out = subprocess.run(args, cwd=self.root, capture_output=True)
        if out.returncode:
            raise GitError(out.stderr.decode("utf-8", "replace").strip())
        return out.stdout

    def apply(self, patch: bytes, *flags, env=None) -> tuple[bool, str]:
        if not patch:
            return True, ""
        full_env = {**os.environ, **(env or {})}
        out = subprocess.run(["git", "apply", "--whitespace=nowarn", *flags],
                             cwd=self.root, capture_output=True, input=patch, env=full_env)
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
        index_commit = None
        if self.config["preserve_staging"]:
            index_commit = self.commit_tree(self.run("write-tree"), self.head(),
                                            f"worktree-handoff index backup of {host_name()} at {stamp}")
        backup = self.commit_tree(cur_tree, self.head(),
                                  f"worktree-handoff backup of {host_name()} at {stamp}",
                                  parent2=index_commit)
        name = f"{BACKUP_PREFIX}{stamp}"
        self.run("update-ref", name, backup)
        if index_commit:
            # unstage only what the bundle holds; staged excludes and held files stay staged
            staged_tree, _ = self.staging_tree()
            paths = sorted({c["path"] for c in self.changes(self.head_tree(), cur_tree)}
                           | {c["path"] for c in self.changes(self.head_tree(), staged_tree)})
            if paths:
                self.run("reset", "-q", "HEAD", "--pathspec-from-file=-", "--pathspec-file-nul",
                         input="\0".join(paths) + "\0")
        ok, msg = self.apply(self.patch(cur_tree, self.head_tree()))
        if not ok:
            raise GitError(f"could not revert the tree to HEAD: {msg}")
        return name

    def read_bundle(self, ref) -> dict | None:
        commit = self.rev(ref)
        if not commit:
            return None
        raw = self.run("log", "-1", "--format=%H%n%T%n%P%n%ct%n%B", commit)
        commit, tree, parent_text, ctime, *body = raw.split("\n", 4)
        parent_list = parent_text.split()
        parent = parent_list[0] if parent_list else ""
        index_tree = self.rev(f"{parent_list[1]}^{{tree}}") if len(parent_list) > 1 else None
        first = (body[0] if body else "").splitlines()[0] if body and body[0] else ""
        # "wip from HOST at TIME"
        host = first.split(" from ", 1)[1].split(" at ", 1)[0] if " from " in first else "?"
        when = first.split(" at ", 1)[1] if " at " in first else "?"
        return {"commit": commit, "tree": tree, "parent": parent, "index_tree": index_tree,
                "time": int(ctime), "host": host, "when": when, "ref": ref}

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

    # other-branch bundles this folder has already told the agent about: {branch: host}
    def load_announced(self) -> dict:
        try:
            data = json.loads((self.state_dir / "announced.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_announced(self, seen: dict):
        with contextlib.suppress(OSError):
            (self.state_dir / "announced.json").write_text(json.dumps(seen, indent=2), encoding="utf-8")

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
