"""Application entry point."""
from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .config import Settings


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


def main(argv: list[str] | None = None) -> int:
    from .gui.main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("SceneSorter")
    _dark_palette(app)
    win = MainWindow(Settings.load())
    win.show()
    return app.exec()
