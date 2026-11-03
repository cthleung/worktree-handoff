# How I build this

This repo's small on purpose, and honestly it's the place I'd point to if you want to see how I work. Every change lands as a small pull request with green CI on Linux, macOS and Windows, and each one gets an entry here: what was wrong, what I decided and why, and how I know it works. The PRs are all in the [pull request tab](https://github.com/cthleung/worktree-handoff/pulls?q=is%3Apr), so you can read the diffs next to the reasoning.

One rule sits over all of it. I run this plugin on my own 2 computers every day, and they update it straight from `main`, so a bad merge breaks my own work first. Nothing merges unless the 30 end-to-end tests (2 throwaway clones and a local stand-in for GitHub) pass on every OS.

## 1. Lint and type checks in CI

**What was wrong.** The script had grown to 964 lines, and the only thing checking it was the tests. The tests prove it behaves, but they don't catch a variable named `l`, an exception that loses its cause, or a function that quietly returns `None` where a caller indexes into it.

**What I did.** I added [ruff](https://docs.astral.sh/ruff/) and [mypy](https://mypy-lang.org) as their own CI job, configured in `pyproject.toml`, and fixed what they found, which was 24 lint findings and 12 type errors, pretty much all of them small.

**Decisions worth a note.**
- **The line limit is 110,** because that's the width the code was already written at. Picking 88 would've reformatted most of the file and buried the real fixes in noise.
- **`zip(..., strict=False)` on purpose.** Ruff asks for an explicit `strict=`, and the obvious answer is `True`. Here it'd crash on every run, because git's `-z` output ends with a NUL, so the list of fields always carries 1 extra empty one. There's a comment in the code now that says so.
- **Most of the type errors were really just 1 problem.** The plan that `decide()` returns was an untyped dict, so every lookup in `cmd_start` looked like it might be `None`. Typing it as `dict[str, Any]`, and reading `plan["remote"]` (which `decide()` always sets) instead of `plan.get("remote")`, cleared 9 of the 12.
- **The Windows check is `sys.platform == "win32"` now,** which mypy understands, so it stops complaining about `ctypes.windll` on the other 2 systems.

**How I know it works.** Ruff and mypy pass in their own CI job, and the 30 end-to-end tests pass on all 3 systems, so I'm pretty confident the fixes didn't change any behavior.

## 2. Fast unit tests, and coverage that follows the subprocesses

**What was wrong.** Every test was end to end. That's the right default for a tool like this, since the bugs that matter live in how it talks to git, but it means the cheapest checks (does the secrets guard catch a GitHub token? does the log keep quiet events off the screen?) cost 30 seconds each time. There was also no measure of what the tests actually reach. And a small thing from PR 1: every check ran twice on a pull request, once for the push and once for the PR.

**What I did.**
- `tests/test_units.py`, 14 tests for the pure pieces (the secrets guard, the file-name matcher, `summarize`, the log, `pid_alive`), and they run in about 0.03 seconds.
- A coverage job in CI that runs the whole suite under [coverage.py](https://coverage.readthedocs.io) and fails under 80%, with the report on the run's summary page.
- CI on push now only runs for `main`, so a pull request gets 1 run instead of 2.

**Decisions worth a note.**
- **Coverage follows the subprocesses.** The end-to-end tests run the real script as a separate process, which a plain `coverage run` can't see, so a naive setup would've reported the unit tests and nothing else. Coverage.py's `patch = ["subprocess"]` measures inside those processes too, which is how it gets to 83% (lines and branches) instead of an embarrassing, and wrong, number.
- **The floor is 80%, not 100%.** Most of what's missed is the Windows-only code (the `ctypes` process check, the computer-name lookup), which the Linux coverage job can't reach. Those paths still run in the Windows test job. Chasing 100% from 1 OS would mean faking the platform, and I'd rather the number be honest.
- **No real-looking secrets in the source.** The guard's tests need strings shaped like AWS and GitHub tokens, and a public repo with those in it gets flagged by secret scanning. So the tests build them from pieces at runtime (`"gh" + "p_" + "a" * 36`), and the file itself never contains one.

**How I know it works.** 44 tests pass (30 end to end plus 14 unit), lint and types are still clean, and the coverage job reports 83% with the floor at 80.

## 3. Splitting 1 big file into a small package

**What was wrong.** `worktree_handoff.py` did everything in 964 lines: settings, the secrets guard, the computer's name, logging, git plumbing and the 5 commands. It worked, and the tests proved it, but finding anything meant scrolling, and a reviewer couldn't tell at a glance which parts touch git and which don't.

**What I did.** It's now a package, `scripts/handoff/`, with 1 module per job: `config`, `guard`, `host`, `log`, `gitops`, `commands` and `cli`. The package's `__init__.py` lists them in the order a run touches them. `scripts/worktree_handoff.py` is still there as a 13-line entry point, because the hooks, the `bin/` wrapper and the tests all run that same path.

**Decisions worth a note.**
- **No behavior change, and I can prove it.** I didn't move the code by hand. A small script sliced the old file along its own syntax tree, so every function body moved byte for byte, and worked out each module's imports from what its code actually uses. Afterward, all 41 functions, classes and constants on `main` were identical to their new copies when I compared the 2 syntax trees, and `--help` printed the same text.
- **The 30 end-to-end tests didn't change at all.** They run the real entry point against real git, so them passing untouched is what says the refactor is safe. Only the unit tests moved their imports, since they reach into the modules on purpose.
- **The entry path stays put.** My own 2 computers auto-update this plugin from `main`, and their hooks run `scripts/worktree_handoff.py`. Moving that file would've broken my own setup the moment this merged.
- **Coverage got more useful for free.** It's reported per module now, which shows the gap is where I said it was in PR 2: `host.py` is at 66% because that's where the Windows-only code lives.

**How I know it works.** All 44 tests pass on Linux, macOS and Windows, lint and types are clean across the 9 files, coverage is 84%, and the hook script still exits quietly outside a repo.

## 4. Writing down the decisions, and a first release

**What was wrong.** The README's safety table says what happens in every situation, and the tests prove it, but nothing said why. Why a hidden ref and not a branch? Why does a computer's name live in git config? Somebody changing those later (me included, in a year) would have to reverse-engineer the reasons from the code, and some of them only exist because the obvious version broke.

**What I did.**
- `docs/design.md`, 1 short section per decision, each with the trade-off I took.
- `CHANGELOG.md` in the Keep a Changelog format, with everything so far as 0.1.0.
- After this merges, a `v0.1.0` tag and a GitHub release built from the changelog.

**Decisions worth a note.**
- **The design notes include the bug.** The stable computer name exists because the first version trusted the network hostname, and a Mac on a phone hotspot stopped recognizing its own bundle. I'd rather the doc say that plainly than make the design look like it was right the first time, since the bug is the best argument for keeping the fix.
- **No version in the plugin manifest.** Claude Code treats a version in `plugin.json` as the update signal, so pinning one would stop my own installed copies from following `main` until I remembered to bump it. The version lives in `pyproject.toml` and the git tag, and the changelog says why.

**How I know it works.** It's docs, so the test is whether they're true. Every claim in `design.md` points at something real (a test, a constant, a commit), and CI's still green.

## 5. A README that shows the thing working

**What was wrong.** The README explained the mechanics well, but you had to read the whole "how it works" section before seeing what the tool does for a person. It also opened with a shorthand for unfinished work that I've banned from my own writing, since "unfinished" just says it better.

**What I did.**
- Badges for CI, the Python version, zero dependencies and the license.
- A "What it looks like" section near the top, with a real run between 2 throwaway clones: the laptop bundles in silence, the desktop opens a chat, and 2 files show up as uncommitted edits with a note telling the agent what to say.
- The Tests section now describes both layers, plus lint, types and coverage.
- A "How this is built" section pointing at this log, the design notes and the changelog.

**Decisions worth a note.**
- **The capture is real.** I ran the tool in 2 fresh clones and pasted the output, only wrapping the long log line for width. A made-up example would've been easier to make pretty, but honestly it's the first place a reader would check.
- **No coverage badge.** A badge needs a hosted service or a number that goes stale the moment I forget to update it, and the coverage job already prints the real figure on every run.

**How I know it works.** Every link resolves, the capture came from the current code on `main`, and CI's green on all 3 systems.
