"""EncodedFapFiles source.

Site: https://encodedfapfiles.pages.dev/
Backend: https://encodedfapfiles-backend.liquidxdrive.workers.dev

Login is OTP-based via Telegram (@EncodedFapFilesbot). The user must obtain
a session token (Bearer) and put it in source options as ``session_token``.

How to get the token:
1. Open the site in a browser and complete the Telegram login flow.
2. Open DevTools → Application / Storage → Local Storage → the site.
3. Copy the value of ``fi_session_token``.
4. Paste it into the source's options.session_token in Settings.

Direct downloads (via temporary signed URLs) require a premium account on the
site. Free accounts can only "Send to Telegram". This source resolves a
download URL when load_details() is called.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlencode

from ..models import DownloadLink, LinkKind, Release
from ..parsing import enrich
from .base import AuthRequired, RateLimited, Source

BACKEND = "https://encodedfapfiles-backend.liquidxdrive.workers.dev"
SITE = "https://encodedfapfiles.pages.dev"


class EncodedFapFilesSource(Source):
    display_name = "EncodedFapFiles"
    requires_login = True
    daily_quota = None  # site has its own limits; track if needed later

    def __init__(self, cfg, session=None):
        super().__init__(cfg, session)
        self.token = (self.options.get("session_token") or "").strip()
        self.per_page = int(self.options.get("per_page", 48))

    def is_logged_in(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise AuthRequired("session_token missing in source options")
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def list_page(self, page: int = 1, query: str = "") -> list[Release]:
        params = {
            "page": page,
            "limit": self.per_page,
            "sort": self.options.get("sort", "date_desc"),
        }
        if query:
            params["q"] = query
        # Optional filters from options (studio, performer, tags, quality, dates)
        for key in ("studio", "performer", "tag", "quality", "upload_from", "upload_to",
                    "release_from", "release_to"):
            val = self.options.get(key)
            if val:
                params[key] = val

        url = f"{BACKEND}/api/files?{urlencode(params)}"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        if resp.status_code in (401, 403):
            raise AuthRequired("session expired or invalid — refresh the token")
        if resp.status_code in (429, 503):
            ra = resp.headers.get("Retry-After")
            raise RateLimited(float(ra) if ra and ra.isdigit() else None)
        resp.raise_for_status()
        data = resp.json()

        total = data.get("total") or 0
        if total and not query:
            self.total_pages = max(1, (total + self.per_page - 1) // self.per_page)

        return [self._to_release(f) for f in data.get("files") or []]

    def load_details(self, release: Release) -> Release:
        """Resolve a temporary direct download URL if the account allows it."""
        if not release.post_id or ":" not in release.post_id:
            release.details_loaded = True
            return release
        cid, mid = release.post_id.split(":", 1)
        try:
            resp = self.session.get(
                f"{BACKEND}/api/stream/token/{cid}/{mid}",
                headers=self._headers(),
                timeout=20,
            )
            if resp.status_code == 200:
                d = resp.json()
                url = d.get("download_url") or d.get("url")
                if url:
                    release.links = [DownloadLink(
                        url=url,
                        kind=LinkKind.DIRECT,
                        label="Direct",
                        filename=d.get("file_name") or release.filename,
                        size_bytes=release.size_bytes,
                    )]
            # 402/403 often means premium required — leave links empty
        except Exception:
            pass
        release.details_loaded = True
        return release

    def _to_release(self, f: dict) -> Release:
        cid = f.get("channel_id")
        mid = f.get("message_id")
        post_id = f"{cid}:{mid}" if cid is not None and mid is not None else str(f.get("id") or "")

        # Thumbnail preference: hamster CDN, then screenshot proxy
        thumb = None
        if f.get("hamster_url"):
            thumb = f"{BACKEND}/api/img/{f['hamster_url']}"
        elif f.get("screenshot_file_id"):
            thumb = f"{BACKEND}/screenshot/{f['screenshot_file_id']}"

        # Resolution from height or filename
        height = f.get("height") or 0
        res = ""
        if height >= 2160:
            res = "2160p"
        elif height >= 1080:
            res = "1080p"
        elif height >= 720:
            res = "720p"
        elif height >= 480:
            res = "480p"
        else:
            m = re.search(r"(2160|1080|720|480|576)p", f.get("file_name") or "", re.I)
            if m:
                res = m.group(1).lower() + "p"

        posted = None
        if f.get("file_date"):
            try:
                posted = datetime.fromisoformat(f["file_date"].replace("Z", "+00:00")).date()
            except Exception:
                pass

        scene_date = None
        if f.get("release_date"):
            try:
                scene_date = datetime.fromisoformat(f["release_date"].replace("Z", "+00:00")).date()
            except Exception:
                pass

        performers = [p.get("name") for p in (f.get("performers") or []) if isinstance(p, dict) and p.get("name")]
        tags = [t.get("name") for t in (f.get("tags") or []) if isinstance(t, dict) and t.get("name")]

        r = Release(
            source=self.id,
            post_id=post_id,
            post_url=f"{SITE}/?file={post_id}" if post_id else SITE,
            raw_title=f.get("file_name") or f.get("title") or "",
            thumbnail_url=thumb,
            posted=posted,
            scene_date=scene_date,
            links=[],  # resolved in load_details
            size_bytes=f.get("file_size"),
            filename=f.get("file_name") or "",
            duration_s=f.get("duration"),
            tags=tags,
            studio=f.get("studio_name") or "",
            performers=performers,
            resolution=res,
            details_loaded=False,
        )
        return enrich(r)
