"""Spot "image removed" placeholders that image hosts serve with HTTP 200.

When a picture is taken down, hosts such as pixhost answer with a normal-looking
"Picture removed" image, so a plain download check sees success. Each one is the
same picture, so its stored bytes (after our downscale/re-encode) are identical.
Once the same bytes turn up for several *different scenes*, that image is a
placeholder: its files are dropped, its URLs are treated as dead, and the card
falls back to the next image (another site's thumbnail or the stash-box cover).

Distinct scenes are counted, not URLs, because one scene's 2160p/1080p/720p posts
often share the same picture.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict, defaultdict
from pathlib import Path

SCENES_TO_CONFIRM = 4       # same picture for this many different scenes = placeholder
CACHE_SCAN_MIN_FILES = 8    # one-off cache sweep (no scene info): this many identical files
DEAD_RETRY_S = 14 * 86400   # a dead URL is tried again after two weeks
TRACK_MAX = 50_000          # pictures remembered while waiting for repeats


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class PlaceholderTracker:
    def __init__(self, path: Path | None = None, confirm: int = SCENES_TO_CONFIRM):
        self.path = path
        self.confirm = confirm
        self.hashes: set[str] = set()
        self.dead: dict[str, float] = {}
        self.cache_swept = False
        self.dirty = False
        # picture hash -> {scene key: (url, cached file)} for pictures seen recently
        self._seen: OrderedDict[str, dict[str, tuple[str, str]]] = OrderedDict()
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.hashes = set(raw.get("hashes", []))
                self.dead = {u: float(t) for u, t in raw.get("dead", {}).items()}
                self.cache_swept = bool(raw.get("cache_swept"))
            except (OSError, ValueError, AttributeError):
                pass

    def is_dead(self, url: str, now: float | None = None) -> bool:
        t = self.dead.get(url)
        return t is not None and (now or time.time()) - t < DEAD_RETRY_S

    def mark_dead(self, url: str) -> None:
        self.dead[url] = time.time()
        self.dirty = True

    def add_hashes(self, hashes) -> None:
        new = set(hashes) - self.hashes
        if new:
            self.hashes |= new
            self.dirty = True

    def check(self, h: str, key: str, url: str, path: str) -> list[tuple[str, str, str]] | None:
        """A picture for scene ``key`` was downloaded from ``url`` and stored at ``path``.

        Returns None if it looks like a real picture. Otherwise returns the *other*
        (key, url, path) entries that turned out to be the same placeholder (possibly
        empty); the caller treats this one as failed and evicts those."""
        if h in self.hashes:
            self.mark_dead(url)
            return []
        group = self._seen.get(h)
        if group is None:
            group = self._seen[h] = {}
            if len(self._seen) > TRACK_MAX:
                self._seen.popitem(last=False)
        else:
            self._seen.move_to_end(h)
        group[key] = (url, path)
        if len(group) < self.confirm:
            return None
        self.add_hashes([h])
        del self._seen[h]
        for k, (u, _p) in group.items():
            self.mark_dead(u)
        return [(k, u, p) for k, (u, p) in group.items() if k != key]

    def save(self) -> None:
        if not (self.path and self.dirty):
            return
        now = time.time()
        self.dead = {u: t for u, t in self.dead.items() if now - t < DEAD_RETRY_S}
        data = {"hashes": sorted(self.hashes), "dead": self.dead, "cache_swept": self.cache_swept}
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            pass


def sweep_cache(folder: Path, known: set[str], min_files: int = CACHE_SCAN_MIN_FILES) -> tuple[set[str], int]:
    """One-off: find placeholders already in the thumbnail cache and delete them.

    A picture stored for ``min_files`` or more different URLs (or one already known)
    is a placeholder. Returns (placeholder hashes, files deleted)."""
    by_hash: dict[str, list[Path]] = defaultdict(list)
    if not folder.is_dir():
        return set(), 0
    for f in folder.iterdir():
        try:
            if f.is_file() and not f.name.endswith(".tmp"):
                by_hash[digest(f.read_bytes())].append(f)
        except OSError:
            continue
    found = {h for h, files in by_hash.items() if len(files) >= min_files or h in known}
    deleted = 0
    for h in found:
        for f in by_hash[h]:
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass
    return found, deleted
