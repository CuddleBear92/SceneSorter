"""Filtering + sorting the scene grid, off the UI thread.

``compute_view`` is plain Python (no Qt) so it can run on a worker thread and
be unit-tested. The UI thread only swaps in the resulting list.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields
from datetime import date

from .models import Scene
from .parsing import normalise_studio
from .status import LIB_OK, LIB_UPGRADE, group_of, library_upgrade

SORTS = {
    "posted": "Newest posted",
    "date": "Scene date",
    "studio": "Studio A–Z",
    "title": "Title A–Z",
    "duration": "Longest first",
}


@dataclass
class ViewSpec:
    text: str = ""
    resolution: str = ""
    source: str = ""
    group: str = ""
    codec: str = ""
    studio: str = ""        # normalised studio key
    performer: str = ""     # performer key
    fav_only: bool = False
    # "" all · "missing" still wanted · "partial" grabbed/partial · "have" complete · "any" not none
    # · "lib" in library · "upgrade" library file below your preference and a release that
    # meets it is posted (not grabbed yet) · "below" library file below your preference
    have: str = ""
    show_hidden: bool = False
    sort: str = "posted"
    # resolution/source/group/codec must all match the *same* release (one file), not
    # just appear somewhere among the scene's releases
    same_file: bool = False

    def copy(self) -> "ViewSpec":
        return ViewSpec(**{f.name: getattr(self, f.name) for f in fields(self)})


@dataclass
class ViewState:
    """Everything filtering needs to know besides the scenes themselves."""
    owned: set[str] = field(default_factory=set)
    status: dict[str, str] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)
    studio_names: dict[str, str] = field(default_factory=dict)
    scene_performers: dict[str, list[str]] = field(default_factory=dict)
    performer_names: dict[str, str] = field(default_factory=dict)
    fav_studios: set[str] = field(default_factory=set)
    fav_performers: set[str] = field(default_factory=set)
    hidden_studios: set[str] = field(default_factory=set)      # studio keys
    hidden_performers: set[str] = field(default_factory=set)   # performer keys
    fav_overrides_hidden_studio: bool = False
    library_files: dict[str, list[tuple]] = field(default_factory=dict)  # key -> [(res, group, codec)]
    preferred_groups: tuple[str, ...] = ()                             # lowercased
    wanted_resolution: str = ""
    grabbed: frozenset = frozenset()   # (source, post_id) of releases grabbed/done/verified
    assume_group: str = ""             # count HEVC/AV1 posts without a group as this group
    source_order: tuple = ()

    def state(self, key: str) -> str:
        return "complete" if key in self.owned else self.status.get(key, "none")

    def display_studio(self, s: Scene) -> str:
        return self.studio_names.get(normalise_studio(s.studio), s.studio) if s.studio else ""

    def is_favorite(self, s: Scene) -> bool:
        return (bool(s.studio) and normalise_studio(s.studio) in self.fav_studios) or \
            any(k in self.fav_performers for k in self.scene_performers.get(s.key, ()))


def is_hidden(s: Scene, st: ViewState) -> bool:
    """Hidden scene, a hidden studio (unless a favourite performer is in it and the user
    allows that override), or a hidden performer in it."""
    if s.key in st.hidden:
        return True
    perfs = st.scene_performers.get(s.key, ())
    if st.hidden_performers and any(k in st.hidden_performers for k in perfs):
        return True
    if st.hidden_studios and s.studio and normalise_studio(s.studio) in st.hidden_studios:
        if st.fav_overrides_hidden_studio and any(k in st.fav_performers for k in perfs):
            return False
        return True
    return False


def haystack(s: Scene, st: ViewState) -> str:
    """Searchable text of a scene, without performer names (those change often and are
    looked up separately, so new stash-box matches don't force a full index rebuild)."""
    parts = [s.studio, st.display_studio(s)]
    for r in s.releases:
        parts += (r.title, r.raw_title, r.filename, r.release_group, r.codec, *r.performers, *r.tags)
    return " ".join(parts).lower()


class _Index:
    """Per-scene values that are expensive to recompute on every keystroke."""

    def __init__(self, scenes: list[Scene], st: ViewState):
        self.studio = [normalise_studio(s.studio) if s.studio else "" for s in scenes]
        self.display = [(st.studio_names.get(k, s.studio) if s.studio else "").lower()
                        for k, s in zip(self.studio, scenes)]
        self.codecs = [frozenset(s.codecs) for s in scenes]
        self.res = [frozenset(s.resolutions) for s in scenes]
        self.groups = [frozenset(s.release_groups) for s in scenes]
        self.sources = [frozenset(s.sources) for s in scenes]
        self.hay = [haystack(s, st) for s in scenes]


_cache: dict = {}
_lock = threading.Lock()


def _index(scenes: list[Scene], st: ViewState) -> _Index:
    # Rebuilt only when the scene list or studio names are replaced (they're swapped, not
    # mutated). Locked so two panes filtering at once don't both build it.
    sig = (id(scenes), len(scenes), id(st.studio_names))
    with _lock:
        if _cache.get("sig") != sig:
            _cache["index"] = _Index(scenes, st)
            _cache["sig"] = sig
        return _cache["index"]


def upgrade_wanted(s: Scene, st: ViewState, include_unavailable: bool = False) -> bool:
    """Library file below your preference, and (unless ``include_unavailable``) a release
    that meets it is posted and hasn't been grabbed yet."""
    verdict, rel = library_upgrade(s, st.library_files.get(s.key, []), st.wanted_resolution,
                                   st.preferred_groups, list(st.source_order), st.assume_group)
    if verdict == LIB_OK:
        return False
    if include_unavailable:
        return True
    if verdict != LIB_UPGRADE:
        return False
    return not any((r.source, r.post_id) in st.grabbed for r in s.releases
                   if r.resolution_rank == rel.resolution_rank
                   and group_of(r.release_group.lower(), r.codec, st.assume_group.lower()) in st.preferred_groups)


def compute_view(scenes: list[Scene], spec: ViewSpec, st: ViewState) -> list[Scene]:
    ix = _index(scenes, st)
    words = spec.text.lower().split()
    out: list[Scene] = []
    for i, s in enumerate(scenes):
        if not spec.show_hidden and is_hidden(s, st):
            continue
        if spec.have:
            state = st.state(s.key)
            if spec.have == "have" and state != "complete":
                continue
            if spec.have == "missing" and state == "complete":
                continue
            if spec.have == "partial" and state not in ("partial", "grabbed"):
                continue
            if spec.have == "any" and state == "none":
                continue
            if spec.have in ("lib", "upgrade", "below"):
                if s.key not in st.owned:
                    continue
                if spec.have != "lib" and not upgrade_wanted(s, st, spec.have == "below"):
                    continue
        if spec.studio and ix.studio[i] != spec.studio:
            continue
        if spec.performer and spec.performer not in st.scene_performers.get(s.key, ()):
            continue
        if spec.fav_only and not (ix.studio[i] in st.fav_studios or
                                  any(k in st.fav_performers for k in st.scene_performers.get(s.key, ()))):
            continue
        if spec.resolution and spec.resolution not in ix.res[i]:
            continue
        if spec.source and spec.source not in ix.sources[i]:
            continue
        if spec.group and spec.group not in ix.groups[i]:
            continue
        if spec.codec and spec.codec not in ix.codecs[i]:
            continue
        if spec.same_file and (spec.resolution or spec.source or spec.group or spec.codec) and not any(
                (not spec.resolution or r.resolution == spec.resolution)
                and (not spec.source or r.source == spec.source)
                and (not spec.group or r.release_group == spec.group)
                and (not spec.codec or r.codec == spec.codec) for r in s.releases):
            continue
        if words:
            hay = ix.hay[i]
            if not all(w in hay for w in words):
                pk = st.scene_performers.get(s.key)
                if not pk:
                    continue
                hay = hay + " " + " ".join(st.performer_names.get(k, "") for k in pk).lower()
                if not all(w in hay for w in words):
                    continue
        out.append(s)
    return sort_scenes(out, spec.sort, st, ix, scenes)


def sort_scenes(scenes: list[Scene], how: str, st: ViewState, ix: _Index | None = None,
                all_scenes: list[Scene] | None = None) -> list[Scene]:
    newest = lambda s: s.posted or s.scene_date or date.min  # noqa: E731
    if how == "date":
        return sorted(scenes, key=lambda s: (s.scene_date or date.min, newest(s)), reverse=True)
    if how == "studio":
        if ix is not None and all_scenes is not None:
            pos = {id(s): i for i, s in enumerate(all_scenes)}
            name = lambda s: ix.display[pos[id(s)]] or "\uffff"  # noqa: E731
        else:
            name = lambda s: (st.display_studio(s) or "\uffff").lower()  # noqa: E731
        by_new = sorted(scenes, key=newest, reverse=True)           # stable: newest first within a studio
        return sorted(by_new, key=name)
    if how == "title":
        return sorted(scenes, key=lambda s: (s.title or s.primary.raw_title).lower())
    if how == "duration":
        return sorted(scenes, key=lambda s: s.duration_s or 0, reverse=True)
    return sorted(scenes, key=newest, reverse=True)                  # "posted" (default)
