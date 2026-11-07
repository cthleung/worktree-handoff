"""What each command does: bundle (end, tick), unwrap (start), status, unwrap, drop."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from .config import BACKUP_PREFIX, CONFIG_FILE, DENIED_BACKOFF_H, NET_TIMEOUT, REF_PREFIX
from .gitops import Git, summarize
from .host import host_name

DENIED = re.compile(r"denied|403|not allowed|protected|permission|forbidden|unauthori", re.I)


def push(g: Git, log, refspec: str, what: str) -> bool:
    state = g.load_state()
    denied_at = state.get("push_denied_at", 0)
    if denied_at and time.time() - denied_at < DENIED_BACKOFF_H * 3600:
        log.info("push.skipped", detail=f"{g.remote} refused a push earlier; trying again "
                 f"{DENIED_BACKOFF_H}h after that", refused_at=datetime.fromtimestamp(denied_at)
                 .strftime("%Y-%m-%d %H:%M"))
        return False
    ok, msg = g.try_run("push", "-q", "--force", g.remote, refspec, timeout=NET_TIMEOUT)
    if ok:
        if denied_at:
            g.save_state(push_denied_at=0)
        return True
    if DENIED.search(msg):
        g.save_state(push_denied_at=int(time.time()))
        log.warning("push.denied", what=what, error=msg.splitlines()[-1] if msg else "",
                    detail=f"no push access to {g.remote}? pausing pushes for {DENIED_BACKOFF_H}h. "
                           "Opt this repo out with: git config worktree-handoff.enabled false")
    else:
        log.warning("push.failed", what=what, error=msg.splitlines()[-1] if msg else "",
                    retry="the next end, tick or start on this computer pushes it again")
    return False


def cmd_end(g: Git, log) -> int:
    """Bundle the working tree and push it."""
    head, head_tree = g.head(), g.head_tree()
    tree, held = g.worktree_tree()
    state = g.load_state()
    log = log.bind(head=head[:10], branch=g.branch())
    if held:
        log.warning("bundle.secrets_skipped", files=held,
                    detail="left out of the bundle; add a path to \"allow\" in "
                           f"{CONFIG_FILE} if it's safe to carry")

    if tree == head_tree:
        log.info("tree.clean", detail="nothing uncommitted to carry")
        remote = g.read_bundle(g.ref)
        if remote and remote["host"] == host_name():
            ok, msg = g.try_run("push", "-q", g.remote, f":{g.ref}", timeout=NET_TIMEOUT)
            g.run("update-ref", "-d", g.ref)
            g.save_state(sent=None, sent_tree=None, pushed=True)
            log.info("bundle.dropped", reason="our work is all committed now, so our old "
                     "bundle would be stale on the other computer", remote_deleted=ok)
        return 0

    changes = g.changes(head_tree, tree)
    files = summarize(changes)
    if state.get("sent_tree") == tree and state.get("pushed"):
        log.info("bundle.unchanged", detail="same tree as the last bundle we pushed",
                 bundle=(state.get("sent") or "")[:10], count=files["count"])
        return 0

    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"wip from {host_name()} at {when}", "", f"parent {head[:10]} on {g.branch()}",
             f"{files['count']} files", ""]
    lines += [f"{c['status']} {c['path']}" for c in changes]
    commit = g.commit_tree(tree, head, "\n".join(lines))
    g.run("update-ref", g.ref, commit)
    log.info("bundle.made", bundle=commit[:10], tree=tree[:10], **files, **g.shortstat(head_tree, tree))

    ok = push(g, log, f"{g.ref}:{g.ref}", "bundle")
    g.save_state(sent=commit, sent_tree=tree, sent_time=int(time.time()), pushed=ok)
    if ok:
        log.info("bundle.pushed", bundle=commit[:10], remote=g.remote, ref=g.ref)
    return 0


def _pull(g: Git, log) -> bool:
    """ff-only pull of the branch's upstream, retried once if another process holds git's
    index lock at the same moment."""
    if not g.config["pull"]:
        return True
    if not g.rev("@{upstream}"):
        log.info("pull.no_upstream", detail="this branch tracks nothing, so there's nothing to pull")
        return True
    for attempt in (1, 2):
        ok, msg = g.try_run("pull", "--ff-only", "-q", timeout=NET_TIMEOUT)
        if ok:
            log.info("branch.pulled", head=g.head()[:10])
            return True
        if "index.lock" in msg and attempt == 1:
            time.sleep(3)
            continue
        log.warning("pull.skipped", error=msg.splitlines()[-1] if msg else "",
                    hint="offline, a diverged branch, or local edits in a file the pull "
                         "touches; `git pull` by hand shows which")
        return False
    return False


def _fetch(g: Git, log, remember: bool = True) -> dict | None:
    ok, msg = g.try_run("fetch", "-q", "--prune", g.remote,
                        f"+{REF_PREFIX}*:{REF_PREFIX}*", timeout=NET_TIMEOUT)
    if not ok:
        log.warning("bundle.fetch_failed", error=msg.splitlines()[-1] if msg else "",
                    using="whatever ref is already local")
    remote = g.read_bundle(g.ref)
    if remote:
        log.info("bundle.found", bundle=remote["commit"][:10], from_host=remote["host"],
                 bundled_at=remote["when"], parent=remote["parent"][:10])
    else:
        log.info("bundle.none", detail=f"no {g.ref} on {g.remote}")
    _other_branches(g, log, remember)
    return remote


def _other_branches(g: Git, log, remember: bool = True):
    """Bundles another computer left on a branch this folder isn't on. Each one is announced
    once per folder, so a side task on another branch that keeps pushing heartbeat bundles
    doesn't come up at every chat start. It's announced again only after it goes away and
    comes back. `status` passes remember=False, so looking doesn't use up the announcement."""
    out = g.run("for-each-ref", "--format=%(refname)", REF_PREFIX, check=False)
    others = []
    for ref in out.splitlines():
        if ref == g.ref:
            continue
        b = g.read_bundle(ref)
        if b and b["host"] != host_name():
            others.append({"branch": ref[len(REF_PREFIX):], "from_host": b["host"], "bundled_at": b["when"]})
    seen = g.load_announced() if remember else {}
    new = [o for o in others if seen.get(o["branch"]) != o["from_host"]]
    if remember:
        g.save_announced({o["branch"]: o["from_host"] for o in others})
    if new:
        log.info("bundle.other_branches", bundles=new, current_branch=g.branch(),
                 agent="Another computer left uncommitted work on a different branch. Mention "
                       "it to the user once. Opening that branch (a checkout, or a second "
                       "folder made with `git worktree add`) and starting a chat there brings "
                       "it over.")
    elif others:
        log.info("bundle.other_branches_known", count=len(others),
                 branches=[o["branch"] for o in others], detail="already announced in this folder")


def decide(g: Git, log, remember: bool = True) -> dict:
    """Work out what `start` should do. Returns a plan dict; `start` carries it out and
    `status` only shows it."""
    me = host_name()
    head, head_tree = g.head(), g.head_tree()
    cur, held = g.worktree_tree()
    state = g.load_state()
    dirty = g.changes(head_tree, cur)
    log = log.bind(head=head[:10], branch=g.branch())
    plan: dict[str, Any] = {"action": "nothing", "cur": cur, "head": head, "head_tree": head_tree,
            "dirty": summarize(dirty), "state": state, "held": held}

    log.info("tree.dirty" if dirty else "tree.clean", **(plan["dirty"] if dirty else {}))
    if held:
        log.info("tree.secrets_held", files=held)

    remote = _fetch(g, log, remember)
    plan["remote"] = remote
    if not remote:
        plan["action"] = "push_ours" if state.get("sent") and not state.get("pushed") else "pull_only"
        return plan

    if remote["host"] == me:
        plan["action"] = "resume_own" if dirty else "own_bundle_but_clean"
        if not state.get("pushed") and state.get("sent") and state["sent"] != remote["commit"]:
            plan["action"] = "push_ours"
        return plan

    if state.get("applied") == remote["commit"]:
        plan["action"] = "already_applied"
        return plan
    if cur == remote["tree"]:
        plan["action"] = "already_identical"
        return plan

    ours_stale = bool(dirty) and state.get("sent_tree") == cur
    if dirty and not ours_stale:
        plan["action"] = "conflict"
        plan["why"] = ("this computer has uncommitted edits that are not the bundle it last "
                       "sent, and the remote bundle is from another computer")
        return plan
    if ours_stale and state.get("sent_time", 0) > remote["time"]:
        plan["action"] = "conflict"
        plan["why"] = ("our last bundle is newer than the remote one, so the other computer "
                       "worked without unwrapping ours; both sides have edits")
        return plan

    plan["action"] = "unwrap"
    plan["revert_first"] = ours_stale
    return plan


def cmd_start(g: Git, log) -> int:
    plan = decide(g, log)
    # decide() always sets "remote"; it's None only for pull_only and push_ours
    action, remote, state = plan["action"], plan["remote"], plan["state"]
    cur = plan["cur"]

    if action == "pull_only":
        _pull(g, log)
        return 0

    if action == "push_ours":
        if state.get("sent") and not g.rev(g.ref):  # a fetch --prune can drop an unpushed ref
            g.run("update-ref", g.ref, state["sent"])
        ok = push(g, log, f"{g.ref}:{g.ref}", "bundle")
        g.save_state(pushed=ok)
        if ok:
            log.info("bundle.pushed", bundle=state["sent"][:10], note="sent late; the last push failed")
        _pull(g, log)
        return 0

    if action == "resume_own":
        log.info("resume.own_work", detail="the remote bundle is this computer's and the "
                 "tree still holds it; nothing to unwrap", **plan["dirty"])
        if _pull(g, log) or state.get("sent_tree") != cur:
            return 0
        # The pull refused because our own uncommitted files overlap what the branch now
        # has (the other computer committed our bundle). The tree is exactly our bundle,
        # so it's safe to lift it off, pull, and put back only what the branch lacks.
        backup = g.revert_to_head(cur)
        log.info("tree.reverted_to_head", backup_ref=backup,
                 reason="our own bundle overlaps what the branch now has; lifting it off to pull")
        _pull(g, log)
        _apply_bundle(g, log, remote, note="re-applying this computer's own bundle after the pull")
        return 0

    if action == "own_bundle_but_clean":
        log.warning("resume.own_bundle_tree_clean", bundle=remote["commit"][:10],
                    bundled_at=remote["when"],
                    detail="the remote bundle is this computer's but the tree is clean. Either "
                           "that work got committed (then `worktree-handoff drop`), or it was "
                           "discarded here (then `worktree-handoff unwrap --force` brings it back).",
                    agent="Ask the user which before editing anything.")
        _pull(g, log)
        return 0

    if action in ("already_applied", "already_identical"):
        g.save_state(applied=remote["commit"])
        log.info("bundle.already_here", bundle=remote["commit"][:10], from_host=remote["host"],
                 how="recorded as applied" if action == "already_applied" else "tree matches it")
        _pull(g, log)
        return 0

    if action == "conflict":
        bundle_changes = g.changes(remote["parent"], remote["tree"])
        overlap = sorted(set(p for s in ("added", "modified", "deleted") for p in plan["dirty"][s])
                         & set(c["path"] for c in bundle_changes))
        log.error("handoff.conflict", why=plan["why"], local=plan["dirty"],
                  bundle=remote["commit"][:10], from_host=remote["host"],
                  bundled_at=remote["when"], bundle_files=summarize(bundle_changes), overlap=overlap,
                  agent="Nothing was changed. Tell the user before editing: show them the "
                        "local files, the bundle's files and the overlap, and offer "
                        "`worktree-handoff unwrap --force` (backs up the local tree to "
                        f"{BACKUP_PREFIX}<time> first) or keeping local and `worktree-handoff drop`.")
        return 0

    return _unwrap(g, log, plan)


def _unwrap(g: Git, log, plan, force=False) -> int:
    remote, cur, head_tree = plan["remote"], plan["cur"], plan["head_tree"]
    log = log.bind(bundle=remote["commit"][:10], from_host=remote["host"])

    if cur != head_tree:
        backup = g.revert_to_head(cur)
        log.info("tree.reverted_to_head", backup_ref=backup,
                 reason="forced over local edits" if force else
                        "the tree still held the bundle this computer sent, which the other "
                        "computer has since carried on from",
                 **plan["dirty"])

    _pull(g, log)
    return _apply_bundle(g, log, remote)


def _apply_bundle(g: Git, log, remote, note="") -> int:
    """Apply a bundle's changes onto a working tree that is clean at HEAD. Files the
    bundle has in the same state as HEAD already (work that got committed in the
    meantime) are left out of the patch. If the branch moved on since the bundle's parent
    the patch goes on with --3way, and real conflicts are left as markers and reported."""
    head, head_tree = g.head(), g.head_tree()
    head_blobs, bundle_blobs = g.blobs(head_tree), g.blobs(remote["tree"])
    bundle_changes = g.changes(remote["parent"], remote["tree"])
    already = [c["path"] for c in bundle_changes if bundle_blobs.get(c["path"]) == head_blobs.get(c["path"])]
    todo = [c for c in bundle_changes if c["path"] not in already]
    files = summarize(todo)

    if not todo:
        g.save_state(applied=remote["commit"], sent_tree=None)
        log.info("bundle.already_in_main", bundled_at=remote["when"], files=already,
                 detail="every file in the bundle already matches HEAD, so there's nothing "
                        "uncommitted to bring back")
        return 0

    patch = g.patch(remote["parent"], remote["tree"], exclude=already)
    if remote["parent"] == head:
        how = "plain apply (bundle parent is HEAD)"
        ok, msg = g.apply(patch, "--check")
        if not ok:
            log.error("unwrap.blocked", error=msg, agent="Nothing was changed. Tell the user.")
            return 0
        ok, msg = g.apply(patch)
    else:
        how = f"3-way apply (the branch moved from {remote['parent'][:10]} to {head[:10]})"
        ok, msg = g.apply(patch, "--3way")
        if not ok:
            unmerged = g.run("diff", "--name-only", "--diff-filter=U").splitlines()
            g.run("reset", "-q")  # --3way stages what it merged; reset to HEAD so nothing's left staged
            g.save_state(applied=remote["commit"], sent_tree=None)
            log.error("unwrap.conflict_markers", how=how, note=note,
                      error=msg.splitlines()[-1] if msg else "", files_with_markers=unmerged,
                      skipped_already_committed=already, **files,
                      agent="The bundle is in the tree but these files have <<<<<<< markers. "
                            "Tell the user and resolve them together before other edits.")
            return 0
        g.run("reset", "-q")
    if not ok:
        log.error("unwrap.failed", how=how, error=msg, agent="Tell the user.")
        return 0

    g.save_state(applied=remote["commit"], sent_tree=None)
    log.info("handoff.unwrapped", how=how, note=note, bundled_at=remote["when"], **files,
             skipped_already_committed=already, **g.shortstat(remote["parent"], remote["tree"]),
             agent="These files are now uncommitted edits in the working tree, carried over "
                   "from the other computer. Tell the user which files came over.")
    return 0


def cmd_unwrap(g: Git, log, force: bool) -> int:
    plan = decide(g, log)
    if plan["action"] == "unwrap" or (force and plan.get("remote")):
        return _unwrap(g, log, plan, force=force)
    log.info("unwrap.skipped", action=plan["action"], why=plan.get("why", ""))
    return 0


def cmd_status(g: Git, log) -> int:
    log.info("config", **g.config, config_file=str(g.root / CONFIG_FILE),
             log_file=str(log.path), ref=g.ref)
    plan = decide(g, log, remember=False)
    remote = plan.get("remote")
    if remote:
        log.info("bundle.contents", **summarize(g.changes(remote["parent"], remote["tree"])))
    log.info("status.would_do", action=plan["action"], why=plan.get("why", ""),
             state={k: (v[:10] if isinstance(v, str) and len(v) == 40 else v)
                    for k, v in plan["state"].items()})
    return 0


def cmd_drop(g: Git, log) -> int:
    ok, msg = g.try_run("push", "-q", g.remote, f":{g.ref}", timeout=NET_TIMEOUT)
    if g.rev(g.ref):
        g.run("update-ref", "-d", g.ref)
    g.save_state(sent=None, sent_tree=None, pushed=True)
    log.info("bundle.dropped", ref=g.ref, remote_deleted=ok, note="" if ok else msg)
    return 0
