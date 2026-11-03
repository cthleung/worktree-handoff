#!/usr/bin/env python3
"""Entry point. hooks/hooks.json (through scripts/hook.sh), bin/worktree-handoff and the tests all
run this file, so it stays at this path. The code lives in the handoff package next to it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handoff.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
