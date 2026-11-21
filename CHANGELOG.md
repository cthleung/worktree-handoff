# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org). The plugin manifest has no version on purpose, so installed copies keep following `main`. Releases are git tags.

## [Unreleased]

### Fixed
- Installed copies stopped updating: Claude Code's plugin loader rejects the manifest's `displayName` key (`claude plugin validate` only warned about it), so auto-update kept every computer on the first version. The key is gone.

### Added
- `preserve_staging` (off by default, [#14](https://github.com/cthleung/worktree-handoff/issues/14)): bundles carry the staged and unstaged split, partial hunks included, as a second tree on the bundle commit. That tree goes through the same guards as the working-tree one, ignored files included. If the patches clash after the branch moved, everything still comes back, unstaged, and the log says so. A copy from before this can't read these bundles, so turn it on after every computer has updated.
- `worktree-handoff switch BRANCH` (and `switch -c NEW [START]`): parks this branch's uncommitted work in its bundle, switches, and brings back what the new branch had parked. A refused switch puts everything back, files the bundle leaves out are never wiped, and work parked while offline is pushed the next time it comes back. A chat that opens on a clean tree holding its own parked bundle brings it back by itself.
- The bundled skill tells the agent to switch branches through `switch`.
- Tests for a second folder on its own branch (`git worktree add`), on the same computer as the main folder.
- A README section on running a side task in its own folder.

### Changed
- When a pull clashes with this computer's own uncommitted work and the tool lifts it off and puts it back, that's logged as `handoff.reapplied`, with its own `agent` text. Before, it said the files came over from the other computer.
- Every printed line starts with the repo's folder name (`worktree-handoff [notes]: ...`), and each `agent` instruction starts with "This is the notes repo." Several repos can use the plugin at once, and a bundle from another computer has to say which repo it's for. The log file gets a `repo` field on every event too.
- A bundle on another branch is announced once per folder, then stays quiet until it goes away and comes back. Before, a side task pushing heartbeat bundles came up at every chat start. `status` still shows them all without using the announcement up.

## [0.1.0] - 2026-11-03

The first tagged release.

### Added
- Carry uncommitted work (edits, new files, deletions) between computers through a hidden ref, `refs/worktree-handoff/<branch>`, built from a scratch index so `.git/index` is never touched.
- Unwrap on `SessionStart` with `git apply`, falling back to `--3way` when the branch moved on, with real clashes left as conflict markers and reported.
- A guard that stops instead of unwrapping over local edits that aren't this computer's own last bundle, with backup refs before any destructive step.
- Bundle on `SessionEnd` and on a `Stop` heartbeat at most every 20 minutes.
- A secrets guard for secret-looking file names and token patterns, with an `allow` list.
- A lock that records its owner's pid and clears a dead owner's lock at once.
- A 24-hour pause on pushes after the remote refuses one.
- Commands: `status`, `start`, `end`, `unwrap [--force]`, `drop`.
- JSON-lines log, with only the events that need attention printed for the coding agent.

### Fixed
- A computer's name is picked once and kept in global git config. The network hostname changes on a phone hotspot or a new router, which made a Mac mistake its own bundle for the other computer's.

### Engineering
- ruff and mypy in CI ([#1](https://github.com/cthleung/worktree-handoff/pull/1)).
- 14 fast unit tests, and coverage measured inside the end-to-end subprocesses, with an 80% floor ([#2](https://github.com/cthleung/worktree-handoff/pull/2)).
- The script split into the `handoff` package, with the entry point and behavior unchanged ([#3](https://github.com/cthleung/worktree-handoff/pull/3)).
- Design notes in `docs/design.md` and this changelog ([#4](https://github.com/cthleung/worktree-handoff/pull/4)).

[0.1.0]: https://github.com/cthleung/worktree-handoff/releases/tag/v0.1.0
