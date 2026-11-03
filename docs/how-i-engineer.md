# How I engineer

This repo's tiny - about 1,300 lines of Python - but it's still the one I'd hand you if you asked how I build software. The [build log](building-log.md) goes PR by PR and the [design notes](design.md) go decision by decision. This page is the step back from both, where I just say what I care about as an engineer and point at the spot in this repo where you can check it.

If you want the bigger picture (the novel this came out of, and my day job building AI tooling at a bank), that's in [CreatorLoop](https://github.com/cthleung/CreatorLoop), and its [for-reviewers page](https://github.com/cthleung/CreatorLoop/blob/main/docs/for-reviewers.md) has the work history.

## Where it came from

I write a long novel with AI help, usually with several chats open in the same folder, and I switch between a Mac and a Windows PC. Unfinished work kept getting stuck on whichever computer I'd left it on, but committing unfinished drafts just to move them felt wrong, since a commit's supposed to mean "this is ready." So I wrote a hook that carries the unfinished work across instead.

It started life inside the novel's own repo, and it broke on my real machines a few times that first day, which is honestly where most of its safety rules come from (I wasn't planning on writing any). Once it held up, I moved it out into a plugin, and the next day I went back over it as if I were handing it to a team.

```mermaid
%%{init: {"themeVariables": {"cScale0": "#FBE3CC", "cScaleLabel0": "#122123", "cScale1": "#D7E6E3", "cScaleLabel1": "#122123"}}}%%
timeline
    title From a hook in my novel's repo to a plugin
    2 Nov 2026 : A hook that bundles unfinished work into a hidden ref
               : Unit tests for it
               : A heartbeat, because editor chats rarely close
               : A killed run stops blocking the next one
               : Moved out into its own plugin
               : The hotspot bug, and a stable computer name
    3 Nov 2026 : PR 1 lint and type checks
               : PR 2 unit tests and coverage
               : PR 3 the script becomes a package
               : PR 4 design notes and a changelog
               : PR 5 a README that shows it working
```

None of that was planned as a portfolio piece. I needed it, and the habits below are really just what I do when I'm going to depend on something.

## 1. I'm the first user, so a bad merge breaks me first

My own 2 computers run this plugin every day, and they update it straight from `main`. That's probably the best test discipline I know, because there's nobody downstream to absorb a mistake (it's my unfinished chapter that gets mangled). So every change goes through the same gate before it merges.

```mermaid
flowchart LR
    pr[A small pull request] --> lint[ruff and mypy]
    pr --> tests["44 tests on Linux,<br/>macOS and Windows"]
    pr --> cov["Coverage, measured inside<br/>the subprocesses too<br/>(floor 80%)"]
    lint --> merge{All green?}
    tests --> merge
    cov --> merge
    merge -- yes --> main[main]
    merge -- no --> fix[Fix it on the branch]
    main --> mine["My 2 computers<br/>auto-update"]
```

The Windows job doesn't look like much, but it matters. The pid check and the computer-name lookup both have Windows-only code, and `os.kill(pid, 0)`, the usual way to ask "is this process alive?", kills the process on Windows instead of checking it. You only meet that bug by running the real thing on the real OS, so CI does.

## 2. When it isn't sure, it stops and asks

The tool's 1 job is to put files into someone's working tree, which is where their unfinished work lives, so a wrong guess costs a lot more than a pause. It's built on 1 stated assumption - one person on one computer at a time - and anything that doesn't fit gets stopped instead of merged on a guess. This is the decision `start` makes every time a chat opens ([`decide()` in commands.py](../scripts/handoff/commands.py)).

```mermaid
flowchart TD
    start([A chat opens]) --> fetch{Is there a bundle<br/>on the remote?}
    fetch -- no --> pull[Pull and carry on]
    fetch -- yes --> mine{Did this computer<br/>send it?}
    mine -- "yes, and the tree still has it" --> resume[Resume my own work]
    mine -- "yes, but the tree is clean" --> ask1[Ask: was it committed,<br/>or thrown away?]
    mine -- no --> done{Applied already, or<br/>the tree matches it?}
    done -- yes --> record[Record it and pull]
    done -- no --> dirty{Local edits that aren't<br/>my own last bundle?}
    dirty -- yes --> stop1[Conflict: touch nothing,<br/>show both sides]
    dirty -- no --> newer{Is my own last bundle<br/>newer than theirs?}
    newer -- yes --> stop2[Conflict: both sides<br/>have edits]
    newer -- no --> backup[Back up the tree<br/>to a hidden ref] --> unwrap[Unwrap: git apply,<br/>3-way if the branch moved]

    classDef stop stroke-width:3px
    class ask1,stop1,stop2 stop
```

The thick boxes are the places where it refuses to guess. That being said, each one has an explicit way out (`unwrap --force`, which backs up first, or `drop`), so being careful never leaves anyone stuck. And every step that replaces a working tree commits it to `refs/worktree-handoff-backup/<time>` first, which means anything this tool does can be undone with plain git.

## 3. Logs written for 2 readers

The person using this never reads its output directly, but the coding agent does, because whatever a `SessionStart` hook prints goes into the agent's context. So every event gets written twice over, once in full for me and once in short for the agent, and they're really 2 different documents.

```mermaid
flowchart LR
    run[A hook runs] --> file["handoff.log<br/>every decision, with its inputs"]
    run --> loud{Does it need<br/>attention?}
    loud -- no --> quiet[Nothing printed]
    loud -- yes --> ctx["Printed into the agent's context,<br/>with an agent field"]
    ctx --> agent[The agent]
    agent --> person["The person hears<br/>'2 files came over from your laptop'"]
    file --> me[Me, when something looks off]
```

The `agent` field is plain instructions ("Nothing was changed. Tell the user before editing: show them the local files, the bundle's files and the overlap..."). I think of it as an API whose client happens to be a language model, so it gets the same care as any other API. It's short and specific, and it only shows up when there's something to do. A normal session start prints nothing at all.

## 4. Test the real thing first, then the cheap things

Mocks are cheap, but most of the bugs that matter here live in how the tool talks to git, so the main layer of tests doesn't mock git at all. Each of the 30 end-to-end tests makes 2 throwaway clones and a local bare repo standing in for GitHub, then runs the real entry point, the same file the hooks run, and checks the result with git itself.

```mermaid
flowchart TB
    subgraph e2e ["30 end-to-end tests (about 30 seconds)"]
        direction LR
        a[Clone A, the laptop] -- push --> bare[(A bare repo<br/>standing in for GitHub)]
        bare -- fetch --> b[Clone B, the desktop]
    end
    subgraph unit ["14 unit tests (about 0.03 seconds)"]
        direction LR
        g[The secrets guard] ~~~ l[The log] ~~~ p[The pid check] ~~~ s[summarize]
    end
    e2e ~~~ unit
```

The unit tests came later, and they're for the pure pieces that don't need git (does the guard catch a GitHub token? do quiet events stay off the screen?), because paying 30 seconds to check a regex is just silly. Coverage follows the end-to-end tests into their subprocesses, so the 84% is what the tests really reach, and the floor's 80% instead of 100% because the missing lines are the Windows-only ones, which the Windows job runs anyway. I'd rather report an honest 84 than fake the platform to get a prettier number.

## 5. Small modules, each with 1 job, pointing 1 way

The package split ([PR 3](https://github.com/cthleung/worktree-handoff/pull/3)) gave each job its own module, and the imports only ever point down. Nothing near the bottom knows the commands exist.

```mermaid
flowchart TD
    cli["cli.py<br/>reads the arguments, runs a command"] --> commands["commands.py<br/>start, end, unwrap, status, drop"]
    cli --> gitops
    cli --> log
    cli --> host
    commands --> gitops["gitops.py<br/>every git call on the repo, and the lock"]
    commands --> host
    gitops --> guard["guard.py<br/>the secrets guard"]
    gitops --> host["host.py<br/>the computer's name, the pid check"]
    log["log.py<br/>the 2-reader log"] --> config["config.py<br/>settings and constants"]
    host --> config
    gitops --> config
    commands --> config
    cli --> config
```

The unit tests aim at the pieces that never need a repo, which sit near the bottom: `guard`, `log` and `config`, plus the pid check in `host` and `summarize` in `gitops`. And if you're asking "what touches my repo?", the answer's 1 file, `gitops.py` (`host` reads and writes 1 global git config key for the computer's name, and that's it).

## 6. Prove a refactor instead of hoping

Moving 964 lines into 7 modules by hand would've been a great way to lose a line somewhere, so I didn't do it by hand. A small script sliced the old file along its own syntax tree, and afterward I compared the trees, and all 41 functions, classes and constants came out identical to the originals, with `--help` printing the same text. The 30 end-to-end tests also passed without being touched, which is the part I really trust, since they don't even know the package exists.

In other words, I hold behavior changes and refactors to different standards. A behavior change needs a test that fails first. A refactor needs proof that nothing changed, though, and "the tests still pass" only proves it when the tests run the real thing.

## 7. Write down why, including the bug

The [design notes](design.md) give every decision its trade-off, and honestly the most useful section's the embarrassing one. The first version used the network hostname to tell my 2 computers apart, and on a phone hotspot my Mac got a new hostname, stopped recognizing its own bundle and tried to unwrap its own work over itself (the fix is commit `adac36c`). Now the name's picked once and saved in git config. I could've written that section as if the design was right from day 1, but then the next person (me, probably, in a year) would see a strange config key and "simplify" it right back into the bug.

## 8. Boring on purpose

- **No dependencies.** It needs Python 3.10+ and git, and that's it. A plugin that has to pip install on every machine is a plugin that fails on the 1 machine where pip is broken, and I've been that machine.
- **Plain git underneath.** A hidden ref, a scratch index and `git apply` are all stock git, so if the tool ever disappears, everything it left behind still makes sense to someone who knows git.
- **Small PRs.** Each of the 5 is reviewable in 1 sitting, with its reasoning in the build log right next to the diff. That's how I work at my day job too, several small PRs a day when the work allows, because a change I can review in 1 sitting is a change I'd want to inherit.

## The same habits at work

These aren't habits I put on for a portfolio, either. At the bank I deployed an NER model for PII detection to GPU inference servers and tuned it 1 variable at a time against a recorded baseline, and I wrote the losing result down as plainly as the win. I shipped it behind a feature toggle, with a fallback to the old path if the new service went down, which is pretty much section 2 of this page in a different costume. The full version's on [CreatorLoop's for-reviewers page](https://github.com/cthleung/CreatorLoop/blob/main/docs/for-reviewers.md#shipping-reliable-tested-code).

## What I'd do next

- **Strict mypy.** It runs in its default mode right now, and a lot of the `log` parameters are still untyped (I know, I know). Strict mode would catch more, and it's the next PR I'd write.
- **Two people on one branch.** The tool stops and asks when 2 computers both have unsent edits, which is right for 1 person, but it's too cautious for a team. It's doable, but supporting it properly would mean real merge logic, and I'd want a design note before a line of code.
- **A tagged release.** The changelog already says 0.1.0. The tag and the GitHub release are just waiting on me.
