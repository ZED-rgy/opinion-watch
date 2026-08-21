"""主窗口：侧边导航、页面栈与全局状态。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QProcess, QSize, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from opinion_watch import __version__
from opinion_watch.config import Settings
from opinion_watch.desktop.components import icon
from opinion_watch.desktop.constants import Page
from opinion_watch.desktop.login_window import BrowserLoginWindow
from opinion_watch.desktop.pages import (
    AccountsPage,
    KeywordsPage,
    NotificationsPage,
    OpinionsPage,
    SchedulerPage,
    SettingsPage,
)
from opinion_watch.desktop.theme import COLORS, SIDEBAR, repolish
from opinion_watch.models import Platform
from opinion_watch.storage import Storage

BADGE_ROLE = Qt.ItemDataRole.UserRole + 1

_NAV_ITEMS: tuple[tuple[Page, str, str], ...] = (
    (Page.SCHEDULER, "自动巡检", "fa6s.clock-rotate-left"),
    (Page.KEYWORDS, "品牌与关键词", "fa6s.tags"),
    (Page.ACCOUNTS, "平台账号", "fa6s.user-shield"),
    (Page.OPINIONS, "舆情中心", "fa6s.magnifying-glass-chart"),
    (Page.NOTIFICATIONS, "应用内播报", "fa6s.bell"),
    (Page.SETTINGS, "设置", "fa6s.gear"),
)


class NavBadgeDelegate(QStyledItemDelegate):
    """在导航项右侧绘制未读徽标；随滚动、缩放和 DPI 自动对齐。"""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        super().paint(painter, option, index)
        value = index.data(BADGE_ROLE)
        count = int(value) if value else 0
        if count <= 0:
            return
        text = str(count) if count < 100 else "99+"
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = painter.font()
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = max(18, metrics.horizontalAdvance(text) + 10)
        height = 18
        rect = option.rect
        x = rect.right() - width - 10
        y = rect.top() + (rect.height() - height) // 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["badge"]))
        painter.drawRoundedRect(x, y, width, height, 9, 9)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(x, y, width, height, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, storage: Storage) -> None:
        super().__init__()
        self.settings = settings
        self.storage = storage
        self.browser_windows: dict[int, BrowserLoginWindow] = {}
        self._sync_account_profile_status()
        self.setWindowTitle("品牌舆情监控")
        self.setWindowIcon(icon("fa6s.shield-halved", "#2F5BEA"))
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
        logo_icon.setPixmap(icon("fa6s.shield-halved", "#5B82FF").pixmap(QSize(28, 28)))
        logo.addWidget(logo_icon)
        brand = QLabel("品牌舆情监控")
        brand.setObjectName("brandName")
        logo.addWidget(brand)
        logo.addStretch()
        sidebar_layout.addLayout(logo)
        sidebar_layout.addSpacing(24)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setItemDelegate(NavBadgeDelegate(self.navigation))
        self.nav_items: dict[Page, QListWidgetItem] = {}
        self._nav_icons: dict[Page, str] = {}
        for page, text, icon_name in _NAV_ITEMS:
            item = QListWidgetItem(icon(icon_name, SIDEBAR["icon"]), text)
            item.setSizeHint(QSize(0, 44))
            self.navigation.addItem(item)
            self.nav_items[page] = item
            self._nav_icons[page] = icon_name
        sidebar_layout.addWidget(self.navigation, 1)
        ready = QFrame()
        ready.setObjectName("statusPill")
        ready_layout = QHBoxLayout(ready)
        ready_layout.setContentsMargins(10, 7, 10, 7)
        self.ready_dot = QLabel("●")
        self.ready_dot.setObjectName("readyDot")
        self.ready_dot.setProperty("state", "ok")
        ready_layout.addWidget(self.ready_dot)
        self.ready_label = QLabel("本地监控已就绪")
        ready_layout.addWidget(self.ready_label)
        sidebar_layout.addWidget(ready)
        version = QLabel(f"v{__version__}")
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
        for page_widget in (
            self.scheduler,
            self.keywords,
            self.accounts,
            self.opinions,
            self.notifications,
            self.settings_page,
        ):
            self.stack.addWidget(page_widget)
        self.navigation.currentRowChanged.connect(self.change_page)
        self.keywords.changed.connect(self.on_data_changed)
        self.accounts.changed.connect(self.on_data_changed)
        self.accounts.login_requested.connect(self.open_account_browser)
        self.opinions.changed.connect(self.on_data_changed)
        self.notifications.changed.connect(self.on_data_changed)
        self.scheduler.scan_finished.connect(self.on_data_changed)
        self.scheduler.manage_scope_requested.connect(
            lambda: self.navigation.setCurrentRow(Page.KEYWORDS)
        )
        for page in Page:
            shortcut = QShortcut(QKeySequence(f"Ctrl+{page.value + 1}"), self)
            shortcut.activated.connect(lambda row=page.value: self.navigation.setCurrentRow(row))
        for key in ("F5", "Ctrl+R"):
            refresh_shortcut = QShortcut(QKeySequence(key), self)
            refresh_shortcut.activated.connect(self.refresh_current_page)
        self.navigation.setCurrentRow(Page.SCHEDULER)
        self.update_global_status()

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
        # 深色侧边栏上激活项高亮为白色图标，其余保持浅灰。
        for page, item in self.nav_items.items():
            color = SIDEBAR["icon_active"] if page.value == index else SIDEBAR["icon"]
            item.setIcon(icon(self._nav_icons[page], color))
        self.refresh_current_page()

    def refresh_current_page(self) -> None:
        page = self.stack.currentWidget()
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def on_data_changed(self) -> None:
        # 只刷新全局状态和当前可见页面；其他页面切换时会自行刷新。
        self.update_global_status()
        self.refresh_current_page()

    def update_global_status(self) -> None:
        accounts = self.storage.list_accounts(enabled_only=True)
        healthy = bool(accounts) and all(str(item["status"]) == "ready" for item in accounts)
        if healthy:
            self.ready_label.setText("本地监控已就绪")
            self.ready_dot.setProperty("state", "ok")
        elif accounts:
            self.ready_label.setText("有账号待处理")
            self.ready_dot.setProperty("state", "warn")
        else:
            self.ready_label.setText("待配置账号")
            self.ready_dot.setProperty("state", "warn")
        repolish(self.ready_dot)
        unread_count = self.storage.count_unread_notifications()
        self.nav_items[Page.NOTIFICATIONS].setData(BADGE_ROLE, unread_count)
        self.navigation.viewport().update()

    def refresh_all(self) -> None:
        """保留给外部调用（如登录窗口回调）的全量刷新入口。"""
        self.update_global_status()
        self.refresh_current_page()

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
