"""桌面应用入口：QApplication 装配、托盘与租约心跳。"""

from __future__ import annotations

import json
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

from opinion_watch.desktop.main_window import MainWindow
from opinion_watch.desktop.runtime import (
    DESKTOP_HEARTBEAT_INTERVAL_MS,
    DESKTOP_LEASE_SECONDS,
    DesktopRuntime,
    build_parser,
    create_runtime,
)
from opinion_watch.desktop.theme import FONT_FAMILY, FONT_SIZE_PT, build_stylesheet


def create_tray(app: QApplication, window: QMainWindow) -> QSystemTrayIcon | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QSystemTrayIcon(window.windowIcon(), window)
    tray.setToolTip("品牌舆情监控")
    tray_menu = QMenu(window)
    show_action = tray_menu.addAction("显示主窗口")
    show_action.triggered.connect(window.showNormal)
    show_action.triggered.connect(window.raise_)
    show_action.triggered.connect(window.activateWindow)
    tray_menu.addSeparator()
    quit_action = tray_menu.addAction("退出")
    quit_action.triggered.connect(app.quit)
    tray.setContextMenu(tray_menu)

    def activate_tray(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            window.showNormal()
            window.raise_()
            window.activateWindow()

    tray.activated.connect(activate_tray)
    tray.show()
    app.aboutToQuit.connect(tray.hide)
    return tray


def _start_lease_heartbeat(runtime: DesktopRuntime, window: QMainWindow) -> QTimer:
    timer = QTimer(window)
    timer.setInterval(DESKTOP_HEARTBEAT_INTERVAL_MS)
    timer.timeout.connect(
        lambda: runtime.storage.heartbeat_task_lease(
            "desktop", runtime.lease_owner, lease_seconds=DESKTOP_LEASE_SECONDS
        )
    )
    timer.start()
    return timer


def main() -> None:
    args = build_parser().parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("品牌舆情监控")
    app.setOrganizationName("opinion-watch")
    app.setFont(QFont(FONT_FAMILY, FONT_SIZE_PT))
    app.setStyleSheet(build_stylesheet())
    runtime = create_runtime(args.runtime_dir)
    app.aboutToQuit.connect(
        lambda: runtime.storage.release_task_lease("desktop", runtime.lease_owner)
    )
    window = MainWindow(runtime.settings, runtime.storage)
    window.navigation.setCurrentRow(args.page)
    _start_lease_heartbeat(runtime, window)
    if not args.smoke_test:
        create_tray(app, window)
    if args.screenshot:
        screenshot_path = args.screenshot.resolve()
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        window.show()

        def capture() -> None:
            window.grab().save(str(screenshot_path))
            app.quit()

        QTimer.singleShot(1000, capture)
    elif args.smoke_test:
        QTimer.singleShot(100, app.quit)
    else:
        window.show()
    exit_code = app.exec()
    if args.smoke_test:
        print(json.dumps({"status": "ok", "pages": window.stack.count()}, ensure_ascii=False))
    elif args.screenshot:
        print(json.dumps({"status": "ok", "screenshot": str(screenshot_path)}, ensure_ascii=False))
    raise SystemExit(exit_code)
