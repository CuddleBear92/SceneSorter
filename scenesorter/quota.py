"""Per-source daily action counter (rolling 24h window), persisted to disk.

Premium sources allow N actions per 24h; every started download costs one.
We track timestamps locally so the UI can show "412 / 500 left, next slot in
3h 12m" and refuse to start a download that would exceed the limit. If the
site itself reports a remaining count, call ``sync_remaining`` to trust it.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

WINDOW = 24 * 3600


class QuotaTracker:
    def __init__(self, path: Path, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}

    def _events(self, source: str) -> list[float]:
        now = self.clock()
        entry = self._data.setdefault(source, {"events": [], "override": None})
        entry["events"] = [t for t in entry["events"] if now - t < WINDOW]
        return entry["events"]

    def used(self, source: str) -> int:
        with self._lock:
            return len(self._events(source))

    def remaining(self, source: str, limit: int) -> int:
        with self._lock:
            ev = self._events(source)
            ov = self._data[source].get("override")
            if ov and self.clock() - ov["at"] < WINDOW:
                # site-reported remaining, minus what we've spent since
                spent = sum(1 for t in ev if t >= ov["at"])
                return max(0, min(limit, ov["remaining"] - spent))
            return max(0, limit - len(ev))

    def seconds_until_next(self, source: str) -> float:
        with self._lock:
            ev = self._events(source)
            return max(0.0, WINDOW - (self.clock() - min(ev))) if ev else 0.0

    def try_consume(self, source: str, limit: int) -> bool:
        """Record one action if allowed. Returns False if the limit is reached."""
        if self.remaining(source, limit) <= 0:
            return False
        with self._lock:
            self._events(source).append(self.clock())
            self._save()
        return True

    def refund(self, source: str) -> None:
        """Undo the latest action (e.g. the site rejected the request)."""
        with self._lock:
            ev = self._events(source)
            if ev:
                ev.pop()
                self._save()

    def sync_remaining(self, source: str, remaining: int) -> None:
        with self._lock:
            self._events(source)
            self._data[source]["override"] = {"at": self.clock(), "remaining": remaining}
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data), encoding="utf-8")
        tmp.replace(self.path)
