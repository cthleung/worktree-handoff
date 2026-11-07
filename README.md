# worktree-handoff

[![tests](https://github.com/cthleung/worktree-handoff/actions/workflows/test.yml/badge.svg)](https://github.com/cthleung/worktree-handoff/actions/workflows/test.yml)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Switch computers mid-task without committing unfinished work.

worktree-handoff is a [Claude Code](https://code.claude.com) plugin. Close a chat on your laptop (a chat is a Claude Code session), open one on your desktop, and the edits you hadn't committed are already in the working tree, new files and deletions included. The plugin does the carrying whenever a chat opens or closes, so there's nothing to run yourself.

Behind the scenes, closing a chat saves a snapshot of your uncommitted files (I call it a bundle) to a hidden git ref on your remote, and opening one on the other computer applies it as ordinary uncommitted changes. The remote you already push to is all it needs.

## What it looks like

A real run between 2 throwaway clones (I wrapped the log line to fit):

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

That log line only prints when something came over. Claude reads it from the session's startup context and opens the chat with something like "2 files came over from your laptop: notes.md and ideas.md", so you hear about it right in the conversation. A normal start, with nothing to carry, prints nothing.

## Install

**Before you turn it on:** a bundle holds the contents of your unfinished files, and it gets pushed to your remote. On a public repo, that makes them public. The plugin is on for every repo by default, so switch it off wherever that matters:

```bash
git config worktree-handoff.enabled false   # this repo, this computer
export WORKTREE_HANDOFF=off                  # everywhere
```

Then, inside Claude Code:

```
/plugin marketplace add cthleung/worktree-handoff
/plugin install worktree-handoff@worktree-handoff
```

Or from a shell: `claude plugin marketplace add cthleung/worktree-handoff`, then `claude plugin install worktree-handoff@worktree-handoff`.

Install it on each computer, with the same remote on both. You need `git` and Python 3.10+ on your PATH, and that's all, since it only uses Python's standard library. On Windows it runs through Git Bash, which comes with Git for Windows. It works in Claude Code in the terminal and in the VS Code extension, which is where I use it. To check it, ask Claude to run `worktree-handoff status`.

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

When a chat closes, the plugin stages your working tree in a scratch index (so bundling never touches your real one), turns that tree into a commit on top of `HEAD` with the computer's name in the message, and force-pushes the commit to `refs/worktree-handoff/<branch>`. The ref sits outside `refs/heads`, so it never shows up as a branch and a normal `git fetch` skips it.

When a chat opens on the other computer, the plugin fetches the ref, checks that the bundle came from somewhere else and hasn't been applied yet, pulls your branch and applies the bundle's diff with `git apply`. If the branch has moved on since, it uses `--3way` and leaves any real clash as conflict markers for you to sort out.

Three Claude Code hooks set all of this off:

- `SessionStart` unwraps in the foreground, so the files are in place before Claude reads anything.
- `SessionEnd` bundles in the background, so closing a chat never waits on the network.
- `Stop` is the heartbeat. It bundles again after Claude finishes a reply, at most every 20 minutes. Editor chat panels keep sessions running when you switch tabs, so `SessionEnd` alone fires too rarely.

## Safety

It assumes one person on one computer at a time. If both machines have unsent edits, it stops and asks you to sort it out.

| Situation | What happens |
|---|---|
| You have edits here that didn't come from a bundle | **Conflict:** nothing is touched. The session's startup context lists your local files, the bundle's files and where they overlap, and Claude raises it with you. |
| The files here are still the last bundle this computer sent | The newer bundle replaces them, after a backup ref (`refs/worktree-handoff-backup/<time>`). |
| You've committed some of the bundle's files since | Those files are skipped and the rest applies. |
| The branch moved on since the bundle | 3-way apply. A clash on the same line leaves markers and gets reported. |
| A new file looks like a secret (`.env`, `*.pem`, `id_rsa`), or a file holds a token | Its changes stay out of the bundle (the other computer gets the committed version, or nothing for a new file) and it's logged. The `allow` setting lets a file through. |
| A new file is over 50 MB | It stays out. |
| It isn't a git repo, there's no remote, HEAD is detached, there are no commits yet, or a merge or rebase is in progress | Nothing happens. |

The smaller guards (a lock that clears when its owner dies, a 24-hour pause on pushes after the remote says no) are covered in the [design notes](docs/design.md).

## Limits

- The heartbeat only runs after Claude replies. If a laptop dies mid-task, you lose what changed since the last bundle, which is 20 minutes at most while Claude's been replying, and longer if you've been hand-editing with no replies.
- Bundles carry file contents. File modes and staging state stay behind, so everything shows up unstaged.
- The secrets check catches the common shapes of keys and tokens. Keep real secrets in `.gitignore`.

## A side task on another branch

You can run a second job, like a long autonomous task, in its own folder on its own branch while you keep working on `main` next to it. Git calls that second folder a worktree:

```bash
git worktree add ../myrepo-side -b side/my-task
```

A chat in that folder works like any other. Its bundle goes to `refs/worktree-handoff/side/my-task`, and its state lives in that folder's own git directory, so your main folder and `main`'s bundle are left alone. The other computer mentions the side branch's bundle once, then stays quiet about it until it goes away and comes back.

Keep the folder outside the repo's folder, or the main folder would try to bundle it as new files. If you'd rather keep one folder, `worktree-handoff switch side/my-task` moves that folder between branches, and each branch keeps its own parked work (see [Switching branches](#switching-branches)). An autonomous run counts as one long reply, so the heartbeat rarely fires during it, which means the task should commit and push its branch as it goes.

## Switching branches

Git and your editor make you stash or carry uncommitted changes when you switch branches. Ask Claude to switch instead, or run `worktree-handoff switch BRANCH` yourself, and the plugin parks this branch's uncommitted work in its bundle, puts those files back to the last commit, switches, and brings back whatever the new branch had parked. The parked work is pushed like any bundle, so the other computer can pick it up too, and a chat that opens later on that branch brings it back by itself.

Files the bundle leaves out stay in the folder as they are, so excluded paths and held-back secrets are never wiped. If one of them differs on the other branch, git refuses the switch and everything goes back as it was. Git has no hook that runs before a switch, so a switch from your editor's branch picker still carries changes across the way it always did.

## Configure

Everything is optional. Settings go in `.worktree-handoff.json` at the repo root, so they travel with the repo. A small one looks like this:

```json
{ "exclude": ["scratch"], "tick_minutes": 5 }
```

| Key | Default | What it does |
|---|---|---|
| `enabled` | `true` | Set to `false` to switch the plugin off for this repo |
| `exclude` | `[]` | Paths to keep out of bundles |
| `remote` | `"origin"` | The remote to push to and fetch from |
| `tick_minutes` | `20` | The shortest gap between heartbeat bundles |
| `pull` | `true` | Runs `git pull --ff-only` before unwrapping |
| `allow` | `[]` | Files the secrets check should let through |

## Commands

The hooks run these for you. While the plugin is enabled, `worktree-handoff` is on Claude's PATH, and you can run `bin/worktree-handoff` from the plugin folder yourself.

| Command | What it does |
|---|---|
| `status` | Shows the config, the local state, the remote bundle and what `start` would do |
| `start` / `end` | Unwrap or bundle right now |
| `unwrap [--force]` | Apply the bundle. `--force` applies it over local edits, after a backup ref |
| `drop` | Delete this branch's bundle from the remote |
| `switch BRANCH` | Park this branch's work in its bundle, switch, and bring back what BRANCH had parked. `switch -c NEW [START]` makes a new branch |

Each new bundle replaces the last one, and the ref is deleted once your work is all committed. To clear everything the plugin left on your remote, `drop` each branch, or delete the refs yourself with `git push origin --delete refs/worktree-handoff/<branch>`. To remove the plugin, run `claude plugin uninstall worktree-handoff@worktree-handoff`.

## Logs

Every decision gets a line in `.git/worktree-handoff/handoff.log` (JSON, one event per line), so you can see why it did what it did. Only the events that need attention reach Claude, like `handoff.unwrapped` and `handoff.conflict`, and each one carries an `agent` field telling Claude what to do about it.

## Development

Run the tests with `python -m unittest`. Most of them run 2 throwaway clones against a local bare repo standing in for GitHub, which covers the cases in the safety table. CI runs ruff, mypy and the tests on Linux, macOS and Windows.

Every change here goes in as a small pull request with green CI, and the [build log](docs/building-log.md) has an entry for each one about what broke and how I fixed it. The [design notes](docs/design.md) explain why it works the way it does, the [changelog](CHANGELOG.md) lists the releases, and if you're curious how I work with coding agents, that's [section 9 of how I engineer](docs/how-i-engineer.md#9-working-with-a-coding-agent).
