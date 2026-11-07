"""worktree-handoff: carry uncommitted work between computers through a hidden git ref.

Commits carry finished work. This carries the UNFINISHED work: tracked files you've edited,
new untracked files and deletions, exactly as they sit in the working tree. It never
commits to your branch and never leaves anything staged, and bundles are built in a
scratch index, so "what changed since my last commit" in your editor stays as it was.

  worktree-handoff end     bundle the working tree and push it (SessionEnd)
  worktree-handoff tick    the same, at most every tick_minutes (Stop, after each reply)
  worktree-handoff start   pull, fetch the bundle, unwrap it into the tree (SessionStart)
  worktree-handoff status  what's here, what's on the remote, and what `start` would do
  worktree-handoff unwrap [--force]   apply the bundle now (--force: over local edits,
                           after a backup ref)
  worktree-handoff drop    delete this branch's bundle from the remote
  worktree-handoff switch BRANCH   park this branch's uncommitted work in its bundle,
                           switch, and bring back what BRANCH had parked
  worktree-handoff switch -c NEW [START]   the same, onto a new branch

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

import os
import signal
import sys
from pathlib import Path

from .commands import cmd_drop, cmd_end, cmd_start, cmd_status, cmd_switch, cmd_unwrap
from .config import CONFIG_FILE, env
from .gitops import Git, GitError, find_root
from .host import host_name
from .log import Log

COMMANDS = ("end", "tick", "start", "status", "unwrap", "drop", "switch")


def main(argv) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command, flags = argv[0], argv[1:]
    if command not in COMMANDS:
        print(f"worktree-handoff: unknown command {command!r}; try --help", file=sys.stderr)
        return 2
    by_hand = command in ("status", "unwrap", "drop", "switch")
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
        if command == "switch":
            return cmd_switch(g, log, flags)
        return cmd_drop(g, log)
    except GitError as e:
        log.error("git.failed", error=str(e), agent="Tell the user; nothing else was changed.")
        return 0
    finally:
        g.unlock()
        log.info("run.end")
