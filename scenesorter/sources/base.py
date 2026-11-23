"""Source plugin interface.

A Source knows how to list posts on one site and turn them into Releases.
Each site gets its own subclass; the rest of the app (merge, GUI, download,
library) only ever talks to this interface.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass

import requests

from .. import __version__
from ..models import Release


@dataclass
class SourceConfig:
    id: str
    type: str
    base_url: str
    enabled: bool = True
    # Free-form per-source options (selectors, API paths, ...)
    options: dict | None = None


class AuthRequired(Exception):
    """Raised when a source needs the user to (re)log in."""


class RateLimited(Exception):
    """The site asked us to slow down (HTTP 429/503). ``retry_after`` in seconds."""

    def __init__(self, retry_after: float | None = None):
        super().__init__(f"rate limited, retry after {retry_after or '?'}s")
        self.retry_after = retry_after


class Source(abc.ABC):
    #: Shown in the UI (badge on each scene card)
    display_name: str = "Source"
    #: True if this source needs a login (Settings shows a login button)
    requires_login: bool = False
    #: Daily action limit; None = unlimited. Each started download is one action.
    daily_quota: int | None = None
    #: Total listing pages, if the site reports it (set by list_page).
    total_pages: int | None = None

    def __init__(self, cfg: SourceConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.id = cfg.id
        self.options = cfg.options or {}
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", f"SceneSorter/{__version__}")

    @abc.abstractmethod
    def list_page(self, page: int = 1, query: str = "") -> list[Release]:
        """Return releases on listing page *page* (1-based), newest first."""

    def load_details(self, release: Release) -> Release:
        """Fetch the post page and fill ``release.links`` etc. Default: no-op."""
        release.details_loaded = True
        return release

    # Auth hooks — override in sources that need login.
    def is_logged_in(self) -> bool:
        return not self.requires_login

    def logout(self) -> None:
        pass
