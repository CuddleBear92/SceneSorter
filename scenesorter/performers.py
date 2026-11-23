"""Build the performer list from three places:

1. stash-box scene matches (authoritative names + ids),
2. performer names parsed from post titles ("Studio - Jane Doe, John Roe - Title"),
3. known names spotted inside dotted titles ("Studio.24.01.15.Jane.Doe.Title...")
   — a name found by (1) or (2) anywhere is looked for in every other scene.
   Only multi-word names are spotted this way, to avoid false hits on "Angel".
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import Scene
from .parsing import plausible_performer, tokens


def performer_key(name: str) -> str:
    return "".join(tokens(name))


@dataclass
class PerformerEntry:
    key: str
    name: str
    scenes: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)   # "stashbox", "post", "title"


def collect(scenes: list[Scene], matches: dict[str, list[dict]]) -> tuple[dict[str, PerformerEntry], dict[str, list[str]]]:
    """Returns (performers by key, scene key -> ordered performer keys)."""
    perf: dict[str, PerformerEntry] = {}
    by_scene: dict[str, list[str]] = defaultdict(list)

    def add(name: str, scene_key: str, source: str) -> None:
        k = performer_key(name)
        if not k or len(k) < 3:
            return
        if source != "stashbox" and not plausible_performer(name):  # "Scene 1", "Part 2", ...
            return
        e = perf.get(k)
        if e is None:
            e = perf[k] = PerformerEntry(k, name.strip())
        elif source == "stashbox" and "stashbox" not in e.sources:
            e.name = name.strip()  # prefer the official spelling
        e.scenes.add(scene_key)
        e.sources.add(source)
        if k not in by_scene[scene_key]:
            by_scene[scene_key].append(k)

    for s in scenes:
        for m in matches.get(s.key, []):
            for p in m.get("performers", []):
                add(p["name"], s.key, "stashbox")
        for r in s.releases:
            for name in r.performers:
                add(name, s.key, "post")

    # Spot known multi-word names inside titles (dotted release names have no separators).
    first: dict[str, list[tuple[tuple[str, ...], str]]] = defaultdict(list)
    for k, e in perf.items():
        t = tuple(tokens(e.name))
        if len(t) >= 2:
            first[t[0]].append((t, k))
    if first:
        for s in scenes:
            seq: list[str] = []
            for r in s.releases[:3]:
                seq += tokens(r.title) + ["|"]
            for i, tok in enumerate(seq):
                for t, k in first.get(tok, ()):
                    if tuple(seq[i:i + len(t)]) == t and k not in by_scene[s.key]:
                        perf[k].scenes.add(s.key)
                        perf[k].sources.add("title")
                        by_scene[s.key].append(k)
    return perf, dict(by_scene)
