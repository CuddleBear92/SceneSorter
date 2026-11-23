"""Merge releases from every source and resolution into one Scene per scene."""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from collections.abc import Iterable
from datetime import date

from .models import Release, Scene
from .parsing import VIDEO_EXT_RE, match_key, normalise_studio, parse_title, tokens
from .stashbox import duration_check

FUZZY_THRESHOLD = 0.75
STRICT_THRESHOLD = 0.85


def _body_tokens(r: Release) -> frozenset[str]:
    return frozenset(tokens(" ".join(r.performers) + " " + r.title))


def _duration(group: list[Release]) -> int | None:
    return max((r.duration_s for r in group if r.duration_s), default=None)


def _near(a: set, b: set) -> bool:
    """Dates within a day of each other (timezones / posting around midnight)."""
    return any(abs((x - y).days) <= 1 for x in a for y in b)


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


def titles_match(a: frozenset[str], b: frozenset[str]) -> bool:
    """Title words only (performer lists differ between sites). Used when the
    runtime already agrees, so a looser bar is fine."""
    if not a or not b:
        return False
    inter = len(a & b)
    return inter / len(a | b) >= 0.75 or (min(len(a), len(b)) >= 2 and inter == min(len(a), len(b)))


def release_name_key(r: Release) -> str:
    """Key from the scene-style release name alone (file name, else post title),
    ignoring performers/titles a site supplies separately. '' if the name isn't
    specific enough (needs studio, date and 2+ words)."""
    return _name_key(r.filename or r.raw_title)


@lru_cache(maxsize=200_000)
def _name_key(name: str) -> str:
    if not name or " - " in name:  # only scene-style dotted names are reliable for this
        return ""
    p = parse_title(VIDEO_EXT_RE.sub("", name))
    words = tokens(" ".join(p["performers"]) + " " + p["title"])
    if not (p["studio"] and p["scene_date"] and len(set(words)) >= 2):
        return ""
    return f"{normalise_studio(p['studio'])}|{p['scene_date'].isoformat()}|{' '.join(sorted(set(words)))}"


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
    keys = list(exact.keys())
    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Release-name pass: the same release name (e.g. "SisLovesMe.26.11.21.Luna.
    # Princess.XXX.1080p.HEVC.x265.PRT") on two sites is the same scene, even when
    # one site adds performers or a different title the other doesn't have.
    # (source, date) pairs per group root. Two groups where the *same site* lists *different*
    # dates are different scenes (a site doesn't post one scene twice on different days, but
    # a series like "Cum Compilation" posts many alike-named ones) — never merge those.
    seen_dates: dict[int, dict[str, set]] = {}
    for i, g in enumerate(groups):
        d: dict[str, set] = {}
        for r in g:
            if r.scene_date:
                d.setdefault(r.source, set()).add(r.scene_date)
        seen_dates[i] = d

    def conflict(i: int, j: int) -> bool:
        a, b = seen_dates[find(i)], seen_dates[find(j)]
        for src, ds in a.items():
            other = b.get(src)
            if other and other != ds and not (_near(ds, other)):
                return True
        return False

    def join(i: int, j: int) -> bool:
        ri, rj = find(i), find(j)
        if ri == rj or conflict(ri, rj):
            return False
        parent[rj] = ri
        merged_dates = seen_dates[ri]
        for src, ds in seen_dates.pop(rj).items():
            merged_dates.setdefault(src, set()).update(ds)
        return True

    by_name: dict[str, int] = {}
    for i, g in enumerate(groups):
        for r in g:
            # Plain scene-style post titles already produced exactly this key when
            # grouping; only separate file names need parsing (cached across rebuilds).
            if not r.filename:
                k = keys[i] if (r.scene_date and r.studio and " - " not in r.raw_title) else ""
            else:
                k = release_name_key(r)
            if not k:
                continue
            j = by_name.setdefault(k, i)
            join(j, i)

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
        title_toks = {i: frozenset(t for r in groups[i] for t in tokens(r.title)) for i in idxs}
        by_date: dict[date, list[int]] = defaultdict(list)
        undated: list[int] = []
        for i in idxs:
            (by_date[dates[i]] if dates[i] else undated).append(i)

        def try_union(i: int, j: int, strict: bool) -> bool:
            if find(i) == find(j):
                return False
            dur = duration_check(durs[i], durs[j])
            if dur == "far":
                return False
            if similar(toks[i], toks[j], strict) or (dur == "close" and titles_match(title_toks[i], title_toks[j])):
                return join(i, j)
            return False

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

        # Undated posts: may join undated look-alikes, but a *dated* scene only if exactly one
        # dated scene fits — otherwise a generic title would chain a whole series together.
        for i in undated:
            dated_hits: set[int] = set()
            for j in candidates(i):
                if dates[j] is None:
                    try_union(j, i, strict=False)
                elif len(toks[i]) >= 3 and duration_check(durs[i], durs[j]) != "far" \
                        and similar(toks[i], toks[j], strict=True):
                    dated_hits.add(find(j))
            if len(dated_hits) == 1:
                join(dated_hits.pop(), i)

        # Same scene, sites disagree on the date: needs a specific title (4+ words) and exactly
        # one candidate.
        for i in idxs:
            if not dates[i] or len(toks[i]) < 4:
                continue
            hits = {find(j) for j in candidates(i)
                    if dates[j] and abs((dates[i] - dates[j]).days) > 1 and find(j) != find(i)
                    and same_scene_other_date(toks[i], toks[j], duration_check(durs[i], durs[j]))}
            if len(hits) == 1:
                join(hits.pop(), i)

    merged: dict[int, list[Release]] = defaultdict(list)
    for i, g in enumerate(groups):
        merged[find(i)].extend(g)

    scenes = [Scene(key=match_key(rs[0]), releases=rs) for rs in merged.values()]
    scenes.sort(key=lambda s: (s.posted or s.scene_date or date.min), reverse=True)
    return scenes



# ---------------------------------------------------------------- incremental
# Every merge rule works within one studio (keys and fuzzy passes are per studio), so
# merging each studio on its own gives exactly the same result as merging everything at
# once. That lets a rebuild re-merge only the studios that got new/changed posts, and lets
# the grouping be cached on disk between runs.

#: Bump when merge rules change: cached groupings are then discarded and rebuilt.
MERGE_VERSION = "1"


def studio_key(r: Release) -> str:
    return normalise_studio(r.studio) if r.studio else ""


def _newest(s: Scene) -> date:
    return s.posted or s.scene_date or date.min


def merge_incremental(releases: Iterable[Release], cache: dict[str, list[Scene]] | None,
                      dirty: set[str] | None, progress=None) -> tuple[list[Scene], dict[str, list[Scene]], set[str]]:
    """Merge, reusing ``cache`` (studio key -> scenes) for studios not in ``dirty``.

    ``dirty=None`` or ``cache=None`` means everything is merged. Returns
    (all scenes newest first, new cache, studios that were merged).
    ``progress(done, total)`` is called while merging.
    """
    by_studio: dict[str, list[Release]] = defaultdict(list)
    for r in releases:
        by_studio[studio_key(r)].append(r)
    cache = cache or {}
    todo = [k for k in by_studio if dirty is None or k in dirty or k not in cache]
    todo_set = set(todo)
    new_cache: dict[str, list[Scene]] = {}
    for i, k in enumerate(todo):
        new_cache[k] = merge_releases(by_studio[k])
        if progress and (i % 200 == 0 or i == len(todo) - 1):
            progress(i + 1, len(todo))
    for k in by_studio:
        if k not in todo_set:
            new_cache[k] = cache[k]
    scenes = [s for group in new_cache.values() for s in group]
    scenes.sort(key=_newest, reverse=True)
    return scenes, new_cache, todo_set


def cache_from_groups(releases: Iterable[Release], groups: dict[tuple[str, str], str],
                      stale: set[tuple[str, str]]) -> tuple[dict[str, list[Scene]], set[str]]:
    """Rebuild the per-studio scene cache from a saved grouping (post -> scene key).

    Studios with a post that has no saved group, or whose post changed since it was
    grouped (``stale``), are returned as dirty and left out of the cache."""
    by_studio: dict[str, list[Release]] = defaultdict(list)
    for r in releases:
        by_studio[studio_key(r)].append(r)
    cache: dict[str, list[Scene]] = {}
    dirty: set[str] = set()
    for k, rels in by_studio.items():
        scenes: dict[str, list[Release]] = {}
        for r in rels:
            ident = (r.source, r.post_id)
            g = groups.get(ident)
            if g is None or ident in stale:
                dirty.add(k)
                break
            scenes.setdefault(g, []).append(r)
        else:
            cache[k] = [Scene(key=key, releases=rs) for key, rs in scenes.items()]
    return cache, dirty


def group_rows(cache: dict[str, list[Scene]], studios: Iterable[str]) -> list[tuple[str, str, str, str]]:
    """(source, post_id, scene_key, studio_key) rows to save for the given studios."""
    return [(r.source, r.post_id, s.key, k) for k in studios for s in cache.get(k, []) for r in s.releases]
