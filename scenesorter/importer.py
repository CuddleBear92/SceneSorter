"""Scan files/folders and match the videos in them to cached scenes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .filematch import FileMatch, SceneIndex, walk_videos
from .models import Scene
from .parsing import normalise_studio


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


def match_with_folder(index: SceneIndex, p: Path, studio_folder: str = "", scheme: str = "") -> FileMatch:
    """Match a file by its name, falling back to its folder's name. ``studio_folder``
    is the studio a sorted library puts it under (Library/<Studio>/...): used when the
    file name doesn't carry the studio, and preferred when the two disagree. When the
    name fits ``scheme`` (the library's naming format), its fields are read from that."""
    m = index.match_scheme(p.name, scheme) if scheme else None
    if m is not None and m.scene is not None:
        return m
    m = index.match(p.name)
    if m.scene is None and p.parent.name and p.parent.name != studio_folder:
        m = index.match(p.parent.name + p.suffix)
    hint = index.resolve_studio(normalise_studio(studio_folder)) if studio_folder else ""
    if hint and (m.scene is None or index.studio_key(m.scene) != hint):
        h = index.match(f"{studio_folder} - {p.name}")
        if h.scene is not None and index.studio_key(h.scene) == hint:
            return h
    return m


@dataclass
class LibraryScan:
    owned: dict[str, list[Path]] = field(default_factory=dict)  # scene key -> files
    rows: list[tuple] = field(default_factory=list)              # new/changed rows for save_scanned
    files: int = 0
    unmatched: int = 0


def scan_library(root: str | Path, scenes: list[Scene], known: dict[str, tuple] | None = None,
                 studio_names: dict[str, str] | None = None, scheme: str = "") -> LibraryScan:
    """Scan the final, studio-sorted library (read-only) and map scene key -> files.

    ``known`` maps path -> (size, mtime, source, post_id) from earlier scans, so unchanged
    files that already matched are not matched again. Files that didn't match are retried.
    """
    root = Path(root)
    res = LibraryScan()
    if not root.is_dir():
        return res
    by_post = {(r.source, r.post_id): s.key for s in scenes for r in s.releases}
    index: SceneIndex | None = None
    for p in walk_videos([root]):
        res.files += 1
        st = _stat(p)
        k = (known or {}).get(str(p))
        if k and (k[0], k[1]) == st and k[2]:
            key = by_post.get((k[2], k[3]))
            if key:
                res.owned.setdefault(key, []).append(p)
                continue
        if index is None:
            index = SceneIndex(scenes, studio_names)
        rel = p.relative_to(root).parts
        m = match_with_folder(index, p, rel[0] if len(rel) > 1 else "", scheme)
        if m.scene is not None:
            res.owned.setdefault(m.scene.key, []).append(p)
            res.rows.append((str(p), *st, m.scene.key, m.release.source, m.release.post_id, m.score))
        else:
            res.unmatched += 1
            res.rows.append((str(p), *st, None, None, None, 0.0))
    return res
