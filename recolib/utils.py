"""Small shared helpers: human-readable sizes, timestamps, safe names, progress."""
from __future__ import annotations
import os
import re
import sys
import time


def human_size(n: float) -> str:
    """Return a byte count as a human-readable string (e.g. 1.5 GB)."""
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.2f} {u}"
        f /= 1024.0
    return f"{f:.2f} PB"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Make a string safe to use as a Windows filename."""
    if not name:
        return fallback
    name = _BAD.sub("_", name).strip().strip(".")
    name = name[:200]  # keep well under MAX_PATH pressure
    return name or fallback


class Progress:
    """A tiny carriage-return progress meter for long scans."""

    def __init__(self, total: int, label: str = "Scanning", enabled: bool = True):
        self.total = max(1, int(total))
        self.label = label
        self.enabled = enabled and sys.stderr.isatty()
        self.start = time.time()
        self._last = 0.0

    def update(self, done: int, extra: str = "") -> None:
        if not self.enabled:
            return
        now = time.time()
        if now - self._last < 0.2 and done < self.total:
            return
        self._last = now
        frac = min(1.0, done / self.total)
        elapsed = now - self.start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (self.total - done) / rate if rate > 0 else 0
        bar_w = 28
        filled = int(bar_w * frac)
        bar = "#" * filled + "-" * (bar_w - filled)
        msg = (f"\r{self.label} [{bar}] {frac*100:5.1f}%  "
               f"{human_size(rate)}/s  ETA {human_duration(eta)}  {extra}")
        sys.stderr.write(msg[:120].ljust(120))
        sys.stderr.flush()

    def done(self, msg: str = "") -> None:
        if not self.enabled:
            return
        sys.stderr.write("\r" + " " * 120 + "\r")
        if msg:
            sys.stderr.write(msg + "\n")
        sys.stderr.flush()
