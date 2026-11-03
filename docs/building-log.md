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
