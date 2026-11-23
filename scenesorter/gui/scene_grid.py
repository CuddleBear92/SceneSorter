"""Grid of scene cards: model, filter proxy and painting delegate."""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..models import Scene
from ..parsing import normalise_studio as studio_key
from ..views import ViewState
from .workers import ThumbnailLoader

SceneRole = Qt.ItemDataRole.UserRole + 1
CARD_W, THUMB_H, TEXT_H = 260, 146, 74

SOURCE_COLORS = ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6"]


class SceneModel(QObject):
    """Shared scene store for every pane: the full scene list plus everything the
    cards need to know (download state, hidden, names, favourites). Panes keep
    their own filtered/sorted list (see SceneListModel) computed off the UI thread."""

    scenes_changed = Signal()   # new scene list -> panes recompute
    status_changed = Signal()   # state/hidden/names changed -> panes refilter + repaint
    repaint = Signal(str)       # one scene key ("" = all) needs repainting

    def __init__(self, thumbs: ThumbnailLoader, parent=None):
        super().__init__(parent)
        self.scenes: list[Scene] = []
        self.by_key: dict[str, Scene] = {}
        self.owned: set[str] = set()          # scene keys found in the library on disk
        self.status: dict[str, str] = {}      # scene key -> grabbed / partial / complete (see status.py)
        self.hidden: set[str] = set()         # scene keys hidden by the user (scene or studio blacklist)
        self.studio_names: dict[str, str] = {}  # studio key -> official stash-box name
        self.scene_performers: dict[str, list[str]] = {}  # scene key -> performer keys
        self.performer_names: dict[str, str] = {}         # performer key -> display name
        self.covers: dict[str, list[str]] = {}  # scene key -> stash-box cover URLs (TPDB first)
        self.fav_studios: set[str] = set()
        self.fav_performers: set[str] = set()
        self.on_visible = None  # callback(scene) when a card is painted (drives on-screen-first matching)
        self.thumbs = thumbs

    def set_scenes(self, scenes: list[Scene], owned: set[str] | None = None) -> None:
        self.scenes = scenes
        self.by_key = {s.key: s for s in scenes}
        if owned is not None:
            self.owned = owned
        self.scenes_changed.emit()

    def set_status(self, owned: set[str] | None = None, status: dict[str, str] | None = None,
                   hidden: set[str] | None = None) -> None:
        """Update marks (panes refilter in the background and repaint)."""
        if owned is not None:
            self.owned = owned
        if status is not None:
            self.status = status
        if hidden is not None:
            self.hidden = hidden
        self.status_changed.emit()
        self.repaint.emit("")

    def names_changed(self) -> None:
        """Studio/performer names or favourites changed: refilter (search/filters use them)."""
        self.status_changed.emit()
        self.repaint.emit("")

    def repaint_all(self) -> None:
        self.repaint.emit("")

    def refresh_row(self, key: str) -> None:
        self.repaint.emit(key)

    def view_state(self) -> ViewState:
        return ViewState(self.owned, self.status, self.hidden, self.studio_names, self.scene_performers,
                         self.performer_names, self.fav_studios, self.fav_performers)

    def state(self, key: str) -> str:
        """'complete' (incl. found in library), 'partial', 'grabbed' or 'none'."""
        if key in self.owned:
            return "complete"
        return self.status.get(key, "none")

    def has(self, key: str) -> bool:
        return self.state(key) == "complete"

    def thumb_urls(self, s: Scene) -> list[str]:
        """The post's own thumbnails first, then stash-box covers as a fallback."""
        return s.thumbnail_urls + [u for u in self.covers.get(s.key, []) if u not in s.thumbnail_urls]

    def is_favorite(self, s: Scene) -> bool:
        return (bool(s.studio) and studio_key(s.studio) in self.fav_studios) or \
            any(k in self.fav_performers for k in self.scene_performers.get(s.key, ()))

    def display_studio(self, s: Scene) -> str:
        return self.studio_names.get(studio_key(s.studio), s.studio) if s.studio else ""


class SceneListModel(QAbstractListModel):
    """One pane's visible scenes (already filtered and sorted)."""

    def __init__(self, store: SceneModel, parent=None):
        super().__init__(parent)
        self.store = store
        self.rows: list[Scene] = []
        self._row: dict[str, int] = {}
        store.repaint.connect(self._repaint)
        store.thumbs.loaded.connect(lambda key, _pm: self._repaint(key))

    def set_rows(self, rows: list[Scene]) -> None:
        self.beginResetModel()
        self.rows = rows
        self._row = {s.key: i for i, s in enumerate(rows)}
        self.endResetModel()

    def replace_rows(self, rows: list[Scene]) -> None:
        """Same keys in the same order, newer objects: swap without a model reset."""
        self.rows = rows
        if rows:
            self.dataChanged.emit(self.index(0), self.index(len(rows) - 1))

    def row_of(self, key: str) -> int | None:
        return self._row.get(key)

    def _repaint(self, key: str) -> None:
        if not self.rows:
            return
        if not key:
            self.dataChanged.emit(self.index(0), self.index(len(self.rows) - 1))
        elif (row := self._row.get(key)) is not None:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        s = self.rows[index.row()]
        if role == SceneRole:
            return s
        if role == Qt.ItemDataRole.DisplayRole:
            return s.title
        if role == Qt.ItemDataRole.DecorationRole:
            if self.store.on_visible:
                self.store.on_visible(s)
            return self.store.thumbs.get(s.key, self.store.thumb_urls(s))
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(sorted({r.raw_title for r in s.releases}))
        return None


def fmt_duration(sec: int | None) -> str:
    if not sec:
        return ""
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class SceneDelegate(QStyledItemDelegate):
    def __init__(self, source_ids: list[str], parent=None):
        super().__init__(parent)
        self.preferred_groups: list[str] = []
        self.set_sources(source_ids)

    def set_sources(self, source_ids: list[str]) -> None:
        self.colors = {sid: QColor(SOURCE_COLORS[i % len(SOURCE_COLORS)]) for i, sid in enumerate(source_ids)}

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(CARD_W + 12, THUMB_H + TEXT_H + 12)

    def paint(self, p: QPainter, opt: QStyleOptionViewItem, index) -> None:
        s: Scene = index.data(SceneRole)
        model = index.model().store
        owned = s.key in model.owned
        state = model.state(s.key)
        hidden = s.key in model.hidden
        r = opt.rect.adjusted(6, 6, -6, -6)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        p.setPen(QColor("#60a5fa") if selected else QColor("#333a45"))
        p.setBrush(QColor("#1f2530") if selected else QColor("#171b22"))
        p.drawRoundedRect(r, 8, 8)

        # thumbnail
        thumb_rect = QRect(r.left(), r.top(), r.width(), THUMB_H)
        pm: QPixmap | None = index.data(Qt.ItemDataRole.DecorationRole)
        p.setClipRect(thumb_rect)
        if pm:
            scaled = pm.scaled(thumb_rect.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
            sx = (scaled.width() - thumb_rect.width()) // 2
            sy = (scaled.height() - thumb_rect.height()) // 2
            p.drawPixmap(thumb_rect, scaled, QRect(sx, sy, thumb_rect.width(), thumb_rect.height()))
        else:
            p.fillRect(thumb_rect, QColor("#0e1116"))
            p.setPen(QColor("#555"))
            p.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter,
                       "loading…" if model.thumb_urls(s) else "no thumbnail")
        p.setClipping(False)

        # resolution chips (top-left)
        small = QFont(opt.font)
        small.setPointSizeF(max(7.0, opt.font.pointSizeF() - 1.5))
        small.setBold(True)
        p.setFont(small)
        x = thumb_rect.left() + 6
        for res in s.resolutions:
            w = p.fontMetrics().horizontalAdvance(res) + 10
            chip = QRect(x, thumb_rect.top() + 6, w, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 180))
            p.drawRoundedRect(chip, 4, 4)
            p.setPen(QColor("white"))
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, res)
            x += w + 4

        # source dots (top-right) + owned badge
        x = thumb_rect.right() - 6
        for sid in reversed(s.sources):
            x -= 12
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self.colors.get(sid, QColor("#999")))
            p.drawEllipse(QRect(x, thumb_rect.top() + 9, 10, 10))
        p.setFont(small)
        x = thumb_rect.left() + 6
        for grp in s.release_groups:
            w = p.fontMetrics().horizontalAdvance(grp) + 10
            chip = QRect(x, thumb_rect.bottom() - 24, w, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#d97706") if grp in self.preferred_groups else QColor(0, 0, 0, 180))
            p.drawRoundedRect(chip, 4, 4)
            p.setPen(QColor("white"))
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, grp)
            x += w + 4
        badge_info = (("IN LIBRARY", "#16a34a") if owned else
                      {"complete": ("DOWNLOADED", "#2563eb"), "partial": ("PARTIAL", "#d97706"),
                       "grabbed": ("GRABBED", "#7c3aed")}.get(state))
        if badge_info:
            text, color = badge_info
            w = p.fontMetrics().horizontalAdvance(text) + 12
            badge = QRect(thumb_rect.right() - w - 6, thumb_rect.bottom() - 24, w, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawRoundedRect(badge, 4, 4)
            p.setPen(QColor("white"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, text)

        # text
        text_rect = QRect(r.left() + 8, thumb_rect.bottom() + 4, r.width() - 16, TEXT_H - 8)
        p.setPen(QColor("#e5e7eb"))
        f = QFont(opt.font)
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        title = fm.elidedText(s.title or s.primary.raw_title, Qt.TextElideMode.ElideRight, text_rect.width())
        p.drawText(text_rect.adjusted(0, 0, 0, -TEXT_H // 2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, title)
        p.setFont(opt.font)
        p.setPen(QColor("#9ca3af"))
        fm = p.fontMetrics()
        line2 = ("★ " if model.is_favorite(s) else "") + " · ".join(x for x in [model.display_studio(s), s.scene_date.isoformat() if s.scene_date else "",
                                       fmt_duration(s.duration_s)] if x)
        line3 = ", ".join(s.performers)
        y = text_rect.top() + fm.height() + 4
        p.drawText(QRect(text_rect.left(), y, text_rect.width(), fm.height()), 0,
                   fm.elidedText(line2, Qt.TextElideMode.ElideRight, text_rect.width()))
        if line3:
            p.drawText(QRect(text_rect.left(), y + fm.height(), text_rect.width(), fm.height()), 0,
                       fm.elidedText(line3, Qt.TextElideMode.ElideRight, text_rect.width()))
        if hidden:  # dim blacklisted cards (only visible with "Show hidden")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(r, 8, 8)
            p.setPen(QColor("#f87171"))
            p.setFont(small)
            p.drawText(QRect(r.left(), r.top() + THUMB_H // 2 - 10, r.width(), 20), Qt.AlignmentFlag.AlignCenter, "HIDDEN")
        p.restore()
