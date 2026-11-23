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

SCHEMA_VERSION = 6

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

-- What the user has downloaded, per post (= per resolution/site).
-- state: see status.py — grabbed / done / failed / verified
CREATE TABLE IF NOT EXISTS flags (
    source     TEXT NOT NULL,
    post_id    TEXT NOT NULL,
    state      TEXT NOT NULL,
    marked_at  REAL,
    path       TEXT,
    PRIMARY KEY (source, post_id)
);

-- Blacklist: hidden scenes (by match key AND by post, so a re-parse or a new
-- resolution of the same scene stays hidden) and hidden studios.
CREATE TABLE IF NOT EXISTS hidden_scenes (key TEXT PRIMARY KEY, name TEXT, hidden_at REAL);
CREATE TABLE IF NOT EXISTS hidden_posts (source TEXT, post_id TEXT, PRIMARY KEY (source, post_id));
CREATE TABLE IF NOT EXISTS hidden_studios (key TEXT PRIMARY KEY, name TEXT, hidden_at REAL);

-- Video files seen on disk (drag & drop, import folders, library) and what they matched.
CREATE TABLE IF NOT EXISTS scanned_files (
    path       TEXT PRIMARY KEY,
    size       INTEGER,
    mtime      REAL,
    scene_key  TEXT,
    source     TEXT,
    post_id    TEXT,
    score      REAL,
    scanned_at REAL
);

-- Scenes matched on stash-box instances (one row per scene key + endpoint;
-- stash_id NULL = looked up, nothing found).
CREATE TABLE IF NOT EXISTS scene_matches (
    key        TEXT NOT NULL,
    endpoint   TEXT NOT NULL,
    stash_id   TEXT,
    title      TEXT,
    date       TEXT,
    studio     TEXT,
    performers TEXT,      -- JSON [{id, name}]
    image_url  TEXT,
    duration   INTEGER,
    score      REAL,
    checked_at REAL,
    PRIMARY KEY (key, endpoint)
);

-- Performer pictures/ids from stash-box, keyed by normalised name.
CREATE TABLE IF NOT EXISTS performers (
    key        TEXT PRIMARY KEY,
    name       TEXT,
    endpoint   TEXT,
    stash_id   TEXT,
    image_url  TEXT,
    image_file TEXT,
    checked_at REAL
);

-- Favourite studios / performers (kind = 'studio' | 'performer', key = normalised name)
CREATE TABLE IF NOT EXISTS favorites (kind TEXT, key TEXT, name TEXT, added_at REAL, PRIMARY KEY (kind, key));

-- Studio metadata/logos looked up on stash-box instances (cached forever).
CREATE TABLE IF NOT EXISTS studios (
    key         TEXT PRIMARY KEY,   -- normalised studio name
    name        TEXT,
    endpoint    TEXT,               -- stash-box that matched (NULL = not found)
    stash_id    TEXT,
    logo_url    TEXT,
    logo_file   TEXT,               -- cached file name under cache/logos
    checked_at  REAL
);
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
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "parsed_fields" not in cols:  # which fields came from title parsing (JSON list), v6
            self.conn.execute("ALTER TABLE posts ADD COLUMN parsed_fields TEXT")
        old = int(self.get_meta("schema_version") or SCHEMA_VERSION)
        if old < 2:
            # carry over downloads started by build 1-2 as flags
            self.conn.execute("INSERT OR IGNORE INTO flags(source, post_id, state, marked_at) "
                              "SELECT source, post_id, 'started', started_at FROM downloads "
                              "WHERE state IN ('Done', 'Sent to torrent client', 'Downloading', 'Sorting')")
        if old < 3:
            # build 3 flag names -> grabbed/done; finished HTTP downloads count as done
            self.conn.execute("UPDATE flags SET state='grabbed' WHERE state='started'")
            self.conn.execute("UPDATE flags SET state='done' WHERE state='manual'")
            self.conn.execute("UPDATE flags SET state='done' WHERE state='grabbed' AND EXISTS ("
                              "SELECT 1 FROM downloads d WHERE d.source=flags.source "
                              "AND d.post_id=flags.post_id AND d.state='Done')")
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
                        # Listing data for a post we fetched in full before: keep the detail-only
                        # fields (links, file name, size, duration, tags), but take the listing's
                        # own studio/performers/title/date when it has them — this also restores
                        # fields that an older parser upgrade wrongly reset.
                        self.conn.execute(
                            """UPDATE posts SET last_seen=?, thumbnail_url=COALESCE(thumbnail_url, ?),
                                   raw_title=COALESCE(NULLIF(?, ''), raw_title),
                                   studio=COALESCE(NULLIF(?, ''), studio),
                                   performers=CASE WHEN ? != '[]' THEN ? ELSE performers END,
                                   title=COALESCE(NULLIF(?, ''), title),
                                   scene_date=COALESCE(?, scene_date),
                                   resolution=COALESCE(NULLIF(?, ''), resolution),
                                   release_group=COALESCE(NULLIF(?, ''), release_group),
                                   parsed_fields=?
                               WHERE source=? AND post_id=?""",
                            (now, r.thumbnail_url, r.raw_title, r.studio, json.dumps(r.performers),
                             json.dumps(r.performers), r.title, r.scene_date.isoformat() if r.scene_date else None,
                             r.resolution, r.release_group, None if r.parsed is None else json.dumps(r.parsed),
                             r.source, r.post_id))
                        continue
                    self.conn.execute(
                        """INSERT INTO posts (source, post_id, post_url, raw_title, studio, performers, title,
                               scene_date, resolution, release_group, filename, duration_s, size_bytes, tags,
                               posted, thumbnail_url, links, details_loaded, first_seen, last_seen, parsed_fields)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(source, post_id) DO UPDATE SET
                               post_url=excluded.post_url, raw_title=excluded.raw_title, studio=excluded.studio,
                               performers=excluded.performers, title=excluded.title, scene_date=excluded.scene_date,
                               resolution=excluded.resolution, release_group=excluded.release_group,
                               filename=excluded.filename, duration_s=excluded.duration_s,
                               size_bytes=excluded.size_bytes, tags=excluded.tags, posted=excluded.posted,
                               thumbnail_url=excluded.thumbnail_url, links=excluded.links,
                               details_loaded=excluded.details_loaded, last_seen=excluded.last_seen,
                               parsed_fields=excluded.parsed_fields""",
                        (r.source, r.post_id, r.post_url, r.raw_title, r.studio, json.dumps(r.performers), r.title,
                         r.scene_date.isoformat() if r.scene_date else None, r.resolution, r.release_group,
                         r.filename, r.duration_s, r.size_bytes, json.dumps(r.tags),
                         r.posted.isoformat() if r.posted else None, r.thumbnail_url,
                         json.dumps([_link_to_json(l) for l in r.links]), int(r.details_loaded), now, now,
                         None if r.parsed is None else json.dumps(r.parsed)),
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

    def reparse_if_needed(self, title_only_sources: set[str] = frozenset()) -> int:
        """Re-run the title parser on cached posts if the parser has changed. Only fields
        that came from title parsing are recomputed (see parsing.reparse)."""
        from .parsing import PARSER_VERSION, reparse

        if self.get_meta("parser_version") == PARSER_VERSION:
            return 0
        rels = [reparse(r, r.source in title_only_sources) for r in self.load_releases()]
        with self._lock:
            self.conn.execute("BEGIN")
            for r in rels:
                self.conn.execute(
                    "UPDATE posts SET studio=?, performers=?, title=?, scene_date=?, resolution=?, release_group=?, "
                    "tags=?, parsed_fields=? WHERE source=? AND post_id=?",
                    (r.studio, json.dumps(r.performers), r.title, r.scene_date.isoformat() if r.scene_date else None,
                     r.resolution, r.release_group, json.dumps(r.tags),
                     None if r.parsed is None else json.dumps(r.parsed), r.source, r.post_id))
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
                 now, now if job.state.name in ("DONE", "FAILED", "CANCELLED", "HANDED_OFF", "BLACKHOLE") else None),
            )

    def mark_interrupted_downloads(self) -> int:
        """HTTP downloads that were running when the app last closed/crashed are marked
        failed (still wanted, shows Retry). Torrent hand-offs are left alone: the torrent
        client keeps downloading them."""
        with self._lock:
            rows = self.conn.execute("SELECT id, source, post_id FROM downloads WHERE link_kind='direct' "
                                     "AND state IN ('Queued', 'Downloading', 'Sorting')").fetchall()
            for r in rows:
                self.conn.execute("UPDATE downloads SET state='Failed', error='Interrupted (app closed)' "
                                  "WHERE id=?", (r["id"],))
                self.conn.execute("UPDATE flags SET state='failed' WHERE source=? AND post_id=? AND state='grabbed'",
                                  (r["source"], r["post_id"]))
        return len(rows)

    # ---------------------------------------------------------------- flags
    def flags(self) -> dict[tuple[str, str], str]:
        with self._lock:
            rows = self.conn.execute("SELECT source, post_id, state FROM flags").fetchall()
        return {(r["source"], r["post_id"]): r["state"] for r in rows}

    def set_flag(self, source: str, post_id: str, state: str, path: str | None = None,
                 force: bool = False) -> None:
        """Record a release's download state.

        Automatic updates never lose progress: 'verified' always stays, and a
        finished ('done') release isn't knocked back to grabbed/failed by a
        later retry. ``force`` (user action) sets the state as given.
        """
        with self._lock:
            self.conn.execute(
                "INSERT INTO flags(source, post_id, state, marked_at, path) VALUES (?,?,?,?,?) "
                "ON CONFLICT(source, post_id) DO UPDATE SET state=CASE "
                "  WHEN ? THEN excluded.state "
                "  WHEN flags.state='verified' THEN 'verified' "
                "  WHEN flags.state='done' AND excluded.state IN ('grabbed','failed') THEN 'done' "
                "  ELSE excluded.state END, "
                "marked_at=excluded.marked_at, path=COALESCE(excluded.path, flags.path)",
                (source, post_id, state, time.time(), path, int(force)))

    # -------------------------------------------------------------- hidden
    def hidden(self) -> tuple[set[str], set[tuple[str, str]], dict[str, str]]:
        """(hidden scene keys, hidden posts, hidden studios key->name)"""
        with self._lock:
            keys = {r[0] for r in self.conn.execute("SELECT key FROM hidden_scenes")}
            posts = {(r[0], r[1]) for r in self.conn.execute("SELECT source, post_id FROM hidden_posts")}
            studios = {r[0]: r[1] for r in self.conn.execute("SELECT key, name FROM hidden_studios")}
        return keys, posts, studios

    def hide_scene(self, key: str, name: str, posts: list[tuple[str, str]], hide: bool = True) -> None:
        with self._lock:
            if hide:
                self.conn.execute("INSERT OR REPLACE INTO hidden_scenes VALUES (?,?,?)", (key, name, time.time()))
                self.conn.executemany("INSERT OR IGNORE INTO hidden_posts VALUES (?,?)", posts)
            else:
                self.conn.execute("DELETE FROM hidden_scenes WHERE key=?", (key,))
                self.conn.executemany("DELETE FROM hidden_posts WHERE source=? AND post_id=?", posts)

    def hide_studio(self, key: str, name: str, hide: bool = True) -> None:
        with self._lock:
            if hide:
                self.conn.execute("INSERT OR REPLACE INTO hidden_studios VALUES (?,?,?)", (key, name, time.time()))
            else:
                self.conn.execute("DELETE FROM hidden_studios WHERE key=?", (key,))

    def clear_flags(self, posts: list[tuple[str, str]]) -> None:
        with self._lock:
            self.conn.executemany("DELETE FROM flags WHERE source=? AND post_id=?", posts)

    # -------------------------------------------------------------- studios
    # -------------------------------------------------------- scanned files
    def scanned(self, matched_only: bool = False) -> dict[str, tuple[int, float]]:
        q = "SELECT path, size, mtime FROM scanned_files" + (" WHERE scene_key IS NOT NULL" if matched_only else "")
        with self._lock:
            return {r["path"]: (r["size"], r["mtime"]) for r in self.conn.execute(q)}

    def save_scanned(self, rows: list[tuple]) -> None:
        """rows: (path, size, mtime, scene_key, source, post_id, score)"""
        now = time.time()
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO scanned_files(path, size, mtime, scene_key, source, post_id, score, scanned_at) "
                "VALUES (?,?,?,?,?,?,?,?)", [(*r, now) for r in rows])

    def unmatched_files(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.conn.execute("SELECT path FROM scanned_files WHERE scene_key IS NULL")]

    # ------------------------------------------------------- scene matches
    def save_scene_match(self, key: str, endpoint: str, m: dict | None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO scene_matches(key, endpoint, stash_id, title, date, studio, performers, "
                "image_url, duration, score, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key, endpoint, *(
                    (m["id"], m["title"], m.get("date"), m.get("studio"), json.dumps(m.get("performers", [])),
                     m.get("image_url"), m.get("duration"), m.get("score")) if m else (None,) * 8), time.time()))

    def scene_matches(self) -> dict[str, list[dict]]:
        """scene key -> matches found (not-found rows excluded)."""
        out: dict[str, list[dict]] = {}
        with self._lock:
            rows = self.conn.execute("SELECT * FROM scene_matches WHERE stash_id IS NOT NULL").fetchall()
        for r in rows:
            d = dict(r)
            d["performers"] = json.loads(d["performers"] or "[]")
            out.setdefault(r["key"], []).append(d)
        return out

    def scene_matches_for(self, key: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM scene_matches WHERE key=? AND stash_id IS NOT NULL",
                                     (key,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["performers"] = json.loads(d["performers"] or "[]")
            out.append(d)
        return out

    def scene_match_checked(self, max_age_s: float) -> set[tuple[str, str]]:
        with self._lock:
            rows = self.conn.execute("SELECT key, endpoint FROM scene_matches WHERE stash_id IS NOT NULL "
                                     "OR checked_at > ?", (time.time() - max_age_s,)).fetchall()
        return {(r[0], r[1]) for r in rows}

    def delete_scene_matches(self, rows: list[tuple[str, str]]) -> None:
        """Forget matches (key, endpoint) so they get looked up again."""
        with self._lock:
            self.conn.executemany("DELETE FROM scene_matches WHERE key=? AND endpoint=?", rows)

    # ------------------------------------------------------------ favorites
    def favorites(self, kind: str) -> dict[str, str]:
        with self._lock:
            return {r[0]: r[1] for r in self.conn.execute("SELECT key, name FROM favorites WHERE kind=?", (kind,))}

    def set_favorite(self, kind: str, key: str, name: str, on: bool) -> None:
        with self._lock:
            if on:
                self.conn.execute("INSERT OR REPLACE INTO favorites VALUES (?,?,?,?)", (kind, key, name, time.time()))
            else:
                self.conn.execute("DELETE FROM favorites WHERE kind=? AND key=?", (kind, key))

    # ----------------------------------------------------------- performers
    def performer(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM performers WHERE key=?", (key,)).fetchone()

    def save_performer(self, key: str, name: str, endpoint: str | None, stash_id: str | None,
                       image_url: str | None, image_file: str | None) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO performers VALUES (?,?,?,?,?,?,?)",
                              (key, name, endpoint, stash_id, image_url, image_file, time.time()))

    def performer_keys_checked(self, max_age_s: float) -> set[str]:
        with self._lock:
            rows = self.conn.execute("SELECT key FROM performers WHERE endpoint IS NOT NULL OR checked_at > ?",
                                     (time.time() - max_age_s,)).fetchall()
        return {r[0] for r in rows}

    def studio_names(self) -> dict[str, str]:
        """Official stash-box spelling for every matched studio (key -> name)."""
        with self._lock:
            rows = self.conn.execute("SELECT key, name FROM studios WHERE endpoint IS NOT NULL").fetchall()
        return {r["key"]: r["name"] for r in rows}

    def studio_keys_checked(self, max_age_s: float) -> set[str]:
        """Studios already looked up (matched, or not found within max_age_s)."""
        with self._lock:
            rows = self.conn.execute("SELECT key FROM studios WHERE endpoint IS NOT NULL OR checked_at > ?",
                                     (time.time() - max_age_s,)).fetchall()
        return {r["key"] for r in rows}

    def studio(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM studios WHERE key=?", (key,)).fetchone()

    def save_studio(self, key: str, name: str, endpoint: str | None, stash_id: str | None,
                    logo_url: str | None, logo_file: str | None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO studios(key, name, endpoint, stash_id, logo_url, logo_file, checked_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name, "
                "endpoint=excluded.endpoint, stash_id=excluded.stash_id, logo_url=excluded.logo_url, "
                "logo_file=excluded.logo_file, checked_at=excluded.checked_at",
                (key, name, endpoint, stash_id, logo_url, logo_file, time.time()))


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
        parsed=json.loads(row["parsed_fields"]) if row["parsed_fields"] else None,
    )
