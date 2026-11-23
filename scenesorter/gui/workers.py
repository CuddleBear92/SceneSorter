"""Thread-pool helpers and async thumbnail loading."""
from __future__ import annotations

import hashlib
import traceback
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Run fn(*args) on the global pool; deliver result/exception on the UI thread."""

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _Signals()

    def run(self) -> None:
        try:
            res = self.fn(*self.args, **self.kwargs)
        except Exception as e:  # noqa: BLE001
            self.signals.failed.emit(f"{e}\n{traceback.format_exc(limit=3)}")
        else:
            self.signals.done.emit(res)


_live: set[Task] = set()  # keep Python wrappers alive until the task reports back


def run_async(fn: Callable, *args, on_done=None, on_fail=None, **kwargs) -> Task:
    t = Task(fn, *args, **kwargs)
    t.setAutoDelete(False)
    _live.add(t)
    t.signals.done.connect(lambda _r: _live.discard(t))
    t.signals.failed.connect(lambda _e: _live.discard(t))
    if on_done:
        t.signals.done.connect(on_done)
    if on_fail:
        t.signals.failed.connect(on_fail)
    QThreadPool.globalInstance().start(t)
    return t


class ThumbnailLoader(QObject):
    """Loads thumbnails with a disk cache; tries each URL in turn (site fallback)."""

    loaded = Signal(str, QPixmap)  # scene key, pixmap

    def __init__(self, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.cache_dir = Path(cache_dir) / "thumbs"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.nam = QNetworkAccessManager(self)
        from .images import PixmapLRU

        # ~0.9 MB per 640x360 pixmap -> ~270 MB at most, however far you scroll
        self.mem = PixmapLRU(300)
        self.pending: set[str] = set()
        self.failed: set[str] = set()              # URLs that didn't load this session (don't hammer them)
        self.host_headers: dict[str, dict[str, str]] = {}  # e.g. stash-box image hosts need the ApiKey

    def set_host_headers(self, headers: dict[str, dict[str, str]]) -> None:
        self.host_headers = headers

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".img")

    def get(self, key: str, urls: list[str]) -> QPixmap | None:
        from .images import SCENE_SIZE, load_file, touch

        if key in self.mem:
            return self.mem.get(key)
        urls = [u for u in urls if u not in self.failed]
        if key in self.pending or not urls:
            return None
        for url in urls:
            p = self._cache_path(url)
            if p.exists():
                pm = load_file(p, SCENE_SIZE)
                if pm is not None:
                    touch(p)
                    self.mem.put(key, pm)
                    return pm
        self.pending.add(key)
        self._fetch(key, list(urls))
        return None

    def _fetch(self, key: str, urls: list[str]) -> None:
        if not urls:
            self.pending.discard(key)
            return
        url = urls.pop(0)
        req = QNetworkRequest(QUrl(url))
        req.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        req.setRawHeader(b"User-Agent", b"Mozilla/5.0 SceneSorter")
        for name, value in self.host_headers.get(QUrl(url).host().lower(), {}).items():
            req.setRawHeader(name.encode(), value.encode())
        reply = self.nam.get(req)
        reply.finished.connect(lambda: self._on_reply(reply, key, url, urls))

    def _on_reply(self, reply: QNetworkReply, key: str, url: str, rest: list[str]) -> None:
        data = bytes(reply.readAll())
        ok = reply.error() == QNetworkReply.NetworkError.NoError
        reply.deleteLater()
        from .images import SCENE_SIZE, save_and_load

        pm = save_and_load(self._cache_path(url), data, SCENE_SIZE) if ok and data else None
        if pm is not None:
            self.mem.put(key, pm)
            self.pending.discard(key)
            self.loaded.emit(key, pm)
        else:
            self.failed.add(url)
            self._fetch(key, rest)  # fall back to the next image (other site / stash-box cover)


class BackgroundQueue(QObject):
    """A slow, polite work queue: one job every ``interval_ms`` on the thread pool.

    Items on screen go to the front (``push(..., front=True)``); prefetch work
    goes to the back. ``worker(key, payload)`` runs off the UI thread and its
    return value is delivered to ``on_result(key, result)`` on the UI thread.
    """

    def __init__(self, interval_ms: int, worker: Callable, on_result: Callable, parent=None):
        super().__init__(parent)
        from collections import deque

        self.worker, self.on_result = worker, on_result
        self.queue: deque = deque()
        self.queued: set = set()
        self.busy = False
        self.enabled = True
        self.timer = QTimer(self, interval=interval_ms, timeout=self._next)
        self.timer.start()

    def set_interval(self, ms: int) -> None:
        self.timer.setInterval(ms)

    def push(self, key, payload, front: bool = False) -> None:
        if key in self.queued:
            if not front:
                return
            for i, (k, _p) in enumerate(self.queue):
                if k == key:
                    del self.queue[i]
                    break
            else:
                return  # currently running
        self.queued.add(key)
        (self.queue.appendleft if front else self.queue.append)((key, payload))

    def clear(self) -> None:
        self.queue.clear()
        self.queued.clear()

    def pending(self) -> int:
        return len(self.queue) + (1 if self.busy else 0)

    def _next(self) -> None:
        if self.busy or not self.queue or not self.enabled:
            return
        key, payload = self.queue.popleft()
        self.busy = True

        def done(res, key=key):
            self.busy = False
            self.queued.discard(key)
            self.on_result(key, res)

        def failed(_err, key=key):
            self.busy = False
            self.queued.discard(key)  # transient error: it will be queued again when needed

        run_async(self.worker, key, payload, on_done=done, on_fail=failed)
