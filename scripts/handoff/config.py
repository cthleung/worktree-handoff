"""Settings, defaults and the environment variables the handoff reads."""

from __future__ import annotations

import os

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


def env(name, default=None):
    return os.environ.get(f"WORKTREE_HANDOFF_{name}", default)
