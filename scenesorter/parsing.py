"""Release-title parsing and normalisation.

Both sites post each resolution of a scene as its own entry, and they don't
always format titles identically. This module turns a raw post title into
structured fields plus a *match key* that is identical for every
resolution/encode/site variant of the same scene.

Supported shapes (case-insensitive, mixed separators are fine):

    Studio.24.01.15.Jane.Doe.And.John.Roe.Some.Title.XXX.1080p.HEVC.x265-GRP
    Studio 24 01 15 Jane Doe Some Title XXX 2160p MP4-GRP
    [Studio] Jane Doe, John Roe - Some Title (2024-01-15) [1080p]
    Studio - Jane Doe - Some Title 720p
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from .models import Release

RESOLUTION_RE = re.compile(r"(?<![a-z0-9])(4320|2160|1440|1080|720|576|540|480|360)[pi](?![a-z0-9])", re.I)
UHD_RE = re.compile(r"(?<![a-z0-9])(4k|uhd)(?![a-z0-9])", re.I)

# Tokens that describe the *file*, not the scene. Removed before matching.
NOISE_TOKENS = {
    "xxx", "mp4", "mkv", "wmv", "avi", "web", "webrip", "webdl", "web-dl", "dl", "hdtv",
    "hevc", "h264", "h265", "x264", "x265", "avc", "av1", "aac", "10bit", "8bit", "hdr",
    "sdr", "60fps", "30fps", "fps", "uhd", "4k", "fhd", "hd", "sd", "prt", "repack",
    "proper", "internal", "encoded", "encode", "reencode", "re-encode", "split", "scenes",
    "remastered", "full", "ktr", "gush", "xc", "p2p", "nbq", "tg", "nr", "rq", "wrb", "iak",
    "vsex", "cpd", "vbq", "fsp", "sexors", "grp", "rarbg",
}
# Words joining performer names; dropped from the match key.
JOINERS = {"and", "with", "feat", "ft", "featuring", "vs", "x", "&", "aka"}

DATE_PATTERNS = [
    # 2024-01-15 / 2024.01.15 / 2024 01 15
    (re.compile(r"(?<!\d)(20\d{2})[ ._\-/](\d{2})[ ._\-/](\d{2})(?!\d)"), "ymd4"),
    # 24.01.15 (scene-release convention, YY.MM.DD)
    (re.compile(r"(?<!\d)(\d{2})[ ._\-](\d{2})[ ._\-](\d{2})(?!\d)"), "ymd2"),
    # 15.01.2024
    (re.compile(r"(?<!\d)(\d{2})[ ._\-/](\d{2})[ ._\-/](20\d{2})(?!\d)"), "dmy4"),
]


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def find_resolution(text: str) -> str:
    m = RESOLUTION_RE.search(text)
    if m:
        return f"{m.group(1)}p"
    if UHD_RE.search(text):
        return "2160p"
    return ""


def find_date(text: str) -> tuple[date | None, tuple[int, int] | None]:
    """Return (date, (start, end) span) of the first plausible date in text."""
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(text):
            a, b, c = (int(x) for x in m.groups())
            try:
                if kind == "ymd4":
                    d = date(a, b, c)
                elif kind == "ymd2":
                    d = date(2000 + a, b, c)
                else:
                    d = date(c, b, a)
            except ValueError:
                continue
            return d, m.span()
    return None, None


def _clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" -–—_|:,.")


def _title_case(s: str) -> str:
    return " ".join(w if w.isupper() and len(w) > 1 else w[:1].upper() + w[1:] for w in s.split())


def parse_title(raw: str) -> dict:
    """Parse a raw post title into studio/performers/title/date/resolution."""
    text = strip_accents(raw).strip()
    resolution = find_resolution(text)
    scene_date, span = find_date(text)

    studio = ""
    performers: list[str] = []
    title = ""

    bracket = re.match(r"^\s*[\[(]([^\])]+)[\])]\s*(.*)$", text)
    if bracket:
        # "[Studio] Performers - Title (date) [res]"
        studio = bracket.group(1)
        rest = bracket.group(2)
        rest = _strip_tail(rest, span_text=text[span[0]:span[1]] if span else None)
        parts = [p for p in re.split(r"\s+[-–—|]\s+", rest) if p.strip()]
        if len(parts) >= 2:
            performers = _split_performers(parts[0])
            title = " - ".join(parts[1:])
        elif parts:
            title = parts[0]
    elif re.search(r"\s[-–—|]\s", text):
        # "Studio - Performers - Title 720p"
        body = _strip_tail(text, span_text=text[span[0]:span[1]] if span else None)
        parts = [p for p in re.split(r"\s+[-–—|]\s+", body) if p.strip()]
        if len(parts) >= 3:
            studio, performers, title = parts[0], _split_performers(parts[1]), " - ".join(parts[2:])
        elif len(parts) == 2:
            studio, title = parts
        elif parts:
            title = parts[0]
    else:
        # Scene-release dotted/spaced format: Studio.YY.MM.DD.rest...
        if span:
            studio = text[: span[0]]
            rest = text[span[1]:]
        else:
            first, _, rest = re.sub(r"[._]", " ", text).partition(" ")
            studio = first
        rest = re.sub(r"[._]", " ", rest)
        rest = _strip_tail(rest)
        title = rest  # performers can't be reliably separated from the title here

    studio = _title_case(_clean_spaces(re.sub(r"[._]", " ", studio)))
    title = _clean_spaces(title)
    return {
        "studio": studio,
        "performers": performers,
        "title": title,
        "scene_date": scene_date,
        "resolution": resolution,
    }


def _strip_tail(s: str, span_text: str | None = None) -> str:
    """Remove date, resolution and encoding junk from a title fragment."""
    if span_text:
        s = s.replace(span_text, " ")
    s = re.sub(r"[\[(]\s*[\])]", " ", s)
    s = RESOLUTION_RE.sub(" ", s)
    s = re.sub(r"[\[(][^\])]*[\])]", lambda m: " " if _is_noise(m.group(0)[1:-1]) else m.group(0), s)
    # cut at first "XXX" marker (everything after is encode info) or trailing -GROUP
    s = re.split(r"(?i)(?<![a-z])xxx(?![a-z])", s)[0]
    s = re.sub(r"-[A-Za-z0-9]{2,12}\s*$", "", s) if re.search(r"\S-[A-Za-z0-9]{2,12}\s*$", s) else s
    words = [w for w in s.split() if w.lower().strip("[]()") not in NOISE_TOKENS]
    return _clean_spaces(" ".join(words))


def _is_noise(inner: str) -> bool:
    toks = re.split(r"[\s._\-]+", inner.lower())
    return all(t in NOISE_TOKENS or RESOLUTION_RE.fullmatch(t) or not t for t in toks)


def _split_performers(s: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|\band\b|\bwith\b|\bfeat\.?|\bft\.?)\s*", s, flags=re.I)
    return [_clean_spaces(p) for p in parts if p and p.strip()]


def tokens(s: str) -> list[str]:
    s = strip_accents(s).lower()
    s = re.sub(r"['’`]", "", s)
    toks = re.split(r"[^a-z0-9]+", s)
    return [t for t in toks if t and t not in NOISE_TOKENS and t not in JOINERS and not RESOLUTION_RE.fullmatch(t)]


def normalise_studio(s: str) -> str:
    return "".join(tokens(s))


def match_key(r: Release) -> str:
    """Exact-match key; identical for all resolution variants of one scene."""
    studio = normalise_studio(r.studio)
    d = r.scene_date.isoformat() if r.scene_date else ""
    body = tokens(" ".join(r.performers) + " " + r.title)
    return f"{studio}|{d}|{' '.join(sorted(set(body)))}"


def enrich(r: Release) -> Release:
    """Fill parsed fields on a Release that the source didn't supply."""
    p = parse_title(r.raw_title)
    r.studio = r.studio or p["studio"]
    r.performers = r.performers or p["performers"]
    r.title = r.title or p["title"]
    r.scene_date = r.scene_date or p["scene_date"]
    r.resolution = r.resolution or p["resolution"]
    return r
