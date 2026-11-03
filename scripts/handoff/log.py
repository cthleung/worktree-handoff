"""The JSON-lines log, with only the events that need attention printed to stdout."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import LOUD_EVENTS


class Log:
    """structlog's shape (bind / info / warning / error) without needing structlog. Every
    event goes to the log file as one JSON line. stdout gets the loud events (or all of
    them when verbose) as readable key=value lines. When structlog is installed it renders
    the stdout lines; the file format is the same either way."""

    def __init__(self, path: Path, verbose: bool, context=None):
        self.path, self.verbose, self.context = path, verbose, dict(context or {})
        try:
            import structlog
            self._render = structlog.dev.ConsoleRenderer(colors=False)
        except ImportError:
            self._render = None

    def bind(self, **kw):
        return Log(self.path, self.verbose, {**self.context, **kw})

    def _emit(self, level, event, kw):
        rec = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level,
               "event": event, **self.context, **kw}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        if not (self.verbose or level != "info" or event in LOUD_EVENTS):
            return
        if self._render:
            line = self._render(None, level, {**rec})
        else:
            rest = " ".join(f"{k}={_short(v)}" for k, v in rec.items()
                            if k not in ("timestamp", "level", "event"))
            line = f"{rec['timestamp']} [{level:<7}] {event:<30} {rest}".rstrip()
        print(f"worktree-handoff: {line}", flush=True)

    def info(self, event, **kw): self._emit("info", event, kw)
    def warning(self, event, **kw): self._emit("warning", event, kw)
    def error(self, event, **kw): self._emit("error", event, kw)


def _short(v):
    s = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else str(v)
    return s if len(s) <= 400 else s[:400] + "..."
