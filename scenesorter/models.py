"""Core data models: a *Release* is one post on one site; a *Scene* is the merged view."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class LinkKind(str, Enum):
    DIRECT = "direct"      # plain HTTP(S) file download
    MAGNET = "magnet"      # magnet URI, handed to the torrent client
    TORRENT = "torrent"    # .torrent file URL, handed to the torrent client
    HOSTER = "hoster"      # file-host page that needs a browser / resolver


@dataclass
class DownloadLink:
    url: str
    kind: LinkKind = LinkKind.DIRECT
    label: str = ""
    size_bytes: int | None = None


@dataclass
class Release:
    """A single post on a single source, for a single resolution."""

    source: str                      # source id, e.g. "blog" / "premium"
    post_id: str                     # unique within the source
    post_url: str
    raw_title: str
    thumbnail_url: str | None = None
    posted: date | None = None
    links: list[DownloadLink] = field(default_factory=list)
    size_bytes: int | None = None
    # Filled by parsing.parse_title() if not supplied by the source:
    studio: str = ""
    performers: list[str] = field(default_factory=list)
    title: str = ""
    scene_date: date | None = None
    resolution: str = ""             # "2160p", "1080p", "720p", ...
    details_loaded: bool = False

    @property
    def resolution_rank(self) -> int:
        try:
            return int(self.resolution.rstrip("p"))
        except ValueError:
            return 0


@dataclass
class Scene:
    """One logical scene, merged across sources and resolutions."""

    key: str
    releases: list[Release] = field(default_factory=list)

    # Display fields are taken from the "best" release.
    @property
    def primary(self) -> Release:
        return max(self.releases, key=_display_score)

    @property
    def studio(self) -> str:
        return self.primary.studio

    @property
    def title(self) -> str:
        return self.primary.title

    @property
    def performers(self) -> list[str]:
        return self.primary.performers

    @property
    def scene_date(self) -> date | None:
        return next((r.scene_date for r in self.releases if r.scene_date), None)

    @property
    def posted(self) -> date | None:
        dates = [r.posted for r in self.releases if r.posted]
        return max(dates) if dates else None

    @property
    def thumbnail_urls(self) -> list[str]:
        """All known thumbnails, best first, so the UI can fall back between sites."""
        seen: list[str] = []
        for r in sorted(self.releases, key=_display_score, reverse=True):
            if r.thumbnail_url and r.thumbnail_url not in seen:
                seen.append(r.thumbnail_url)
        return seen

    @property
    def resolutions(self) -> list[str]:
        res = {r.resolution for r in self.releases if r.resolution}
        return sorted(res, key=lambda s: int(s.rstrip("p") or 0), reverse=True)

    @property
    def sources(self) -> list[str]:
        return sorted({r.source for r in self.releases})

    def releases_for(self, resolution: str) -> list[Release]:
        return [r for r in self.releases if r.resolution == resolution]


def _display_score(r: Release) -> tuple:
    # Prefer releases with thumbnails, parsed metadata, then higher resolution.
    return (bool(r.thumbnail_url), bool(r.studio), bool(r.performers), r.resolution_rank)
