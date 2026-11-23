"""Settings dialog: folders, rename template, downloads, sources."""
from __future__ import annotations

import json
from dataclasses import asdict, replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView, QTableWidget, QTableWidgetItem,
    QCheckBox, QComboBox, QDialog, QGroupBox, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..config import DEFAULT_TEMPLATE, Settings
from ..library import render_name
from ..models import Release
from ..parsing import enrich
from ..sources import SOURCE_TYPES, SourceConfig
from ..stashbox import StashBoxConfig

SAMPLE = enrich(Release("preview", "0", "", "StudioName.24.01.15.Jane.Doe.Example.Scene.XXX.1080p.MP4-GRP"))
SAMPLE.performers = ["Jane Doe"]
SAMPLE.title = "Example Scene"


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None, rescan=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(720, 520)
        self.s = settings
        tabs = QTabWidget()

        # --- general
        gen = QWidget()
        form = QFormLayout(gen)
        self.download_dir = self._dir_row(form, "Download folder", settings.download_dir)
        self.library_dir = self._dir_row(form, "Library (destination)", settings.library_dir)
        self.template = QLineEdit(settings.rename_template)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.template.textChanged.connect(self._update_preview)
        reset = QPushButton("Reset")
        reset.clicked.connect(lambda: self.template.setText(DEFAULT_TEMPLATE))
        row = QHBoxLayout()
        row.addWidget(self.template)
        row.addWidget(reset)
        form.addRow("Rename template", row)
        form.addRow("", QLabel("Fields: {studio} {date} {year} {performers} {title} {resolution} {group} {source}"
                               " — use / for sub-folders"))
        form.addRow("Preview", self.preview)
        self.parallel = QSpinBox(minimum=1, maximum=8, value=settings.max_parallel_downloads)
        form.addRow("Parallel downloads", self.parallel)
        self.blackhole = self._dir_row(form, "Torrent black hole", settings.torrent_blackhole_dir)
        self.blackhole.setPlaceholderText("empty = open torrents/magnets with your default torrent app")
        self.magnets_bh = QCheckBox("Also save magnet links into the black hole as .magnet files")
        self.magnets_bh.setChecked(settings.magnets_to_blackhole)
        form.addRow("", self.magnets_bh)
        self.wanted_res = QComboBox()
        for label, val in (("Best available", ""), ("2160p", "2160p"), ("1080p", "1080p"), ("720p", "720p")):
            self.wanted_res.addItem(label, val)
        self.wanted_res.setCurrentIndex(max(0, self.wanted_res.findData(settings.preferred_resolution)))
        self.wanted_res.setToolTip("A scene counts as downloaded once this resolution (or higher) is done. "
                                   "If it isn't posted, the best posted one counts. Anything less = partial.")
        form.addRow("Wanted resolution", self.wanted_res)
        self.pref_source = QComboBox()
        self.pref_source.addItem("Sources list order", "")
        for c in settings.sources:
            self.pref_source.addItem(c.id, c.id)
        self.pref_source.setCurrentIndex(max(0, self.pref_source.findData(settings.preferred_source)))
        self.pref_source.setToolTip("“Download preferred” picks: wanted resolution → preferred release group → "
                                    "this source → the rest in Sources order")
        form.addRow("Preferred source", self.pref_source)
        self.auto_sort = QCheckBox("Move finished downloads into the library")
        self.auto_sort.setChecked(settings.auto_sort_completed)
        form.addRow("", self.auto_sort)
        self.rename = QCheckBox("Rename them with the template above (off = keep the original file name)")
        self.rename.setChecked(settings.rename_files)
        self.rename.setEnabled(settings.auto_sort_completed)
        self.auto_sort.toggled.connect(self.rename.setEnabled)
        form.addRow("", self.rename)
        self.groups = QLineEdit(", ".join(settings.preferred_groups))
        self.groups.setPlaceholderText("PRT, GUSH, …")
        form.addRow("Preferred release groups", self.groups)
        form.addRow("", QLabel("Comma-separated, best first. Preferred groups are highlighted and picked first."))
        self.refresh_start = QCheckBox("Check sources for new posts on start")
        self.refresh_start.setChecked(settings.refresh_on_start)
        form.addRow("", self.refresh_start)
        self.max_pages = QSpinBox(minimum=1, maximum=100, value=settings.max_refresh_pages)
        form.addRow("Max pages per refresh", self.max_pages)
        self.crawl = QCheckBox("Background caching: slowly fetch every page until the whole site is cached")
        self.crawl.setChecked(settings.crawl_enabled)
        form.addRow("", self.crawl)
        self.crawl_interval = QSpinBox(minimum=5, maximum=3600, value=settings.crawl_interval_s, suffix=" s")
        self.crawl_interval.setToolTip("Seconds between page requests while caching. Higher = gentler on the site.")
        form.addRow("Seconds between page requests", self.crawl_interval)
        self.auto_refresh = QSpinBox(minimum=0, maximum=1440, value=settings.auto_refresh_minutes, suffix=" min")
        self.auto_refresh.setSpecialValueText("off")
        form.addRow("Check for new posts every", self.auto_refresh)
        self.updates = QCheckBox("Check for updates on start")
        self.updates.setChecked(settings.check_updates_on_start)
        form.addRow("", self.updates)
        tabs.addTab(gen, "General")

        # --- sources (JSON for now; per-source UIs come with each adapter)
        src = QWidget()
        v = QVBoxLayout(src)
        v.addWidget(QLabel(f"Sources, in priority order (first = preferred for downloads). "
                           f"Available types: {', '.join(sorted(SOURCE_TYPES))}"))
        self.sources_edit = QPlainTextEdit(json.dumps([asdict(c) for c in settings.sources], indent=2))
        v.addWidget(self.sources_edit)
        add = QPushButton("Add default sources")
        add.setToolTip("Adds PornRips.to and EncodedFapFiles (skips any id already in the list)")
        add.clicked.connect(self._add_example)
        v.addWidget(add)

        # Fill gaps: full pass over every page, adds missing posts, never removes anything
        self.rescan = rescan
        gaps = QGroupBox("Fill gaps — rescan every page")
        gl = QVBoxLayout(gaps)
        gl.addWidget(QLabel("Walks all listing pages from newest to oldest and adds any post that's missing from the "
                            "local cache. Nothing is removed. Faster than background caching; resumes after a restart."))
        self.rescan_rows: list[tuple[str, QPushButton, QLabel]] = []
        for c in settings.sources:
            row = QHBoxLayout()
            name = (c.options or {}).get("display_name", c.id)
            btn = QPushButton()
            state = QLabel()
            state.setStyleSheet("color:#9ca3af")
            btn.clicked.connect(lambda _=False, sid=c.id: self._toggle_rescan(sid))
            row.addWidget(QLabel(f"<b>{name}</b>"), 1)
            row.addWidget(state, 2)
            row.addWidget(btn)
            gl.addLayout(row)
            self.rescan_rows.append((c.id, btn, state))
        speed = QHBoxLayout()
        self.rescan_interval = QSpinBox(minimum=2, maximum=600, value=settings.rescan_interval_s, suffix=" s")
        self.rescan_interval.setToolTip("Seconds between page requests during a rescan (higher = gentler on the site)")
        speed.addWidget(QLabel("Seconds between page requests:"))
        speed.addWidget(self.rescan_interval)
        speed.addStretch(1)
        gl.addLayout(speed)
        v.addWidget(gaps)
        self._update_rescan_rows()
        tabs.addTab(src, "Sources")

        # --- import folders + matching
        imp = QWidget()
        f2 = QFormLayout(imp)
        f2.addRow(QLabel("Video files in these folders are matched to scenes by file name and marked as "
                         "downloaded — e.g. your browser or JDownloader download folder, or Whisparr's library.\n"
                         "Unfinished downloads (.part, .crdownload, files still being written) are skipped. "
                         "You can also drag files or folders onto the main window."))
        self.import_dirs = QPlainTextEdit("\n".join(settings.import_dirs))
        self.import_dirs.setPlaceholderText("One folder per line")
        self.import_dirs.setMaximumHeight(110)
        add_dir = QPushButton("Add folder…")
        add_dir.clicked.connect(self._add_import_dir)
        col = QVBoxLayout()
        col.addWidget(self.import_dirs)
        col.addWidget(add_dir, alignment=Qt.AlignmentFlag.AlignLeft)
        f2.addRow("Import folders", col)
        self.import_minutes = QSpinBox(minimum=0, maximum=1440, value=settings.import_scan_minutes, suffix=" min")
        self.import_minutes.setSpecialValueText("off")
        f2.addRow("Scan every", self.import_minutes)
        self.scan_lib = QCheckBox("Also scan the library folder")
        self.scan_lib.setChecked(settings.scan_library_dir)
        f2.addRow("", self.scan_lib)
        self.matching = QCheckBox("Match scenes on stash-box (ThePornDB / StashDB) in the background")
        self.matching.setChecked(settings.scene_matching)
        f2.addRow("", self.matching)
        self.match_interval = QSpinBox(minimum=1, maximum=600, value=settings.match_interval_s, suffix=" s")
        self.match_interval.setToolTip("One scene lookup every N seconds (each lookup asks every enabled stash-box)")
        f2.addRow("Seconds between lookups", self.match_interval)
        tabs.addTab(imp, "Import && matching")

        # --- stash-box
        sb = QWidget()
        v2 = QVBoxLayout(sb)
        v2.addWidget(QLabel("stash-box instances, used for studio logos now and scene matching later.\n"
                            "Get an API key from your account page on each site. Keys are stored in "
                            "data/settings.json next to the exe."))
        self.sb_table = QTableWidget(0, 4)
        self.sb_table.setHorizontalHeaderLabels(["Enabled", "Name", "GraphQL endpoint", "API key"])
        self.sb_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.sb_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for c in settings.stashboxes:
            self._add_sb_row(c)
        v2.addWidget(self.sb_table)
        row2 = QHBoxLayout()
        add_sb = QPushButton("Add")
        add_sb.clicked.connect(lambda: self._add_sb_row(StashBoxConfig("New", "https://", "")))
        del_sb = QPushButton("Remove selected")
        del_sb.clicked.connect(lambda: [self.sb_table.removeRow(i.row()) for i in
                                        sorted(self.sb_table.selectionModel().selectedRows(), reverse=True)])
        row2.addWidget(add_sb)
        row2.addWidget(del_sb)
        row2.addStretch(1)
        v2.addLayout(row2)
        tabs.addTab(sb, "Stash-box")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)
        self._update_preview()

    def _dir_row(self, form: QFormLayout, label: str, value: str) -> QLineEdit:
        edit = QLineEdit(value)
        btn = QPushButton("Browse…")
        btn.clicked.connect(lambda: (d := QFileDialog.getExistingDirectory(self, label, edit.text())) and edit.setText(d))
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(btn)
        form.addRow(label, row)
        return edit

    def _toggle_rescan(self, sid: str) -> None:
        if self.rescan is None:
            return
        if self.rescan.is_rescanning(sid):
            self.rescan.stop_rescan(sid)
        else:
            # use the pace from this dialog right away, even before OK is pressed
            self.rescan.settings.rescan_interval_s = self.rescan_interval.value()
            self.rescan._apply_timers()
            self.rescan.start_rescan(sid)
        self._update_rescan_rows()

    def _update_rescan_rows(self) -> None:
        for sid, btn, state in self.rescan_rows:
            loaded = self.rescan is not None and sid in self.rescan.sources
            btn.setEnabled(loaded)
            if not loaded:
                btn.setText("Rescan all pages")
                state.setText("save and enable this source first")
                continue
            db = self.rescan.db
            total = db.get_meta(f"total_pages:{sid}") or "?"
            if self.rescan.is_rescanning(sid):
                btn.setText("Stop rescan")
                state.setText(f"running: page {db.get_meta(f'rescan_page:{sid}', '0')}/{total} · "
                              f"{db.get_meta(f'rescan_new:{sid}', '0')} new")
            else:
                btn.setText("Rescan all pages")
                cached = "fully cached" if db.get_meta(f"crawl_done:{sid}") == "1" else \
                    f"{db.get_meta(f'backfill_page:{sid}', '0')}/{total} pages cached"
                state.setText(cached)

    def _add_import_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Import folder")
        if d:
            cur = self.import_dirs.toPlainText().strip()
            self.import_dirs.setPlainText((cur + "\n" if cur else "") + d)

    def _add_sb_row(self, c: StashBoxConfig) -> None:
        r = self.sb_table.rowCount()
        self.sb_table.insertRow(r)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk.setCheckState(Qt.CheckState.Checked if c.enabled else Qt.CheckState.Unchecked)
        self.sb_table.setItem(r, 0, chk)
        self.sb_table.setItem(r, 1, QTableWidgetItem(c.name))
        self.sb_table.setItem(r, 2, QTableWidgetItem(c.endpoint))
        key = QLineEdit(c.api_key)
        key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.sb_table.setCellWidget(r, 3, key)

    def _stashboxes(self) -> list[StashBoxConfig]:
        out = []
        for r in range(self.sb_table.rowCount()):
            out.append(StashBoxConfig(
                name=self.sb_table.item(r, 1).text().strip(),
                endpoint=self.sb_table.item(r, 2).text().strip(),
                api_key=self.sb_table.cellWidget(r, 3).text().strip(),
                enabled=self.sb_table.item(r, 0).checkState() == Qt.CheckState.Checked))
        return out

    def _update_preview(self) -> None:
        try:
            self.preview.setText(str(render_name(self.template.text(), SAMPLE, ".mp4")))
        except (KeyError, ValueError, IndexError) as e:
            self.preview.setText(f"<span style='color:#f87171'>Invalid template: {e}</span>")

    DEFAULT_SOURCES = [
        {"id": "PornRips.to", "type": "wordpress", "base_url": "https://pornrips.to/", "enabled": True,
         "options": {"display_name": "PornRips.to", "per_page": 50}},
        {"id": "encodedfap", "type": "encodedfapfiles", "base_url": "https://encodedfapfiles.pages.dev/",
         "enabled": True, "options": {"session_token": "", "per_page": 48, "sort": "date_desc"}},
    ]

    def _add_example(self) -> None:
        try:
            data = json.loads(self.sources_edit.toPlainText() or "[]")
        except ValueError:
            data = []
        have = {d.get("id") for d in data if isinstance(d, dict)}
        data += [json.loads(json.dumps(d)) for d in self.DEFAULT_SOURCES if d["id"] not in have]
        self.sources_edit.setPlainText(json.dumps(data, indent=2))

    def _accept(self) -> None:
        try:
            srcs = [SourceConfig(**c) for c in json.loads(self.sources_edit.toPlainText() or "[]")]
            ids = [c.id for c in srcs]
            if len(ids) != len(set(ids)):
                raise ValueError("source ids must be unique")
            for c in srcs:
                if c.type not in SOURCE_TYPES:
                    raise ValueError(f"unknown source type {c.type!r}")
            render_name(self.template.text(), SAMPLE, ".mp4")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Invalid settings", str(e))
            return
        self._result = replace(
            self.s,
            download_dir=self.download_dir.text(),
            library_dir=self.library_dir.text(),
            rename_template=self.template.text(),
            max_parallel_downloads=self.parallel.value(),
            torrent_blackhole_dir=self.blackhole.text().strip(),
            magnets_to_blackhole=self.magnets_bh.isChecked(),
            preferred_resolution=self.wanted_res.currentData(),
            auto_sort_completed=self.auto_sort.isChecked(),
            rename_files=self.rename.isChecked(),
            preferred_source=self.pref_source.currentData(),
            check_updates_on_start=self.updates.isChecked(),
            refresh_on_start=self.refresh_start.isChecked(),
            max_refresh_pages=self.max_pages.value(),
            crawl_enabled=self.crawl.isChecked(),
            crawl_interval_s=self.crawl_interval.value(),
            auto_refresh_minutes=self.auto_refresh.value(),
            rescan_interval_s=self.rescan_interval.value(),
            preferred_groups=[g.strip() for g in self.groups.text().split(",") if g.strip()],
            sources=srcs,
            stashboxes=self._stashboxes(),
            import_dirs=[l.strip() for l in self.import_dirs.toPlainText().splitlines() if l.strip()],
            import_scan_minutes=self.import_minutes.value(),
            scan_library_dir=self.scan_lib.isChecked(),
            scene_matching=self.matching.isChecked(),
            match_interval_s=self.match_interval.value(),
        )
        self.accept()

    def result_settings(self) -> Settings:
        return self._result
