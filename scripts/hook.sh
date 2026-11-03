#!/bin/bash
# Hook entry point for Claude Code. It runs worktree_handoff.py, and the logic lives in the
# handoff package next to it.
#
#   hook.sh start  (SessionStart)  foreground, so unwrapped files are there before the agent
#                                  reads anything; its stdout goes into the agent's context
#   hook.sh end    (SessionEnd)    detached, so closing a chat never waits on the push
#   hook.sh tick   (Stop)          detached, and the script skips it unless it's due

EVENT="${1:-end}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

# Windows often has no working "python3", so try the usual names.
for PY in python3 python py; do "$PY" -c "" </dev/null >/dev/null 2>&1 && break; PY=; done
[ -n "$PY" ] || exit 0

if [ "$EVENT" = "end" ] || [ "$EVENT" = "tick" ]; then
  NOHUP=$(command -v nohup)
  $NOHUP "$PY" "$HERE/worktree_handoff.py" "$EVENT" </dev/null >/dev/null 2>&1 &
  exit 0
fi
exec "$PY" "$HERE/worktree_handoff.py" "$EVENT"
