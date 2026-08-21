"""平台账号管理页。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.desktop.components import (
    EmptyState,
    TablePanel,
    add_context_menu,
    bind_selection,
    button,
    make_table,
    populate,
    selected_id,
    set_text_cell,
    show_toast,
    surface,
    title,
)
from opinion_watch.desktop.constants import ACCOUNT_STATUS_NAMES, PLATFORM_NAMES
from opinion_watch.desktop.dialogs import confirm_destructive, show_error
from opinion_watch.desktop.theme import STATE_COLORS
from opinion_watch.desktop.utils import format_timestamp
from opinion_watch.storage import Storage


class AccountsPage(QWidget):
    changed = Signal()
    login_requested = Signal(object)

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.rows: dict[int, dict[str, Any]] = {}
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(title("平台账号"))
        subtitle = QLabel("每个账号使用与自动巡检一致的登录档案")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(button("新增账号", self.add_account, icon_name="fa6s.plus"))
        root.addLayout(header)
        card, layout = surface()
        actions = QHBoxLayout()
        self.login_button = button("打开自动巡检浏览器", self.open_login, icon_name="fa6s.globe")
        self.toggle_button = button("启用 / 停用", self.toggle_account, role="secondary")
        self.delete_button = button("删除账号", self.delete_account, role="danger")
        actions.addWidget(self.login_button)
        actions.addWidget(self.toggle_button)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.table = make_table(
            ["ID", "平台", "账号名称", "登录状态", "状态", "最近检查"], sortable=True
        )
        self.table.setColumnHidden(0, True)
        bind_selection(self.table, self.login_button, self.toggle_button, self.delete_button)
        add_context_menu(
            self.table,
            [
                ("打开自动巡检浏览器", self.open_login),
                ("启用 / 停用", self.toggle_account),
                ("删除账号", self.delete_account),
            ],
        )
        self.table.itemDoubleClicked.connect(lambda _item: self.open_login())
        self.panel = TablePanel(
            self.table,
            EmptyState(
                "还没有平台账号", "新增账号并在自动巡检浏览器中完成登录，登录态会独立保存在本机。"
            ),
        )
        layout.addWidget(self.panel, 1)
        root.addWidget(card, 1)

    def refresh(self) -> None:
        rows = self.storage.list_accounts()
        self.rows = {int(item["id"]): item for item in rows}
        with populate(self.table):
            self.table.setRowCount(len(rows))
            for row, item in enumerate(rows):
                values = (
                    item["id"],
                    PLATFORM_NAMES.get(str(item["platform"]), item["platform"]),
                    item["display_name"],
                    ACCOUNT_STATUS_NAMES.get(str(item["status"]), item["status"]),
                    "启用" if item["enabled"] else "停用",
                    format_timestamp(item["last_checked_at"]),
                )
                for column, value in enumerate(values):
                    cell = set_text_cell(self.table, row, column, value, tooltip=column == 2)
                    if column == 3:
                        state = "ok" if item["status"] == "ready" else "warn"
                        cell.setForeground(QColor(STATE_COLORS[state]))
        self.panel.show_count(len(rows))

    def add_account(self) -> None:
        platform_name, ok = QInputDialog.getItem(
            self, "新增平台账号", "平台", list(PLATFORM_NAMES.values()), editable=False
        )
        if not ok:
            return
        display_name, ok = QInputDialog.getText(self, "新增平台账号", "账号备注名称")
        if not ok:
            return
        platform = next(key for key, value in PLATFORM_NAMES.items() if value == platform_name)
        try:
            self.storage.add_account(platform, display_name)
            self.refresh()
            self.changed.emit()
            show_toast(self, f"已添加账号“{display_name.strip()}”，请打开浏览器完成登录")
        except Exception as exc:
            show_error(self, exc)

    def selected_account(self) -> dict[str, Any] | None:
        account_id = selected_id(self.table)
        return self.rows.get(account_id) if account_id is not None else None

    def open_login(self) -> None:
        account = self.selected_account()
        if account is not None:
            self.login_requested.emit(account)

    def toggle_account(self) -> None:
        account = self.selected_account()
        if account:
            self.storage.set_account_enabled(int(account["id"]), not bool(account["enabled"]))
            self.refresh()
            self.changed.emit()

    def delete_account(self) -> None:
        account = self.selected_account()
        if account and confirm_destructive(
            self, "删除账号", "只删除账号记录，不删除本地浏览器档案。确认继续？"
        ):
            self.storage.delete_account(int(account["id"]))
            self.refresh()
            self.changed.emit()
