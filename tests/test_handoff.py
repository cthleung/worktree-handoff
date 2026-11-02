"""worktree-handoff between two throwaway clones, "mac" and "pc", that share a local bare
repo standing in for GitHub. Nothing here touches a real repo or a real remote. Each test
reads the JSON log to see what the script decided.

Run from the repo root:  python -m unittest
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = str(ROOT / "scripts" / "worktree_handoff.py")
PREFIX = "refs/worktree-handoff/"
REF = PREFIX + "main"


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k != "CLAUDE_PROJECT_DIR" and not k.startswith("WORKTREE_HANDOFF")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", GIT_TERMINAL_PROMPT="0", **extra)
    return env


class TwoComputers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="worktree-handoff-test-")
        self.dirs = {}
        self.origin = os.path.join(self.tmp, "origin.git")
        self.sh("git", "init", "-q", "--bare", "-b", "main", self.origin, cwd=self.tmp)
        seed = os.path.join(self.tmp, "seed")
        self.sh("git", "init", "-q", "-b", "main", seed, cwd=self.tmp)
        self.configure(seed)
        self.write(seed, "src/one.txt", "one v1\n")
        self.write(seed, "src/two.txt", "two v1\n")
        self.write(seed, ".gitignore", "ignored.txt\n")
        self.sh("git", "add", "-A", cwd=seed)
        self.sh("git", "commit", "-qm", "init", cwd=seed)
        self.sh("git", "remote", "add", "origin", self.origin, cwd=seed)
        self.sh("git", "push", "-q", "origin", "main", cwd=seed)
        for host in ("mac", "pc"):
            d = os.path.join(self.tmp, host)
            self.sh("git", "clone", "-q", self.origin, d, cwd=self.tmp)
            self.configure(d)
            self.dirs[host] = d
        self.logs = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers

    def sh(self, *args, cwd, check=True):
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=clean_env())
        if check and r.returncode:
            raise AssertionError(f"{' '.join(args)} failed: {r.stderr}")
        return r.stdout.strip()

    def git(self, host, *args, check=True):
        return self.sh("git", *args, cwd=self.dirs[host], check=check)

    def configure(self, d):
        self.sh("git", "config", "user.email", "t@t", cwd=d)
        self.sh("git", "config", "user.name", "t", cwd=d)

    def write(self, where, path, text):
        full = os.path.join(self.dirs.get(where, where), path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, host, path):
        full = os.path.join(self.dirs[host], path)
        if not os.path.exists(full):
            return None
        with open(full, encoding="utf-8") as f:
            return f.read()

    def log_file(self, host):
        return os.path.join(self.tmp, f"{host}.log")

    def run_handoff(self, host, *args, **env):
        return subprocess.run([sys.executable, "-I", SCRIPT, *args], cwd=self.dirs[host],
                              capture_output=True, text=True,
                              env=clean_env(WORKTREE_HANDOFF_HOST=host,
                                            WORKTREE_HANDOFF_LOG=self.log_file(host), **env))

    def handoff(self, host, *args, **env):
        """Run the script on one computer; return the events it logged, by name."""
        r = self.run_handoff(host, *args, **env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.last_stdout = r.stdout
        path = self.log_file(host)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            f.seek(self.logs.get(host, 0))
            lines = f.read()
            self.logs[host] = f.tell()
        return {e["event"]: e for e in map(json.loads, lines.splitlines())}

    def status(self, host):
        return self.git(host, "status", "--porcelain")

    def commit_all(self, host, msg):
        self.git(host, "add", "-A")
        self.git(host, "commit", "-qm", msg)
        self.git(host, "push", "-q", "origin", "HEAD")

    def remote_refs(self, host="mac"):
        return self.git(host, "ls-remote", "origin")

    def hold_lock(self, host, pid):
        lock = os.path.join(self.dirs[host], ".git", "worktree-handoff", "lock")
        os.makedirs(lock)
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(str(pid))

    # ------------------------------------------------------------ the basics

    def test_bundle_leaves_the_tree_and_index_alone(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.write("mac", "new.txt", "brand new\n")
        self.git("mac", "add", "src/one.txt")  # something staged by hand stays staged
        before = self.status("mac")
        ev = self.handoff("mac", "end")
        self.assertIn("bundle.pushed", ev)
        self.assertEqual(self.status("mac"), before)
        self.assertIn(REF, self.remote_refs())
        self.assertNotIn("refs/heads/worktree", self.remote_refs())

    def test_plain_handoff(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.write("mac", "new.txt", "brand new\n")
        self.handoff("mac", "end")
        ev = self.handoff("pc", "start")
        self.assertIn("handoff.unwrapped", ev)
        self.assertEqual(self.read("pc", "src/one.txt"), "one mac\n")
        self.assertEqual(self.read("pc", "new.txt"), "brand new\n")
        self.assertEqual(self.git("pc", "diff", "--cached", "--name-only"), "")  # nothing staged
        self.assertIn("handoff.unwrapped", self.last_stdout)  # the agent hears about it

    def test_ignored_and_excluded_files_stay_home(self):
        self.write("mac", ".worktree-handoff.json", json.dumps({"exclude": ["chats"]}))
        self.write("mac", "ignored.txt", "private\n")
        self.write("mac", "chats/today.md", "chat log\n")
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.handoff("pc", "start")
        self.assertEqual(self.read("pc", "src/one.txt"), "one mac\n")
        self.assertIsNone(self.read("pc", "ignored.txt"))
        self.assertIsNone(self.read("pc", "chats/today.md"))

    def test_deletions_travel(self):
        os.remove(os.path.join(self.dirs["mac"], "src/two.txt"))
        self.handoff("mac", "end")
        self.handoff("pc", "start")
        self.assertIsNone(self.read("pc", "src/two.txt"))

    def test_back_and_forth_replaces_the_stale_bundle(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.handoff("pc", "start")
        self.write("pc", "src/one.txt", "one mac then pc\n")
        self.handoff("pc", "end")
        ev = self.handoff("mac", "start")
        self.assertIn("tree.reverted_to_head", ev)
        self.assertIn("handoff.unwrapped", ev)
        self.assertEqual(self.read("mac", "src/one.txt"), "one mac then pc\n")
        self.assertTrue(self.git("mac", "for-each-ref", "refs/worktree-handoff-backup/"))

    def test_committed_work_is_not_reapplied(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.handoff("pc", "start")
        self.commit_all("pc", "pc commits the mac work")
        self.handoff("pc", "end")  # clean tree: drops nothing of its own, mac's bundle stays
        ev = self.handoff("mac", "start")
        self.assertNotIn("handoff.conflict", ev)
        self.assertEqual(self.read("mac", "src/one.txt"), "one mac\n")
        self.assertEqual(self.status("mac"), "")

    def test_three_way_when_the_branch_moved(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.write("pc", "src/two.txt", "two pc, committed\n")
        self.commit_all("pc", "pc commits something else")
        self.git("pc", "pull", "-q")
        ev = self.handoff("pc", "start")
        self.assertIn("handoff.unwrapped", ev)
        self.assertIn("3-way", ev["handoff.unwrapped"]["how"])
        self.assertEqual(self.read("pc", "src/one.txt"), "one mac\n")
        self.assertEqual(self.git("pc", "diff", "--cached", "--name-only"), "")

    def test_same_line_clash_leaves_markers(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.write("pc", "src/one.txt", "one pc, committed\n")
        self.commit_all("pc", "pc changes the same line")
        ev = self.handoff("pc", "start")
        self.assertIn("unwrap.conflict_markers", ev)
        self.assertIn("<<<<<<<", self.read("pc", "src/one.txt"))
        self.assertEqual(self.git("pc", "diff", "--cached", "--name-only"), "")

    def test_guard_refuses_over_unsent_local_edits(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.write("pc", "src/two.txt", "two pc, never sent\n")
        ev = self.handoff("pc", "start")
        self.assertIn("handoff.conflict", ev)
        self.assertEqual(self.read("pc", "src/one.txt"), "one v1\n")  # nothing changed
        self.assertIn("handoff.conflict", self.last_stdout)
        ev = self.handoff("pc", "status")
        self.assertEqual(ev["status.would_do"]["action"], "conflict")
        ev = self.handoff("pc", "unwrap", "--force")
        self.assertIn("handoff.unwrapped", ev)
        self.assertEqual(self.read("pc", "src/one.txt"), "one mac\n")
        backup = self.git("pc", "for-each-ref", "--format=%(refname)", "refs/worktree-handoff-backup/")
        self.assertEqual(self.git("pc", "show", f"{backup}:src/two.txt"), "two pc, never sent")

    def test_clean_tree_drops_our_own_bundle(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.git("mac", "checkout", "--", ".")
        self.assertIn("bundle.dropped", self.handoff("mac", "end"))
        self.assertNotIn(REF, self.remote_refs())

    def test_unchanged_tree_is_not_pushed_twice(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.assertIn("bundle.unchanged", self.handoff("mac", "end"))

    def test_drop(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.assertIn("bundle.dropped", self.handoff("pc", "drop"))
        self.assertNotIn(REF, self.remote_refs("pc"))

    # ------------------------------------------------------------ branches

    def test_each_branch_gets_its_own_bundle(self):
        self.git("mac", "checkout", "-qb", "feature/x")
        self.git("mac", "push", "-q", "-u", "origin", "feature/x")
        self.write("mac", "src/one.txt", "one on feature\n")
        self.handoff("mac", "end")
        self.assertIn(PREFIX + "feature/x", self.remote_refs())
        self.assertNotIn(REF + "\n", self.remote_refs() + "\n")
        ev = self.handoff("pc", "start")  # pc is on main
        self.assertIn("bundle.other_branches", ev)
        self.assertEqual(ev["bundle.other_branches"]["bundles"][0]["branch"], "feature/x")
        self.assertEqual(self.read("pc", "src/one.txt"), "one v1\n")
        self.git("pc", "checkout", "-q", "feature/x")
        ev = self.handoff("pc", "start")
        self.assertIn("handoff.unwrapped", ev)
        self.assertEqual(self.read("pc", "src/one.txt"), "one on feature\n")

    # ------------------------------------------------------------ opting out and skipping

    def test_opt_out_by_config_file(self):
        self.write("mac", ".worktree-handoff.json", json.dumps({"enabled": False}))
        self.write("mac", "src/one.txt", "one mac\n")
        self.assertEqual(self.handoff("mac", "end"), {})
        self.assertNotIn(PREFIX, self.remote_refs())

    def test_opt_out_on_one_computer(self):
        self.git("mac", "config", "worktree-handoff.enabled", "false")
        self.write("mac", "src/one.txt", "one mac\n")
        self.assertEqual(self.handoff("mac", "end"), {})
        self.assertNotIn(PREFIX, self.remote_refs())

    def test_off_everywhere(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.assertEqual(self.handoff("mac", "end", WORKTREE_HANDOFF="off"), {})
        self.assertNotIn(PREFIX, self.remote_refs())

    def test_no_remote_is_skipped_quietly(self):
        self.git("mac", "remote", "remove", "origin")
        self.write("mac", "src/one.txt", "one mac\n")
        ev = self.handoff("mac", "start")
        self.assertIn("skip.no_remote", ev)
        self.assertEqual(self.last_stdout, "")

    def test_not_a_repo_is_ignored(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        r = subprocess.run([sys.executable, "-I", SCRIPT, "start"], cwd=plain,
                           capture_output=True, text=True, env=clean_env())
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_skips_during_a_rebase(self):
        self.write("mac", "src/one.txt", "one mac\n")
        os.makedirs(os.path.join(self.dirs["mac"], ".git", "rebase-merge"))
        ev = self.handoff("mac", "end")
        self.assertIn("skip.in_progress", ev)
        self.assertNotIn(PREFIX, self.remote_refs())

    def test_start_is_silent_when_nothing_happens(self):
        self.handoff("mac", "start")
        self.assertEqual(self.last_stdout, "")

    # ------------------------------------------------------------ the secrets guard

    def test_secret_looking_files_stay_home(self):
        self.write("mac", ".env", "API_KEY=hunter2\n")
        self.write("mac", "deploy.pem", "not really a key\n")
        self.write("mac", "notes.md", "token ghp_" + "a" * 36 + "\n")
        self.write("mac", "fine.md", "nothing to see\n")
        ev = self.handoff("mac", "end")
        held = {h["path"] for h in ev["bundle.secrets_skipped"]["files"]}
        self.assertEqual(held, {".env", "deploy.pem", "notes.md"})
        self.handoff("pc", "start")
        self.assertEqual(self.read("pc", "fine.md"), "nothing to see\n")
        for path in (".env", "deploy.pem", "notes.md"):
            self.assertIsNone(self.read("pc", path), path)

    def test_a_key_pasted_into_a_tracked_file_stays_home(self):
        self.write("mac", "src/one.txt", "one v1\nAKIA" + "B" * 16 + "\n")
        self.write("mac", "src/two.txt", "two mac\n")
        ev = self.handoff("mac", "end")
        self.assertEqual(ev["bundle.secrets_skipped"]["files"][0]["path"], "src/one.txt")
        self.handoff("pc", "start")
        self.assertEqual(self.read("pc", "src/one.txt"), "one v1\n")
        self.assertEqual(self.read("pc", "src/two.txt"), "two mac\n")

    def test_allow_list_lets_a_file_through(self):
        self.write("mac", ".worktree-handoff.json", json.dumps({"allow": [".env.example"]}))
        self.write("mac", ".env.example", "API_KEY=\n")
        self.handoff("mac", "end")
        self.handoff("pc", "start")
        self.assertEqual(self.read("pc", ".env.example"), "API_KEY=\n")

    # ------------------------------------------------------------ a remote that says no

    def test_refused_push_backs_off(self):
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w", newline="\n") as f:
            f.write("#!/bin/sh\necho 'permission denied for this ref' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        self.write("mac", "src/one.txt", "one mac\n")
        ev = self.handoff("mac", "end")
        self.assertIn("push.denied", ev)
        self.write("mac", "src/one.txt", "one mac, more\n")
        ev = self.handoff("mac", "end")
        self.assertIn("push.skipped", ev)

    # ------------------------------------------------------------ the heartbeat

    def test_tick_waits_its_turn(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.assertIn("bundle.pushed", self.handoff("mac", "tick"))
        self.write("mac", "src/one.txt", "one mac again\n")
        self.assertEqual(self.handoff("mac", "tick"), {})  # too soon: no run, no log lines
        self.assertIn("bundle.pushed", self.handoff("mac", "tick", WORKTREE_HANDOFF_TICK_MIN="0"))

    def test_tick_then_new_chat_on_the_same_computer(self):
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "tick")
        self.write("mac", "src/two.txt", "two mac, after the tick\n")
        ev = self.handoff("mac", "start")
        self.assertNotIn("handoff.conflict", ev)
        self.assertEqual(self.read("mac", "src/two.txt"), "two mac, after the tick\n")
        self.handoff("pc", "start")
        self.assertEqual(self.read("pc", "src/one.txt"), "one mac\n")

    # ------------------------------------------------------------ the lock

    def test_lock_left_by_a_killed_run_is_cleared(self):
        """Resuming a chat starts, kills and restarts it; a killed start used to leave its
        lock behind, so the real start stepped aside and nothing got unwrapped."""
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        self.write("mac", "src/one.txt", "one mac\n")
        self.handoff("mac", "end")
        self.hold_lock("pc", dead.pid)
        ev = self.handoff("pc", "start")
        self.assertIn("lock.cleared", ev)
        self.assertIn("handoff.unwrapped", ev)

    def test_live_lock_is_waited_for_then_stepped_around(self):
        self.hold_lock("mac", os.getpid())  # this test process is alive
        self.write("mac", "src/one.txt", "one mac\n")
        ev = self.handoff("mac", "end", WORKTREE_HANDOFF_LOCK_WAIT="1")
        self.assertIn("lock.waiting", ev)
        self.assertIn("lock.busy", ev)
        self.assertNotIn("bundle.pushed", ev)

    @unittest.skipIf(os.name == "nt", "no SIGTERM to send on Windows")
    def test_stopped_run_logs_and_exits(self):
        self.hold_lock("mac", os.getpid())
        proc = subprocess.Popen([sys.executable, "-I", SCRIPT, "end"], cwd=self.dirs["mac"],
                                env=clean_env(WORKTREE_HANDOFF_HOST="mac",
                                              WORKTREE_HANDOFF_LOG=self.log_file("mac")),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log = Path(self.log_file("mac"))
        for _ in range(100):
            if log.exists() and "lock.waiting" in log.read_text():
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 128 + signal.SIGTERM)
        self.assertIn("run.stopped", log.read_text())


if __name__ == "__main__":
    unittest.main()
