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
from ..status import DONE, FINISHED, GRABBED, preferred_release, scene_status, source_order, target_release
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
        act_refresh = QAction("⟳ Refresh", self, triggered=self.refresh)
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
        self.act_login = QAction("Login…", self, triggered=self._open_source_login)
        self.act_login.setToolTip("Log in or re-login to EncodedFapFiles via Telegram OTP")
        tb.addAction(self.act_login)

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
        dirs = [str(Settings.resolve(d)) for d in self.settings.import_dirs if d.strip()]
        if self.settings.scan_library_dir:
            dirs.append(str(Settings.resolve(self.settings.library_dir)))
        dirs = [d for d in dirs if Path(d).is_dir()]
        if dirs:
            self.import_paths(dirs, interactive=interactive, incremental=True)

    def import_paths(self, paths: list[str], interactive: bool, incremental: bool = False) -> None:
        """Match video files by name and flag their releases as downloaded (verified)."""
        if self._import_running or not self.scenes:
            if interactive:
                QMessageBox.information(self, "Import", "Still loading scenes or another scan is running — "
                                                        "try again in a moment.")
            return
        self._import_running = True
        known = self.db.scanned(matched_only=True) if incremental else None
        if interactive:
            self.status_label.setText("Scanning files…")
        run_async(scan_files, paths, list(self.scenes), known, 60 if incremental else 0,
                  on_done=lambda r: self._import_done(r, interactive),
                  on_fail=lambda e: self._import_failed(e, interactive))

    def _import_failed(self, err: str, interactive: bool) -> None:
        self._import_running = False
        if interactive:
            QMessageBox.warning(self, "Import failed", err.splitlines()[0])

    def _import_done(self, res, interactive: bool) -> None:
        self._import_running = False
        self.db.save_scanned(res.rows())
        new = 0
        for path, m in res.matched:
            if self.flags.get((m.release.source, m.release.post_id)) != VERIFIED:
                new += 1
            self.db.set_flag(m.release.source, m.release.post_id, VERIFIED, path=str(path))
        if res.matched:
            self._flags_changed()
        if interactive:
            self.status_label.setText(f"Import: {len(res.matched)} matched, {len(res.unmatched)} not matched")
            msg = QMessageBox(self)
            msg.setWindowTitle("Import")
            msg.setText(f"Matched {len(res.matched)} file(s) to scenes and marked them as downloaded "
                        f"({new} newly).\n{len(res.unmatched)} file(s) didn't match anything.")
            if res.unmatched or res.matched:
                lines = [f"✓ {p.name}  →  {m.scene.title or m.scene.primary.raw_title} "
                         f"[{m.release.resolution} {m.release.release_group}]".rstrip() for p, m in res.matched]
                lines += [f"✗ {p.name}" for p in res.unmatched]
                msg.setDetailedText("\n".join(lines))
            msg.exec()
        elif new:
            self.status_label.setText(f"Import folders: {new} new file(s) matched and marked as downloaded")

    def _update_studio_chip(self) -> None:
        """Show the studio's logo in the filter chip when we have one, else its name."""
        key, name = self.studio_filter
        if not key:
            return
        name = self.model.studio_names.get(key, name)
        pm = self.logos.get(key, name)
        self.studio_btn.setToolTip(f"Showing only {name} — click to show all studios")
        if pm and not pm.isNull():
            self.studio_btn.setIcon(pm.scaled(96, 22, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
            self.studio_btn.setText("  ✕")
        else:
            self.studio_btn.setIcon(QPixmap())
            self.studio_btn.setText(f"Studio: {name}  ✕")

    def _studio_info_ready(self, key: str) -> None:
        name = self.db.studio_names().get(key)
        if name and self.model.studio_names.get(key) != name:
            self.model.studio_names[key] = name
            self.model.repaint_all()
        if key == self.studio_filter[0]:
            self._update_studio_chip()

    def _update_tab_counts(self) -> None:
        n = sum(1 for s in self.scenes if self.model.state(s.key) != "none" and s.key not in self.model.hidden)
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
        self.delegate.set_sources(list(self.sources))
        self.src_box.blockSignals(True)
        self.src_box.clear()
        self.src_box.addItem("All sources", "")
        for sid, src in self.sources.items():
            self.src_box.addItem(src.display_name, sid)
        self.src_box.blockSignals(False)
        self._update_quota_label()
        # Kick off a background quota refresh for sources that support it
        QTimer.singleShot(500, self._refresh_all_quotas)

    def _refresh_all_quotas(self) -> None:
        for src in self.sources.values():
            if hasattr(src, "refresh_quota") and src.is_logged_in():
                run_async(src.refresh_quota, on_done=lambda _=None: self._update_quota_label())

    def _open_source_login(self) -> None:
        """Open the Telegram OTP login dialog for EncodedFapFiles (even if already logged in)."""
        for src in self.sources.values():
            if hasattr(src, "request_otp") and hasattr(src, "verify_otp"):
                dlg = EncodedFapLoginDialog(src, self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    # Persist token
                    for cfg in self.settings.sources:
                        if cfg.id == src.id:
                            cfg.options = dict(src.options)
                            break
                    self.settings.save()
                    if hasattr(src, "refresh_quota"):
                        src.refresh_quota()
                    self._update_quota_label()
                    self.status_label.setText(f"Logged in to {src.display_name}")
                return
        QMessageBox.information(self, "No login source", "No EncodedFapFiles source found. Add it in Settings first.")

    def _load_cached(self) -> None:
        """Show everything already in the local DB without touching the network."""
        self.releases = {(r.source, r.post_id): r for r in self.db.load_releases(list(self.sources))}
        self._rebuild()
        self.status_label.setText(f"{len(self.releases)} posts loaded from the local cache")

    def refresh(self) -> None:
        """Incremental refresh: fetch newest pages until a page has nothing new."""
        if not self.sources:
            self.browse.show_message("No sources configured yet. Open Settings ▸ Sources to add one.")
            return
        for sid in self.sources:
            self._fetch(sid, 1, "refresh")

    def _fetch(self, sid: str, page: int, mode: str) -> None:
        """mode: 'refresh' (newest pages until nothing new) or 'crawl' (backfill one older page)."""
        if sid in self.in_flight or time.time() < self.backoff_until.get(sid, 0):
            return
        src = self.sources[sid]
        self.in_flight.add(sid)
        if mode == "refresh":
            self.status_label.setText(f"Checking {src.display_name} for new posts (page {page})…")

        def job():
            try:
                return src.list_page(page)
            except RateLimited as e:
                return e

        run_async(job, on_done=lambda res: self._page_loaded(sid, page, res, mode),
                  on_fail=lambda err: self._page_failed(sid, page, err))

    # ------------------------------------------------------------ crawler
    def _apply_timers(self) -> None:
        self.crawl_timer.start(max(5, self.settings.crawl_interval_s) * 1000)
        if self.settings.auto_refresh_minutes > 0:
            self.refresh_timer.start(self.settings.auto_refresh_minutes * 60_000)
        else:
            self.refresh_timer.stop()
        self._update_crawl_label()

    def _toggle_crawl(self, on: bool) -> None:
        self.settings.crawl_enabled = on
        self.settings.save()
        self._update_crawl_label()

    def _crawl_done(self, sid: str) -> bool:
        return self.db.get_meta(f"crawl_done:{sid}") == "1"

    def _crawl_tick(self) -> None:
        """One page request per tick, round-robin over sources that aren't fully cached."""
        if not self.settings.crawl_enabled:
            self._update_crawl_label()
            return
        pending = [sid for sid in self.sources if not self._crawl_done(sid)]
        for k in range(len(pending)):
            sid = pending[(self._crawl_rr + k) % len(pending)]
            if sid in self.in_flight or time.time() < self.backoff_until.get(sid, 0):
                continue
            self._crawl_rr = (self._crawl_rr + k + 1) % len(pending)
            self._fetch(sid, int(self.db.get_meta(f"backfill_page:{sid}", "0")) + 1, "crawl")
            break
        self._update_crawl_label()

    def _update_crawl_label(self) -> None:
        parts = []
        for sid, src in self.sources.items():
            if self._crawl_done(sid):
                parts.append(f"{src.display_name}: fully cached")
                continue
            done = int(self.db.get_meta(f"backfill_page:{sid}", "0"))
            total = src.total_pages or self.db.get_meta(f"total_pages:{sid}")
            wait = self.backoff_until.get(sid, 0) - time.time()
            txt = f"{src.display_name}: {done}/{total or '?'} pages cached"
            if wait > 0:
                txt += f" (waiting {int(wait)}s)"
            elif not self.settings.crawl_enabled:
                txt += " (paused)"
            parts.append(txt)
        self.crawl_label.setText("  ·  ".join(parts))

    def _backoff(self, sid: str, seconds: float | None) -> None:
        n = self.fail_count.get(sid, 0) + 1
        self.fail_count[sid] = n
        delay = seconds or min(3600, self.settings.crawl_interval_s * (2 ** n))
        self.backoff_until[sid] = time.time() + delay

    def _page_loaded(self, sid: str, page: int, rels, mode: str) -> None:
        self.in_flight.discard(sid)
        src = self.sources.get(sid)
        if src is None:
            return
        if isinstance(rels, RateLimited):
            self._backoff(sid, rels.retry_after)
            self.status_label.setText(f"{src.display_name} asked us to slow down — pausing.")
            self._update_crawl_label()
            return
        self.fail_count[sid] = 0
        new = self.db.upsert_releases(rels) if rels else 0
        for r in rels:
            self.releases[(r.source, r.post_id)] = r
        if src.total_pages:
            self.db.set_meta(f"total_pages:{sid}", str(src.total_pages))
        backfill = int(self.db.get_meta(f"backfill_page:{sid}", "0"))
        if mode == "refresh" and rels and page == backfill + 1 and not self._crawl_done(sid):
            self.db.set_meta(f"backfill_page:{sid}", str(page))  # refresh pages count as crawled too
        if mode == "crawl":
            if rels:
                self.db.set_meta(f"backfill_page:{sid}", str(page))
                self.status_label.setText(f"Cached {src.display_name} page {page} ({new} new) · "
                                          f"{len(self.releases)} posts in local DB")
            if not rels or (src.total_pages and page >= src.total_pages):
                self.db.set_meta(f"crawl_done:{sid}", "1")
                self.status_label.setText(f"{src.display_name}: every page is now cached.")
        elif rels and new == len(rels) and page < self.settings.max_refresh_pages:
            self.status_label.setText(f"{src.display_name}: {new} new post(s) on page {page}, checking further…")
            self._fetch(sid, page + 1, "refresh")
        else:
            self.status_label.setText(f"{src.display_name}: up to date · {len(self.releases)} posts cached")
        if new:
            self._schedule_rebuild()
        self._update_crawl_label()

    def _page_failed(self, sid: str, page: int, err: str) -> None:
        self.in_flight.discard(sid)
        self._backoff(sid, None)
        first = err.splitlines()[0] if err else "unknown error"
        self.status_label.setText(f"{sid}: page {page} failed ({first}) — retrying later")
        self._update_crawl_label()

        # If this is an EncodedFapFiles source that needs login, open the dialog
        src = self.sources.get(sid)
        if src and hasattr(src, "request_otp") and hasattr(src, "verify_otp"):
            if "login" in first.lower() or "not logged in" in first.lower() or "auth" in first.lower():
                QTimer.singleShot(100, lambda: self._open_source_login_for(src))

    def _open_source_login_for(self, src) -> None:
        """Open the Telegram OTP dialog for a specific source and persist the result."""
        dlg = EncodedFapLoginDialog(src, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for cfg in self.settings.sources:
                if cfg.id == src.id:
                    cfg.options = dict(src.options)
                    break
            self.settings.save()
            if hasattr(src, "refresh_quota"):
                src.refresh_quota()
            self._update_quota_label()
            self.status_label.setText(f"Logged in to {src.display_name}")
            # Optionally kick a refresh so the user immediately sees results
            QTimer.singleShot(300, lambda: self._fetch(src.id, 1, "refresh"))

    # ------------------------------------------------------------ rebuild
    def _schedule_rebuild(self) -> None:
        if self._rebuild_running:
            self._rebuild_dirty = True
        elif not self.rebuild_timer.isActive():
            self.rebuild_timer.start()

    def _rebuild(self) -> None:
        self._schedule_rebuild()

    def _start_rebuild(self) -> None:
        """Merge off the UI thread; 40k+ posts take about a second."""
        self._rebuild_running = True
        self._rebuild_dirty = False
        snapshot = list(self.releases.values())
        run_async(merge_releases, snapshot, on_done=self._apply_rebuild,
                  on_fail=lambda e: self._apply_rebuild(None, e))

    def _apply_rebuild(self, scenes, err: str | None = None) -> None:
        self._rebuild_running = False
        if scenes is not None:
            self.scenes = scenes
            self.model.studio_names = self.db.studio_names()
            self.model.covers = self._covers()
            self.model.status, self.model.hidden = self._scene_status(), self._hidden_scene_keys()
            self.model.set_scenes(scenes, set(self.owned))
            self._update_group_box()
            self._update_tab_counts()
            self.studios.set_scenes(scenes, self.model.has, bool(self.logos.boxes))
            self.studios.set_hidden(set(self.hidden_studios))
            self._drop_bad_matches()
            self.matcher.prefetch(scenes)
            self.perf_timer.start()
        elif err:
            self.status_label.setText(f"Merge failed: {err.splitlines()[0]}")
        if self._rebuild_dirty:
            self._schedule_rebuild()

    # ------------------------------------------------------- status/hidden
    def _scene_status(self) -> dict[str, str]:
        pref = self.settings.preferred_resolution
        out = {}
        for s in self.scenes:
            st = scene_status(s, self.flags, pref)
            if st != "none":
                out[s.key] = st
        return out

    def _hidden_scene_keys(self) -> set[str]:
        hs = self.hidden_studios
        return {s.key for s in self.scenes
                if s.key in self.hidden_keys or normalise_studio(s.studio) in hs
                or any((r.source, r.post_id) in self.hidden_posts for r in s.releases)}

    def _scene_hidden(self, s: Scene) -> bool:
        """Hidden as a scene (not just because its studio is hidden)."""
        return s.key in self.hidden_keys or any((r.source, r.post_id) in self.hidden_posts for r in s.releases)

    def _flags_changed(self) -> None:
        self.flags = self.db.flags()
        self.model.set_status(status=self._scene_status())
        for pane in self.panes:
            pane.refresh_detail()
        self._update_tab_counts()
        self.studios.set_scenes(self.scenes, self.model.has, bool(self.logos.boxes))

    def _hidden_changed(self) -> None:
        self.hidden_keys, self.hidden_posts, self.hidden_studios = self.db.hidden()
        self.model.set_status(hidden=self._hidden_scene_keys())
        self.studios.set_hidden(set(self.hidden_studios))
        self._update_tab_counts()

    def mark_downloaded(self, releases: list[Release], on: bool) -> None:
        """User action: flag releases as done, or clear their flags (= wanted again)."""
        if on:
            for r in releases:
                self.db.set_flag(r.source, r.post_id, DONE, force=True)
        else:
            self.db.clear_flags([(r.source, r.post_id) for r in releases])
        self._flags_changed()

    def mark_scenes(self, scenes: list[Scene], on: bool) -> None:
        if on:
            rels = [target_release(s, self.settings.preferred_resolution, self._source_priority) for s in scenes]
        else:
            rels = [r for s in scenes for r in s.releases]
        self.mark_downloaded(rels, on)

    def hide_scenes(self, scenes: list[Scene], hide: bool) -> None:
        for s in scenes:
            self.db.hide_scene(s.key, s.title or s.primary.raw_title,
                               [(r.source, r.post_id) for r in s.releases], hide)
        self._hidden_changed()

    def hide_studio(self, key: str, name: str, hide: bool) -> None:
        self.db.hide_studio(key, name, hide)
        if hide and self.studio_filter[0] == key:
            self.filter_studio("", "")
        self._hidden_changed()
        self.status_label.setText(f"{'Hid' if hide else 'Unhid'} all scenes from {name}"
                                  + (" — tick “Show hidden” to see them" if hide else ""))

    def _scene_menu(self, scenes: list[Scene], pos) -> None:
        if not scenes:
            return
        one = scenes[0] if len(scenes) == 1 else None
        what = "scene" if one else f"{len(scenes)} scenes"
        menu = QMenu(self)
        states = {self.model.state(s.key) for s in scenes}
        if states != {"complete"} or one:
            menu.addAction(f"⬇ Download preferred{'' if one else f' ({len(scenes)} scenes)'}",
                           lambda: self.download_preferred(scenes))
            menu.addSeparator()
        if states != {"complete"}:
            menu.addAction(f"Mark {what} as downloaded", lambda: self.mark_scenes(scenes, True))
        if states != {"none"}:
            menu.addAction(f"Mark {what} as not downloaded (wanted)", lambda: self.mark_scenes(scenes, False))
        menu.addSeparator()
        hidden = all(self._scene_hidden(s) for s in scenes)
        menu.addAction(f"{'Unhide' if hidden else 'Hide'} {what}", lambda: self.hide_scenes(scenes, not hidden))
        studios = {normalise_studio(s.studio): s for s in scenes if s.studio}
        for skey, s in list(studios.items())[:3]:
            name = self.model.display_studio(s)
            sh = skey in self.hidden_studios
            menu.addAction(f"{'Unhide' if sh else 'Hide'} all from studio “{name}”",
                           lambda k=skey, n=name, h=sh: self.hide_studio(k, n, not h))
        if one and one.studio:
            menu.addSeparator()
            sk, sn = normalise_studio(one.studio), self.model.display_studio(one)
            sf = sk in self.fav["studio"]
            menu.addAction(f"{'★ Remove' if sf else '☆ Add'} studio “{sn}” {'from' if sf else 'to'} favorites",
                           lambda: self.set_favorite("studio", sk, sn, not sf))
            menu.addAction(f"Show only “{self.model.display_studio(one)}”",
                           lambda: self.filter_studio(normalise_studio(one.studio), self.model.display_studio(one)))
        if one:
            menu.addAction("Open post in browser", lambda: webbrowser.open(one.primary.post_url))
        menu.exec(pos)

    def _update_group_box(self) -> None:
        current = self.group_box.currentData() or ""
        counts: dict[str, int] = {}
        for r in self.releases.values():
            if r.release_group:
                counts[r.release_group] = counts.get(r.release_group, 0) + 1
        prefs = [g for g in self.settings.preferred_groups if g in counts]
        others = sorted((g for g in counts if g not in prefs), key=lambda g: -counts[g])
        self.group_box.blockSignals(True)
        self.group_box.clear()
        self.group_box.addItem("All groups", "")
        for g in prefs + others:
            self.group_box.addItem(f"{'★ ' if g in prefs else ''}{g} ({counts[g]})", g)
        idx = self.group_box.findData(current)
        self.group_box.setCurrentIndex(max(idx, 0))
        self.group_box.blockSignals(False)

    def rescan_library(self) -> None:
        lib = Settings.resolve(self.settings.library_dir)
        run_async(scan_library, lib, on_done=self._library_scanned)

    def _library_scanned(self, owned: dict) -> None:
        self.owned = owned
        self.model.set_status(owned=set(owned))

    # --------------------------------------------------------------- detail
    def render_detail(self, pane: ScenePane, scene: Scene) -> None:
        lay = pane.detail_layout
        thumb = QLabel()
        thumb.setFixedHeight(210)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = self.thumbs.get(scene.key, self.model.thumb_urls(scene))
        if pm:
            thumb.setPixmap(pm.scaled(360, 210, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(thumb)

        title = QLabel(f"<h3>{scene.title or scene.primary.raw_title}</h3>")
        title.setWordWrap(True)
        lay.addWidget(title)
        studio_name = self.model.display_studio(scene)
        skey = normalise_studio(scene.studio) if scene.studio else ""
        if scene.studio:
            sfav = skey in self.fav["studio"]
            studio = QLabel(f"<a href='fav' style='color:{'#fbbf24' if sfav else '#6b7280'};text-decoration:none'>"
                            f"{'★' if sfav else '☆'}</a> "
                            f"<a href='show' style='color:#60a5fa'>{studio_name}</a>")
            studio.setToolTip("Click the name to show all scenes from this studio; the star toggles favourite")
            studio.linkActivated.connect(lambda l: self.set_favorite("studio", skey, studio_name, not sfav) if l == "fav"
                                         else self.filter_studio(skey, studio_name))
            lay.addWidget(studio)
        perf_keys = self.model.scene_performers.get(scene.key, [])
        if perf_keys:
            favs = self.fav["performer"]
            links = ", ".join(
                f"<a href='fav:{k}' style='color:{'#fbbf24' if k in favs else '#6b7280'};text-decoration:none'>"
                f"{'★' if k in favs else '☆'}</a>"
                f"<a href='{k}' style='color:#60a5fa'>{self.model.performer_names.get(k, k)}</a>" for k in perf_keys)
            perf = QLabel(f"<b>Performers:</b> {links}")
            perf.setWordWrap(True)
            perf.setToolTip("Click a name to show their scenes; the star toggles favourite")

            def perf_link(link: str) -> None:
                k = link[4:] if link.startswith("fav:") else link
                name = self.model.performer_names.get(k, k)
                if link.startswith("fav:"):
                    self.set_favorite("performer", k, name, k not in self.fav["performer"])
                else:
                    self.filter_performer(k, name)
            perf.linkActivated.connect(perf_link)
            lay.addWidget(perf)
        found = self.matches.get(scene.key, [])
        if found:
            parts = []
            for m in found:
                base = m["endpoint"].rsplit("/graphql", 1)[0]
                box = next((b.name for b in self.settings.stashboxes if b.endpoint == m["endpoint"]), base)
                parts.append(f"<a href='{base}/scenes/{m['stash_id']}' style='color:#60a5fa'>{box}</a>")
            ml = QLabel("<b>Matched on:</b> " + " · ".join(parts) + f"<br><i>{found[0]['title']}</i>")
            ml.setWordWrap(True)
            ml.setOpenExternalLinks(True)
            lay.addWidget(ml)
        meta = [("Date", scene.scene_date.isoformat() if scene.scene_date else ""),
                ("Posted", scene.posted.isoformat() if scene.posted else ""),
                ("Duration", fmt_duration(scene.duration_s or next((m["duration"] for m in found if m["duration"]), None))),
                ("Groups", ", ".join(scene.release_groups)),
                ("Tags", ", ".join(scene.tags)),
                ("Sources", ", ".join(self.sources[s].display_name if s in self.sources else s for s in scene.sources))]
        info = QLabel("<br>".join(f"<b>{k}:</b> {v}" for k, v in meta if v))
        info.setWordWrap(True)
        lay.addWidget(info)

        flagged = [r for r in scene.releases if (r.source, r.post_id) in self.flags]
        state = self.model.state(scene.key)
        if scene.key in self.owned:
            own = QLabel("✔ In library:<br>" + "<br>".join(p.name for p in self.owned[scene.key]))
            own.setWordWrap(True)
            own.setStyleSheet("color:#22c55e")
            lay.addWidget(own)
        if state in ("partial", "grabbed"):
            note = QLabel("◐ Partial — " + ("a lower resolution finished, the wanted one hasn't"
                                            if state == "partial" else "grabbed, nothing finished yet")
                          + ". Still wanted.")
            note.setWordWrap(True)
            note.setStyleSheet("color:#f59e0b")
            lay.addWidget(note)
        elif state == "complete" and scene.key not in self.owned:
            done = QLabel("✔ Downloaded")
            done.setStyleSheet("color:#60a5fa")
            lay.addWidget(done)
        if scene.key in self.model.hidden:
            hid = QLabel("Hidden (blacklisted scene or studio)")
            hid.setStyleSheet("color:#f87171")
            lay.addWidget(hid)
        complete = state == "complete"
        best = self._preferred(scene)
        src = self.sources.get(best.source)
        bits = " · ".join(x for x in [best.resolution, best.release_group, src.display_name if src else best.source] if x)
        dl = QPushButton(f"⬇  Download preferred  ({bits})")
        dl.setStyleSheet("QPushButton { background:#2563eb; color:white; padding:6px; border-radius:4px; font-weight:bold }"
                         "QPushButton:hover { background:#3b82f6 }")
        dl.setToolTip("Picks your wanted resolution, then preferred release group, then preferred source "
                      "(Settings ▸ General)." + ("\nAlready downloaded — this grabs it again." if complete else ""))
        dl.clicked.connect(lambda: self.download_preferred([scene]))
        lay.addWidget(dl)
        mark = QPushButton("Mark as not downloaded" if complete else "Mark as downloaded")
        mark.setToolTip("Clear every flag on this scene so it's wanted again" if complete else
                        f"Flag the {self.settings.preferred_resolution or 'best'} release as downloaded")
        mark.clicked.connect(lambda: self.mark_scenes([scene], not complete))
        lay.addWidget(mark)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        lay.addWidget(QLabel("<b>Releases</b>"))
        for res in scene.resolutions or [""]:
            for rel in sorted(scene.releases_for(res), key=self._source_priority):
                lay.addWidget(self._release_row(rel))

    def _source_order(self) -> list[str]:
        return source_order([c.id for c in self.settings.sources], self.settings.preferred_source)

    def _source_priority(self, r: Release) -> tuple[int, int]:
        # Preferred release groups first, then the preferred source, then sources in settings order.
        groups = self.settings.preferred_groups
        ids = self._source_order()
        return (groups.index(r.release_group) if r.release_group in groups else 99,
                ids.index(r.source) if r.source in ids else 99)

    def _preferred(self, scene: Scene) -> Release:
        return preferred_release(scene, self.settings.preferred_resolution, self.settings.preferred_groups,
                                 self._source_order())

    def download_preferred(self, scenes: list[Scene]) -> None:
        """Grab the preferred release of each scene (wanted resolution -> group -> source).

        For several scenes, ones already downloaded at the wanted resolution are skipped."""
        if len(scenes) > 1:
            scenes = [s for s in scenes if self.model.state(s.key) != "complete"]
            if not scenes:
                self.status_label.setText("All selected scenes are already downloaded.")
                return
            if len(scenes) > 10 and QMessageBox.question(
                    self, "Download preferred", f"Start {len(scenes)} downloads?") != QMessageBox.StandardButton.Yes:
                return
        for sc in scenes:
            self.start_download(self._preferred(sc))
        if len(scenes) > 1:
            self.status_label.setText(f"Started {len(scenes)} preferred downloads")

    def _release_row(self, rel: Release) -> QWidget:
        outer = QWidget()
        v = QVBoxLayout(outer)
        v.setContentsMargins(0, 2, 0, 4)
        v.setSpacing(0)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        v.addWidget(w)
        # zero-width spaces let long dotted release names wrap instead of scrolling
        fname = QLabel((rel.filename or rel.raw_title).replace(".", ".\u200b").replace("_", "_\u200b"))
        fname.setStyleSheet("color:#8b95a5; font-size:11px")
        fname.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fname.setWordWrap(True)
        v.addWidget(fname)
        src = self.sources.get(rel.source)
        name = src.display_name if src else rel.source
        quota_txt = ""
        if src and src.daily_quota:
            quota_txt = f" · {self.quota.remaining(src.id, src.daily_quota)} left today"
        grp = rel.release_group or "?"
        grp_html = (f"<span style='color:#f59e0b'><b>{grp}</b></span>"
                    if grp in self.settings.preferred_groups else grp)
        lbl = QLabel(f"<b>{rel.resolution or '?'}</b> · {grp_html} — {name}"
                     f"{' · ' + fmt_bytes(rel.size_bytes) if rel.size_bytes else ''}{quota_txt}")
        lbl.setToolTip(rel.raw_title)
        h.addWidget(lbl, 1)
        btn_dl = QPushButton("Download")
        btn_dl.clicked.connect(lambda: self.start_download(rel))
        btn_open = QPushButton("Post")
        btn_open.clicked.connect(lambda: webbrowser.open(rel.post_url))
        flag = self.flags.get((rel.source, rel.post_id))
        got = flag in FINISHED
        if got:
            btn_dl.setText("Again")
            btn_dl.setToolTip("Already downloaded")
        elif flag == GRABBED:
            btn_dl.setText("Retry")
            btn_dl.setToolTip("Grabbed but not confirmed finished — download again")
        elif flag == "failed":
            btn_dl.setText("Retry")
            btn_dl.setToolTip("The last attempt failed or was cancelled")
        icon, tip = {"done": ("✓", "Done"), "verified": ("✓", "Verified on disk"),
                     "grabbed": ("◐", "Grabbed — not finished yet"),
                     "failed": ("✗", "Failed / cancelled — still wanted")}.get(flag, ("○", "Not downloaded"))
        btn_flag = QPushButton(icon)
        btn_flag.setFixedWidth(30)
        btn_flag.setToolTip(f"{tip}. Click to {'clear' if flag else 'mark as done'}.")
        btn_flag.clicked.connect(lambda: self.mark_downloaded([rel], not flag))
        h.addWidget(btn_flag)
        h.addWidget(btn_dl)
        h.addWidget(btn_open)
        return outer

    # ------------------------------------------------------------ downloads
    def start_download(self, rel: Release) -> None:
        src = self.sources.get(rel.source)
        if src is None:
            return
        if src.requires_login and not src.is_logged_in():
            if hasattr(src, "request_otp") and hasattr(src, "verify_otp"):
                dlg = EncodedFapLoginDialog(src, self)
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    return
                # Persist the new token
                for cfg in self.settings.sources:
                    if cfg.id == src.id:
                        cfg.options = dict(src.options)
                        break
                self.settings.save()
                if hasattr(src, "refresh_quota"):
                    src.refresh_quota()
                self._update_quota_label()
            else:
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
        self.downloads.enqueue(rel, link, Settings.resolve(self.settings.download_dir))
        self.db.set_flag(rel.source, rel.post_id, GRABBED)
        self._update_quota_label()
        self.tabs.setTabText(self.QUEUE_TAB, f"Queue ({len(self.downloads.jobs)})")
        self._flags_changed()

    def _on_download_finished(self, job: Job) -> None:
        """Runs on the download thread."""
        if self.settings.auto_sort_completed and job.path:
            job.final_path = sort_into_library(job.path, job.release, Settings.resolve(self.settings.library_dir),
                                               self.settings.rename_template, rename=self.settings.rename_files)

    def _job_changed(self, job: Job) -> None:
        self.db.log_download(job)
        new_flag = {State.DONE: DONE, State.FAILED: "failed", State.CANCELLED: "failed"}.get(job.state)
        if new_flag:  # set_flag never knocks a finished release back to failed
            self.db.set_flag(job.release.source, job.release.post_id, new_flag,
                             path=str(job.final_path or job.path or "") or None)
            self._flags_changed()
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
        res = f"{r.resolution} {r.release_group}".strip()
        for col, val in enumerate([r.title or r.raw_title, res, r.source, status, prog, dest]):
            item = QTableWidgetItem(val)
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            self.table.setItem(row, col, item)
        if job.state == State.DONE and job.final_path:
            self.rescan_library()

    def _cancel_selected(self) -> None:
        for idx in self.table.selectionModel().selectedRows():
            self.downloads.cancel(self.table.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole))

    def _apply_blackhole(self) -> None:
        d = self.settings.torrent_blackhole_dir.strip()
        self.downloads.blackhole_dir = Settings.resolve(d) if d else None
        self.downloads.magnets_to_blackhole = self.settings.magnets_to_blackhole

    def _update_quota_label(self) -> None:
        parts = []
        for s in self.sources.values():
            if not s.daily_quota:
                continue
            # Prefer live remote remaining when the source provides it
            rem = getattr(s, "remote_remaining", None)
            if rem is not None:
                parts.append(f"{s.display_name}: {rem}/{s.daily_quota} left")
            else:
                parts.append(f"{s.display_name}: {self.quota.remaining(s.id, s.daily_quota)}/{s.daily_quota}")
        self.quota_label.setText("  ".join(parts))

    # ------------------------------------------------------------- settings
    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.settings = dlg.result_settings()
            self.settings.save()
            self.delegate.preferred_groups = self.settings.preferred_groups
            self.logos.configure(self.settings.stashboxes)
            self._apply_image_headers()
            self.matcher.configure(self.settings.stashboxes, self.settings.scene_matching,
                                   self.settings.match_interval_s)
            self.perf_images.configure(self.settings.stashboxes)
            self._apply_import_timer()
            self._apply_blackhole()
            self.act_crawl.setChecked(self.settings.crawl_enabled)
            self._load_sources()
            self._apply_timers()
            self._load_cached()
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
        self.db.close()
        super().closeEvent(ev)

