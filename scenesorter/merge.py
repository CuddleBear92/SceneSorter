"""Merge releases from every source and resolution into one Scene per scene."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from .models import Release, Scene
from .parsing import match_key, normalise_studio, tokens

FUZZY_THRESHOLD = 0.75


def _body_tokens(r: Release) -> frozenset[str]:
    return frozenset(tokens(" ".join(r.performers) + " " + r.title))


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    if inter / len(a | b) >= FUZZY_THRESHOLD:
        return True
    # One site often appends/omits a performer or tag: subset with enough overlap.
    small = min(len(a), len(b))
    return small >= 3 and inter == small


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

    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        r = g[0]
        buckets[(normalise_studio(r.studio), r.scene_date.isoformat() if r.scene_date else "")].append(i)

    for idxs in buckets.values():
        if len(idxs) < 2:
            continue
        toks = {i: _body_tokens(groups[i][0]) for i in idxs}
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]
                if find(i) != find(j) and similar(toks[i], toks[j]):
                    parent[find(j)] = find(i)

    merged: dict[int, list[Release]] = defaultdict(list)
    for i, g in enumerate(groups):
        merged[find(i)].extend(g)

    scenes = [Scene(key=match_key(rs[0]), releases=rs) for rs in merged.values()]
    scenes.sort(key=lambda s: (s.posted or s.scene_date or date.min), reverse=True)
    return scenes

