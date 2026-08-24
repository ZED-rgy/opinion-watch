"""账号自动巡检登录窗口：驱动 `opinion_watch login` 子进程。"""

from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from typing import Any

from PySide6.QtCore import QProcess, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.config import Settings
from opinion_watch.desktop.components import button, title
from opinion_watch.desktop.constants import PLATFORM_NAMES
from opinion_watch.desktop.utils import decode_process_output, process_json_result
from opinion_watch.models import Platform
from opinion_watch.storage import Storage


class BrowserLoginWindow(QMainWindow):
    account_updated = Signal()

    def __init__(self, settings: Settings, storage: Storage, account: dict[str, Any]) -> None:
        super().__init__()
        self.storage = storage
        self.settings = settings
        self.account = account
        account_id = int(account["id"])
        platform = Platform(str(account["platform"]))
        self.account_id = account_id
        self.platform = platform
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.process_finished)
        self.output_buffer: list[str] = []
        self._cancelled = False
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(title("自动巡检登录", "sectionTitle"))
        note = QLabel(
            f"已打开 {PLATFORM_NAMES[platform.value]} 的自动巡检登录档案。"
            "请在弹出的 Chrome 窗口中完成登录，登录后回到这里点击“登录完成并检查”。"
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.status_label = QLabel("正在启动自动巡检登录档案…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        layout.addStretch()
        actions = QHBoxLayout()
        self.complete_button = button("登录完成并检查", self.mark_ready, icon_name="fa6s.check")
        actions.addWidget(self.complete_button)
        actions.addWidget(button("取消", self.cancel_login, role="secondary"))
        actions.addStretch()
        layout.addLayout(actions)
        self.setCentralWidget(central)
        self.resize(620, 260)
        self.setWindowTitle(
            f"{PLATFORM_NAMES[platform.value]} · {account['display_name']} · 自动巡检登录"
        )
        self._start_login_process()

    def _start_login_process(self) -> None:
        self._cancelled = False
        self.output_buffer.clear()
        self.complete_button.setText("登录完成并检查")
        self.complete_button.setEnabled(True)
        self.status_label.setText("正在启动自动巡检登录档案…")
        self.process.setProgram(sys.executable)
        self.process.setArguments(
            [
                "-m",
                "opinion_watch",
                "login",
                "--platform",
                self.platform.value,
                "--account-id",
                str(self.account_id),
            ]
        )
        self.process.start()

    def mark_ready(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._start_login_process()
            return
        self.complete_button.setEnabled(False)
        self.status_label.setText("正在检查登录状态，请稍候…")
        self.process.write(b"\n")

    def cancel_login(self) -> None:
        self._cancelled = True
        self.close()

    def read_output(self) -> None:
        value = decode_process_output(self.process.readAllStandardOutput())
        if value.strip():
            self.output_buffer.append(value)
            self.status_label.setText(value.strip().splitlines()[-1])

    def read_error(self) -> None:
        value = decode_process_output(self.process.readAllStandardError())
        if value.strip():
            self.output_buffer.append(value)

    def process_finished(self, _exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._cancelled:
            return
        output = "".join(self.output_buffer)
        result = process_json_result(output)
        self.complete_button.setEnabled(True)
        if result and result.get("status") == "healthy":
            self.storage.update_account_status(int(self.account["id"]), "ready")
            self.account_updated.emit()
            QMessageBox.information(self, "账号状态", "登录成功，自动巡检将使用这个登录档案。")
            self.close()
            return
        status = str(result.get("status") if result else "error")
        self.storage.update_account_status(int(self.account["id"]), status)
        self.status_label.setText(f"登录检查结果：{status}")
        QMessageBox.warning(
            self, "登录检查失败", "未检测到有效登录态，请在 Chrome 中完成登录后重试。"
        )
        self.complete_button.setText("重新打开浏览器")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        event.accept()

    def shutdown(self) -> None:
        """Close the login CLI and its Chrome process before the app exits."""
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._cancelled = True
            self.process.write(b"\n")
            if self.process.waitForFinished(3_000):
                return
            process_id = int(self.process.processId())
            if sys.platform == "win32" and process_id > 0:
                with suppress(Exception):
                    subprocess.run(
                        ["taskkill", "/PID", str(process_id), "/T", "/F"],
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            else:
                self.process.kill()
            self.process.waitForFinished(2_000)
