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
