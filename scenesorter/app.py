"""Application entry point."""
from __future__ import annotations

import os
import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import DATA_DIR, Settings


def _dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#111418", QPalette.ColorRole.WindowText: "#e5e7eb",
        QPalette.ColorRole.Base: "#0d1014", QPalette.ColorRole.AlternateBase: "#161a20",
        QPalette.ColorRole.Text: "#e5e7eb", QPalette.ColorRole.Button: "#1c2129",
        QPalette.ColorRole.ButtonText: "#e5e7eb", QPalette.ColorRole.Highlight: "#2563eb",
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.ToolTipBase: "#1c2129",
        QPalette.ColorRole.ToolTipText: "#e5e7eb", QPalette.ColorRole.PlaceholderText: "#6b7280",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    # Fusion's checkbox indicator is near-invisible on this dark palette; give it an outline.
    app.setStyleSheet(
        "QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #6b7280; border-radius: 3px;"
        " background: #0d1014; }"
        "QCheckBox::indicator:checked { background: #2563eb; border-color: #3b82f6; }"
        "QCheckBox::indicator:hover { border-color: #9ca3af; }")


def main(argv: list[str] | None = None) -> int:
    # Heavy work (merging, filtering, matching) runs on worker threads. With Python's
    # default 5 ms GIL switch interval, every small Qt->Python callback on the UI thread
    # can wait that long for the lock, and thousands of them add up to visible stutter.
    sys.setswitchinterval(0.0005)
    # Fewer, cheaper collections: the app holds a lot of long-lived data (see gc.freeze() in
    # MainWindow), and young-generation passes are what keep the UI responsive.
    import gc
    gc.set_threshold(50_000, 50, 100)
    from .gui.main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("SceneSorter")
    _dark_palette(app)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        settings = Settings.load()
        settings.save()  # creates data/settings.json on first run
    except OSError as e:
        QMessageBox.critical(None, "SceneSorter",
                             f"Can't write to the data folder:\n{DATA_DIR}\n\n{e}\n\n"
                             "SceneSorter is portable and keeps its settings and database next to the exe. "
                             "Move it to a folder you can write to (not Program Files).")
        return 1
    from . import updater

    pending = updater.leftover_update()
    if pending:
        f, ver = pending
        ans = QMessageBox.question(None, "SceneSorter",
                                   f"Build {ver} was downloaded earlier but didn't get installed.\n\nInstall it now?")
        if ans == QMessageBox.StandardButton.Yes:
            updater.apply_update_and_restart(f)
            return 0
    win = MainWindow(settings)
    win.show()
    rc = app.exec()
    # The window has already finished/saved its work (closeEvent drains, closes the DB).
    # Don't let a background job that's still running (e.g. a long merge on a busy, swapping
    # machine) keep the process alive: Qt's thread pool would wait for it on exit, which also
    # blocks the self-updater from replacing the exe. Give threads a moment, then leave.
    from PySide6.QtCore import QThreadPool

    QThreadPool.globalInstance().waitForDone(3000)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
