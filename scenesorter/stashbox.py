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
