"""Main application window."""
from __future__ import annotations

import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
    QMainWindow, QMessageBox, QPushButton, QTableWidget,
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
from .scene_grid import SceneDelegate, SceneModel, fmt_duration
from .scene_pane import ScenePane
from .settings_dialog import SettingsDialog
from .studios import LogoService, StudiosTab
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
        self.setWindowTitle(f"SceneSorter — build {__version__}")
        self.resize(1400, 860)

        self.quota = QuotaTracker(DATA_DIR / "quota.json")
        self.db = Database(DATA_DIR / "scenesorter.db")
        self.db.reparse_if_needed()
        self.flags: dict[tuple[str, str], str] = self.db.flags()
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
        self.downloads.subscribe(self.bridge.changed.emit)
        self.job_rows: dict[str, int] = {}

        self.thumbs = ThumbnailLoader(CACHE_DIR)
        self.logos = LogoService(self.db, CACHE_DIR, self)
        self.logos.configure(settings.stashboxes)
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
        for label, mode in (("Downloaded or not", ""), ("Not downloaded", "missing"), ("Downloaded", "have")):
            self.have_box.addItem(label, mode)
        self.have_box.currentIndexChanged.connect(lambda _i: self._set_filter(have=self.have_box.currentData()))
        tb.addWidget(self.have_box)
        self.studio_btn = QPushButton()
        self.studio_btn.setToolTip("Showing one studio — click to show all")
        self.studio_btn.clicked.connect(lambda: self.filter_studio("", ""))
        self.studio_btn_action = tb.addWidget(self.studio_btn)
        self.studio_btn_action.setVisible(False)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        tb.addAction(act_settings)
        tb.addAction(act_update)

        # --- scene panes (Browse + Downloaded share model, delegate and detail rendering)
        self.model = SceneModel(self.thumbs)
        self.delegate = SceneDelegate([])
        self.delegate.preferred_groups = self.settings.preferred_groups
        self.browse = ScenePane(self.model, self.delegate, self.render_detail)
        self.have_pane = ScenePane(self.model, self.delegate, self.render_detail, have="have",
                                   empty_text="Scenes you've downloaded or marked as downloaded show up here.")
        self.panes = [self.browse, self.have_pane]
        self.model.status_changed.connect(self._update_tab_counts)

        self.studios = StudiosTab(self.logos)
        self.studios.studio_activated.connect(self.filter_studio)

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
        self.tabs.addTab(dl_tab, "Queue")
        self.QUEUE_TAB = 3
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

    def _set_filter(self, **kw) -> None:
        for pane in self.panes:
            pane.filter.set(**kw)

    def filter_studio(self, key: str, name: str) -> None:
        """Show only one studio's scenes in Browse/Downloaded ('' = all)."""
        self._set_filter(studio=key)
        self.studio_btn.setText(f"Studio: {name}  ✕")
        self.studio_btn_action.setVisible(bool(key))
        if key:
            self.tabs.setCurrentWidget(self.browse)

    def _update_tab_counts(self) -> None:
        self.tabs.setTabText(1, f"Downloaded ({len(self.model.owned | self.model.downloaded)})")

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
        self.status_label.setText(f"{sid}: page {page} failed ({err.splitlines()[0]}) — retrying later")
        self._update_crawl_label()

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
            self.model.set_scenes(scenes, set(self.owned), self._downloaded_keys())
            self._update_group_box()
            self._update_tab_counts()
            self.studios.set_scenes(scenes, self.model.has, bool(self.logos.boxes))
        elif err:
            self.status_label.setText(f"Merge failed: {err.splitlines()[0]}")
        if self._rebuild_dirty:
            self._schedule_rebuild()

    def _downloaded_keys(self) -> set[str]:
        return {s.key for s in self.scenes if any((r.source, r.post_id) in self.flags for r in s.releases)}

    def _flags_changed(self) -> None:
        self.flags = self.db.flags()
        self.model.set_status(downloaded=self._downloaded_keys())
        for pane in self.panes:
            pane.refresh_detail()
        self.studios.set_scenes(self.scenes, self.model.has, bool(self.logos.boxes))

    def mark_downloaded(self, releases: list[Release], on: bool) -> None:
        if on:
            for r in releases:
                self.db.set_flag(r.source, r.post_id, "manual")
        else:
            self.db.clear_flags([(r.source, r.post_id) for r in releases])
        self._flags_changed()

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
        pm = self.thumbs.get(scene.key, scene.thumbnail_urls)
        if pm:
            thumb.setPixmap(pm.scaled(360, 210, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(thumb)

        title = QLabel(f"<h3>{scene.title or scene.primary.raw_title}</h3>")
        title.setWordWrap(True)
        lay.addWidget(title)
        studio = QLabel(f"<a href='#' style='color:#60a5fa'>{scene.studio}</a>") if scene.studio else None
        if studio:
            studio.setToolTip("Show all scenes from this studio")
            studio.linkActivated.connect(lambda _l: self.filter_studio(normalise_studio(scene.studio), scene.studio))
            lay.addWidget(studio)
        meta = [("Date", scene.scene_date.isoformat() if scene.scene_date else ""),
                ("Posted", scene.posted.isoformat() if scene.posted else ""),
                ("Duration", fmt_duration(scene.duration_s)),
                ("Performers", ", ".join(scene.performers)),
                ("Groups", ", ".join(scene.release_groups)),
                ("Tags", ", ".join(scene.tags)),
                ("Sources", ", ".join(self.sources[s].display_name if s in self.sources else s for s in scene.sources))]
        info = QLabel("<br>".join(f"<b>{k}:</b> {v}" for k, v in meta if v))
        info.setWordWrap(True)
        lay.addWidget(info)

        flagged = [r for r in scene.releases if (r.source, r.post_id) in self.flags]
        if scene.key in self.owned:
            own = QLabel("✔ In library:<br>" + "<br>".join(p.name for p in self.owned[scene.key]))
            own.setWordWrap(True)
            own.setStyleSheet("color:#22c55e")
            lay.addWidget(own)
        if flagged:
            got = QLabel("✔ Downloaded: " + ", ".join(
                f"{r.resolution or '?'} {r.release_group}".strip() for r in flagged))
            got.setStyleSheet("color:#60a5fa")
            lay.addWidget(got)
        mark = QPushButton("Unmark as downloaded" if flagged else "Mark as downloaded")
        mark.setToolTip("Flag this scene as downloaded (e.g. you got it elsewhere)" if not flagged
                        else "Remove the downloaded flag from every release of this scene")
        best = sorted(scene.releases, key=self._source_priority)[:1]
        mark.clicked.connect(lambda: self.mark_downloaded(flagged if flagged else best, not flagged))
        lay.addWidget(mark)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(line)
        lay.addWidget(QLabel("<b>Releases</b>"))
        for res in scene.resolutions or [""]:
            for rel in sorted(scene.releases_for(res), key=self._source_priority):
                lay.addWidget(self._release_row(rel))

    def _source_priority(self, r: Release) -> tuple[int, int]:
        # Preferred release groups first, then sources in settings order (put premium first).
        groups = self.settings.preferred_groups
        ids = [c.id for c in self.settings.sources]
        return (groups.index(r.release_group) if r.release_group in groups else 99,
                ids.index(r.source) if r.source in ids else 99)

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
        got = (rel.source, rel.post_id) in self.flags
        if got:
            btn_dl.setText("Again")
            btn_dl.setToolTip("Already downloaded")
        btn_flag = QPushButton("✓" if got else "○")
        btn_flag.setFixedWidth(30)
        btn_flag.setToolTip("Unmark this release" if got else "Mark this release as downloaded")
        btn_flag.clicked.connect(lambda: self.mark_downloaded([rel], not got))
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
        self.db.set_flag(rel.source, rel.post_id, "started")
        self._update_quota_label()
        self.tabs.setTabText(self.QUEUE_TAB, f"Queue ({len(self.downloads.jobs)})")
        self._flags_changed()

    def _on_download_finished(self, job: Job) -> None:
        """Runs on the download thread."""
        if self.settings.auto_sort_completed and job.path:
            job.final_path = sort_into_library(job.path, job.release, Settings.resolve(self.settings.library_dir),
                                               self.settings.rename_template)

    def _job_changed(self, job: Job) -> None:
        self.db.log_download(job)
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
            self.delegate.preferred_groups = self.settings.preferred_groups
            self.logos.configure(self.settings.stashboxes)
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

