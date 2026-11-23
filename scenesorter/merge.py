"""Merge releases from every source and resolution into one Scene per scene."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from .models import Release, Scene
from .parsing import match_key, normalise_studio, tokens
from .stashbox import duration_check

FUZZY_THRESHOLD = 0.75
STRICT_THRESHOLD = 0.85


def _body_tokens(r: Release) -> frozenset[str]:
    return frozenset(tokens(" ".join(r.performers) + " " + r.title))


def _duration(group: list[Release]) -> int | None:
    return max((r.duration_s for r in group if r.duration_s), default=None)


def same_scene_other_date(a: frozenset[str], b: frozenset[str], dur: str) -> bool:
    """Same studio, but the sites list different dates (one uses the shoot date,
    the other the release date, or a re-post). Only merge on strong evidence:

    * runtimes agree (``dur == 'close'``) and the words (nearly) match — one side
      may add performer names the other has only in the title;
    * no runtime to compare: the word sets must be identical and long enough
      not to be a generic title ("Hot Sex").
    """
    if dur == "far" or not a or not b:
        return False
    inter = len(a & b)
    jaccard = inter / len(a | b)
    small = min(len(a), len(b))
    if dur == "close":
        return jaccard >= 0.8 or (small >= 4 and inter == small)
    return a == b and len(a) >= 6


def similar(a: frozenset[str], b: frozenset[str], strict: bool = False) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    if inter / len(a | b) >= (STRICT_THRESHOLD if strict else FUZZY_THRESHOLD):
        return True
    # One site often appends/omits a performer or tag: subset with enough overlap.
    small = min(len(a), len(b))
    return small >= (4 if strict else 3) and inter == small


def merge_releases(releases: Iterable[Release]) -> list[Scene]:
    """Group releases into scenes.

    1. Exact key match (studio + date + sorted title tokens).
    2. Fuzzy match between groups with the same studio and date (or same
       studio and both undated), using token Jaccard / subset similarity.
    """
    exact: dict[str, list[Release]] = defaultdict(list)
    seen_ids: set[tuple[str, str]] = set()
    for r in releases:
        ident = (r.source, r.post_id)
        if ident in seen_ids:
            continue
        seen_ids.add(ident)
        exact[match_key(r)].append(r)

    groups = list(exact.values())
    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Fuzzy pass, per studio:
    #   same date        -> normal word threshold
    #   one side undated -> stricter threshold (messy titles often omit the date)
    #   different dates  -> only with matching runtime or identical long titles
    # A clearly different runtime (see stashbox.duration_check) never merges.
    by_studio: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        by_studio[normalise_studio(g[0].studio)].append(i)

    for idxs in by_studio.values():
        if len(idxs) < 2:
            continue
        toks = {i: _body_tokens(groups[i][0]) for i in idxs}
        dates = {i: groups[i][0].scene_date for i in idxs}
        durs = {i: _duration(groups[i]) for i in idxs}
        by_date: dict[date, list[int]] = defaultdict(list)
        undated: list[int] = []
        for i in idxs:
            (by_date[dates[i]] if dates[i] else undated).append(i)

        def union(i: int, j: int) -> None:
            parent[find(j)] = find(i)

        def try_union(i: int, j: int, strict: bool) -> None:
            if find(i) != find(j) and duration_check(durs[i], durs[j]) != "far" \
                    and similar(toks[i], toks[j], strict):
                union(i, j)

        # Same date: buckets are tiny, compare all pairs.
        for same in by_date.values():
            for x in range(len(same)):
                for y in range(x + 1, len(same)):
                    try_union(same[x], same[y], strict=False)

        # Everything else only compares against groups sharing one of its rarer
        # words (keeps this near-linear for studios with thousands of posts).
        index: dict[str, list[int]] = defaultdict(list)
        for i in idxs:
            for t in toks[i]:
                index[t].append(i)

        def candidates(i: int) -> set[int]:
            rare = sorted(toks[i], key=lambda t: len(index[t]))[:2]
            return {j for t in rare for j in index[t] if j != i}

        for i in undated:
            for j in candidates(i):
                try_union(j, i, strict=dates[j] is not None)

        for i in idxs:
            if not dates[i]:
                continue
            for j in candidates(i):
                if j < i or not dates[j] or abs((dates[i] - dates[j]).days) <= 1 or find(i) == find(j):
                    continue
                if same_scene_other_date(toks[i], toks[j], duration_check(durs[i], durs[j])):
                    union(i, j)

    merged: dict[int, list[Release]] = defaultdict(list)
    for i, g in enumerate(groups):
        merged[find(i)].extend(g)

    scenes = [Scene(key=match_key(rs[0]), releases=rs) for rs in merged.values()]
    scenes.sort(key=lambda s: (s.posted or s.scene_date or date.min), reverse=True)
    return scenes

