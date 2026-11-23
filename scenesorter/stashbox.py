"""Minimal stash-box GraphQL client (StashDB, ThePornDB's stash-box, ...).

Only what we need so far: look up a studio by name to get its stash ID and
logo. Scene matching will build on this.

Not every instance has the same API surface: StashDB has ``searchStudio``;
ThePornDB's stash-box endpoint does not, so studio lookups there are skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from . import __version__
from .parsing import normalise_studio


@dataclass
class StashBoxConfig:
    name: str
    endpoint: str               # e.g. https://stashdb.org/graphql
    api_key: str = ""
    enabled: bool = True


@dataclass
class StudioMatch:
    endpoint: str
    stash_id: str
    name: str
    logo_url: str | None


class StashBoxError(Exception):
    pass


class StashBox:
    def __init__(self, cfg: StashBoxConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": f"SceneSorter/{__version__}",
                                     "Content-Type": "application/json"})
        if cfg.api_key:
            self.session.headers["ApiKey"] = cfg.api_key
        self._unsupported: set[str] = set()

    def query(self, q: str, variables: dict | None = None) -> dict:
        resp = self.session.post(self.cfg.endpoint, json={"query": q, "variables": variables or {}}, timeout=30)
        if resp.status_code in (401, 403):
            raise StashBoxError(f"{self.cfg.name}: API key rejected (HTTP {resp.status_code})")
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise StashBoxError("; ".join(e.get("message", "?") for e in data["errors"]))
        return data["data"]

    def supports(self, field: str) -> bool:
        return field not in self._unsupported

    def match_scene(self, scene) -> dict | None:
        """Find this merged scene on the stash-box (verified by studio + date)."""
        if not self.supports("searchScene"):
            return None
        for term in scene_terms(scene)[:2]:
            try:
                data = self.query(f"query($t: String!) {{ searchScene(term: $t, limit: 15) {{ {SCENE_FIELDS} }} }}",
                                  {"t": term[:120]})
            except StashBoxError as e:
                if "Cannot query field" in str(e):
                    self._unsupported.add("searchScene")
                    return None
                raise
            m = pick_scene(scene, [normalise_scene(x) for x in data.get("searchScene") or []])
            if m:
                return m
        return None

    def find_performer(self, name: str) -> dict | None:
        """Exact (normalised) name/alias match -> {id, name, image_url}."""
        if not self.supports("searchPerformer"):
            return None
        try:
            data = self.query("query($t: String!) { searchPerformer(term: $t, limit: 10) "
                              "{ id name aliases images { url width } } }", {"t": name})
        except StashBoxError as e:
            if "Cannot query field" in str(e):
                self._unsupported.add("searchPerformer")
                return None
            raise
        want = normalise_studio(name)  # same squash-and-lowercase normalisation
        for p in data.get("searchPerformer") or []:
            if any(normalise_studio(n) == want for n in [p.get("name", "")] + list(p.get("aliases") or []) if n):
                imgs = sorted(p.get("images") or [], key=lambda i: (i.get("width") or 0), reverse=True)
                return {"id": p["id"], "name": p["name"], "image_url": imgs[0]["url"] if imgs else None}
        return None

    def find_studio(self, name: str) -> StudioMatch | None:
        """Best match for a studio name, or None. Exact (normalised) name/alias match only."""
        if not self.supports("searchStudio"):
            return None
        # Release names often glue words together ("BrazzersExxtra"); try the split form too.
        terms = [name]
        split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
        if split != name:
            terms.append(split)
        for term in terms:
            try:
                data = self.query(
                    "query($t: String!) { searchStudio(term: $t, limit: 10) "
                    "{ id name aliases images { url width height } } }", {"t": term})
            except StashBoxError as e:
                if "Cannot query field" in str(e):
                    self._unsupported.add("searchStudio")
                    return None
                raise
            m = pick_studio(name, data.get("searchStudio") or [], self.cfg.endpoint)
            if m:
                return m
        return None


SCENE_FIELDS = ("id title date release_date duration studio { name parent { name } } "
                "performers { as performer { id name } } images { url width }")


def scene_terms(scene) -> list[str]:
    """Search strings for a merged scene, most specific first."""
    studio = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", scene.studio or "")
    title = scene.title or scene.primary.raw_title
    perf = " ".join(scene.performers)
    terms = []
    if perf and perf.lower() not in title.lower():
        terms.append(f"{perf} {title}")
    terms.append(f"{studio} {title}".strip())
    terms.append(title)
    seen: list[str] = []
    return [t for t in terms if t and not (t in seen or seen.append(t))]


def normalise_scene(raw: dict) -> dict:
    imgs = sorted(raw.get("images") or [], key=lambda i: (i.get("width") or 0), reverse=True)
    st = raw.get("studio") or {}
    return {
        "id": raw["id"], "title": raw.get("title") or "",
        "date": raw.get("date") or raw.get("release_date"),
        "studio": st.get("name") or "", "parent": (st.get("parent") or {}).get("name") or "",
        "performers": [{"id": p["performer"]["id"], "name": p["performer"]["name"]}
                       for p in raw.get("performers") or [] if p.get("performer")],
        "image_url": imgs[0]["url"] if imgs else None, "duration": raw.get("duration"),
    }


def duration_check(ours: int | None, theirs: int | None) -> str:
    """'close', 'far' or '' (unknown / in between).

    Close = within 30 s or 3 %; far = off by more than 3 min AND 15 % (site
    encodes often trim intros/outros, so small gaps are normal)."""
    if not ours or not theirs:
        return ""
    diff, longest = abs(ours - theirs), max(ours, theirs)
    if diff <= max(30, 0.03 * longest):
        return "close"
    if diff > max(180, 0.15 * longest):
        return "far"
    return ""


def pick_scene(scene, candidates: list[dict]) -> dict | None:
    """Accept a stash-box scene only if studio and date agree (±1 day) and the
    words overlap; undated scenes need a strong title match.

    Duration (when both sides know it) is a check too: a clearly different
    length rejects the candidate, a near-identical length lowers the bar for
    the title match and wins ties (e.g. "Part 1" vs "Part 2" on the same day).
    """
    from datetime import date as _date

    from .parsing import tokens

    want_studio = normalise_studio(scene.studio) if scene.studio else ""
    want_words = set(tokens(" ".join(scene.performers) + " " + (scene.title or "")))
    our_duration = scene.duration_s
    best, best_score = None, 0.0
    for c in candidates:
        studios = {normalise_studio(c["studio"]), normalise_studio(c.get("parent", ""))} - {""}
        if want_studio and want_studio not in studios:
            continue
        dur = duration_check(our_duration, c.get("duration"))
        if dur == "far":
            continue
        words = set(tokens(c["title"] + " " + " ".join(p["name"] for p in c["performers"])))
        overlap = len(want_words & words) / len(want_words | words) if want_words and words else 0.0
        contain = len(want_words & words) / len(want_words) if want_words else 0.0
        sim = max(overlap, 0.9 * contain)
        cdate = None
        try:
            cdate = _date.fromisoformat(c["date"][:10]) if c.get("date") else None
        except ValueError:
            pass
        if scene.scene_dates and cdate:
            delta = min(abs((cdate - d).days) for d in scene.scene_dates)
            if delta > 1 or sim < (0.15 if dur == "close" else 0.3):
                continue
            score = 0.6 + 0.4 * sim - 0.05 * delta
        else:
            if sim < (0.5 if dur == "close" else 0.7):
                continue
            score = 0.3 + 0.5 * sim
        if dur == "close":
            score += 0.05
        if score > best_score:
            best, best_score = c, score
    if best:
        best = dict(best, score=round(min(best_score, 1.0), 3),
                    duration_check=duration_check(our_duration, best.get("duration")) or "unknown")
    return best


def pick_studio(name: str, results: list[dict], endpoint: str) -> StudioMatch | None:
    want = normalise_studio(name)
    for s in results:
        names = [s.get("name", "")] + list(s.get("aliases") or [])
        if any(normalise_studio(n) == want for n in names if n):
            imgs = sorted(s.get("images") or [], key=lambda i: (i.get("width") or 0), reverse=True)
            return StudioMatch(endpoint, s["id"], s.get("name", name), imgs[0]["url"] if imgs else None)
    return None


DEFAULT_STASHBOXES = [
    StashBoxConfig("StashDB", "https://stashdb.org/graphql"),
    StashBoxConfig("ThePornDB", "https://theporndb.net/graphql"),
]
