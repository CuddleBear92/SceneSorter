"""Main application window."""
from __future__ import annotations

import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from .. import __version__, updater
from ..config import CACHE_DIR, DATA_DIR, Settings
from ..downloader import DownloadManager, Job, State
from ..library import scan_library, sort_into_library
from ..merge import merge_releases
from ..models import LinkKind, Release, Scene
from ..quota import QuotaTracker
from ..sources import Source, build_source
from .scene_grid import SceneDelegate, SceneFilter, SceneModel, SceneRole
from .settings_dialog import SettingsDialog
from .workers import ThumbnailLoader, run_async

LINK_PREFERENCE = {LinkKind.DIRECT: 0, LinkKind.TORRENT: 1, LinkKind.MAGNET: 2, LinkKind.HOSTER: 3}


def fmt_bytes(n: float | None) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class _JobBridge(QObject):
    changed = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle(f"SceneSorter {__version__}")
        self.resize(1400, 860)

        self.quota = QuotaTracker(DATA_DIR / "quota.json")
        self.releases: dict[tuple[str, str], Release] = {}
        self.next_page: dict[str, int] = {}
        self.sources: dict[str, Source] = {}
        self.owned: dict[str, list[Path]] = {}
        self.current: Scene | None = None

        self.bridge = _JobBridge()
        self.bridge.changed.connect(self._job_changed)
        self.downloads = DownloadManager(settings.max_parallel_downloads, on_finished=self._on_download_finished)
        self.downloads.subscribe(self.bridge.changed.emit)
        self.job_rows: dict[str, int] = {}

        self.thumbs = ThumbnailLoader(CACHE_DIR)
        self._build_ui()
        self._load_sources()
        self.rescan_library()
        QTimer.singleShot(0, self.refresh)
        if settings.check_updates_on_start:
            QTimer.singleShot(1500, lambda: self.check_updates(silent=True))

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        act_refresh = QAction("⟳ Refresh", self, triggered=self.refresh)
        act_more = QAction("Load more", self, triggered=self.load_more)
        act_settings = QAction("Settings", self, triggered=self.open_settings)
        act_update = QAction("Check for updates", self, triggered=lambda: self.check_updates(False))
        for a in (act_refresh, act_more):
            tb.addAction(a)
        tb.addSeparator()
        self.search = QLineEdit(placeholderText="Search studio, performer, title…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(360)
        self.search.textChanged.connect(lambda t: self.filter.set(text=t))
        tb.addWidget(self.search)
        self.res_box = QComboBox()
        self.res_box.addItems(["All resolutions", "2160p", "1080p", "720p", "480p"])
        self.res_box.currentTextChanged.connect(
            lambda t: self.filter.set(resolution="" if t.startswith("All") else t))
        tb.addWidget(self.res_box)
        self.src_box = QComboBox()
        self.src_box.currentIndexChanged.connect(
            lambda _i: self.filter.set(source=self.src_box.currentData() or ""))
        tb.addWidget(self.src_box)
        self.hide_owned = QCheckBox("Hide owned")
        self.hide_owned.toggled.connect(lambda v: self.filter.set(hide_owned=v))
        tb.addWidget(self.hide_owned)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        tb.addAction(act_settings)
        tb.addAction(act_update)

        # --- browse tab: grid + detail panel
        self.model = SceneModel(self.thumbs)
        self.filter = SceneFilter()
        self.filter.setSourceModel(self.model)
        self.grid = QListView()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setSpacing(2)
        self.grid.setModel(self.filter)
        self.delegate = SceneDelegate([])
        self.grid.setItemDelegate(self.delegate)
        self.grid.selectionModel().currentChanged.connect(lambda cur, _prev: self.show_scene(cur.data(SceneRole)))
        self.grid.verticalScrollBar().valueChanged.connect(self._maybe_autoload)

        self.detail = QWidget()
        self.detail_layout = QVBoxLayout(self.detail)
        self.detail_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.detail)
        scroll.setMinimumWidth(380)

        split = QSplitter()
        split.addWidget(self.grid)
        split.addWidget(scroll)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)

        # --- downloads tab
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Scene", "Res", "Source", "Status", "Progress", "Destination"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        dl_tab = QWidget()
        dl_layout = QVBoxLayout(dl_tab)
        dl_layout.addWidget(self.table)
        btn_cancel = QPushButton("Cancel selected")
        btn_cancel.clicked.connect(self._cancel_selected)
        dl_layout.addWidget(btn_cancel, alignment=Qt.AlignmentFlag.AlignRight)

        self.tabs = QTabWidget()
        self.tabs.addTab(split, "Browse")
        self.tabs.addTab(dl_tab, "Downloads")
        self.setCentralWidget(self.tabs)

        self.status_label = QLabel()
        self.quota_label = QLabel()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.quota_label)
        self._render_empty_detail()

    def _render_empty_detail(self, msg: str = "Select a scene") -> None:
        self._clear_detail()
        lbl = QLabel(msg)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#888; padding:20px")
        self.detail_layout.addWidget(lbl)

    def _clear_detail(self) -> None:
        while self.detail_layout.count():
            w = self.detail_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

    # -------------------------------------------------------------- sources
    def _load_sources(self) -> None:
        self.sources.clear()
        for cfg in self.settings.sources:
            if not cfg.enabled:
                continue
            try:
                self.sources[cfg.id] = build_source(cfg)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Source error", f"{cfg.id}: {e}")
        self.delegate.set_sources(list(self.sources))
        self.src_box.blockSignals(True)
        self.src_box.clear()
        self.src_box.addItem("All sources", "")
        for sid, src in self.sources.items():
            self.src_box.addItem(src.display_name, sid)
        self.src_box.blockSignals(False)
        self._update_quota_label()

    def refresh(self) -> None:
        if not self.sources:
            self._render_empty_detail("No sources configured yet. Open Settings ▸ Sources to add one.")
            return
        self.releases.clear()
        self.next_page = {sid: 1 for sid in self.sources}
        self.load_more()

    def load_more(self) -> None:
        for sid, src in self.sources.items():
            page = self.next_page.get(sid, 1)
            if page is None:
                continue
            self.next_page[sid] = None  # in flight; restored on completion
            self.status_label.setText(f"Loading {src.display_name} page {page}…")
            run_async(src.list_page, page,
                      on_done=lambda rels, sid=sid, page=page: self._page_loaded(sid, page, rels),
                      on_fail=lambda err, sid=sid, page=page: self._page_failed(sid, page, err))

    def _maybe_autoload(self, value: int) -> None:
        bar = self.grid.verticalScrollBar()
        if bar.maximum() and value >= bar.maximum() - 50 and not self.search.text():
            self.load_more()

    def _page_loaded(self, sid: str, page: int, rels: list[Release]) -> None:
        for r in rels:
            self.releases[(r.source, r.post_id)] = r
        self.next_page[sid] = page + 1 if rels else None
        self._rebuild()
        self.status_label.setText(f"{len(self.model.scenes)} scenes from {len(self.releases)} posts")

    def _page_failed(self, sid: str, page: int, err: str) -> None:
        self.next_page[sid] = page
        self.status_label.setText(f"{sid}: failed to load page {page}: {err.splitlines()[0]}")

    def _rebuild(self) -> None:
        current_key = self.current.key if self.current else None
        scenes = merge_releases(self.releases.values())
        self.model.set_scenes(scenes, set(self.owned))
        if current_key:
            self.current = next((s for s in scenes if s.key == current_key), self.current)

    def rescan_library(self) -> None:
        lib = Path(self.settings.library_dir)
        run_async(scan_library, lib, on_done=self._library_scanned)

    def _library_scanned(self, owned: dict) -> None:
        self.owned = owned
        self.model.set_scenes(self.model.scenes, set(owned))

    # --------------------------------------------------------------- detail
    def show_scene(self, scene: Scene | None) -> None:
        self.current = scene
        if scene is None:
            return self._render_empty_detail()
        self._clear_detail()
        thumb = QLabel()
        thumb.setFixedHeight(210)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = self.thumbs.get(scene.key, scene.thumbnail_urls)
        if pm:
            thumb.setPixmap(pm.scaled(360, 210, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        self.detail_layout.addWidget(thumb)

        title = QLabel(f"<h3>{scene.title or scene.primary.raw_title}</h3>")
        title.setWordWrap(True)
        self.detail_layout.addWidget(title)
        meta = [("Studio", scene.studio), ("Date", scene.scene_date.isoformat() if scene.scene_date else ""),
                ("Performers", ", ".join(scene.performers)),
                ("Sources", ", ".join(self.sources[s].display_name if s in self.sources else s for s in scene.sources))]
        info = QLabel("<br>".join(f"<b>{k}:</b> {v}" for k, v in meta if v))
        info.setWordWrap(True)
        self.detail_layout.addWidget(info)
        if scene.key in self.owned:
            own = QLabel("✔ In library:<br>" + "<br>".join(p.name for p in self.owned[scene.key]))
            own.setWordWrap(True)
            own.setStyleSheet("color:#22c55e")
            self.detail_layout.addWidget(own)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        self.detail_layout.addWidget(line)
        self.detail_layout.addWidget(QLabel("<b>Releases</b>"))
        for res in scene.resolutions or [""]:
            for rel in sorted(scene.releases_for(res), key=self._source_priority):
                self.detail_layout.addWidget(self._release_row(rel))

    def _source_priority(self, r: Release) -> int:
        # Sources listed first in settings are preferred (put premium first).
        ids = [c.id for c in self.settings.sources]
        return ids.index(r.source) if r.source in ids else 99

    def _release_row(self, rel: Release) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 2, 0, 2)
        src = self.sources.get(rel.source)
        name = src.display_name if src else rel.source
        quota_txt = ""
        if src and src.daily_quota:
            quota_txt = f" · {self.quota.remaining(src.id, src.daily_quota)} left today"
        lbl = QLabel(f"<b>{rel.resolution or '?'}</b> — {name}"
                     f"{' · ' + fmt_bytes(rel.size_bytes) if rel.size_bytes else ''}{quota_txt}")
        lbl.setToolTip(rel.raw_title)
        h.addWidget(lbl, 1)
        btn_dl = QPushButton("Download")
        btn_dl.clicked.connect(lambda: self.start_download(rel))
        btn_open = QPushButton("Post")
        btn_open.clicked.connect(lambda: webbrowser.open(rel.post_url))
        h.addWidget(btn_dl)
        h.addWidget(btn_open)
        return w

    # ------------------------------------------------------------ downloads
    def start_download(self, rel: Release) -> None:
        src = self.sources.get(rel.source)
        if src is None:
            return
        if src.requires_login and not src.is_logged_in():
            QMessageBox.information(self, "Login required", f"Log in to {src.display_name} in Settings first.")
            return
        if not rel.details_loaded:
            self.status_label.setText("Loading post details…")
            run_async(src.load_details, rel, on_done=self._enqueue,
                      on_fail=lambda e: QMessageBox.warning(self, "Error", e.splitlines()[0]))
        else:
            self._enqueue(rel)

    def _enqueue(self, rel: Release) -> None:
        if not rel.links:
            QMessageBox.information(self, "No links", "No downloadable links found on this post.")
            return
        src = self.sources.get(rel.source)
        if src and src.daily_quota:
            if not self.quota.try_consume(src.id, src.daily_quota):
                wait = self.quota.seconds_until_next(src.id)
                QMessageBox.warning(self, "Daily limit reached",
                                    f"{src.display_name}: no actions left. Next one frees up in "
                                    f"{int(wait // 3600)}h {int(wait % 3600 // 60)}m.")
                return
        link = min(rel.links, key=lambda l: LINK_PREFERENCE.get(l.kind, 9))
        self.downloads.enqueue(rel, link, Path(self.settings.download_dir))
        self._update_quota_label()
        self.tabs.setTabText(1, f"Downloads ({len(self.downloads.jobs)})")
        if self.current:
            self.show_scene(self.current)

    def _on_download_finished(self, job: Job) -> None:
        """Runs on the download thread."""
        if self.settings.auto_sort_completed and job.path:
            job.final_path = sort_into_library(job.path, job.release, Path(self.settings.library_dir),
                                               self.settings.rename_template)

    def _job_changed(self, job: Job) -> None:
        row = self.job_rows.get(job.id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.job_rows[job.id] = row
        r = job.release
        prog = f"{job.progress * 100:.0f}%  {fmt_bytes(job.bytes_done)}/{fmt_bytes(job.bytes_total)}"
        if job.speed_bps:
            prog += f"  {fmt_bytes(job.speed_bps)}/s"
        status = job.state.value + (f": {job.error}" if job.error else "")
        dest = str(job.final_path or job.path or job.dest_dir)
        for col, val in enumerate([r.title or r.raw_title, r.resolution, r.source, status, prog, dest]):
            item = QTableWidgetItem(val)
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            self.table.setItem(row, col, item)
        if job.state == State.DONE and job.final_path:
            self.rescan_library()

    def _cancel_selected(self) -> None:
        for idx in self.table.selectionModel().selectedRows():
            self.downloads.cancel(self.table.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole))

    def _update_quota_label(self) -> None:
        parts = [f"{s.display_name}: {self.quota.remaining(s.id, s.daily_quota)}/{s.daily_quota}"
                 for s in self.sources.values() if s.daily_quota]
        self.quota_label.setText("  ".join(parts))

    # ------------------------------------------------------------- settings
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.result_settings()
            self.settings.save()
            self._load_sources()
            self.rescan_library()
            self.refresh()

    # -------------------------------------------------------------- updates
    def check_updates(self, silent: bool) -> None:
        run_async(updater.check_for_update, self.settings.update_repo,
                  on_done=lambda info: self._update_result(info, silent),
                  on_fail=lambda e: None if silent else QMessageBox.warning(self, "Update", e.splitlines()[0]))

    def _update_result(self, info: updater.UpdateInfo | None, silent: bool) -> None:
        if info is None:
            if not silent:
                QMessageBox.information(self, "Up to date", f"You're on the latest version ({__version__}).")
            return
        if not updater.is_frozen():
            QMessageBox.information(self, "Update available",
                                    f"Version {info.version} is available:\n{info.page_url}")
            return
        ans = QMessageBox.question(self, "Update available",
                                   f"SceneSorter {info.version} is available (you have {__version__}).\n\n"
                                   f"{info.notes[:800]}\n\nDownload and restart now?")
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText(f"Downloading update {info.version}…")
        run_async(updater.download_update, info, on_done=self._apply_update,
                  on_fail=lambda e: QMessageBox.warning(self, "Update failed", e.splitlines()[0]))

    def _apply_update(self, path: Path) -> None:
        updater.apply_update_and_restart(path)
        self.close()

    def closeEvent(self, ev) -> None:  # noqa: N802
        self.downloads.shutdown()
        super().closeEvent(ev)

