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

# Bump whenever parsing changes; cached posts are re-parsed on next start.
PARSER_VERSION = "5"

RESOLUTION_RE = re.compile(r"(?<![a-z0-9])(4320|2160|1440|1080|720|576|540|480|360)[pi](?![a-z0-9])", re.I)
UHD_RE = re.compile(r"(?<![a-z0-9])(4k|uhd)(?![a-z0-9])", re.I)

# Tokens that describe the *file*, not the scene. Removed before matching.
FORMAT_TOKENS = {
    "xxx", "mp4", "mkv", "wmv", "avi", "web", "webrip", "webdl", "web-dl", "dl", "hdtv",
    "hevc", "h264", "h265", "x264", "x265", "avc", "av1", "aac", "10bit", "8bit", "hdr",
    "sdr", "60fps", "30fps", "fps", "uhd", "4k", "fhd", "hd", "sd", "repack",
    "proper", "internal", "encoded", "encode", "reencode", "re-encode", "split", "scenes",
    "remastered", "full", "rarbg",
}
# Known release groups, canonical spelling. Unknown groups are still detected
# from position (trailing "-GRP", "[GRP]", last token after the encode info);
# this list is only used to recognise a group in less standard positions and
# to normalise its case. Extend freely.
KNOWN_GROUPS = [
    "PRT", "GUSH", "KTR", "XLeech", "NBQ", "P2P", "WRB", "iak", "VSEX", "CPD", "SEXORS",
    "XC", "YAPG", "BTWA", "XVX", "VBQ", "FSP", "TBP", "WhiteHat", "GalaXXXy", "NoGrp",
]
_GROUP_CANON = {g.lower(): g for g in KNOWN_GROUPS}
NOISE_TOKENS = FORMAT_TOKENS | set(_GROUP_CANON)
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


VIDEO_EXT_RE = re.compile(r"\.(mp4|mkv|wmv|avi|mov|m4v|webm)$", re.I)


def _is_format_token(t: str) -> bool:
    t = t.lower()
    return t in FORMAT_TOKENS or bool(RESOLUTION_RE.fullmatch(t)) or bool(re.fullmatch(r"\d+(\.\d+)?\s*[gm]i?b", t))


def canon_group(g: str) -> str:
    g = g.strip(" -_.[]()")
    return _GROUP_CANON.get(g.lower(), g)


def find_group(raw: str) -> str:
    """Best-effort release group from a release name / post title.

    Handles ``...XXX.1080p.HEVC.x265.PRT``, ``...MP4-GRP``, ``... [GRP]``,
    ``... (GRP)`` and known group names anywhere after the resolution.
    """
    text = VIDEO_EXT_RE.sub("", strip_accents(raw).strip())
    # 1. trailing bracketed token that isn't format info: "[PRT]" / "(PRT)"
    m = re.search(r"[\[(]\s*([A-Za-z0-9]{2,15})\s*[\])]\s*$", text)
    if m and not _is_format_token(m.group(1)):
        return canon_group(m.group(1))
    # 2. scene-style trailing "-GRP" (directly attached to the previous token)
    m = re.search(r"\S-([A-Za-z0-9]{2,15})\s*$", text)
    if m and not _is_format_token(m.group(1)) and not m.group(1).isdigit():
        return canon_group(m.group(1))
    # 3. last token after the encode block: "XXX.1080p.HEVC.x265.PRT"
    toks = [t for t in re.split(r"[\s._\[\]()\-]+", text) if t]
    res_idx = max((i for i, t in enumerate(toks) if RESOLUTION_RE.fullmatch(t) or t.lower() == "xxx"), default=None)
    if res_idx is not None:
        tail = [t for t in toks[res_idx + 1:] if not _is_format_token(t)]
        if len(tail) == 1 and not tail[0].isdigit():
            return canon_group(tail[0])
        for t in reversed(tail):
            if t.lower() in _GROUP_CANON:
                return _GROUP_CANON[t.lower()]
    # 4. a known group name standing alone anywhere near the end
    for t in reversed(toks[-4:]):
        if t.lower() in _GROUP_CANON:
            return _GROUP_CANON[t.lower()]
    return ""


def parse_duration(s: str) -> int | None:
    """'1:02:03' / '32:15' / '32 min' / '1h 2m' -> seconds."""
    s = s.strip().lower()
    m = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", s)
    if m:
        h, mi, se = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        return h * 3600 + mi * 60 + se
    m = re.fullmatch(r"(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?|s)?)?)?\s*(?:(\d+)\s*s(?:ec(?:onds?|s)?)?)?", s)
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)
    return None


_SIZE_MULT = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def parse_size(num: str, unit: str) -> int | None:
    try:
        return int(float(num.replace(",", ".")) * _SIZE_MULT[unit[0].lower()])
    except (ValueError, KeyError):
        return None


_FIELD = r"(?:^|\n|\|)\s*{name}\s*[:\-–]\s*([^\n|]+)"


def extract_post_metadata(text: str) -> dict:
    """Pull 'Label: value' style metadata out of a post body (plain text).

    Returns any of: filename, duration_s, size_bytes, tags, release_group.
    """
    out: dict = {}

    def grab(names: str) -> str | None:
        m = re.search(_FIELD.format(name=f"(?:{names})"), text, re.I)
        return m.group(1).strip() if m else None

    fn = grab(r"file\s*name|filename|release(?:\s*name)?|file")
    if fn:
        out["filename"] = fn
    dur = grab(r"duration|runtime|run\s*time|length|time")
    if dur and (d := parse_duration(re.split(r"[,(]", dur)[0])):
        out["duration_s"] = d
    size = grab(r"file\s*size|size")
    if size and (m := re.search(r"([\d.,]+)\s*([KMGT])i?B", size, re.I)):
        out["size_bytes"] = parse_size(m.group(1), m.group(2))
    tags = grab(r"tags|genres?|categories|niches?")
    if tags:
        out["tags"] = [t.strip() for t in re.split(r"[,;#]", tags) if t.strip()]
    grp = grab(r"group|release\s*group|encoder|ripper")
    if grp:
        out["release_group"] = canon_group(grp.split()[0])
    return out


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

    studio = re.sub(r"(?i)\.(com|net|xxx|tv)\s*$", "", studio.strip())
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
    s = re.sub(r"(?i)[ .](com|net|xxx|tv)$", "", s.strip())
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
    r.release_group = r.release_group or find_group(r.filename or "") or find_group(r.raw_title)
    if not r.resolution and r.filename:
        r.resolution = find_resolution(r.filename)
    r.tags = clean_tags(r.tags, r.studio)
    return r


def clean_tags(tags: list[str], studio: str) -> list[str]:
    """Drop studio names (sites often file posts under the studio as a tag/category)
    and duplicates."""
    st = normalise_studio(studio) if studio else ""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        words = tokens(t)
        if not words:  # pure resolution/format tags like "1080p", "720p", "HEVC"
            continue
        key = "".join(words)
        if not t.strip() or key in seen or (st and normalise_studio(t) == st):
            continue
        seen.add(key)
        out.append(t.strip())
    return out


def reparse(r: Release) -> Release:
    """Recompute title-derived fields from scratch (after a parser upgrade)."""
    r.studio, r.performers, r.title, r.scene_date = "", [], "", None
    r.resolution = r.resolution if r.resolution and not find_resolution(r.raw_title) else ""
    r.release_group = ""
    return enrich(r)
