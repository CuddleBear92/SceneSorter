"""A scene grid + detail panel. Used by both the Browse and Downloaded tabs.

Filtering and sorting run on a worker thread (views.compute_view); the UI
thread only swaps in the finished list, keeping the card at the top of the
view where it was (no jumping to the selected scene when new posts arrive).
"""
from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QSize, QItemSelectionModel, QModelIndex, QPoint, Qt, QTimer, Signal
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


def breakable(text: str) -> str:
    """Let long dotted/underscored file names wrap (zero-width spaces after separators)."""
    return text.replace(".", ".\u200b").replace("_", "_\u200b").replace("-", "-\u200b")


class FitImage(QLabel):
    """Thumbnail that fills the panel's width (16:9, capped) instead of a fixed size."""

    MAX_W, RATIO = 640, 9 / 16

    def __init__(self, pm=None, parent=None):
        super().__init__(parent)
        self._pm = pm
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(1, 1)
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(sp.Policy.Ignored)  # never pushes the panel wider
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return int(min(w, self.MAX_W) * self.RATIO)

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(360, int(360 * self.RATIO))

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if self._pm is not None and not self._pm.isNull():
            w = min(self.width(), self.MAX_W)
            self.setPixmap(self._pm.scaled(w, int(w * self.RATIO), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))


class _DetailWidget(QWidget):
    """Detail panel contents. Never asks for more width than the panel has: a long
    unbreakable line used to make the whole panel wider than its visible area, cutting
    off the thumbnail and the buttons on the right. Now such a line wraps or is clipped
    on its own and everything else fits."""

    def minimumSizeHint(self):  # noqa: N802
        h = super().minimumSizeHint()
        return QSize(0, h.height())


class ScenePane(QSplitter):
    #: right-click on the grid: (selected scenes, global position)
    context_requested = Signal(list, QPoint)
    #: number of visible scenes after each update
    count_changed = Signal(int)
    #: True while a background filter/sort pass is running (for the progress bar)
    busy_changed = Signal(bool)

    def __init__(self, model: SceneModel, delegate: SceneDelegate,
                 render_detail: Callable[["ScenePane", Scene], None], have: str = "",
                 empty_text: str = "Select a scene", render_multi=None):
        super().__init__()
        self.model = model                      # the shared store
        self.list = SceneListModel(model)       # this pane's visible rows
        self.render_detail_cb = render_detail
        self.render_multi_cb = render_multi  # (pane, [scenes]) when several are selected
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
        self.grid.selectionModel().selectionChanged.connect(lambda *_: self._selection_changed())
        self.grid.deselected.connect(lambda: self.show_scene(None))
        self.list.needs_repaint.connect(self._repaint)

        # Coalesce bursts of changes (typing, several pages arriving) into one background pass.
        self._debounce = QTimer(self, singleShot=True, interval=120, timeout=self._compute)
        model.scenes_changed.connect(self.request_update)
        model.status_changed.connect(self.request_update)

        self.detail = _DetailWidget()
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.detail)
        scroll.setMinimumWidth(420)
        self._scroll = scroll
        self._sized = False

        self.addWidget(self.grid)
        self.addWidget(scroll)
        self.setStretchFactor(0, 1)
        self.setStretchFactor(1, 0)  # extra window width goes to the grid; the panel keeps its size
        self.show_message(empty_text)

    # default detail panel width: about a third of the window, within these limits
    DETAIL_MIN, DETAIL_MAX, DETAIL_SHARE = 460, 620, 0.32

    def showEvent(self, ev) -> None:  # noqa: N802
        super().showEvent(ev)
        if not self._sized:  # first show: give the panel a sensible width once laid out
            self._sized = True
            QTimer.singleShot(0, self._default_sizes)

    def _default_sizes(self) -> None:
        total = sum(self.sizes()) or self.width()
        panel = int(min(self.DETAIL_MAX, max(self.DETAIL_MIN, total * self.DETAIL_SHARE)))
        panel = min(panel, max(420, total // 2))  # small windows: never more than half
        self.setSizes([max(total - panel, 200), panel])

    # ------------------------------------------------------------ filtering
    def request_update(self) -> None:
        self._debounce.start()

    def _compute(self) -> None:
        if self._running:          # one pass at a time; run once more when it finishes
            self._again = True
            return
        self._running = True
        self.busy_changed.emit(True)
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
        else:
            self.busy_changed.emit(False)

    def _apply(self, gen: int, rows: list[Scene]) -> None:
        if gen == self._generation:
            self._set_rows(rows)
        self._finish()

    def _set_rows(self, rows: list[Scene]) -> None:
        """Swap in new rows without moving what the user is looking at."""
        old = self.list.rows
        if len(old) == len(rows) and all(a.key == b.key for a, b in zip(old, rows)):
            # Same scenes in the same order (status/name/favourite updates): no reset at all,
            # so the scroll position and selection can't move.
            self.list.replace_rows(rows)
            if self.current is not None:
                self.current = self.list.rows[self.list.row_of(self.current.key)] \
                    if self.list.row_of(self.current.key) is not None else self.current
            self.count_changed.emit(len(rows))
            return

        bar = self.grid.verticalScrollBar()
        at_top = bar.value() == 0
        anchor_key = anchor_offset = None
        geo = None
        if not at_top and old:
            top = self.grid.indexAt(QPoint(20, 20))
            if top.isValid():
                anchor_key = top.data(SceneRole).key
                anchor_offset = self.grid.visualRect(top).top()
            geo = self._grid_geometry()
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
        self.grid.setAutoScroll(True)
        self._restoring = False

        if not at_top and anchor_key and geo:
            row = self.list.row_of(anchor_key)
            if row is not None:
                # Cards are laid out in batches, so the anchor card may not have a position yet.
                # Work out where it will be from the grid geometry and keep applying that until
                # the layout has grown far enough (or the user scrolls themselves).
                first_top, per_row, cell_h = geo
                self._scroll_target = first_top + (row // per_row) * cell_h - anchor_offset
                self._apply_scroll_target()
        self.count_changed.emit(len(rows))

    def _repaint(self, row: int) -> None:
        """Redraw without telling the view the data changed (that would re-layout 100k cards)."""
        if row < 0:
            self.grid.viewport().update()
        else:
            self.grid.update(self.list.index(row))

    def _grid_geometry(self) -> tuple[int, int, int] | None:
        """(content y of the first card, cards per row, row height) of the current layout."""
        if self.list.rowCount() < 2:
            return None
        bar = self.grid.verticalScrollBar()
        r0 = self.grid.visualRect(self.list.index(0))
        if not r0.isValid():
            return None
        per_row = 1
        while per_row < min(self.list.rowCount(), 200) and \
                self.grid.visualRect(self.list.index(per_row)).top() == r0.top():
            per_row += 1
        nxt = self.grid.visualRect(self.list.index(per_row)) if per_row < self.list.rowCount() else None
        cell_h = (nxt.top() - r0.top()) if nxt is not None and nxt.isValid() else r0.height()
        return r0.top() + bar.value(), per_row, max(1, cell_h)

    def _apply_scroll_target(self) -> None:
        """Keep putting the view where it belongs while batched layout catches up (max 3 s);
        stops as soon as it's reached or the user scrolls themselves."""
        t = getattr(self, "_scroll_target", None)
        if t is None:
            return
        target, deadline, last = t if isinstance(t, tuple) else (t, time.time() + 3, None)
        bar = self.grid.verticalScrollBar()
        if last is not None and bar.value() != last:  # the user scrolled meanwhile: leave it
            self._scroll_target = None
            return
        bar.setValue(target)
        if bar.value() >= target or time.time() > deadline:
            self._scroll_target = None
        else:
            self._scroll_target = (target, deadline, bar.value())
            QTimer.singleShot(50, self._apply_scroll_target)

    # ------------------------------------------------------------ selection
    def selected_scenes(self) -> list[Scene]:
        return [i.data(SceneRole) for i in self.grid.selectionModel().selectedIndexes()]

    def _current_changed(self, cur: QModelIndex, _prev) -> None:
        if not self._restoring and cur.isValid() and len(self.grid.selectionModel().selectedIndexes()) <= 1:
            self.show_scene(cur.data(SceneRole))

    def _selection_changed(self) -> None:
        if self._restoring:
            return
        sel = self.selected_scenes()
        if len(sel) > 1 and self.render_multi_cb:
            self.clear_detail()
            self.render_multi_cb(self, sel)
        elif len(sel) == 1 and (self.current is None or self.current.key != sel[0].key):
            self.show_scene(sel[0])

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
        sel = self.selected_scenes()
        if len(sel) > 1 and self.render_multi_cb:
            self.clear_detail()
            self.render_multi_cb(self, sel)
            return
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
