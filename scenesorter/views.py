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
    have: str = ""
    show_hidden: bool = False
    sort: str = "posted"

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

    def state(self, key: str) -> str:
        return "complete" if key in self.owned else self.status.get(key, "none")

    def display_studio(self, s: Scene) -> str:
        return self.studio_names.get(normalise_studio(s.studio), s.studio) if s.studio else ""

    def is_favorite(self, s: Scene) -> bool:
        return (bool(s.studio) and normalise_studio(s.studio) in self.fav_studios) or \
            any(k in self.fav_performers for k in self.scene_performers.get(s.key, ()))


def haystack(s: Scene, st: ViewState) -> str:
    return " ".join([
        s.studio, st.display_studio(s), s.title,
        *(st.performer_names.get(k, "") for k in st.scene_performers.get(s.key, ())),
        *s.tags, *s.release_groups, *s.codecs,
        *(r.raw_title for r in s.releases), *(r.filename for r in s.releases),
    ]).lower()


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
    # Rebuilt only when the scene list or the name maps are replaced (they're swapped, not
    # mutated). Locked so two panes filtering at once don't both build it.
    sig = (id(scenes), len(scenes), id(st.studio_names), id(st.performer_names), id(st.scene_performers))
    with _lock:
        if _cache.get("sig") != sig:
            _cache["index"] = _Index(scenes, st)
            _cache["sig"] = sig
        return _cache["index"]


def compute_view(scenes: list[Scene], spec: ViewSpec, st: ViewState) -> list[Scene]:
    ix = _index(scenes, st)
    words = spec.text.lower().split()
    out: list[Scene] = []
    for i, s in enumerate(scenes):
        if s.key in st.hidden and not spec.show_hidden:
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
        if words:
            hay = ix.hay[i]
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
