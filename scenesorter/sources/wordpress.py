"""Generic WordPress source using the public REST API (/wp-json/wp/v2/posts).

Works for any WordPress blog that exposes the REST API. Download links are
extracted from the rendered post HTML (magnet:, .torrent, and direct file
URLs); thumbnails come from the featured image or the first <img>.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import urljoin

from ..models import DownloadLink, LinkKind, Release
from ..parsing import enrich, extract_post_metadata
from .base import RateLimited, Source

IMG_RE = re.compile(r"<img[^>]+?(?:data-src|data-lazy-src|src)=[\"']([^\"']+)[\"']", re.I)
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
VIDEO_EXT = (".mp4", ".mkv", ".wmv", ".avi", ".mov", ".m4v")


def html_to_text(content_html: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", content_html)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"[ \t]+", " ", t)


def extract_links(content_html: str, base: str) -> list[DownloadLink]:
    links: list[DownloadLink] = []
    seen: set[str] = set()
    for href in HREF_RE.findall(content_html):
        url = html.unescape(href)
        if url in seen:
            continue
        seen.add(url)
        low = url.lower()
        if low.startswith("magnet:"):
            links.append(DownloadLink(url, LinkKind.MAGNET, "Magnet"))
        elif low.split("?")[0].endswith(".torrent"):
            links.append(DownloadLink(urljoin(base, url), LinkKind.TORRENT, "Torrent"))
        elif low.split("?")[0].endswith(VIDEO_EXT):
            links.append(DownloadLink(urljoin(base, url), LinkKind.DIRECT, "Direct"))
    return links


class WordPressSource(Source):
    display_name = "WordPress blog"

    def __init__(self, cfg, session=None):
        super().__init__(cfg, session)
        self.display_name = self.options.get("display_name", cfg.id)
        self.per_page = int(self.options.get("per_page", 30))

    def _api(self, path: str) -> str:
        return urljoin(self.cfg.base_url.rstrip("/") + "/", "wp-json/wp/v2/" + path)

    def list_page(self, page: int = 1, query: str = "") -> list[Release]:
        params = {"page": page, "per_page": self.per_page, "_embed": "wp:featuredmedia,wp:term"}
        if query:
            params["search"] = query
        resp = self.session.get(self._api("posts"), params=params, timeout=30)
        if resp.status_code == 400 and page > 1:
            return []  # past the last page
        if resp.status_code in (429, 503):
            ra = resp.headers.get("Retry-After", "")
            raise RateLimited(float(ra) if ra.isdigit() else None)
        resp.raise_for_status()
        tp = resp.headers.get("X-WP-TotalPages", "")
        if tp.isdigit() and not query:
            self.total_pages = int(tp)
        return [self._to_release(p) for p in resp.json()]

    def _to_release(self, p: dict) -> Release:
        content = p.get("content", {}).get("rendered", "")
        thumb = None
        media = (p.get("_embedded") or {}).get("wp:featuredmedia") or []
        if media and isinstance(media[0], dict):
            thumb = media[0].get("source_url")
        if not thumb:
            m = IMG_RE.search(content)
            thumb = html.unescape(m.group(1)) if m else None
        posted = None
        if p.get("date"):
            try:
                posted = datetime.fromisoformat(p["date"]).date()
            except ValueError:
                pass
        embedded = p.get("_embedded") or {}
        tags = [t.get("name") for grp in embedded.get("wp:term") or [] for t in grp
                if isinstance(t, dict) and t.get("taxonomy") in ("post_tag", "category") and t.get("name")]
        meta = extract_post_metadata(html_to_text(content))
        tags += [t for t in meta.get("tags", []) if t not in tags]
        r = Release(
            source=self.id,
            post_id=str(p.get("id")),
            post_url=p.get("link", ""),
            raw_title=html.unescape(re.sub(r"<[^>]+>", "", p.get("title", {}).get("rendered", ""))),
            thumbnail_url=thumb,
            posted=posted,
            links=extract_links(content, self.cfg.base_url),
            filename=meta.get("filename", ""),
            duration_s=meta.get("duration_s"),
            size_bytes=meta.get("size_bytes"),
            tags=[html.unescape(t) for t in tags if t.lower() != "uncategorized"],
            release_group=meta.get("release_group", ""),
            details_loaded=True,
        )
        return enrich(r)
