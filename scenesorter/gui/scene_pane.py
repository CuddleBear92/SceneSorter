"""A scene grid + detail panel. Used by both the Browse and Downloaded tabs."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QLabel, QListView, QScrollArea, QSplitter, QVBoxLayout, QWidget

from ..models import Scene
from .scene_grid import SceneDelegate, SceneFilter, SceneModel, SceneRole


class ScenePane(QSplitter):
    #: right-click on the grid: (selected scenes, global position)
    context_requested = Signal(list, QPoint)
    def __init__(self, model: SceneModel, delegate: SceneDelegate,
                 render_detail: Callable[["ScenePane", Scene], None], have: str = "",
                 empty_text: str = "Select a scene"):
        super().__init__()
        self.model = model
        self.render_detail_cb = render_detail
        self.empty_text = empty_text
        self.current: Scene | None = None
        self._restoring = False

        self.filter = SceneFilter(have=have)
        self.filter.setSourceModel(model)
        self.grid = QListView()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setSpacing(2)
        self.grid.setModel(self.filter)
        self.grid.setItemDelegate(delegate)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)  # ctrl/shift for bulk
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._context_menu)
        self.grid.selectionModel().currentChanged.connect(lambda cur, _p: self.show_scene(cur.data(SceneRole)))
        model.modelAboutToBeReset.connect(self._before_reset)
        model.modelReset.connect(self._after_reset)
        model.status_changed.connect(self.filter.invalidate)

        self.detail = QWidget()
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.detail)
        scroll.setMinimumWidth(380)

        self.addWidget(self.grid)
        self.addWidget(scroll)
        self.setStretchFactor(0, 3)
        self.setStretchFactor(1, 1)
        self.show_message(empty_text)

    # -- keep scroll position + selection across model resets (new pages arriving)
    def _before_reset(self) -> None:
        self._saved = (self.current.key if self.current else None, self.grid.verticalScrollBar().value())
        self._restoring = True

    def _after_reset(self) -> None:
        key, scroll = getattr(self, "_saved", (None, 0))
        if key:
            row = self.model._row_by_key.get(key)
            if row is not None:
                self.current = self.model.scenes[row]
                self.grid.setCurrentIndex(self.filter.mapFromSource(self.model.index(row)))
        self.grid.verticalScrollBar().setValue(scroll)
        self._restoring = False

    def _context_menu(self, pos) -> None:
        idx = self.grid.indexAt(pos)
        if not idx.isValid():
            return
        if not self.grid.selectionModel().isSelected(idx):
            self.grid.setCurrentIndex(idx)
        scenes = [i.data(SceneRole) for i in self.grid.selectionModel().selectedIndexes()]
        self.context_requested.emit(scenes or [idx.data(SceneRole)], self.grid.viewport().mapToGlobal(pos))

    def show_scene(self, scene: Scene | None) -> None:
        if self._restoring:
            return
        self.current = scene
        if scene is None:
            self.show_message(self.empty_text)
        else:
            self.clear_detail()
            self.render_detail_cb(self, scene)

    def refresh_detail(self) -> None:
        if self.current:
            row = self.model._row_by_key.get(self.current.key)
            if row is not None:
                self.current = self.model.scenes[row]
            self.clear_detail()
            self.render_detail_cb(self, self.current)

    def clear_detail(self) -> None:
        while self.detail_layout.count():
            w = self.detail_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    def show_message(self, msg: str) -> None:
        self.clear_detail()
        lbl = QLabel(msg)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#888; padding:20px")
        self.detail_layout.addWidget(lbl)
