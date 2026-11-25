"""Match video file names on disk to cached scenes (and the exact release).

File names come from many places: the original release name
(``Studio.24.01.15.Jane.Doe.Title.XXX.1080p.HEVC.x265.PRT.mkv``), our own rename
template, a browser download, or a Whisparr/Sonarr-style rename such as
``Studio - 2024-01-15 - Jane Doe - Some Title [WEBDL-1080p][PRT].mp4``.

Strategy: parse studio / date / resolution / group / words from the file name,
then score candidate scenes:

* same studio (normalised, so "BrazzersExxtra" == "Brazzers Exxtra") and date
  within a day  → accept with modest word overlap;
* same studio, no date → needs strong word overlap;
* unknown studio but same date → needs strong word overlap.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .models import Release, Scene
from .parsing import (
    VIDEO_EXT_RE, find_group, find_resolution, normalise_studio, parse_title, strip_accents, tokens,
)

VIDEO_EXTS = {".mp4", ".mkv", ".wmv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".mpg", ".mpeg"}
# Unfinished downloads from browsers, JDownloader, torrent clients, our own .part files.
PARTIAL_SUFFIXES = (".part", ".crdownload", ".partial", ".tmp", ".download", ".!qb", ".!ut", ".bc!", ".opdownload")
SKIP_NAME_RE = re.compile(r"(?i)(^|[ ._\-\[(])(sample|trailer|preview)([ ._\-\])]|$)")


@dataclass
class FileMatch:
    path: str
    scene: Scene | None
    release: Release | None
    score: float
    reason: str


def is_candidate_file(p: Path, min_age_s: float = 0, now: float | None = None) -> bool:
    name = p.name.lower()
    if name.endswith(PARTIAL_SUFFIXES) or p.suffix.lower() not in VIDEO_EXTS:
        return False
    if SKIP_NAME_RE.search(p.stem):
        return False
    if min_age_s:
        import time

        try:
            if (now or time.time()) - p.stat().st_mtime < min_age_s:
                return False  # probably still being written
        except OSError:
            return False
    return True


def walk_videos(paths: list[str | Path], min_age_s: float = 0) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for root, _dirs, files in os.walk(p):
                for f in files:
                    fp = Path(root) / f
                    if is_candidate_file(fp, min_age_s):
                        out.append(fp)
        elif p.is_file() and is_candidate_file(p, min_age_s):
            out.append(p)
    return out


def _clean_name(name: str) -> str:
    name = VIDEO_EXT_RE.sub("", strip_accents(name))
    # Whisparr/Sonarr quality tags: [WEBDL-1080p] [Bluray-2160p] [HDTV-720p] [Proper]
    name = re.sub(r"(?i)\[(?:web[- ]?dl|webrip|bluray|hdtv|dvd|sdtv|raw[- ]?hd)?[- ]?(\d{3,4}p)\]", r" \1 ", name)
    return name


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _containment(small: set[str], big: set[str]) -> float:
    return len(small & big) / len(small) if small else 0.0


class SceneIndex:
    def __init__(self, scenes: list[Scene], studio_names: dict[str, str] | None = None):
        self.by_studio: dict[str, list[Scene]] = defaultdict(list)
        self.by_date: dict = defaultdict(list)
        self.toks: dict[str, set[str]] = {}
        # official stash-box spellings ("Brazzers Exxtra") -> our studio key
        self.alias = {normalise_studio(n): k for k, n in (studio_names or {}).items() if n}
        for s in scenes:
            k = normalise_studio(s.studio)
            self.by_studio[k].append(s)
            for d in s.scene_dates:
                self.by_date[d].append(s)
            words: set[str] = set()
            for r in s.releases:
                words |= set(tokens(" ".join(r.performers) + " " + r.title))
            self.toks[s.key] = words

    def studio_key(self, s: Scene) -> str:
        return normalise_studio(s.studio)

    def resolve_studio(self, key: str) -> str:
        """Our studio key for a normalised name, '' if no scene has that studio."""
        if key in self.by_studio:
            return key
        return self.alias.get(key, "")

    def match(self, filename: str) -> FileMatch:
        clean = _clean_name(filename)
        p = parse_title(clean)
        return self.match_fields(filename, p["studio"], p["scene_date"],
                                 " ".join(p["performers"]) + " " + p["title"],
                                 find_resolution(clean), find_group(clean))

    def match_scheme(self, filename: str, scheme: str) -> FileMatch | None:
        """Match using a known naming scheme; None if the name doesn't fit it."""
        from .naming import parse_with_scheme
        parts = parse_with_scheme(scheme, filename)
        if parts is None or not (parts.studio or parts.scene_date):
            return None
        return self.match_fields(filename, parts.studio, parts.scene_date, parts.words,
                                 parts.resolution, parts.group)

    def match_fields(self, filename: str, studio_name: str, d, text: str, res: str, grp: str) -> FileMatch:
        studio = normalise_studio(studio_name) if studio_name else ""
        if studio not in self.by_studio and studio in self.alias:
            studio = self.alias[studio]
        words = set(tokens(text))
        words.discard(studio)

        best: tuple[float, Scene | None, str] = (0.0, None, "no match")

        def consider(s: Scene, score: float, why: str) -> None:
            nonlocal best
            if score > best[0]:
                best = (score, s, why)

        cands = self.by_studio.get(studio, []) if studio else []
        wordless: list[Scene] = []
        for s in cands:
            st = self.toks[s.key]
            overlap = max(_jaccard(words, st), 0.9 * _containment(st, words), 0.9 * _containment(words, st))
            if d and s.scene_dates:
                delta = min(abs((sd - d).days) for sd in s.scene_dates)
                if delta <= 1:
                    if not words:
                        wordless.append(s)
                    elif overlap >= 0.34:
                        consider(s, 0.6 + 0.4 * overlap - 0.05 * delta, "studio + date")
                continue  # both dated but different days: not this scene
            if overlap >= 0.7:
                consider(s, 0.4 + 0.5 * overlap, "studio + title")
        if best[1] is None and len(wordless) == 1:
            # name has only studio + date: fine if that's unambiguous
            consider(wordless[0], 0.6, "studio + date (only scene that day)")
        if best[1] is None and d:
            # Studio unknown or spelled differently: same date (±1) and strong word overlap.
            for dd in (d, d - timedelta(days=1), d + timedelta(days=1)):
                for s in self.by_date.get(dd, []):
                    overlap = _jaccard(words, self.toks[s.key])
                    if overlap >= 0.6:
                        consider(s, 0.3 + 0.5 * overlap, "date + title")

        score, scene, why = best
        if scene is None:
            return FileMatch(filename, None, None, 0.0, "no match")
        return FileMatch(filename, scene, pick_release(scene, res, grp), round(score, 3), why)


def pick_release(scene: Scene, resolution: str, group: str) -> Release:
    """The release a file most likely is: same resolution (and group if known)."""
    def key(r: Release):
        return (r.resolution == resolution, bool(group) and r.release_group.lower() == group.lower(),
                r.resolution_rank)
    return max(scene.releases, key=key)
