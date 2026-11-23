"""Performers tab: portrait cards, built from stash-box matches, posts and titles."""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListView, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from ..performers import PerformerEntry
from .matching import PerformerImages
from .studios import StudioEntry, StudioFilter, StudioRole

CARD_W, PHOTO_H, TEXT_H = 160, 210, 46


class PerformerModel(QAbstractListModel):
    def __init__(self, images: PerformerImages, parent=None):
        super().__init__(parent)
        self.images = images
        self.items: list[StudioEntry] = []   # same shape as studios: key, name, scenes, have
        self.sources: dict[str, set[str]] = {}
        self._row: dict[str, int] = {}
        images.ready.connect(self._ready)

    def set_performers(self, perf: dict[str, PerformerEntry], has) -> None:
        self.beginResetModel()
        self.items = sorted((StudioEntry(e.key, e.name, len(e.scenes), sum(1 for k in e.scenes if has(k)))
                             for e in perf.values()), key=lambda e: e.name.lower())
        self.sources = {e.key: e.sources for e in perf.values()}
        self._row = {e.key: i for i, e in enumerate(self.items)}
        self.endResetModel()

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
            return self.images.get(e.key, e.name)
        if role == Qt.ItemDataRole.ToolTipRole:
            src = {"stashbox": "stash-box match", "post": "post title", "title": "spotted in release names"}
            return f"{e.name}\nFound via: " + ", ".join(src[s] for s in sorted(self.sources.get(e.key, ())))
        return None

    def _ready(self, key: str) -> None:
        row = self._row.get(key)
        if row is not None:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx)


class PerformerDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(CARD_W + 12, PHOTO_H + TEXT_H + 12)

    def paint(self, p: QPainter, opt, index) -> None:
        e: StudioEntry = index.data(StudioRole)
        r = opt.rect.adjusted(6, 6, -6, -6)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        sel = bool(opt.state & QStyle.StateFlag.State_Selected)
        p.setPen(QColor("#60a5fa") if sel else QColor("#333a45"))
        p.setBrush(QColor("#1f2530") if sel else QColor("#171b22"))
        p.drawRoundedRect(r, 8, 8)
        photo = QRect(r.left(), r.top(), r.width(), PHOTO_H)
        clip = QPainterPath()
        clip.addRoundedRect(photo, 8, 8)
        p.setClipPath(clip)
        pm: QPixmap | None = index.data(Qt.ItemDataRole.DecorationRole)
        if pm and not pm.isNull():
            scaled = pm.scaled(photo.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
            sx = (scaled.width() - photo.width()) // 2
            p.drawPixmap(photo, scaled, QRect(sx, 0, photo.width(), photo.height()))  # keep faces (top) in frame
        else:
            p.fillRect(photo, QColor("#0e1116"))
            initials = "".join(w[0] for w in e.name.split()[:2]).upper()
            f = QFont(opt.font)
            f.setPointSizeF(opt.font.pointSizeF() * 2.4)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#374151"))
            p.drawText(photo, Qt.AlignmentFlag.AlignCenter, initials)
        p.setClipping(False)
        text = QRect(r.left() + 8, photo.bottom() + 4, r.width() - 16, TEXT_H)
        f = QFont(opt.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#e5e7eb"))
        fm = p.fontMetrics()
        p.drawText(QRect(text.left(), text.top(), text.width(), fm.height()), 0,
                   fm.elidedText(e.name, Qt.TextElideMode.ElideRight, text.width()))
        p.setFont(opt.font)
        p.setPen(QColor("#9ca3af"))
        sub = f"{e.scenes} scene{'s' if e.scenes != 1 else ''}" + (f" · {e.have} have" if e.have else "")
        p.drawText(QRect(text.left(), text.top() + fm.height() + 2, text.width(), fm.height()), 0, sub)
        p.restore()


class PerformersTab(QWidget):
    performer_activated = Signal(str, str)  # key, name

    def __init__(self, images: PerformerImages, parent=None):
        super().__init__(parent)
        self.images = images
        self.model = PerformerModel(images)
        self.proxy = StudioFilter()
        self.proxy.setSourceModel(self.model)
        top = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Filter performers…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(300)
        self.search.textChanged.connect(lambda t: self.proxy.set(text=t))
        self.sort_box = QComboBox()
        for label, mode in (("Sort: most scenes", "scenes"), ("Sort: name", "name"), ("Sort: most downloaded", "have")):
            self.sort_box.addItem(label, mode)
        self.sort_box.currentIndexChanged.connect(lambda _i: self.proxy.set(sort_mode=self.sort_box.currentData()))
        self.proxy.set(sort_mode="scenes")
        self.hint = QLabel()
        self.hint.setStyleSheet("color:#888")
        top.addWidget(self.search)
        top.addWidget(self.sort_box)
        top.addWidget(self.hint, 1)
        self.view = QListView()
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)
        self.view.setUniformItemSizes(True)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setModel(self.proxy)
        self.view.setItemDelegate(PerformerDelegate())
        self.view.clicked.connect(self._activate)
        self.view.activated.connect(self._activate)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.view)

    def _activate(self, idx) -> None:
        e = idx.data(StudioRole)
        if e:
            self.performer_activated.emit(e.key, e.name)

    def set_performers(self, perf: dict[str, PerformerEntry], has, images_enabled: bool) -> None:
        self.model.set_performers(perf, has)
        # prefetch portraits for the most frequent performers first
        top = sorted(self.model.items, key=lambda e: -e.scenes)
        self.images.prefetch([(e.key, e.name) for e in top])
        self.hint.setText(f"{len(self.model.items)} performers · click one to browse their scenes" +
                          ("" if images_enabled else " · add stash-box API keys in Settings for photos"))
