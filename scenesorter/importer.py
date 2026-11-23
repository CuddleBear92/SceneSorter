"""Scan files/folders and match the videos in them to cached scenes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .filematch import FileMatch, SceneIndex, walk_videos
from .models import Scene


@dataclass
class ScanResult:
    matched: list[tuple[Path, FileMatch]] = field(default_factory=list)
    unmatched: list[Path] = field(default_factory=list)
    skipped: int = 0  # already scanned, unchanged

    def rows(self) -> list[tuple]:
        """(path, size, mtime, scene_key, source, post_id, score) for Database.save_scanned"""
        out = []
        for p, m in self.matched:
            st = _stat(p)
            out.append((str(p), *st, m.scene.key, m.release.source, m.release.post_id, m.score))
        for p in self.unmatched:
            out.append((str(p), *_stat(p), None, None, None, 0.0))
        return out


def _stat(p: Path) -> tuple[int, float]:
    try:
        st = p.stat()
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0.0


def scan(paths: list[str | Path], scenes: list[Scene], known: dict[str, tuple[int, float]] | None = None,
         min_age_s: float = 0, rescan_unmatched: bool = False) -> ScanResult:
    """Walk paths, skip files already scanned (same size + mtime), match the rest.

    ``known`` maps path -> (size, mtime) from earlier scans; unmatched files are
    retried when ``rescan_unmatched`` (new posts may have arrived since).
    """
    index = SceneIndex(scenes)
    res = ScanResult()
    for p in walk_videos(paths, min_age_s):
        if known is not None and not rescan_unmatched and known.get(str(p)) == _stat(p):
            res.skipped += 1
            continue
        m = index.match(p.name)
        if m.scene is None:
            # Folder names often carry the release name when the file is just "video.mp4"
            m = index.match(p.parent.name + p.suffix) if p.parent.name else m
        if m.scene is not None:
            res.matched.append((p, m))
        else:
            res.unmatched.append(p)
    return res
