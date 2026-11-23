"""Simple Telegram OTP login dialog for EncodedFapFiles."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox, QHBoxLayout

from ..sources.encodedfapfiles import EncodedFapFilesSource


class EncodedFapLoginDialog(QDialog):
    def __init__(self, source: EncodedFapFilesSource, parent=None):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle(f"Log in to {source.display_name}")
        self.resize(420, 220)
        self.telegram_id: int | None = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Enter your Telegram user ID.\n"
            "The bot @EncodedFapFilesbot will send you a 6-digit code.\n"
            "(Start the bot first if you haven't.)"
        ))

        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("Telegram user ID (numbers only)")
        lay.addWidget(self.id_edit)

        btn_row = QHBoxLayout()
        self.btn_request = QPushButton("Send code")
        self.btn_request.clicked.connect(self._request)
        btn_row.addWidget(self.btn_request)
        lay.addLayout(btn_row)

        self.otp_edit = QLineEdit()
        self.otp_edit.setPlaceholderText("6-digit code from Telegram")
        self.otp_edit.setMaxLength(6)
        self.otp_edit.setEnabled(False)
        lay.addWidget(self.otp_edit)

        self.btn_verify = QPushButton("Verify & log in")
        self.btn_verify.setEnabled(False)
        self.btn_verify.clicked.connect(self._verify)
        lay.addWidget(self.btn_verify)

        self.status = QLabel("")
        lay.addWidget(self.status)

    def _request(self) -> None:
        try:
            tg_id = int(self.id_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Invalid ID", "Enter a numeric Telegram user ID.")
            return
        self.telegram_id = tg_id
        self.btn_request.setEnabled(False)
        self.status.setText("Requesting code…")
        try:
            data = self.source.request_otp(tg_id)
            status = data.get("status", "")
            if status in ("otp_sent", "otp_cooldown"):
                self.status.setText("Code sent! Check Telegram and enter it below.")
                self.otp_edit.setEnabled(True)
                self.btn_verify.setEnabled(True)
                self.otp_edit.setFocus()
            elif status == "bot_not_started":
                bot = data.get("bot_username") or "EncodedFapFilesbot"
                self.status.setText(f"Please start @{bot} in Telegram first, then try again.")
                self.btn_request.setEnabled(True)
            else:
                self.status.setText(data.get("message") or data.get("detail") or f"Status: {status}")
                self.btn_request.setEnabled(True)
        except Exception as e:
            self.status.setText(f"Error: {e}")
            self.btn_request.setEnabled(True)

    def _verify(self) -> None:
        if not self.telegram_id:
            return
        otp = self.otp_edit.text().strip()
        if len(otp) != 6 or not otp.isdigit():
            QMessageBox.warning(self, "Invalid code", "Enter the 6-digit code.")
            return
        self.btn_verify.setEnabled(False)
        self.status.setText("Verifying…")
        try:
            self.source.verify_otp(self.telegram_id, otp)
            self.status.setText("Logged in!")
            self.accept()
        except Exception as e:
            self.status.setText(f"Failed: {e}")
            self.btn_verify.setEnabled(True)
