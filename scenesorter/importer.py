"""Scan files/folders and match the videos in them to cached scenes."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .filematch import FileMatch, SceneIndex, is_candidate_file, walk_videos
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
    gone: list[str] = field(default_factory=list)                # files no longer on disk
    dirs: dict[str, tuple] = field(default_factory=dict)         # changed folders -> cache entry
    dirs_gone: list[str] = field(default_factory=list)           # folders no longer on disk
    files: int = 0
    folders: int = 0
    folders_listed: int = 0      # folders whose date changed (or full scan), so were read again
    matched_now: int = 0
    unmatched: int = 0
    seconds: float = 0.0


def _walk_library(root: Path, dir_cache: dict[str, tuple], full: bool, res: LibraryScan, visited: set[str]):
    """Yield (file path, (size, mtime) or None). None = the file's folder hasn't changed, so the
    file wasn't looked at. A folder's own modified date changes whenever a file in it is added,
    removed or renamed, so an unchanged folder is not even listed: we reuse what it held."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            mtime = d.stat().st_mtime
        except OSError:
            continue
        res.folders += 1
        key = str(d)
        visited.add(key)
        cached = None if full else dir_cache.get(key)
        if cached and cached[0] == mtime:
            subdirs, names = cached[1], cached[2]
            stack.extend(d / n for n in subdirs)
            for n in names:
                yield d / n, None
            continue
        res.folders_listed += 1
        subdirs, names, stats = [], [], []
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            subdirs.append(e.name)
                        elif e.is_file() and is_candidate_file(Path(e.name)):
                            st = e.stat()  # free on Windows: comes with the folder listing
                            names.append(e.name)
                            stats.append((st.st_size, st.st_mtime))
                    except OSError:
                        continue
        except OSError:
            continue
        res.dirs[key] = (mtime, subdirs, names)
        stack.extend(d / n for n in subdirs)
        for n, st in zip(names, stats):
            yield d / n, st


def scan_library(root: str | Path, scenes: list[Scene], known: dict[str, tuple] | None = None,
                 studio_names: dict[str, str] | None = None, scheme: str = "",
                 dir_cache: dict[str, tuple] | None = None, retry_unmatched: bool = True,
                 full: bool = False) -> LibraryScan:
    """Scan the final, studio-sorted library (read-only) and map scene key -> files.

    ``known``: path -> (size, mtime, source, post_id) from earlier scans; unchanged files
    that matched are not matched again. ``dir_cache``: folder -> (mtime, subdirs, files);
    unchanged folders are not listed again. Unmatched files are retried when
    ``retry_unmatched`` (new posts arrived since). ``full`` ignores both caches.
    """
    import time

    t0 = time.monotonic()
    root = Path(root)
    res = LibraryScan()
    if not root.is_dir():
        return res
    known = {} if full or known is None else known
    by_post = {(r.source, r.post_id): s.key for s in scenes for r in s.releases}
    index: SceneIndex | None = None
    seen: set[str] = set()
    visited: set[str] = set()
    for p, st in _walk_library(root, dir_cache or {}, full, res, visited):
        sp = str(p)
        seen.add(sp)
        res.files += 1
        k = known.get(sp)
        if st is None:  # folder unchanged: trust the earlier scan
            if k is None:
                st = _stat(p)
            else:
                st = (k[0], k[1])
        if k and (k[0], k[1]) == st:
            key = by_post.get((k[2], k[3])) if k[2] else None
            if key:
                res.owned.setdefault(key, []).append(p)
                continue
            if not k[2] and not retry_unmatched:
                res.unmatched += 1
                continue
        if index is None:
            index = SceneIndex(scenes, studio_names)
        rel = p.relative_to(root).parts
        m = match_with_folder(index, p, rel[0] if len(rel) > 1 else "", scheme)
        if m.scene is not None:
            res.matched_now += 1
            res.owned.setdefault(m.scene.key, []).append(p)
            res.rows.append((sp, *st, m.scene.key, m.release.source, m.release.post_id, m.score))
        else:
            res.unmatched += 1
            if not k or k[2] or (k[0], k[1]) != st:
                res.rows.append((sp, *st, None, None, None, 0.0))
    res.gone = [p for p in known if p not in seen]
    res.dirs_gone = [d for d in (dir_cache or {}) if d not in visited]
    res.seconds = time.monotonic() - t0
    return res
