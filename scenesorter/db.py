"""Local SQLite database: cached posts, per-resolution files, download history.

Everything scraped from a source is stored here so the app starts instantly
from cache and a refresh only fetches pages until it reaches posts we already
have — keeping traffic to the sites to a minimum.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

from .models import DownloadLink, LinkKind, Release

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    source          TEXT NOT NULL,
    post_id         TEXT NOT NULL,
    post_url        TEXT,
    raw_title       TEXT NOT NULL,
    studio          TEXT,
    performers      TEXT,           -- JSON list
    title           TEXT,
    scene_date      TEXT,           -- ISO date
    resolution      TEXT,
    release_group   TEXT,
    filename        TEXT,
    duration_s      INTEGER,
    size_bytes      INTEGER,
    tags            TEXT,           -- JSON list
    posted          TEXT,           -- ISO date the post went up
    thumbnail_url   TEXT,
    links           TEXT,           -- JSON list of {url, kind, label, size_bytes, filename}
    details_loaded  INTEGER DEFAULT 0,
    first_seen      REAL,
    last_seen       REAL,
    PRIMARY KEY (source, post_id)
);
CREATE INDEX IF NOT EXISTS posts_group ON posts(release_group);
CREATE INDEX IF NOT EXISTS posts_posted ON posts(posted);

CREATE TABLE IF NOT EXISTS downloads (
    id           TEXT PRIMARY KEY,
    source       TEXT,
    post_id      TEXT,
    resolution   TEXT,
    release_group TEXT,
    link_kind    TEXT,
    filename     TEXT,
    final_path   TEXT,
    state        TEXT,
    error        TEXT,
    started_at   REAL,
    finished_at  REAL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ----------------------------------------------------------------- posts
    def upsert_releases(self, releases: list[Release]) -> int:
        """Insert or update posts. Returns how many were new."""
        now = time.time()
        new = 0
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                for r in releases:
                    exists = self.conn.execute("SELECT details_loaded FROM posts WHERE source=? AND post_id=?",
                                               (r.source, r.post_id)).fetchone()
                    if exists is None:
                        new += 1
                    elif exists["details_loaded"] and not r.details_loaded:
                        # don't clobber richer data from a detail fetch with listing data
                        self.conn.execute("UPDATE posts SET last_seen=?, thumbnail_url=COALESCE(thumbnail_url, ?)"
                                          " WHERE source=? AND post_id=?", (now, r.thumbnail_url, r.source, r.post_id))
                        continue
                    self.conn.execute(
                        """INSERT INTO posts (source, post_id, post_url, raw_title, studio, performers, title,
                               scene_date, resolution, release_group, filename, duration_s, size_bytes, tags,
                               posted, thumbnail_url, links, details_loaded, first_seen, last_seen)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(source, post_id) DO UPDATE SET
                               post_url=excluded.post_url, raw_title=excluded.raw_title, studio=excluded.studio,
                               performers=excluded.performers, title=excluded.title, scene_date=excluded.scene_date,
                               resolution=excluded.resolution, release_group=excluded.release_group,
                               filename=excluded.filename, duration_s=excluded.duration_s,
                               size_bytes=excluded.size_bytes, tags=excluded.tags, posted=excluded.posted,
                               thumbnail_url=excluded.thumbnail_url, links=excluded.links,
                               details_loaded=excluded.details_loaded, last_seen=excluded.last_seen""",
                        (r.source, r.post_id, r.post_url, r.raw_title, r.studio, json.dumps(r.performers), r.title,
                         r.scene_date.isoformat() if r.scene_date else None, r.resolution, r.release_group,
                         r.filename, r.duration_s, r.size_bytes, json.dumps(r.tags),
                         r.posted.isoformat() if r.posted else None, r.thumbnail_url,
                         json.dumps([_link_to_json(l) for l in r.links]), int(r.details_loaded), now, now),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return new

    def known_ids(self, source: str, post_ids: list[str]) -> set[str]:
        if not post_ids:
            return set()
        with self._lock:
            q = f"SELECT post_id FROM posts WHERE source=? AND post_id IN ({','.join('?' * len(post_ids))})"
            return {row["post_id"] for row in self.conn.execute(q, (source, *post_ids))}

    def load_releases(self, sources: list[str] | None = None) -> list[Release]:
        with self._lock:
            if sources:
                rows = self.conn.execute(f"SELECT * FROM posts WHERE source IN ({','.join('?' * len(sources))})",
                                         sources).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM posts").fetchall()
        return [_row_to_release(r) for r in rows]

    def reparse_if_needed(self) -> int:
        """Re-run the title parser on cached posts if the parser has changed."""
        from .parsing import PARSER_VERSION, reparse

        if self.get_meta("parser_version") == PARSER_VERSION:
            return 0
        rels = [reparse(r) for r in self.load_releases()]
        with self._lock:
            self.conn.execute("BEGIN")
            for r in rels:
                self.conn.execute(
                    "UPDATE posts SET studio=?, performers=?, title=?, scene_date=?, resolution=?, release_group=? "
                    "WHERE source=? AND post_id=?",
                    (r.studio, json.dumps(r.performers), r.title, r.scene_date.isoformat() if r.scene_date else None,
                     r.resolution, r.release_group, r.source, r.post_id))
            self.conn.execute("COMMIT")
        self.set_meta("parser_version", PARSER_VERSION)
        return len(rels)

    def count_posts(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    def release_groups(self) -> list[tuple[str, int]]:
        with self._lock:
            rows = self.conn.execute("SELECT release_group, COUNT(*) c FROM posts WHERE release_group != '' "
                                     "GROUP BY release_group ORDER BY c DESC").fetchall()
        return [(r["release_group"], r["c"]) for r in rows]

    # ------------------------------------------------------------- downloads
    def log_download(self, job) -> None:
        r = job.release
        now = time.time()
        with self._lock:
            self.conn.execute(
                """INSERT INTO downloads (id, source, post_id, resolution, release_group, link_kind, filename,
                       final_path, state, error, started_at, finished_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET filename=excluded.filename, final_path=excluded.final_path,
                       state=excluded.state, error=excluded.error, finished_at=excluded.finished_at""",
                (job.id, r.source, r.post_id, r.resolution, r.release_group, job.link.kind.value,
                 job.path.name if job.path else (job.link.filename or r.filename or None),
                 str(job.final_path) if job.final_path else None, job.state.value, job.error or None,
                 now, now if job.state.name in ("DONE", "FAILED", "CANCELLED", "HANDED_OFF") else None),
            )

    def downloaded_posts(self) -> set[tuple[str, str]]:
        with self._lock:
            rows = self.conn.execute("SELECT DISTINCT source, post_id FROM downloads "
                                     "WHERE state IN ('Done', 'Sent to torrent client')").fetchall()
        return {(r["source"], r["post_id"]) for r in rows}


def _link_to_json(l: DownloadLink) -> dict:
    return {"url": l.url, "kind": l.kind.value, "label": l.label, "size_bytes": l.size_bytes, "filename": l.filename}


def _row_to_release(row: sqlite3.Row) -> Release:
    links = [DownloadLink(url=l["url"], kind=LinkKind(l["kind"]), label=l.get("label", ""),
                          size_bytes=l.get("size_bytes"), filename=l.get("filename", ""))
             for l in json.loads(row["links"] or "[]")]
    return Release(
        source=row["source"], post_id=row["post_id"], post_url=row["post_url"] or "",
        raw_title=row["raw_title"], thumbnail_url=row["thumbnail_url"], posted=_d(row["posted"]),
        links=links, size_bytes=row["size_bytes"], studio=row["studio"] or "",
        performers=json.loads(row["performers"] or "[]"), title=row["title"] or "",
        scene_date=_d(row["scene_date"]), resolution=row["resolution"] or "",
        release_group=row["release_group"] or "", filename=row["filename"] or "",
        duration_s=row["duration_s"], tags=json.loads(row["tags"] or "[]"),
        details_loaded=bool(row["details_loaded"]),
    )
