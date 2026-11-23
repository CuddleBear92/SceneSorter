"""Merge releases from every source and resolution into one Scene per scene."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from .models import Release, Scene
from .parsing import match_key, normalise_studio, tokens

FUZZY_THRESHOLD = 0.75
STRICT_THRESHOLD = 0.85


def _body_tokens(r: Release) -> frozenset[str]:
    return frozenset(tokens(" ".join(r.performers) + " " + r.title))


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

    # Fuzzy pass, per studio. Same date -> normal threshold. One side undated
    # (messy post titles often omit it) -> stricter threshold.
    by_studio: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        by_studio[normalise_studio(g[0].studio)].append(i)

    for idxs in by_studio.values():
        if len(idxs) < 2:
            continue
        toks = {i: _body_tokens(groups[i][0]) for i in idxs}
        dates = {i: groups[i][0].scene_date for i in idxs}
        by_date: dict[date, list[int]] = defaultdict(list)
        undated: list[int] = []
        for i in idxs:
            (by_date[dates[i]] if dates[i] else undated).append(i)

        def try_union(i: int, j: int, strict: bool) -> None:
            if find(i) != find(j) and similar(toks[i], toks[j], strict):
                parent[find(j)] = find(i)

        for same in list(by_date.values()) + [undated]:
            for x in range(len(same)):
                for y in range(x + 1, len(same)):
                    try_union(same[x], same[y], strict=False)
        for i in undated:
            for same in by_date.values():
                for j in same:
                    try_union(j, i, strict=True)

    merged: dict[int, list[Release]] = defaultdict(list)
    for i, g in enumerate(groups):
        merged[find(i)].extend(g)

    scenes = [Scene(key=match_key(rs[0]), releases=rs) for rs in merged.values()]
    scenes.sort(key=lambda s: (s.posted or s.scene_date or date.min), reverse=True)
    return scenes

