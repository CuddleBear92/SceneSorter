"""EncodedFapFiles source.

Site: https://encodedfapfiles.pages.dev/
Backend: https://encodedfapfiles-backend.liquidxdrive.workers.dev

Login is OTP-based via Telegram (@EncodedFapFilesbot).
The GUI drives the flow (ask for Telegram ID → bot sends code → enter code).
The resulting session token is stored in options["session_token"] and saved.

Direct downloads (temporary signed URLs) require a premium account.
Daily send limits are reported by /api/send-file/status and exposed via
daily_quota / remaining so the main window can show them.
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
BOT = "EncodedFapFilesbot"


class EncodedFapFilesSource(Source):
    display_name = "EncodedFapFiles"
    requires_login = True
    daily_quota = None  # filled from remote status when logged in

    def __init__(self, cfg, session=None):
        super().__init__(cfg, session)
        self.token = (self.options.get("session_token") or "").strip()
        self.per_page = int(self.options.get("per_page", 48))
        self._remote_remaining: int | None = None
        self._remote_limit: int | None = None
        self._remote_used: int | None = None

    def is_logged_in(self) -> bool:
        return bool(self.token)

    def set_token(self, token: str) -> None:
        """Store a new session token (caller should also persist settings)."""
        self.token = token.strip()
        self.options["session_token"] = self.token
        self.cfg.options = self.options

    def logout(self) -> None:
        self.token = ""
        self.options.pop("session_token", None)
        self.cfg.options = self.options
        self._remote_remaining = self._remote_limit = self._remote_used = None

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise AuthRequired("not logged in")
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    # ------------------------------------------------------------------ login
    def request_otp(self, telegram_id: int) -> dict:
        """Start the OTP flow. Returns the JSON status from the backend."""
        resp = self.session.post(
            f"{BACKEND}/api/auth/request-otp",
            json={"telegram_id": telegram_id},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def verify_otp(self, telegram_id: int, otp: str) -> dict:
        """Verify the 6-digit code. On success stores the token and returns the response."""
        resp = self.session.post(
            f"{BACKEND}/api/auth/verify-otp",
            json={"telegram_id": telegram_id, "otp": otp.strip()},
            timeout=20,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("status") == "ok" and data.get("token"):
            self.set_token(data["token"])
            return data
        # Surface a useful error
        raise AuthRequired(data.get("detail") or data.get("message") or f"verify failed ({resp.status_code})")

    def login_status(self) -> dict:
        """Optional: check if login is available / bot username."""
        try:
            resp = self.session.get(f"{BACKEND}/api/auth/login-status", timeout=10)
            return resp.json() if resp.ok else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------ quota
    def refresh_quota(self) -> None:
        """Pull daily send limit from the site and update daily_quota / remaining."""
        if not self.token:
            return
        try:
            resp = self.session.get(f"{BACKEND}/api/send-file/status", headers=self._headers(), timeout=15)
            if resp.status_code in (401, 403):
                return
            resp.raise_for_status()
            data = resp.json()
            limit = data.get("daily_limit")
            remaining = data.get("remaining")
            used = data.get("sends_today")
            if isinstance(limit, int) and limit > 0:
                self.daily_quota = limit
                self._remote_limit = limit
            if isinstance(remaining, int):
                self._remote_remaining = remaining
            if isinstance(used, int):
                self._remote_used = used
        except Exception:
            pass

    @property
    def remote_remaining(self) -> int | None:
        return self._remote_remaining

    # ------------------------------------------------------------------ listing
    def list_page(self, page: int = 1, query: str = "") -> list[Release]:
        if not self.token:
            raise AuthRequired("not logged in — use the login dialog")
        # Refresh quota in the background of the first page
        if page == 1:
            self.refresh_quota()

        params = {
            "page": page,
            "limit": self.per_page,
            "sort": self.options.get("sort", "date_desc"),
        }
        if query:
            params["q"] = query
        for key in ("studio", "performer", "tag", "quality", "upload_from", "upload_to",
                    "release_from", "release_to"):
            val = self.options.get(key)
            if val:
                params[key] = val

        url = f"{BACKEND}/api/files?{urlencode(params)}"
        resp = self.session.get(url, headers=self._headers(), timeout=30)
        if resp.status_code in (401, 403):
            raise AuthRequired("session expired — please log in again")
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
        except Exception:
            pass
        release.details_loaded = True
        return release

    def _to_release(self, f: dict) -> Release:
        cid = f.get("channel_id")
        mid = f.get("message_id")
        post_id = f"{cid}:{mid}" if cid is not None and mid is not None else str(f.get("id") or "")

        thumb = None
        if f.get("hamster_url"):
            thumb = f"{BACKEND}/api/img/{f['hamster_url']}"
        elif f.get("screenshot_file_id"):
            thumb = f"{BACKEND}/screenshot/{f['screenshot_file_id']}"

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
            links=[],
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
