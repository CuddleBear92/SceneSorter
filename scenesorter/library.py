"""Renaming completed downloads into the library and scanning what's owned."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .models import Release
from .parsing import match_key, parse_title, enrich

VIDEO_EXTS = {".mp4", ".mkv", ".wmv", ".avi", ".mov", ".m4v", ".webm"}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_COMPONENT = 180


def safe_component(s: str) -> str:
    s = _ILLEGAL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:MAX_COMPONENT].rstrip(" .") or "Unknown"


def render_name(template: str, r: Release, ext: str) -> Path:
    """Render a template like '{studio}/{studio} - {date} - {title} [{resolution}]'."""
    fields = {
        "studio": r.studio or "Unknown Studio",
        "title": r.title or r.raw_title,
        "performers": ", ".join(r.performers) if r.performers else "",
        "date": r.scene_date.isoformat() if r.scene_date else "",
        "year": str(r.scene_date.year) if r.scene_date else "",
        "resolution": r.resolution or "",
        "source": r.source,
    }
    rendered = template.format(**fields)
    # tidy separators left dangling by empty fields: "A -  - B", "[]"
    rendered = re.sub(r"\[\s*\]|\(\s*\)", "", rendered)
    parts = []
    for comp in rendered.replace("\\", "/").split("/"):
        comp = re.sub(r"(\s*-\s*){2,}", " - ", comp)
        comp = comp.strip(" -")
        parts.append(safe_component(comp))
    parts[-1] = parts[-1] + ext.lower()
    return Path(*parts)


def pick_video(path: Path) -> Path | None:
    """For a finished download (file or folder), return the main video file."""
    if path.is_file():
        return path if path.suffix.lower() in VIDEO_EXTS else None
    vids = [p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS
            and "sample" not in p.name.lower()]
    return max(vids, key=lambda p: p.stat().st_size) if vids else None


def sort_into_library(src: Path, r: Release, library_dir: Path, template: str) -> Path:
    video = pick_video(src)
    if video is None:
        raise FileNotFoundError(f"No video file found in {src}")
    dest = Path(library_dir) / render_name(template, r, video.suffix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stem, n = dest.stem, 2
        while dest.exists():
            dest = dest.with_name(f"{stem} ({n}){dest.suffix}")
            n += 1
    shutil.move(str(video), str(dest))
    if src.is_dir() and src != video:
        shutil.rmtree(src, ignore_errors=True)
    return dest


def scan_library(library_dir: Path) -> dict[str, list[Path]]:
    """Map scene match-key -> files already in the library."""
    owned: dict[str, list[Path]] = {}
    root = Path(library_dir)
    if not root.exists():
        return owned
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            r = enrich(Release(source="library", post_id=str(p), post_url="", raw_title=p.stem))
            # library files carry resolution in the name; key ignores it
            owned.setdefault(match_key(r), []).append(p)
    return owned


__all__ = ["render_name", "sort_into_library", "scan_library", "pick_video", "parse_title"]
