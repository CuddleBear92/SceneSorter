"""Download queue.

* Direct HTTP links are downloaded here with resume support.
* Magnet / .torrent links are handed to the user's torrent client (OS default
  handler, or qBittorrent's Web API if configured) — we watch for the finished
  file via the library "inbox" scan instead.

The manager is GUI-agnostic: it reports progress through a callback, which
the Qt layer marshals onto the UI thread.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from .models import DownloadLink, LinkKind, Release


class State(str, Enum):
    QUEUED = "Queued"
    RUNNING = "Downloading"
    HANDED_OFF = "Sent to torrent client"
    BLACKHOLE = "Saved to black hole"
    SORTING = "Sorting"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


@dataclass
class Job:
    release: Release
    link: DownloadLink
    dest_dir: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: State = State.QUEUED
    bytes_done: int = 0
    bytes_total: int | None = None
    speed_bps: float = 0.0
    path: Path | None = None
    final_path: Path | None = None
    error: str = ""
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def progress(self) -> float:
        return (self.bytes_done / self.bytes_total) if self.bytes_total else 0.0


Listener = Callable[[Job], None]


def open_with_system(uri: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(uri)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", uri])
    else:
        subprocess.Popen(["xdg-open", uri])


def filename_from_response(resp: requests.Response, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    if "filename=" in cd:
        name = cd.split("filename=")[-1].strip().strip("\"';")
        if name:
            return os.path.basename(unquote(name))
    return os.path.basename(unquote(urlparse(url).path)) or "download.bin"


class DownloadManager:
    def __init__(self, max_parallel: int = 2, session: requests.Session | None = None,
                 on_finished: Callable[[Job], None] | None = None):
        self.session = session or requests.Session()
        #: If set, .torrent files are saved here (for a torrent client's watch folder)
        self.blackhole_dir: Path | None = None
        #: Also write magnet links as .magnet files into the black hole
        self.magnets_to_blackhole: bool = True
        self.pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="dl")
        self.jobs: dict[str, Job] = {}
        self.listeners: list[Listener] = []
        self.on_finished = on_finished

    def subscribe(self, fn: Listener) -> None:
        self.listeners.append(fn)

    def _emit(self, job: Job) -> None:
        for fn in self.listeners:
            try:
                fn(job)
            except Exception:  # never let a UI callback kill a download thread
                pass

    def enqueue(self, release: Release, link: DownloadLink, dest_dir: Path) -> Job:
        job = Job(release=release, link=link, dest_dir=Path(dest_dir))
        self.jobs[job.id] = job
        self._emit(job)
        if link.kind in (LinkKind.MAGNET, LinkKind.TORRENT, LinkKind.HOSTER):
            self.pool.submit(self._hand_off, job)
        else:
            self.pool.submit(self._run_http, job)
        return job

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job:
            job._cancel.set()

    def shutdown(self) -> None:
        for j in self.jobs.values():
            j._cancel.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ----------------------------------------------------------------- workers
    def _hand_off(self, job: Job) -> None:
        try:
            kind = job.link.kind
            if self.blackhole_dir and (kind is LinkKind.TORRENT or
                                       (kind is LinkKind.MAGNET and self.magnets_to_blackhole)):
                job.path = self._to_blackhole(job)
                job.state = State.BLACKHOLE
            else:
                open_with_system(job.link.url)
                job.state = State.HANDED_OFF
        except Exception as e:  # noqa: BLE001
            job.state, job.error = State.FAILED, str(e)
        self._emit(job)

    def _to_blackhole(self, job: Job) -> Path:
        from .library import safe_component

        folder = Path(self.blackhole_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stem = safe_component(job.release.filename.rsplit(".", 1)[0] if job.release.filename
                              else job.release.raw_title)
        if job.link.kind is LinkKind.MAGNET:
            target = folder / f"{stem}.magnet"
            target.write_text(job.link.url, encoding="utf-8")
            return target
        resp = self.session.get(job.link.url, timeout=60)
        resp.raise_for_status()
        if not resp.content.startswith(b"d"):  # bencoded dict; anything else is an error page
            raise ValueError("Download didn't return a .torrent file")
        target = folder / f"{stem}.torrent"
        tmp = target.with_suffix(".torrent.part")  # write-then-rename so watchers never see half a file
        tmp.write_bytes(resp.content)
        tmp.replace(target)
        return target

    def _run_http(self, job: Job) -> None:
        if job._cancel.is_set():
            job.state = State.CANCELLED
            self._emit(job)
            return
        job.state = State.RUNNING
        self._emit(job)
        try:
            job.dest_dir.mkdir(parents=True, exist_ok=True)
            with self.session.get(job.link.url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                name = filename_from_response(resp, job.link.url)
                final = job.dest_dir / name
                part = final.with_name(final.name + ".part")
                job.path = final
                total = resp.headers.get("content-length")
                job.bytes_total = int(total) if total and total.isdigit() else job.link.size_bytes
                last_emit, window_start, window_bytes = 0.0, time.monotonic(), 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if job._cancel.is_set():
                            raise _Cancelled()
                        fh.write(chunk)
                        job.bytes_done += len(chunk)
                        window_bytes += len(chunk)
                        now = time.monotonic()
                        if now - window_start >= 1.0:
                            job.speed_bps = window_bytes / (now - window_start)
                            window_start, window_bytes = now, 0
                        if now - last_emit >= 0.25:
                            last_emit = now
                            self._emit(job)
                part.replace(final)
            job.speed_bps = 0.0
            if self.on_finished:
                job.state = State.SORTING
                self._emit(job)
                self.on_finished(job)
            job.state = State.DONE
        except _Cancelled:
            job.state = State.CANCELLED
        except Exception as e:  # noqa: BLE001
            job.state, job.error = State.FAILED, str(e)
        self._emit(job)


class _Cancelled(Exception):
    pass
