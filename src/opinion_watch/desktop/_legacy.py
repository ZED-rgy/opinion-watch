from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QProcess,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.config import DEFAULT_BRANDS, Settings
from opinion_watch.desktop import components as _components
from opinion_watch.desktop.components import (
    icon as _icon,
)
from opinion_watch.desktop.login_window import BrowserLoginWindow
from opinion_watch.desktop.pages import (
    AccountsPage,
    KeywordsPage,
    NotificationsPage,
    OpinionsPage,
    SchedulerPage,
    SettingsPage,
)
from opinion_watch.desktop.runtime import migrate_legacy_schedule
from opinion_watch.desktop.theme import build_stylesheet
from opinion_watch.models import Platform
from opinion_watch.storage import Storage

APP_STYLE = build_stylesheet()


def _button(text, callback, *, icon=None, role="primary"):
    return _components.button(text, callback, icon_name=icon, role=role)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, storage: Storage) -> None:
        super().__init__()
        self.settings = settings
        self.storage = storage
        self.browser_windows: dict[int, BrowserLoginWindow] = {}
        self._sync_account_profile_status()
        self.setWindowTitle("品牌舆情监控")
        self.setWindowIcon(_icon("fa6s.shield-halved", "#2F5BEA"))
        self.resize(1440, 1024)
        self.setMinimumSize(1120, 760)
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(238)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 22, 16, 18)
        logo = QHBoxLayout()
        logo_icon = QLabel()
        logo_icon.setPixmap(_icon("fa6s.shield-halved", "#2F5BEA").pixmap(QSize(28, 28)))
        logo.addWidget(logo_icon)
        brand = QLabel("品牌舆情监控")
        brand.setObjectName("brandName")
        logo.addWidget(brand)
        logo.addStretch()
        sidebar_layout.addLayout(logo)
        sidebar_layout.addSpacing(24)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        nav_items = (
            ("自动巡检", "fa6s.clock-rotate-left"),
            ("品牌与关键词", "fa6s.tags"),
            ("平台账号", "fa6s.user-shield"),
            ("舆情中心", "fa6s.magnifying-glass-chart"),
            ("应用内播报", "fa6s.bell"),
            ("设置", "fa6s.gear"),
        )
        for text, icon_name in nav_items:
            item = QListWidgetItem(_icon(icon_name), text)
            item.setSizeHint(QSize(0, 44))
            self.navigation.addItem(item)
            if text == "应用内播报":
                self.notification_nav_item = item
        sidebar_layout.addWidget(self.navigation, 1)
        self.notification_badge = QLabel("0", self.navigation.viewport())
        self.notification_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notification_badge.setMinimumWidth(18)
        self.notification_badge.setFixedHeight(18)
        self.notification_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.notification_badge.setStyleSheet(
            "background:#D9363E; color:#FFFFFF; border-radius:9px; "
            "font-size:11px; font-weight:700; padding:0 4px;"
        )
        self.notification_badge.hide()
        ready = QFrame()
        ready.setObjectName("statusPill")
        ready_layout = QHBoxLayout(ready)
        ready_layout.setContentsMargins(10, 7, 10, 7)
        dot = QLabel("●")
        self.ready_dot = dot
        dot.setStyleSheet("color:#18845B; font-size:10px;")
        ready_layout.addWidget(dot)
        self.ready_label = QLabel("本地监控已就绪")
        ready_layout.addWidget(self.ready_label)
        sidebar_layout.addWidget(ready)
        version = QLabel("v0.2.0")
        version.setObjectName("muted")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sidebar_layout.addWidget(version)
        root.addWidget(sidebar)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.scheduler = SchedulerPage(storage)
        self.keywords = KeywordsPage(storage)
        self.accounts = AccountsPage(storage)
        self.opinions = OpinionsPage(storage)
        self.notifications = NotificationsPage(storage)
        self.settings_page = SettingsPage(settings, storage)
        for page in (
            self.scheduler,
            self.keywords,
            self.accounts,
            self.opinions,
            self.notifications,
            self.settings_page,
        ):
            self.stack.addWidget(page)
        self.navigation.currentRowChanged.connect(self.change_page)
        self.navigation.setCurrentRow(0)
        self.keywords.changed.connect(self.refresh_all)
        self.accounts.changed.connect(self.refresh_all)
        self.accounts.login_requested.connect(self.open_account_browser)
        self.opinions.changed.connect(self.refresh_all)
        self.notifications.changed.connect(self.refresh_all)
        self.scheduler.scan_finished.connect(self.refresh_all)
        self.scheduler.manage_scope_requested.connect(lambda: self.navigation.setCurrentRow(1))
        self.refresh_all()

    def _sync_account_profile_status(self) -> None:
        for account in self.storage.list_accounts():
            if str(account["status"]) != "ready":
                continue
            try:
                platform = Platform(str(account["platform"]))
            except ValueError:
                continue
            profile = self.settings.account_profile_dir(platform, int(account["id"]))
            if not profile.exists():
                self.storage.update_account_status(int(account["id"]), "not_logged_in")

    def change_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.notification_nav_item.setIcon(
            _icon("fa6s.bell", "#244BC5" if index == 4 else "#5D667A")
        )
        page = self.stack.currentWidget()
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def position_notification_badge(self) -> None:
        rect = self.navigation.visualItemRect(self.notification_nav_item)
        if not rect.isValid():
            return
        self.notification_badge.adjustSize()
        self.notification_badge.setFixedWidth(max(18, self.notification_badge.width()))
        x = rect.right() - self.notification_badge.width() - 8
        y = rect.top() + (rect.height() - self.notification_badge.height()) // 2
        self.notification_badge.move(x, y)
        self.notification_badge.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self.position_notification_badge)

    def refresh_all(self) -> None:
        accounts = self.storage.list_accounts(enabled_only=True)
        healthy = bool(accounts) and all(str(item["status"]) == "ready" for item in accounts)
        if healthy:
            self.ready_label.setText("本地监控已就绪")
            self.ready_dot.setStyleSheet("color:#18845B; font-size:10px;")
        elif accounts:
            self.ready_label.setText("有账号待处理")
            self.ready_dot.setStyleSheet("color:#D18A18; font-size:10px;")
        else:
            self.ready_label.setText("待配置账号")
            self.ready_dot.setStyleSheet("color:#D18A18; font-size:10px;")
        unread_count = self.storage.count_unread_notifications()
        self.notification_badge.setText(str(unread_count if unread_count < 100 else "99+"))
        self.notification_badge.setVisible(unread_count > 0)
        QTimer.singleShot(0, self.position_notification_badge)
        for page in (
            self.scheduler,
            self.keywords,
            self.accounts,
            self.opinions,
            self.notifications,
            self.settings_page,
        ):
            page.refresh()

    def open_account_browser(self, account: dict[str, Any]) -> None:
        account_id = int(account["id"])
        window = self.browser_windows.get(account_id)
        if window is None or window.process.state() == QProcess.ProcessState.NotRunning:
            if window is not None:
                window.close()
                window.deleteLater()
            window = BrowserLoginWindow(self.settings, self.storage, account)
            window.account_updated.connect(self.refresh_all)
            self.browser_windows[account_id] = window
        window.show()
        window.raise_()
        window.activateWindow()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="品牌舆情监控桌面应用")
    parser.add_argument("--smoke-test", action="store_true", help="初始化界面后立即退出")
    parser.add_argument("--screenshot", type=Path, help="保存桌面界面截图后退出")
    parser.add_argument("--page", type=int, choices=range(6), default=0, help="启动时显示的页面")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="运行数据目录；开机自启时用于固定数据库位置，默认取环境变量或当前目录下 runtime",
    )
    return parser


def create_runtime(runtime_dir: Path | None = None) -> tuple[Settings, Storage]:
    if runtime_dir is not None:
        # 写入环境变量而不是只改本进程配置：巡检 QProcess 子进程继承同一个值，
        # 才能保证读写同一个数据库。
        os.environ["OPINION_WATCH_RUNTIME_DIR"] = str(runtime_dir.resolve())
    settings = Settings.from_environment()
    settings.ensure_directories()
    storage = Storage(settings.database_path)
    storage.initialize()
    storage.recover_stale_scan_runs()
    migrate_legacy_schedule(storage)
    owner = str(uuid.uuid4())
    # 短租约 + 定时心跳：桌面进程崩溃后最多几分钟即可重新启动，
    # 而不是等一个超长租约自然过期。
    if not storage.acquire_task_lease("desktop", owner, lease_seconds=900):
        raise RuntimeError("品牌舆情监控已经在运行，请先关闭已有窗口。")
    if not storage.list_brands():
        for brand in DEFAULT_BRANDS:
            storage.add_brand(brand)
    storage._desktop_lease_owner = owner  # type: ignore[attr-defined]
    return settings, storage


def main() -> None:
    args = build_parser().parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("品牌舆情监控")
    app.setOrganizationName("opinion-watch")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)
    settings, storage = create_runtime(args.runtime_dir)
    app.aboutToQuit.connect(
        lambda: storage.release_task_lease(
            "desktop", str(getattr(storage, "_desktop_lease_owner", ""))
        )
    )
    window = MainWindow(settings, storage)
    window.navigation.setCurrentRow(args.page)
    lease_owner = str(getattr(storage, "_desktop_lease_owner", ""))
    lease_timer = QTimer(window)
    lease_timer.setInterval(300_000)
    lease_timer.timeout.connect(
        lambda: storage.heartbeat_task_lease("desktop", lease_owner, lease_seconds=900)
    )
    lease_timer.start()
    tray: QSystemTrayIcon | None = None
    if not args.smoke_test and QSystemTrayIcon.isSystemTrayAvailable():
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
