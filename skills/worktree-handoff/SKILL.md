---
name: worktree-handoff
description: Handles worktree-handoff events and commands. Use when the session's startup context has lines starting "worktree-handoff:" (an unwrap, a conflict, conflict markers, a clean tree holding this computer's old bundle, bundles on other branches, a skipped run), or when the user asks about moving uncommitted work between computers, what came over from the other computer, the handoff log, or running worktree-handoff status, unwrap, or drop.
---

# worktree-handoff

The worktree-handoff plugin carries uncommitted work (edited, new and deleted files) between the user's computers through a hidden git ref, `refs/worktree-handoff/<branch>`:

- **SessionEnd** bundles the working tree and pushes it.
- **Stop** does the same at most every 20 minutes. This is the heartbeat, for chats that stay open.
- **SessionStart** pulls the branch and unwraps the other computer's bundle as plain uncommitted edits.

It never commits to a branch and never leaves anything staged. Bundles are built in a scratch index, never `.git/index`.

The `worktree-handoff` command is on the Bash tool's PATH while the plugin is enabled. If it isn't found, run `"${CLAUDE_PLUGIN_ROOT}/bin/worktree-handoff"` instead.

## When the startup context reports an event

Every printed event carries an `agent` field. Follow it. In short:

| Event | What to do |
|---|---|
| `handoff.unwrapped` | Tell the user in your first reply which files came over and from which computer (`from_host`). Nothing else is needed. |
| `handoff.conflict` | Stop before any edit. Nothing was changed. Show the user the local files, the bundle's files and the overlap. Offer the two ways out: `worktree-handoff unwrap --force` (the bundle wins, and the local tree is backed up to a `refs/worktree-handoff-backup/<time>` ref first), or keep the local work and run `worktree-handoff drop`. Let the user choose. |
| `unwrap.conflict_markers` | The bundle is in the tree, but the listed files have `<<<<<<<` markers because the branch moved on since the bundle was made. Show the user and resolve them together before any other edit. |
| `unwrap.blocked`, `unwrap.failed`, `git.failed` | Nothing (or nothing more) was changed. Tell the user and show the error. |
| `resume.own_bundle_tree_clean` | The remote bundle is this computer's, but the tree is clean. Ask the user whether that work got committed (then `worktree-handoff drop`) or was discarded by accident (then `worktree-handoff unwrap --force` brings it back). |
| `tree.reverted_to_head` | The tree was put back to HEAD before a newer bundle went on. The backup ref named in the event holds what was there. |
| `bundle.other_branches` | Another computer left work on a different branch. Mention it. Checking out that branch and running `worktree-handoff start` brings it over. |
| `skip.in_progress` | A merge, rebase or similar is in progress, so the handoff didn't run. It runs again once that's finished. |
| `push.denied` (in the log) | The remote refused the push, so pushes pause for 24 hours. If the repo isn't the user's, suggest opting it out. |

Never pick between the user's work and the bundle on their behalf. A backup ref restores either side: `git show <backup-ref>:<path>`, or `git diff HEAD <backup-ref>` to see all of it.

## Commands

| Command | What it does |
|---|---|
| `worktree-handoff status` | Prints the config, what's local, what's on the remote and what `start` would do. It changes nothing except fetching. |
| `worktree-handoff unwrap` | Applies the bundle now, when it's safe. |
| `worktree-handoff unwrap --force` | Applies it over local edits, after a backup ref. |
| `worktree-handoff drop` | Deletes this branch's bundle from the remote. |
| `worktree-handoff end` | Bundles and pushes right now. |
| `worktree-handoff start` | Unwraps right now. |

The log is JSON lines in `.git/worktree-handoff/handoff.log`. `status` prints its path.

## Settings

All optional, in `.worktree-handoff.json` at the repo root:

```json
{"enabled": true, "exclude": ["build-cache"], "remote": "origin", "tick_minutes": 20, "pull": true, "allow": [".env.example"]}
```

- **One repo, this computer only:** `git config worktree-handoff.enabled false` opts it out.
- **Everywhere:** `WORKTREE_HANDOFF=off` in the environment turns it off.
- **The secrets guard** keeps new files that look like keys or credentials out of the bundle, and also any file whose new lines contain a token. It logs `bundle.secrets_skipped`. Add a path to `allow` to let a file through.
