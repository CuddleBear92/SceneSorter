"""Settings dialog: folders, rename template, downloads, sources."""
from __future__ import annotations

import json
from dataclasses import asdict, replace

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..config import DEFAULT_TEMPLATE, Settings
from ..library import render_name
from ..models import Release
from ..parsing import enrich
from ..sources import SOURCE_TYPES, SourceConfig

SAMPLE = enrich(Release("preview", "0", "", "StudioName.24.01.15.Jane.Doe.Example.Scene.XXX.1080p.MP4-GRP"))
SAMPLE.performers = ["Jane Doe"]
SAMPLE.title = "Example Scene"


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
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
        form.addRow("", QLabel("Fields: {studio} {date} {year} {performers} {title} {resolution} {source}"
                               " — use / for sub-folders"))
        form.addRow("Preview", self.preview)
        self.parallel = QSpinBox(minimum=1, maximum=8, value=settings.max_parallel_downloads)
        form.addRow("Parallel downloads", self.parallel)
        self.auto_sort = QCheckBox("Rename && move finished downloads into the library")
        self.auto_sort.setChecked(settings.auto_sort_completed)
        form.addRow("", self.auto_sort)
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
        add = QPushButton("Add example source")
        add.clicked.connect(self._add_example)
        v.addWidget(add)
        tabs.addTab(src, "Sources")

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

    def _update_preview(self) -> None:
        try:
            self.preview.setText(str(render_name(self.template.text(), SAMPLE, ".mp4")))
        except (KeyError, ValueError, IndexError) as e:
            self.preview.setText(f"<span style='color:#f87171'>Invalid template: {e}</span>")

    def _add_example(self) -> None:
        try:
            data = json.loads(self.sources_edit.toPlainText() or "[]")
        except ValueError:
            data = []
        data.append({"id": f"blog{len(data) + 1}", "type": "wordpress", "base_url": "https://example.com/",
                     "enabled": True, "options": {"display_name": "My blog", "per_page": 30}})
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
            auto_sort_completed=self.auto_sort.isChecked(),
            check_updates_on_start=self.updates.isChecked(),
            refresh_on_start=self.refresh_start.isChecked(),
            max_refresh_pages=self.max_pages.value(),
            crawl_enabled=self.crawl.isChecked(),
            crawl_interval_s=self.crawl_interval.value(),
            auto_refresh_minutes=self.auto_refresh.value(),
            preferred_groups=[g.strip() for g in self.groups.text().split(",") if g.strip()],
            sources=srcs,
        )
        self.accept()

    def result_settings(self) -> Settings:
        return self._result
