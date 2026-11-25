"""Read file names that follow a known naming scheme (Whisparr/Sonarr-style format).

Example scheme (Whisparr):
    {Studio CleanTitleSlug}{.Release Date}.{Scene.CleanTitle}{.Scene.Performers}{.Quality.Title}{.MediaInfo VideoCodec}{-Release Group}
gives names like
    LittleAsians.2026-06-30.Im.A.Woman.Now.Kaori.Ojima.WEBDL-1080p.HEVC-PRT.mp4

Each ``{...}`` token may carry literal text before/after the name (``{.Release Date}``,
``{-Release Group}``) that only appears when the value isn't empty. Folder parts of the
scheme (``{Studio Title}/...``) are ignored: only the file name is read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

TOKEN_RE = re.compile(r"\{([^A-Za-z{}]*)([A-Za-z][A-Za-z0-9 ._:-]*?[A-Za-z0-9])([^A-Za-z0-9{}]*)\}")
CODEC = r"(?:[xh]\.?26[45]|hevc|avc|av1|vp9|xvid|divx|mpeg-?[24]|vc-?1|wmv\d?)"

# token name (lowercased, only letters) -> (field, regex)
_FIELDS: list[tuple[str, str, str]] = [
    ("releasedate", "date", r"\d{4}[-._ ]\d{2}[-._ ]\d{2}"),
    ("airdate", "date", r"\d{4}[-._ ]\d{2}[-._ ]\d{2}"),
    ("releasegroup", "group", r"[A-Za-z0-9]+"),
    ("quality", "quality", r"(?:[A-Za-z]+[-. ])?\d{3,4}[pi](?:[-. ]?proper|[-. ]?repack)?"),
    ("videocodec", "codec", CODEC),
    ("mediainfosimple", "codec", r"[^{}]+?"),
    ("performer", "performers", r".+?"),
    ("studio", "studio", r".+?"),
    ("site", "studio", r".+?"),
    ("network", "studio", r".+?"),
    ("title", "title", r".+?"),
]


@dataclass
class SchemeParts:
    studio: str = ""
    scene_date: date | None = None
    words: str = ""          # title + performers (their boundary can't be told apart)
    resolution: str = ""
    group: str = ""
    codec: str = ""


def _field(name: str) -> tuple[str, str]:
    flat = re.sub(r"[^a-z]", "", name.lower())
    for needle, field, rx in _FIELDS:
        if needle in flat:
            return field, rx
    return "other", r".*?"


@lru_cache(maxsize=16)
def compile_scheme(scheme: str) -> re.Pattern | None:
    scheme = scheme.strip().replace("\\", "/").split("/")[-1]  # file name part only
    if not scheme or not TOKEN_RE.search(scheme):
        return None
    out, pos, used = [], 0, {}
    for m in TOKEN_RE.finditer(scheme):
        out.append(re.escape(scheme[pos:m.start()]))
        pre, name, post = m.groups()
        field, rx = _field(name)
        n = used[field] = used.get(field, 0) + 1
        group = f"(?P<{field}{n}>{rx})"
        if pre or post:
            out.append(f"(?:{re.escape(pre)}{group}{re.escape(post)})?")
        else:
            out.append(group)
        pos = m.end()
    out.append(re.escape(scheme[pos:]))
    try:
        return re.compile("^" + "".join(out) + r"(?:\.[A-Za-z0-9]{2,4})?$", re.IGNORECASE)
    except re.error:
        return None


def parse_with_scheme(scheme: str, filename: str) -> SchemeParts | None:
    """The fields of ``filename`` if it fits ``scheme``, else None."""
    rx = compile_scheme(scheme)
    if rx is None:
        return None
    m = rx.match(filename)
    if not m:
        return None
    vals: dict[str, list[str]] = {}
    for k, v in m.groupdict().items():
        if v:
            vals.setdefault(re.sub(r"\d+$", "", k), []).append(v)
    get = lambda f: (vals.get(f) or [""])[0]  # noqa: E731
    d = None
    if get("date"):
        y, mo, da = re.split(r"[-._ ]", get("date"))
        try:
            d = date(int(y), int(mo), int(da))
        except ValueError:
            d = None
    q = get("quality")
    res = re.search(r"(\d{3,4})[pi]", q, re.I)
    words = " ".join(vals.get("title", []) + vals.get("performers", []))
    return SchemeParts(studio=get("studio"), scene_date=d, words=re.sub(r"[._]", " ", words),
                       resolution=f"{res.group(1)}p" if res else "", group=get("group"), codec=get("codec"))
