# worktree-handoff

[![tests](https://github.com/cthleung/worktree-handoff/actions/workflows/test.yml/badge.svg)](https://github.com/cthleung/worktree-handoff/actions/workflows/test.yml)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![dependencies](https://img.shields.io/badge/dependencies-none-2ea44f)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Move between computers mid-task without committing unfinished work.

worktree-handoff is a [Claude Code](https://code.claude.com) plugin. It carries your **uncommitted** work (edited files, new files, deletions) from one machine to another through a hidden git ref. Close a chat on your laptop, open one on your desktop, and the desktop's working tree has your edits as plain uncommitted changes, exactly where you left them.

- It never commits to your branch, never stages anything and never touches `.git/index`. Your editor's "changes since the last commit" view stays exactly as it was.
- It needs no server, no sync service and no setup beyond the git remote you already push to.

## What it looks like

A real run between 2 throwaway clones (the log line is wrapped for width):

```console
# on the laptop: the chat closes, the bundle is pushed, nothing is printed
$ worktree-handoff end

# on the desktop: a chat opens
$ worktree-handoff start
worktree-handoff: 2026-11-03 03:23:51 [info] handoff.unwrapped  from_host=laptop host=desktop
    added=['ideas.md'] modified=['notes.md'] shortstat='2 files changed, 2 insertions(+)'
    how='plain apply (bundle parent is HEAD)'
    agent='These files are now uncommitted edits in the working tree, carried over from the
           other computer. Tell the user which files came over.'

$ git status --short
 M notes.md
?? ideas.md
```

The `agent` field is for the coding agent reading the session's startup context. It tells the agent what to say, so the person hears "2 files came over from your laptop" instead of reading a log.

## How it works

```
 laptop                                   GitHub (or any git remote)              desktop
 ──────                                   ──────────────────────────              ───────
 chat closes / every 20 min
   scratch index ← git add -A
   secrets guard drops keys
   commit-tree on top of HEAD  ──push──▶  refs/worktree-handoff/<branch>
                                                     │
                                                     └──fetch──▶  chat opens
                                                                    pull --ff-only
                                                                    git apply (--3way if the
                                                                      branch moved on)
                                                                    = uncommitted edits
```

1. **Bundle:** A scratch index (`GIT_INDEX_FILE`, never your real index) is filled with `git add -A`, so `.gitignore` is respected. The tree is written, and a commit is made on top of `HEAD` that names the computer. That commit is force-pushed to `refs/worktree-handoff/<branch>`. The ref lives outside `refs/heads`, so it never shows up as a branch, and a normal `git fetch` doesn't download it.
2. **Unwrap:** The next computer fetches the ref, checks that the bundle came from elsewhere and hasn't been applied yet, pulls with `--ff-only`, and applies the bundle's diff with `git apply`. If the branch moved on since the bundle was made, it applies with `--3way`, and real clashes are left as conflict markers.
3. **Hooks.**
   - `SessionStart` unwraps, in the foreground, so the files are in place before the agent reads anything.
   - `SessionEnd` bundles, detached, so closing a chat never waits on the network.
   - `Stop` runs a heartbeat that bundles at most every 20 minutes. Editor chat panels keep sessions alive when you switch tabs, so `SessionEnd` alone fires too rarely.

## Safety

**The working assumption is one person, working on one computer at a time.** Everything else is guarded.

| Situation | What happens |
|---|---|
| Local uncommitted edits that aren't the bundle this machine sent | **Conflict:** Nothing is touched. The session's startup context gets the local files, the bundle's files and the overlap, so the agent raises it with you. |
| The tree still holds the bundle this machine sent earlier | It's replaced by the newer bundle, after a backup ref (`refs/worktree-handoff-backup/<time>`). |
| Some of the bundle has been committed since | Those files are skipped, and the rest applies. |
| The branch moved on since the bundle | 3-way apply. A same-line clash leaves markers and gets reported. |
| A merge, rebase, cherry-pick or bisect is in progress | Skipped until it's finished. |
| New files that look like secrets (`.env`, `*.pem`, `id_rsa`, ...) or contain tokens (AWS, GitHub, OpenAI, Anthropic, Slack, Google, GitLab), and tracked files whose new lines contain one | Left out of the bundle and logged. An `allow` list lets a file through. |
| New files over 50 MB | Left out. |
| The remote refuses the push (no access, protected refs) | Logged once, then pushes pause for 24 hours. |
| A hook killed mid-run (editors restart sessions on resume) | The lock records its owner's pid, so a dead owner's lock is cleared at once. A live lock is waited on for up to a minute. |
| Not a git repo, no remote, detached HEAD, no commits | Quietly does nothing. |

## Install

Inside Claude Code:

```
/plugin marketplace add cthleung/worktree-handoff
/plugin install worktree-handoff@worktree-handoff
```

Or from a shell, with `claude plugin marketplace add cthleung/worktree-handoff` and `claude plugin install worktree-handoff@worktree-handoff`.

It needs `git` and Python 3.10+ on the PATH. It uses only the standard library, and pretty-prints with [`structlog`](https://www.structlog.org) when that's installed. On Windows it runs through Git Bash, which comes with Git for Windows.

It's on for every repo by default. Opt out where you don't want it:

```bash
git config worktree-handoff.enabled false   # this repo, this computer
export WORKTREE_HANDOFF=off                  # everywhere
```

## Configure

All of it is optional, in `.worktree-handoff.json` at the repo root, so it travels with the repo:

```json
{
  "enabled": true,
  "exclude": [".specstory", "scratch"],
  "remote": "origin",
  "tick_minutes": 20,
  "pull": true,
  "allow": [".env.example"]
}
```

## Commands

While the plugin is enabled, `worktree-handoff` is on the agent's PATH. You can also run `bin/worktree-handoff` from the plugin folder yourself.

| Command | What it does |
|---|---|
| `status` | Config, local state, the remote bundle, and what `start` would do |
| `start` / `end` | Unwrap or bundle right now |
| `unwrap [--force]` | Apply the bundle. `--force` applies it over local edits, after a backup ref |
| `drop` | Delete this branch's bundle from the remote |

## Logs

The log is JSON lines in `.git/worktree-handoff/handoff.log`, one event per line, with the computer, pid, branch and every decision's inputs. A normal session start prints nothing. Only events that need attention (`handoff.unwrapped`, `handoff.conflict`, `unwrap.conflict_markers`, ...) go into the agent's context, each with an `agent` field saying what to do. The bundled skill covers the rest.

## Tests

```bash
python -m unittest
```

44 tests in 2 layers:

- **30 end-to-end tests** run 2 throwaway clones against a local bare repo standing in for GitHub, and cover the cases in the safety table: the tree and index left alone, conflicts, 3-way applies, markers, backups, branches, opting out, the secrets guard, a remote that refuses pushes, the heartbeat and the lock.
- **14 unit tests** cover the pure pieces (the secrets guard, the log, the pid check) in well under a second.

CI runs ruff and mypy, runs every test on Linux, macOS and Windows, and measures coverage inside the end-to-end subprocesses too (84%, with an 80% floor).

## How this is built

This repo doubles as a record of how I work. Every change lands as a small pull request with green CI, and each one has an entry in the [build log](docs/building-log.md) saying what was wrong, what I decided and why, and how I know it works. The reasons behind the safety table are in the [design notes](docs/design.md), and releases are in the [changelog](CHANGELOG.md). If you'd rather have the short version of how I think as an engineer, with diagrams, that's [how I engineer](docs/how-i-engineer.md). I build with coding agents, and [section 9](docs/how-i-engineer.md#9-working-with-a-coding-agent) of that page says who did what.

The code is a small standard-library package, `scripts/handoff/`, with 1 module per job (settings, the secrets guard, the computer's name, the log, the git plumbing, the commands and the CLI). `scripts/worktree_handoff.py` is the entry point the hooks run.

## Limits

- **The last push wins:** It's built for one person moving between machines, not for two machines editing at once. If both have unsent edits, the guard stops and asks.
- **Bundles carry content, not file modes or the staging state:** On the receiving machine, everything arrives unstaged.
- **The secrets guard is a safety net, not a scanner:** Keep real secrets in `.gitignore`.
