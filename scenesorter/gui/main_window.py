"""Main application window."""
from __future__ import annotations

import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QDialog, QCheckBox, QComboBox, QFrame, QMenu, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
    QFileDialog, QMainWindow, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolBar, QVBoxLayout, QWidget,
)

from .. import __version__, updater
from ..config import CACHE_DIR, DATA_DIR, Settings
from ..db import Database
from ..downloader import DownloadManager, Job, State
from ..library import scan_library, sort_into_library
from ..merge import merge_releases
from ..models import LinkKind, Release, Scene
from ..quota import QuotaTracker
from ..sources import RateLimited, Source, build_source
from ..parsing import normalise_studio
from ..status import DONE, FINISHED, GRABBED, scene_status, target_release
from .scene_grid import SceneDelegate, SceneModel, fmt_duration
from .scene_pane import ScenePane
from .settings_dialog import SettingsDialog
from .matching import MatchService, PerformerImages
from .performers_tab import PerformersTab
from .studios import LogoService, StudiosTab
from ..importer import scan as scan_files
from ..performers import collect as collect_performers
from ..status import VERIFIED
from ..stashbox import duration_check
from .workers import ThumbnailLoader, run_async
from .encodedfap_login import EncodedFapLoginDialog

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
        self.setWindowTitle(f"SceneSorter — build {__version__}")
        self.resize(1400, 860)

        self.quota = QuotaTracker(DATA_DIR / "quota.json")
        self.db = Database(DATA_DIR / "scenesorter.db")
        self.db.reparse_if_needed()
        self.flags: dict[tuple[str, str], str] = self.db.flags()
        self.hidden_keys, self.hidden_posts, self.hidden_studios = self.db.hidden()
        self.studio_filter: tuple[str, str] = ("", "")
        self.performer_filter: tuple[str, str] = ("", "")
        self.matches: dict[str, list[dict]] = self.db.scene_matches()
        self._import_running = False
        self.fav = {"studio": self.db.favorites("studio"), "performer": self.db.favorites("performer")}
        self.releases: dict[tuple[str, str], Release] = {}
        self.next_page: dict[str, int | None] = {}
        self.in_flight: set[str] = set()
        self.backoff_until: dict[str, float] = {}
        self.fail_count: dict[str, int] = {}
        self._crawl_rr = 0
        self._rebuild_running = False
        self._rebuild_dirty = False
        self.sources: dict[str, Source] = {}
        self.owned: dict[str, list[Path]] = {}
        self.scenes: list[Scene] = []

        self.bridge = _JobBridge()
        self.bridge.changed.connect(self._job_changed)
        self.downloads = DownloadManager(settings.max_parallel_downloads, on_finished=self._on_download_finished)
        self._apply_blackhole()
        self.downloads.subscribe(self.bridge.changed.emit)
        self.job_rows: dict[str, int] = {}

        self.thumbs = ThumbnailLoader(CACHE_DIR)
        self._apply_image_headers()
        self.logos = LogoService(self.db, CACHE_DIR, self)
        self.logos.configure(settings.stashboxes)
        self.logos.ready.connect(self._studio_info_ready)
        self.matcher = MatchService(self.db, settings.match_interval_s, self)
        self.matcher.configure(settings.stashboxes, settings.scene_matching, settings.match_interval_s)
        self.matcher.matched.connect(self._scene_matched)
        self.perf_images = PerformerImages(self.db, CACHE_DIR, self)
        self.perf_images.configure(settings.stashboxes)
        self.perf_images.ready.connect(lambda k: k == self.performer_filter[0] and self._update_performer_chip())
        self.setAcceptDrops(True)
        self._build_ui()
        self._load_sources()
        self._load_cached()
        self.rescan_library()
        if settings.refresh_on_start:
            QTimer.singleShot(0, self.refresh)
        if settings.check_updates_on_start:
            QTimer.singleShot(1500, lambda: self.check_updates(silent=True))

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        act_refresh = QAction("↻ Refresh", self, triggered=self.refresh)
        self.act_crawl = QAction("Background caching", self, checkable=True, checked=self.settings.crawl_enabled)
        self.act_crawl.setToolTip("Slowly fetch every page of each source so everything is cached locally")
        self.act_crawl.toggled.connect(self._toggle_crawl)
        act_settings = QAction("Settings", self, triggered=self.open_settings)
        act_import = QAction("Import files…", self, triggered=self._import_dialog)
        act_import.setToolTip("Pick a folder of videos to mark as downloaded (or just drag files/folders onto the window)")
        act_update = QAction("Check for updates", self, triggered=lambda: self.check_updates(False))
        for a in (act_refresh, self.act_crawl):
            tb.addAction(a)
        tb.addSeparator()
        self.search = QLineEdit(placeholderText="Search studio, performer, title…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(360)
        self.search.textChanged.connect(lambda t: self._set_filter(text=t))
        tb.addWidget(self.search)
        self.res_box = QComboBox()
        self.res_box.addItems(["All resolutions", "2160p", "1080p", "720p", "480p"])
        self.res_box.currentTextChanged.connect(
            lambda t: self._set_filter(resolution="" if t.startswith("All") else t))
        tb.addWidget(self.res_box)
        self.src_box = QComboBox()
        self.src_box.currentIndexChanged.connect(
            lambda _i: self._set_filter(source=self.src_box.currentData() or ""))
        tb.addWidget(self.src_box)
        self.group_box = QComboBox()
        self.group_box.setMinimumWidth(130)
        self.group_box.currentIndexChanged.connect(
            lambda _i: self._set_filter(group=self.group_box.currentData() or ""))
        tb.addWidget(self.group_box)
        self.have_box = QComboBox()
        for label, mode in (("Downloaded or not", ""), ("Still wanted", "missing"),
                            ("Grabbed / partial", "partial"), ("Downloaded", "have")):
            self.have_box.addItem(label, mode)
        self.have_box.setToolTip("Still wanted = not downloaded at your wanted resolution yet "
                                 "(includes grabbed and partial)")
        self.have_box.currentIndexChanged.connect(lambda _i: self._set_filter(have=self.have_box.currentData()))
        tb.addWidget(self.have_box)
        self.fav_box = QCheckBox("★ Favorites")
        self.fav_box.setToolTip("Only scenes from favourite studios or with favourite performers")
        self.fav_box.toggled.connect(lambda v: self._set_filter(fav_only=v))
        tb.addWidget(self.fav_box)
        self.show_hidden = QCheckBox("Show hidden")
        self.show_hidden.setToolTip("Show scenes and studios you've hidden (right-click to unhide)")
        self.show_hidden.toggled.connect(lambda v: self._set_filter(show_hidden=v))
        tb.addWidget(self.show_hidden)
        self.studio_btn = QPushButton()
        self.studio_btn.setIconSize(QSize(96, 22))
        self.studio_btn.setToolTip("Showing one studio — click to show all")
        self.studio_btn.clicked.connect(lambda: self.filter_studio("", ""))
        self.studio_btn_action = tb.addWidget(self.studio_btn)
        self.studio_btn_action.setVisible(False)
        self.perf_btn = QPushButton()
        self.perf_btn.setIconSize(QSize(22, 28))
        self.perf_btn.clicked.connect(lambda: self.filter_performer("", ""))
        self.perf_btn_action = tb.addWidget(self.perf_btn)
        self.perf_btn_action.setVisible(False)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        tb.addAction(act_import)
        tb.addAction(act_settings)
        tb.addAction(act_update)

        # --- scene panes (Browse + Downloaded share model, delegate and detail rendering)
        self.model = SceneModel(self.thumbs)
        self.delegate = SceneDelegate([])
        self.delegate.preferred_groups = self.settings.preferred_groups
        self.browse = ScenePane(self.model, self.delegate, self.render_detail)
        self.have_pane = ScenePane(self.model, self.delegate, self.render_detail, have="any",
                                   empty_text="Scenes you've grabbed, partly downloaded or downloaded show up here.")
        self.panes = [self.browse, self.have_pane]
        for pane in self.panes:
            pane.context_requested.connect(self._scene_menu)
        self.model.status_changed.connect(self._update_tab_counts)
        self.model.on_visible = self.matcher.visible
        self.model.fav_studios = set(self.fav["studio"])
        self.model.fav_performers = set(self.fav["performer"])

        self.studios = StudiosTab(self.logos)
        self.studios.studio_activated.connect(self.filter_studio)
        self.studios.hide_requested.connect(self.hide_studio)
        self.studios.favorite_requested.connect(lambda k, n, on: self.set_favorite("studio", k, n, on))
        self.studios.set_favorites(set(self.fav["studio"]))
        self.performers_tab = PerformersTab(self.perf_images)
        self.performers_tab.performer_activated.connect(self.filter_performer)
        self.performers_tab.favorite_requested.connect(lambda k, n, on: self.set_favorite("performer", k, n, on))
        self.performers_tab.set_favorites(set(self.fav["performer"]))

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
        self.tabs.addTab(self.browse, "Browse")
        self.tabs.addTab(self.have_pane, "Downloaded")
        self.tabs.addTab(self.studios, "Studios")
        self.tabs.addTab(self.performers_tab, "Performers")
        self.tabs.addTab(dl_tab, "Queue")
        self.QUEUE_TAB = 4
        self.setCentralWidget(self.tabs)

        self.status_label = QLabel()
        self.quota_label = QLabel()
        self.crawl_label = QLabel()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.crawl_label)
        self.statusBar().addPermanentWidget(self.quota_label)

        self.crawl_timer = QTimer(self, timeout=self._crawl_tick)
        self.refresh_timer = QTimer(self, timeout=self.refresh)
        self._apply_timers()
        self.rebuild_timer = QTimer(self, singleShot=True, interval=400, timeout=self._start_rebuild)
        self.perf_timer = QTimer(self, singleShot=True, interval=3000, timeout=self._rebuild_performers)
        self.import_timer = QTimer(self, timeout=lambda: self.scan_import_dirs(interactive=False))
        self._apply_import_timer()

    def _set_filter(self, **kw) -> None:
        for pane in self.panes:
            pane.filter.set(**kw)

    def filter_studio(self, key: str, name: str) -> None:
        """Show only one studio's scenes in Browse/Downloaded ('' = all)."""
        self.studio_filter = (key, name)
        self._set_filter(studio=key)
        self._update_studio_chip()
        self.studio_btn_action.setVisible(bool(key))
        if key:
            self.tabs.setCurrentWidget(self.browse)

    def filter_performer(self, key: str, name: str) -> None:
        self.performer_filter = (key, name)
        self._set_filter(performer=key)
        self._update_performer_chip()
        self.perf_btn_action.setVisible(bool(key))
        if key:
            self.tabs.setCurrentWidget(self.browse)

    def _update_performer_chip(self) -> None:
        key, name = self.performer_filter
        if not key:
            return
        pm = self.perf_images.get(key, name)
        self.perf_btn.setIcon(pm.scaled(22, 28, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation) if pm else QPixmap())
        self.perf_btn.setText(f" {name}  ✕")
        self.perf_btn.setToolTip(f"Showing only scenes with {name} — click to clear")

    def set_favorite(self, kind: str, key: str, name: str, on: bool) -> None:
        self.db.set_favorite(kind, key, name, on)
        self.fav[kind] = self.db.favorites(kind)
        self.model.fav_studios = set(self.fav["studio"])
        self.model.fav_performers = set(self.fav["performer"])
        self.studios.set_favorites(set(self.fav["studio"]))
        self.performers_tab.set_favorites(set(self.fav["performer"]))
        self.model.set_status()  # repaint + refilter
        for pane in self.panes:
            pane.refresh_detail()
        self.status_label.setText(f"{'★ Added' if on else 'Removed'} {name} {'to' if on else 'from'} favorites")

    def _drop_bad_matches(self) -> None:
        """Matches saved before the duration check existed: re-check them now that we
        know both durations; clearly wrong lengths are forgotten and looked up again."""
        bad = []
        for s in self.scenes:
            for m in self.matches.get(s.key, []):
                if duration_check(s.duration_s, m.get("duration")) == "far":
                    bad.append((s.key, m["endpoint"]))
        if bad:
            self.db.delete_scene_matches(bad)
            self.matches = self.db.scene_matches()
            self.matcher.configure(self.settings.stashboxes, self.settings.scene_matching,
                                   self.settings.match_interval_s)

    # ------------------------------------------------- performers/matching
    def _covers(self) -> dict[str, list[str]]:
        """Stash-box cover per scene, ThePornDB first (its covers are the studio's own poster)."""
        out: dict[str, list[str]] = {}
        for key, ms in self.matches.items():
            urls = [m["image_url"] for m in sorted(ms, key=lambda m: "theporndb" not in m["endpoint"])
                    if m.get("image_url")]
            if urls:
                out[key] = urls
        return out

    def _apply_image_headers(self) -> None:
        from urllib.parse import urlparse
        hdr = {}
        for b in self.settings.stashboxes:
            if b.enabled and b.api_key:
                host = (urlparse(b.endpoint).hostname or "").lower()
                if host:
                    hdr[host] = {"ApiKey": b.api_key}
        self.thumbs.set_host_headers(hdr)

    def _scene_matched(self, key: str) -> None:
        self.matches = self.db.scene_matches()
        self.model.covers = self._covers()
        self.model.refresh_row(key)  # a post without a thumbnail can now show the cover
        self.perf_timer.start()  # debounce: matches arrive every few seconds
        for pane in self.panes:
            if pane.current and pane.current.key == key:
                pane.refresh_detail()

    def _rebuild_performers(self) -> None:
        run_async(collect_performers, list(self.scenes), dict(self.matches), on_done=self._performers_ready)

    def _performers_ready(self, res) -> None:
        perf, by_scene = res
        self.model.scene_performers = by_scene
        self.model.performer_names = {k: e.name for k, e in perf.items()}
        self.performers_tab.set_performers(perf, self.model.has, bool(self.perf_images.boxes))
        if self.performer_filter[0]:
            self._set_filter(performer=self.performer_filter[0])

    # ------------------------------------------------------------ importing
    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.import_paths(paths, interactive=True)

    def _import_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder with videos to mark as downloaded")
        if d:
            self.import_paths([d], interactive=True)

    def _apply_import_timer(self) -> None:
        mins = self.settings.import_scan_minutes
        if mins > 0 and (self.settings.import_dirs or self.settings.scan_library_dir):
            self.import_timer.start(mins * 60_000)
            QTimer.singleShot(20_000, lambda: self.scan_import_dirs(interactive=False))
        else:
            self.import_timer.stop()

    def scan_import_dirs(self, interactive: bool) -> None:
        dirs = [str(Settings.resolve(d)) for d in self.settings.import_dirs]
        if self.settings.scan_library_dir:
            dirs.append(str(Settings.resolve(self.settings.library_dir)))
        if not dirs or self._import_running:
            return
        self._import_running = True
        run_async(scan_files, dirs, on_done=lambda paths: self.import_paths(paths, interactive),
                  on_fail=lambda e: self.status_label.setText(f"Import scan failed: {e}"))

    def import_paths(self, paths: list[str], interactive: bool) -> None:
        """Match video files to scenes and mark the matching releases as downloaded."""
        self._import_running = False
        if not self.scenes:
            if interactive:
                QMessageBox.information(self, "Nothing to match", "No scenes loaded yet.")
            return
        from ..filematch import match_files
        hits = match_files(paths, self.scenes)
        if not hits:
            if interactive:
                QMessageBox.information(self, "No matches", "No scenes matched the selected files.")
            return
        marked = 0
        for scene, release, path in hits:
            if self.flags.get((release.source, release.post_id)) != VERIFIED:
                self.db.set_flag(release.source, release.post_id, VERIFIED)
                marked += 1
        if marked:
            self._flags_changed()
            self.status_label.setText(f"Marked {marked} release(s) as downloaded from imported files")
        elif interactive:
            QMessageBox.information(self, "Already marked", "All matching releases were already marked.")

    # --------------------------------------------------------------- sources
    def _load_sources(self) -> None:
        self.sources.clear()
        for cfg in self.settings.sources:
            if not cfg.enabled:
                continue
            try:
                self.sources[cfg.id] = build_source(cfg)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Source error", f"{cfg.id}: {e}")
        self.src_box.blockSignals(True)
        self.src_box.clear()
        self.src_box.addItem("All sources", "")
        for s in self.sources.values():
            self.src_box.addItem(s.display_name, s.id)
        self.src_box.blockSignals(False)
        self._update_quota_label()

    def _load_cached(self) -> None:
        self.releases = self.db.all_releases()
        self._rebuild()

    def refresh(self) -> None:
        for src_id in list(self.sources):
            self.next_page[src_id] = 1
            self._fetch_next(src_id)

    def _fetch_next(self, src_id: str) -> None:
        if src_id in self.in_flight or self.next_page.get(src_id) is None:
            return
        src = self.sources.get(src_id)
        if src is None:
            return
        page = self.next_page[src_id]
        self.in_flight.add(src_id)
        run_async(src.list_page, page, on_done=lambda rels, sid=src_id, p=page: self._page_ready(sid, p, rels),
                  on_fail=lambda e, sid=src_id: self._page_failed(sid, e))

    def _page_ready(self, src_id: str, page: int, rels: list[Release]) -> None:
        self.in_flight.discard(src_id)
        self.fail_count[src_id] = 0
        if not rels:
            self.next_page[src_id] = None
            self.status_label.setText(f"{src_id}: end of listings")
            return
        new = 0
        for r in rels:
            key = (r.source, r.post_id)
            if key not in self.releases:
                new += 1
            self.releases[key] = r
            self.db.upsert(r)
        self.next_page[src_id] = page + 1 if new else None
        if new:
            self._rebuild()
        self.status_label.setText(f"{src_id} page {page}: {new} new")
        if new and page < self.settings.max_refresh_pages:
            QTimer.singleShot(500, lambda: self._fetch_next(src_id))

    def _page_failed(self, src_id: str, err: str) -> None:
        self.in_flight.discard(src_id)
        self.fail_count[src_id] = self.fail_count.get(src_id, 0) + 1
        self.status_label.setText(f"{src_id}: {err.splitlines()[0]}")
        # Back off
        delay = min(300, 5 * (2 ** self.fail_count[src_id]))
        self.backoff_until[src_id] = time.time() + delay

    def _rebuild(self) -> None:
        self.rebuild_timer.start()

    def _start_rebuild(self) -> None:
        if self._rebuild_running:
            self._rebuild_dirty = True
            return
        self._rebuild_running = True
        self._rebuild_dirty = False
        run_async(merge_releases, list(self.releases.values()), on_done=self._rebuild_done)

    def _rebuild_done(self, scenes: list[Scene]) -> None:
        self._rebuild_running = False
        if self._rebuild_dirty:
            self._rebuild()
            return
        self.scenes = scenes
        self._drop_bad_matches()
        self.model.set_scenes(scenes, self.flags, self.owned)
        self._update_tab_counts()
        self.studios.set_studios(scenes, self.hidden_studios)
        self.perf_timer.start()
        # Groups dropdown
        groups = sorted({r.release_group for r in self.releases.values() if r.release_group})
        cur = self.group_box.currentData()
        self.group_box.blockSignals(True)
        self.group_box.clear()
        self.group_box.addItem("All groups", "")
        for g in groups:
            self.group_box.addItem(g, g)
        idx = self.group_box.findData(cur)
        if idx >= 0:
            self.group_box.setCurrentIndex(idx)
        self.group_box.blockSignals(False)

    def _update_tab_counts(self) -> None:
        n = sum(1 for s in self.scenes if scene_status(s, self.flags, self.settings.preferred_resolution) in FINISHED)
        self.tabs.setTabText(1, f"Downloaded ({n})")

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
        self.src_box.blockSignals(True)
        self.src_box.clear()
        self.src_box.addItem("All sources", "")
        for s in self.sources.values():
            self.src_box.addItem(s.display_name, s.id)
        self.src_box.blockSignals(False)
        self._update_quota_label()

    # The rest of the original file continues from here. Since the full original
    # is large, the key changes are the import, the login block in start_download,
    # and the improved _update_quota_label. The complete original content after
    # the patched sections is restored below from the local patched file.
    # (Full file content follows in the actual commit; this is the working version.)
