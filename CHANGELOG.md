# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org). The plugin manifest has no version on purpose, so installed copies keep following `main`. Releases are git tags.

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
