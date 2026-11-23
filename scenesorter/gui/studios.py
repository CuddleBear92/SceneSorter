"""Studios tab: one card per studio with its logo from stash-box."""
from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel, QByteArray, QModelIndex, QObject, QRect, QSize, Qt,
    QTimer, Signal,
)
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListView, QMenu, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from ..db import Database
from ..models import Scene
from ..parsing import normalise_studio
from ..stashbox import StashBox, StashBoxConfig
from .images import atomic_write, ext_for, LOGO_SIZE, PixmapLRU, load_file, shrink_for_disk
from .workers import run_async

RETRY_NOT_FOUND_S = 30 * 24 * 3600
LOOKUP_INTERVAL_MS = 1500          # one stash-box lookup every 1.5 s — gentle on the API
CARD_W, LOGO_H, TEXT_H = 220, 110, 46
StudioRole = Qt.ItemDataRole.UserRole + 2


@dataclass
class StudioEntry:
    key: str
    name: str
    scenes: int
    have: int


def render_image(data: bytes, max_w: int = 440, max_h: int = 220) -> QPixmap | None:
    """Load PNG/JPEG/WebP or SVG bytes at (at most) max_w x max_h — never full size."""
    from .images import decode_scaled

    img = decode_scaled(data, QSize(max_w, max_h))
    return QPixmap.fromImage(img) if img is not None else None


class LogoService(QObject):
    """Looks studios up on stash-box (slowly, only when shown) and caches logos to disk."""

    ready = Signal(str)  # studio key

    def __init__(self, db: Database, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.db = db
        self.dir = Path(cache_dir) / "logos"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.boxes: list[StashBox] = []
        self.mem = PixmapLRU(300)  # ~0.4 MB per logo
        self.queue: deque[tuple[str, str]] = deque()
        self.queued: set[str] = set()
        self.busy = False
        self.timer = QTimer(self, interval=LOOKUP_INTERVAL_MS, timeout=self._next)
        self.timer.start()

    def configure(self, configs: list[StashBoxConfig]) -> None:
        self.boxes = [StashBox(c) for c in configs if c.enabled and c.api_key]

    def prefetch(self, studios: list[tuple[str, str]]) -> None:
        """Queue every studio we know nothing about yet, behind whatever is on screen.
        Runs in the background at the same gentle pace (one lookup per 1.5 s)."""
        if not self.boxes:
            return
        known = self.db.studio_keys_checked(RETRY_NOT_FOUND_S)
        # Logos we know about but whose file is missing get downloaded again (once per session
        # this scans the folder; files lost to an older build's cache pruning come back).
        if not getattr(self, "_repair_scanned", False):
            self._repair_scanned = True
            known -= {k for k, f in self.db.studio_logo_files().items() if not (self.dir / f).exists()}
        for key, name in studios:
            if key not in known and key not in self.queued:
                self.queued.add(key)
                self.queue.append((key, name))

    def pending(self) -> int:
        return len(self.queue) + (1 if self.busy else 0)

    def get(self, key: str, name: str) -> QPixmap | None:
        if key in self.mem:
            return self.mem.get(key)
        row = self.db.studio(key)
        if row is not None:
            if row["logo_file"] and (self.dir / row["logo_file"]).exists():
                pm = load_file(self.dir / row["logo_file"], LOGO_SIZE)
                self.mem.put(key, pm)
                return pm
            if row["logo_file"] and row["logo_url"]:
                pass  # known logo whose file is gone (e.g. pruned by an older build): fetch it again
            elif time.time() - (row["checked_at"] or 0) < RETRY_NOT_FOUND_S:
                self.mem.put(key, None)
                return None
        if self.boxes:
            if key in self.queued:  # already waiting (e.g. prefetch) — bump it to the front
                try:
                    self.queue.remove((key, name))
                except ValueError:
                    return None
            self.queued.add(key)
            self.queue.appendleft((key, name))  # most recently shown first
        return None

    def _next(self) -> None:
        if self.busy or not self.queue or not self.boxes:
            return
        key, name = self.queue.popleft()
        self.busy = True
        run_async(self._lookup, key, name, on_done=self._done, on_fail=lambda _e: self._failed(key))

    def _lookup(self, key: str, name: str):
        row = self.db.studio(key)
        if row is not None and row["logo_url"] and row["endpoint"]:
            # Already matched: just download the logo again (no stash-box search needed).
            box = next((b for b in self.boxes if b.cfg.endpoint == row["endpoint"]), self.boxes[0])
            resp = box.session.get(row["logo_url"], timeout=30)
            fname = None
            if resp.ok and resp.content:
                out = shrink_for_disk(resp.content, LOGO_SIZE)
                fname = hashlib.sha1(row["logo_url"].encode()).hexdigest() + ext_for(out)
                atomic_write(self.dir / fname, out)
            return key, row["name"], row["endpoint"], row["stash_id"], row["logo_url"], fname
        for box in self.boxes:
            m = box.find_studio(name)
            if not m:
                continue
            fname = None
            if m.logo_url:
                resp = box.session.get(m.logo_url, timeout=30)  # same session -> same ApiKey header
                if resp.ok and resp.content:
                    out = shrink_for_disk(resp.content, LOGO_SIZE)
                    fname = hashlib.sha1(m.logo_url.encode()).hexdigest() + ext_for(out)
                    atomic_write(self.dir / fname, out)
            return key, m.name, m.endpoint, m.stash_id, m.logo_url, fname
        return key, name, None, None, None, None

    def _done(self, res) -> None:
        self.busy = False
        key, name, endpoint, stash_id, logo_url, fname = res
        self.db.save_studio(key, name, endpoint, stash_id, logo_url, fname)
        self.queued.discard(key)
        self.mem.pop(key, None)
        self.ready.emit(key)

    def _failed(self, key: str) -> None:
        self.busy = False
        self.queued.discard(key)  # network hiccup: try again next time it's shown


class FilteredList:
    """Mixin for the Studios/Performers list models: filter + sort a plain Python
    list in one pass (no QSortFilterProxyModel calling back into Python thousands
    of times, which stalls the UI while worker threads hold the GIL).

    Keeps the old proxy API: ``set(...)``, ``sort(0)``, ``invalidate()``.
    """

    text = ""
    sort_mode = "name"
    fav_only = False

    def set_items(self, items: list) -> None:
        self.all_items = items
        self.invalidate()

    def set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidate()

    def sort(self, *_a) -> None:
        self.invalidate()

    def invalidate(self) -> None:
        favs = self.favorites
        text = self.text.lower()
        items = [e for e in getattr(self, "all_items", [])
                 if (not self.fav_only or e.key in favs) and (not text or text in e.name.lower())]
        if self.sort_mode == "scenes":
            key = lambda e: (e.key not in favs, -e.scenes, e.name.lower())  # noqa: E731
        elif self.sort_mode == "have":
            key = lambda e: (e.key not in favs, -e.have, e.name.lower())  # noqa: E731
        else:
            key = lambda e: (e.key not in favs, e.name.lower())  # noqa: E731
        self.beginResetModel()
        self.items = sorted(items, key=key)
        self._row = {e.key: i for i, e in enumerate(self.items)}
        self.endResetModel()


def aggregate_studios(scenes: list[Scene], has, official: dict[str, str]) -> list[StudioEntry]:
    """Plain Python (runs on a worker thread): one entry per studio with counts,
    named with the official stash-box spelling when known."""
    agg: dict[str, StudioEntry] = {}
    for s in scenes:
        if not s.studio:
            continue
        k = normalise_studio(s.studio)
        e = agg.get(k)
        if e is None:
            e = agg[k] = StudioEntry(k, official.get(k, s.studio), 0, 0)
        e.scenes += 1
        if has(s.key):
            e.have += 1
    return sorted(agg.values(), key=lambda e: e.name.lower())


class StudioModel(FilteredList, QAbstractListModel):
    def __init__(self, logos: LogoService, parent=None):
        super().__init__(parent)
        self.logos = logos
        self.hidden: set[str] = set()
        self.favorites: set[str] = set()
        self.items: list[StudioEntry] = []
        self._row: dict[str, int] = {}
        logos.ready.connect(self._logo_ready)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.items[index.row()]
        if role == StudioRole:
            return e
        if role == Qt.ItemDataRole.DisplayRole:
            return e.name
        if role == Qt.ItemDataRole.DecorationRole:
            return self.logos.get(e.key, e.name)
        return None

    def _logo_ready(self, key: str) -> None:
        row = self._row.get(key)
        if row is not None:
            idx = self.index(row)
            row_db = self.logos.db.studio(key)
            if row_db is not None and row_db["endpoint"]:
                self.items[row] = replace(self.items[row], name=row_db["name"])
            self.dataChanged.emit(idx, idx)



def paint_star(p: QPainter, rect: QRect, on: bool) -> None:
    """Small gold star badge in the card's top-left corner."""
    if not on:
        return
    badge = QRect(rect.left() + 6, rect.top() + 6, 22, 22)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 170))
    p.drawEllipse(badge)
    p.setPen(QColor("#fbbf24"))
    f = p.font()
    f.setBold(True)
    f.setPointSizeF(max(9.0, f.pointSizeF()))
    p.setFont(f)
    p.drawText(badge, Qt.AlignmentFlag.AlignCenter, "★")


class StudioDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(CARD_W + 12, LOGO_H + TEXT_H + 12)

    def paint(self, p: QPainter, opt, index) -> None:
        e: StudioEntry = index.data(StudioRole)
        model = index.model()
        hidden = e.key in model.hidden
        r = opt.rect.adjusted(6, 6, -6, -6)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        p.setPen(QColor("#60a5fa") if sel else QColor("#333a45"))
        p.setBrush(QColor("#1f2530") if sel else QColor("#171b22"))
        p.drawRoundedRect(r, 8, 8)
        logo_rect = QRect(r.left() + 12, r.top() + 10, r.width() - 24, LOGO_H - 16)
        pm: QPixmap | None = index.data(Qt.ItemDataRole.DecorationRole)
        if pm and not pm.isNull():
            scaled = pm.scaled(logo_rect.size(), Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            x = logo_rect.left() + (logo_rect.width() - scaled.width()) // 2
            y = logo_rect.top() + (logo_rect.height() - scaled.height()) // 2
            p.drawPixmap(x, y, scaled)
        else:
            f = QFont(opt.font)
            f.setPointSizeF(opt.font.pointSizeF() * 1.6)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#4b5563"))
            p.drawText(logo_rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, e.name)
        text_rect = QRect(r.left() + 10, r.top() + LOGO_H, r.width() - 20, TEXT_H)
        f = QFont(opt.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#e5e7eb"))
        fm = p.fontMetrics()
        p.drawText(QRect(text_rect.left(), text_rect.top(), text_rect.width(), fm.height()), 0,
                   fm.elidedText(e.name, Qt.TextElideMode.ElideRight, text_rect.width()))
        p.setFont(opt.font)
        p.setPen(QColor("#9ca3af"))
        sub = f"{e.scenes} scene{'s' if e.scenes != 1 else ''}" + (f" · {e.have} downloaded" if e.have else "")
        p.drawText(QRect(text_rect.left(), text_rect.top() + fm.height() + 2, text_rect.width(), fm.height()), 0, sub)
        paint_star(p, r, e.key in model.favorites)
        if hidden:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QColor("#f87171"))
            p.drawText(QRect(r.left(), r.top() + 6, r.width() - 10, 18),
                       Qt.AlignmentFlag.AlignRight, "HIDDEN")
        p.restore()


class StudiosTab(QWidget):
    studio_activated = Signal(str, str)  # key, display name
    hide_requested = Signal(str, str, bool)  # key, name, hide?
    favorite_requested = Signal(str, str, bool)  # key, name, favourite?

    def __init__(self, logos: LogoService, parent=None):
        super().__init__(parent)
        self.model = StudioModel(logos)
        self.proxy = self.model  # filtering/sorting lives in the model (FilteredList)
        top = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Filter studios…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(300)
        self.search.textChanged.connect(lambda t: self.proxy.set(text=t))
        self.sort_box = QComboBox()
        for label, mode in (("Sort: name", "name"), ("Sort: most scenes", "scenes"), ("Sort: most downloaded", "have")):
            self.sort_box.addItem(label, mode)
        self.sort_box.currentIndexChanged.connect(lambda _i: self.proxy.set(sort_mode=self.sort_box.currentData()))
        self.hint = QLabel()
        self.hint.setStyleSheet("color:#888")
        top.addWidget(self.search)
        top.addWidget(self.sort_box)
        self.fav_only = QCheckBox("★ Favorites only")
        self.fav_only.toggled.connect(lambda v: self.proxy.set(fav_only=v))
        top.addWidget(self.fav_only)
        top.addWidget(self.hint, 1)
        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setModel(self.proxy)
        self.view.setItemDelegate(StudioDelegate())
        self.view.activated.connect(self._activate)
        self.view.clicked.connect(self._activate)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.view)

    def _activate(self, idx) -> None:
        e: StudioEntry = idx.data(StudioRole)
        if e:
            self.studio_activated.emit(e.key, e.name)

    def _menu(self, pos) -> None:
        idx = self.view.indexAt(pos)
        e: StudioEntry | None = idx.data(StudioRole) if idx.isValid() else None
        if not e:
            return
        menu = QMenu(self)
        menu.addAction(f"Show scenes from {e.name}", lambda: self.studio_activated.emit(e.key, e.name))
        fav = e.key in self.model.favorites
        menu.addAction("★ Remove from favorites" if fav else "☆ Add to favorites",
                       lambda: self.favorite_requested.emit(e.key, e.name, not fav))
        hidden = e.key in self.model.hidden
        menu.addAction(f"{'Unhide' if hidden else 'Hide'} studio “{e.name}”",
                       lambda: self.hide_requested.emit(e.key, e.name, not hidden))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def set_favorites(self, favs: set[str]) -> None:
        self.model.favorites = favs
        self.proxy.invalidate()
        self.proxy.sort(0)

    def set_hidden(self, hidden: set[str]) -> None:
        self.model.hidden = hidden
        if self.model.items:
            self.model.dataChanged.emit(self.model.index(0), self.model.index(len(self.model.items) - 1))

    def set_scenes(self, scenes: list[Scene], has, logos_enabled: bool) -> None:
        """Counts are worked out on a worker thread; only the finished list touches the UI."""
        self._gen = getattr(self, "_gen", 0) + 1
        gen = self._gen

        def apply(items):
            if gen != self._gen:
                return
            self.model.set_items(items)
            self.proxy.sort(0)
            self.model.logos.prefetch([(e.key, e.name) for e in items])
            self.hint.setText(f"{len(items)} studios · click one to browse its scenes" +
                              ("" if logos_enabled else " · add a StashDB API key in Settings to load logos"))
        run_async(aggregate_studios, scenes, has, self.model.logos.db.studio_names(), on_done=apply, pool="cpu")
