"""A scene grid + detail panel. Used by both the Browse and Downloaded tabs.

Filtering and sorting run on a worker thread (views.compute_view); the UI
thread only swaps in the finished list, keeping the card at the top of the
view where it was (no jumping to the selected scene when new posts arrive).
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import QAbstractItemView, QLabel, QListView, QScrollArea, QSplitter, QVBoxLayout, QWidget

from ..models import Scene
from ..views import ViewSpec, compute_view
from .scene_grid import SceneDelegate, SceneListModel, SceneModel, SceneRole
from .workers import run_async


class _Filter(ViewSpec):
    """The pane's ViewSpec; ``set(...)`` changes it and recomputes in the background."""

    def bind(self, pane: "ScenePane", locked_have: bool) -> "_Filter":
        self._pane, self._locked_have = pane, locked_have
        return self

    def set(self, **kw) -> None:
        if self._locked_have:
            kw.pop("have", None)
        for k, v in kw.items():
            setattr(self, k, v)
        self._pane.request_update()


class SceneGrid(QListView):
    """Card grid. Click empty space, click the selected card again, or press Esc to deselect."""

    deselected = Signal()

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        idx = self.indexAt(ev.position().toPoint())
        plain = ev.button() == Qt.MouseButton.LeftButton and ev.modifiers() == Qt.KeyboardModifier.NoModifier
        if plain and not idx.isValid():
            self.clear_selection()
            return
        if plain and idx.isValid() and self.selectionModel().isSelected(idx) \
                and len(self.selectionModel().selectedIndexes()) == 1:
            self.clear_selection()
            return
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Escape:
            self.clear_selection()
            return
        super().keyPressEvent(ev)

    def clear_selection(self) -> None:
        self.selectionModel().clear()
        self.deselected.emit()


class ScenePane(QSplitter):
    #: right-click on the grid: (selected scenes, global position)
    context_requested = Signal(list, QPoint)
    #: number of visible scenes after each update
    count_changed = Signal(int)

    def __init__(self, model: SceneModel, delegate: SceneDelegate,
                 render_detail: Callable[["ScenePane", Scene], None], have: str = "",
                 empty_text: str = "Select a scene"):
        super().__init__()
        self.model = model                      # the shared store
        self.list = SceneListModel(model)       # this pane's visible rows
        self.render_detail_cb = render_detail
        self.empty_text = empty_text
        self.current: Scene | None = None
        self._restoring = False
        self._generation = 0
        self._running = False
        self._again = False

        self.filter = _Filter(have=have).bind(self, locked_have=bool(have))
        self.grid = SceneGrid()
        # Wrapping list (looks like a grid) with batched layout: 67k cards are laid out in
        # small chunks across event-loop turns instead of one long UI-blocking pass.
        self.grid.setViewMode(QListView.ViewMode.ListMode)
        self.grid.setFlow(QListView.Flow.LeftToRight)
        self.grid.setWrapping(True)
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setBatchSize(400)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setSpacing(2)
        self.grid.setModel(self.list)
        self.grid.setItemDelegate(delegate)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)  # ctrl/shift for bulk
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._context_menu)
        self.grid.selectionModel().currentChanged.connect(self._current_changed)
        self.grid.deselected.connect(lambda: self.show_scene(None))

        # Coalesce bursts of changes (typing, several pages arriving) into one background pass.
        self._debounce = QTimer(self, singleShot=True, interval=120, timeout=self._compute)
        model.scenes_changed.connect(self.request_update)
        model.status_changed.connect(self.request_update)

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

    # ------------------------------------------------------------ filtering
    def request_update(self) -> None:
        self._debounce.start()

    def _compute(self) -> None:
        if self._running:          # one pass at a time; run once more when it finishes
            self._again = True
            return
        self._running = True
        self._generation += 1
        gen = self._generation
        run_async(compute_view, self.model.scenes, self.filter.copy(), self.model.view_state(),
                  on_done=lambda rows: self._apply(gen, rows),
                  on_fail=lambda _e: self._finish(), pool="cpu")

    def _finish(self) -> None:
        self._running = False
        if self._again:
            self._again = False
            self._compute()

    def _apply(self, gen: int, rows: list[Scene]) -> None:
        if gen == self._generation:
            self._set_rows(rows)
        self._finish()

    def _set_rows(self, rows: list[Scene]) -> None:
        """Swap in new rows without moving what the user is looking at."""
        bar = self.grid.verticalScrollBar()
        at_top = bar.value() == 0
        anchor_key, anchor_offset = None, 0
        if not at_top:
            top = self.grid.indexAt(QPoint(20, 20))
            if top.isValid():
                anchor_key = top.data(SceneRole).key
                anchor_offset = self.grid.visualRect(top).top()
        selected = {i.data(SceneRole).key for i in self.grid.selectionModel().selectedIndexes()}
        current_key = self.current.key if self.current else None

        self._restoring = True
        self.grid.setAutoScroll(False)
        self.list.set_rows(rows)
        sm = self.grid.selectionModel()
        for key in selected:
            row = self.list.row_of(key)
            if row is not None:
                sm.select(self.list.index(row), QItemSelectionModel.SelectionFlag.Select)
        if current_key:
            row = self.list.row_of(current_key)
            if row is not None:
                self.current = rows[row]
                sm.setCurrentIndex(self.list.index(row), QItemSelectionModel.SelectionFlag.NoUpdate)
        if not at_top and anchor_key:
            row = self.list.row_of(anchor_key)
            if row is not None:
                self.grid.doItemsLayout()  # positions are needed now, not on the next paint
                idx = self.list.index(row)
                self.grid.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtTop)
                # put the anchor card back exactly where it was on screen
                bar.setValue(bar.value() + self.grid.visualRect(idx).top() - anchor_offset)
        self.grid.setAutoScroll(True)
        self._restoring = False
        self.count_changed.emit(len(rows))

    # ------------------------------------------------------------ selection
    def _current_changed(self, cur: QModelIndex, _prev) -> None:
        if not self._restoring and cur.isValid():
            self.show_scene(cur.data(SceneRole))

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
            self.current = self.model.by_key.get(self.current.key, self.current)
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
