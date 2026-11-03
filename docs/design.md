# Design notes

The README says what worktree-handoff does. This is why it does it that way, decision by decision, with the trade-off I took each time. If you're about to change one of these, read its section first, because most of them exist because the obvious alternative broke something.

There's 1 assumption under all of it, which is **one person, working on one computer at a time.** I built this for moving between my Mac and my Windows PC mid-task, and it isn't meant for 2 people sharing a branch. Anything that'd go wrong outside that assumption gets detected and stopped, and nothing gets merged on a guess.

## Where the bundle lives: a hidden ref

A bundle is pushed to `refs/worktree-handoff/<branch>`, outside `refs/heads`, and that's deliberate. A branch shows up in every branch list and every pull request picker, and a normal `git fetch` downloads it, while a ref outside `refs/heads` does none of that, so the only thing that ever sees it is this tool.

It's force-pushed, which sounds scarier than it is. The ref only ever holds the latest bundle for its branch, and nobody builds on top of it, so overwriting it can't lose anyone's commits. Real branches never are.

The trade-off is that a hidden ref is easy to forget about, which is why `worktree-handoff status` shows it and `drop` deletes it.

## How the bundle's made: a scratch index

The working tree gets staged into a separate index (`GIT_INDEX_FILE` pointing at a temp file), never `.git/index`. The user's own staging is their business. If I'd used the real index, every bundle would've wiped out whatever they'd carefully staged, and their editor's "changes since the last commit" view would've shifted under them. With a scratch index, `git add -A` still respects `.gitignore`, and the real index never gets touched.

The cost is that staging doesn't travel, so everything arrives unstaged on the other computer. It's in the README's limits, but honestly I've never missed it.

## What a bundle is: a commit on top of HEAD

The staged tree becomes a commit whose parent is the current `HEAD`, with a message naming the computer. The parent is what makes a 3-way apply possible later, since if the branch moved on while I was away, the receiving side still knows what the bundle was based on. The computer's name is how a machine tells its own bundle from the other one's, and that decides pretty much everything about what happens on the next start.

## A computer's name has to be stable

This one came from a pretty embarrassing bug (commit `adac36c`). The first version used the network hostname, but a Mac on a phone hotspot or a new router gets a different one. So the Mac stopped recognizing its own bundle, decided it came from the other computer, and tried to unwrap its own work over itself.

Now the name's picked once from the OS's own setting (`scutil --get LocalHostName` on macOS, `COMPUTERNAME` on Windows), saved in global git config as `worktree-handoff.host`, and never worked out again. Renaming a machine is just one `git config` away, and the test `test_host_name_is_picked_once_and_kept` holds the behavior in place.

## Unwrapping: apply a diff, never check anything out

The receiving side applies the bundle's diff with `git apply`, and falls back to `--3way` when the branch has moved since the bundle was made. Checkout would stage things, move refs, or both, while applying a diff leaves the tree looking like I'd typed the edits myself, which is the whole point of the tool.

Real clashes get left as conflict markers and reported, because an automatic resolution would just be a guess. Files that already landed in the branch since the bundle was made get skipped, and the rest applies.

## When to stop and ask

The guard refuses to unwrap over local uncommitted edits, unless those edits are the bundle this same computer sent earlier (that's safe to replace, after a backup). It's strict on purpose. The dangerous case is 2 machines with unsent edits each, and a merge there might look fine and still be wrong, and the thing at risk is someone's unfinished work. So it stops, puts both sides' file lists and their overlap into the session's startup context, and the coding agent raises it with the person.

There's an explicit way out, either `unwrap --force` (the local edits get backed up first) or `drop`.

## Nothing's ever lost: backup refs

Before anything replaces a working tree, the tree's committed to `refs/worktree-handoff-backup/<time>`. It's really cheap and invisible. But it means every destructive step in the tool can be undone with plain git.

## When it runs: 3 hooks with different jobs

- **`SessionStart` runs in the foreground,** so unwrapped files are in place before the agent reads anything. Its output goes into the agent's context, which is why it's quiet unless something needs attention.
- **`SessionEnd` runs detached,** so closing a chat never waits on the network.
- **`Stop` runs a heartbeat,** bundling at most every 20 minutes. I added it because editor chat panels keep sessions alive when you switch tabs, so `SessionEnd` alone fired too rarely to be any use.

## Concurrency: a lock that knows its owner

Only 1 run touches a repo at a time, and the lock records its owner's pid. A dead owner's lock gets cleared right away, because editors restart sessions on resume and kill hooks mid-run, so stale locks are normal, and waiting 10 minutes on a process that's already gone would mean quietly skipping work. A live lock gets waited on for up to a minute, then the run steps aside and logs it.

On Windows, `os.kill(pid, 0)` would kill the process it's meant to check, so the pid check asks the kernel through `ctypes` instead.

## The network can say no

If the remote refuses a push (no access, protected refs), that's logged once and pushes pause for 24 hours. Retrying every 20 minutes against a remote that's going to keep saying no would only fill the log.

## The secrets guard is a net

New files with secret-looking names (`.env`, `*.pem`, `id_rsa`, ...), and any file whose new lines contain a token pattern (AWS, GitHub, OpenAI, Anthropic, Slack, Google, GitLab), stay out of the bundle and get logged, and an `allow` list lets a file through. It's there because a bundle gets pushed to a remote, and a working tree is where a pasted key sits before anyone notices. But it isn't a scanner. It catches the common shapes, and real secrets belong in `.gitignore`.

## Logs written for 2 readers

Every decision is a JSON line in `.git/worktree-handoff/handoff.log`, with its inputs, so a bad outcome can be traced after the fact. Only the events that need attention reach the screen, and each of those carries an `agent` field telling the coding agent what to do next ("ask the user which before editing anything"). The person reads the agent's summary, and I read the log when something looks off.

## Standard library only

It needs Python 3.10+ and git, and nothing else. `structlog` pretty-prints if it happens to be installed, and the file format's the same either way. A plugin that has to install dependencies on every machine is a plugin that fails on the one machine where pip is broken, and I've been that machine.
