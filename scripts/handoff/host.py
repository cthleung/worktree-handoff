"""This computer's stable name, and whether a process is still running."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from .config import env

_HOST = None


def host_name() -> str:
    """A name for this computer that never changes. The network hostname can't be trusted
    (a Mac on a phone hotspot or a new router gets renamed), and the handoff tells its own
    bundles from the other computer's by this name. So it's picked once from the OS's own
    setting and kept in global git config (worktree-handoff.host); set that to rename."""
    global _HOST
    if _HOST:
        return _HOST
    name = env("HOST")
    if not name:
        out = subprocess.run(["git", "config", "--global", "--get", "worktree-handoff.host"],
                             capture_output=True, text=True)
        name = out.stdout.strip()
    if not name:
        name = _os_computer_name()
        subprocess.run(["git", "config", "--global", "worktree-handoff.host", name],
                       capture_output=True)
    _HOST = name
    return name


def _os_computer_name() -> str:
    if sys.platform == "darwin":
        out = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    if os.name == "nt" and os.environ.get("COMPUTERNAME"):
        return os.environ["COMPUTERNAME"]
    return (platform.node() or "unknown").split(".")[0]


def _lock_owner(lock: Path):
    try:
        return int((lock / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        # os.kill(pid, 0) would terminate the process on Windows, so ask the kernel.
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
