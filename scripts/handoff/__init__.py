"""worktree-handoff: carry uncommitted work between computers through a hidden git ref.

The modules, in the order a run touches them:

    cli       the entry point: parse the command, check the repo, take the lock, dispatch
    config    settings, defaults and WORKTREE_HANDOFF_* environment variables
    host      this computer's stable name, and whether a pid is still alive
    log       JSON-lines log; only events that need attention reach stdout
    gitops    git plumbing: scratch index, bundle commits, patches, state, the lock
    guard     the secrets guard that keeps keys and credentials out of a bundle
    commands  what each command does (bundle, unwrap, status, drop)

The full description and the command list are in cli.py's docstring (also `--help`).
"""
