"""Background stash-box work: scene matching and performer pictures."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from ..db import Database
from ..models import Scene
from ..stashbox import StashBox, StashBoxConfig
from .studios import render_image
from .workers import BackgroundQueue

RECHECK_NOT_FOUND_S = 14 * 24 * 3600   # new scenes get added to stash-boxes; retry misses after 2 weeks
RECHECK_PERFORMER_S = 30 * 24 * 3600


class MatchService(QObject):
    """Matches cached scenes against every configured stash-box, slowly.

    On-screen scenes jump the queue; everything else is prefetched newest first.
    """

    matched = Signal(str)  # scene key (some endpoint found a match)

    def __init__(self, db: Database, interval_s: int = 3, parent=None):
        super().__init__(parent)
        self.db = db
        self.boxes: list[StashBox] = []
        self.checked: set[tuple[str, str]] = set()
        self.q = BackgroundQueue(interval_s * 1000, self._work, self._done, self)

    def configure(self, configs: list[StashBoxConfig], enabled: bool, interval_s: int) -> None:
        self.boxes = [StashBox(c) for c in configs if c.enabled and c.api_key]
        self.q.enabled = enabled and bool(self.boxes)
        self.q.set_interval(max(1, interval_s) * 1000)
        self.checked = self.db.scene_match_checked(RECHECK_NOT_FOUND_S)

    def _needs(self, s: Scene) -> bool:
        return any((s.key, b.cfg.endpoint) not in self.checked for b in self.boxes)

    def visible(self, s: Scene) -> None:
        if self.q.enabled and self._needs(s):
            self.q.push(s.key, s, front=True)

    def prefetch(self, scenes: list[Scene]) -> None:
        if not self.q.enabled:
            return
        for s in scenes:  # already newest first
            if self._needs(s):
                self.q.push(s.key, s)

    def pending(self) -> int:
        return self.q.pending()

    def _work(self, key: str, scene: Scene):
        out = []
        for b in self.boxes:
            if (key, b.cfg.endpoint) in self.checked:
                continue
            out.append((b.cfg.endpoint, b.match_scene(scene)))
        return out

    def _done(self, key: str, results) -> None:
        found = False
        for endpoint, m in results:
            self.db.save_scene_match(key, endpoint, m)
            self.checked.add((key, endpoint))
            found |= m is not None
        if found:
            self.matched.emit(key)


class PerformerImages(QObject):
    """Performer portraits from stash-box, cached in cache/performers."""

    ready = Signal(str)

    def __init__(self, db: Database, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.db = db
        self.dir = Path(cache_dir) / "performers"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.boxes: list[StashBox] = []
        self.mem: dict[str, QPixmap | None] = {}
        self.q = BackgroundQueue(2000, self._work, self._done, self)

    def configure(self, configs: list[StashBoxConfig]) -> None:
        # StashDB first (sharp, consistent portraits), then the rest
        boxes = [StashBox(c) for c in configs if c.enabled and c.api_key]
        self.boxes = sorted(boxes, key=lambda b: "stashdb" not in b.cfg.endpoint)
        self.q.enabled = bool(self.boxes)

    def get(self, key: str, name: str) -> QPixmap | None:
        if key in self.mem:
            return self.mem[key]
        row = self.db.performer(key)
        if row is not None:
            if row["image_file"] and (self.dir / row["image_file"]).exists():
                pm = render_image((self.dir / row["image_file"]).read_bytes(), 400, 600)
                self.mem[key] = pm
                return pm
            if time.time() - (row["checked_at"] or 0) < RECHECK_PERFORMER_S:
                self.mem[key] = None
                return None
        self.q.push(key, name, front=True)
        return None

    def prefetch(self, items: list[tuple[str, str]]) -> None:
        if not self.q.enabled:
            return
        known = self.db.performer_keys_checked(RECHECK_PERFORMER_S)
        for key, name in items:
            if key not in known:
                self.q.push(key, name)

    def _work(self, key: str, name: str):
        for b in self.boxes:
            p = b.find_performer(name)
            if not p:
                continue
            fname = None
            if p["image_url"]:
                resp = b.session.get(p["image_url"], timeout=30)
                if resp.ok and resp.content:
                    fname = hashlib.sha1(p["image_url"].encode()).hexdigest()
                    (self.dir / fname).write_bytes(resp.content)
            return p["name"], b.cfg.endpoint, p["id"], p["image_url"], fname
        return name, None, None, None, None

    def _done(self, key: str, res) -> None:
        self.db.save_performer(key, *res)
        self.mem.pop(key, None)
        self.ready.emit(key)
