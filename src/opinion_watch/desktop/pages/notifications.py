"""应用内播报页。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.desktop.components import (
    EmptyState,
    TablePanel,
    add_context_menu,
    bind_checked,
    bind_selection,
    button,
    checked_ids,
    make_table,
    populate,
    set_check_cell,
    set_text_cell,
    severity_item,
    show_toast,
    surface,
    title,
    toggle_all,
)
from opinion_watch.desktop.dialogs import confirm_destructive, show_error
from opinion_watch.desktop.theme import COLORS
from opinion_watch.desktop.utils import format_timestamp
from opinion_watch.storage import Storage


class NotificationsPage(QWidget):
    changed = Signal()

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
        heading.addWidget(title("应用内播报"))
        subtitle = QLabel("重要发现和巡检异常会集中出现在这里")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(button("新增播报", self.add_notification, icon_name="fa6s.plus"))
        header.addWidget(
            button(
                "全部标记已读", self.mark_all_read, icon_name="fa6s.check-double", role="secondary"
            )
        )
        root.addLayout(header)
        card, layout = surface()
        actions = QHBoxLayout()
        self.select_all_button = button("全选", self.select_all, role="secondary")
        self.mark_read_button = button("标记选中已读", self.mark_selected_read, role="secondary")
        self.edit_button = button("编辑选中", self.edit_notification, role="secondary")
        self.delete_button = button("删除选中", self.delete_selected, role="danger")
        actions.addWidget(self.select_all_button)
        actions.addWidget(self.mark_read_button)
        actions.addSpacing(12)
        actions.addWidget(QLabel("类型"))
        self.channel_filter = QComboBox()
        self.channel_filter.addItem("全部播报", None)
        self.channel_filter.addItem("舆情提醒", "opinion")
        self.channel_filter.addItem("系统运行", "system")
        self.channel_filter.addItem("人工播报", "manual")
        self.channel_filter.currentIndexChanged.connect(self.refresh)
        actions.addWidget(self.channel_filter)
        actions.addStretch()
        actions.addWidget(self.edit_button)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)
        self.table = make_table(
            ["选择", "ID", "类型", "等级", "标题", "内容", "时间", "状态"],
            multi_select=True,
        )
        self.table.setColumnHidden(1, True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        bind_selection(self.table, self.edit_button)
        bind_checked(self.table, self.mark_read_button, self.delete_button)
        add_context_menu(
            self.table,
            [
                ("标记已读", self.mark_current_read),
                ("编辑播报", self.edit_notification),
                ("删除播报", self.delete_current),
            ],
        )
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_notification())
        self.panel = TablePanel(
            self.table,
            EmptyState("暂无播报", "出现需要关注的新舆情或巡检异常时，这里会第一时间提醒你。"),
        )
        layout.addWidget(self.panel, 1)
        root.addWidget(card, 1)

    def refresh(self) -> None:
        rows = self.storage.list_notifications(
            limit=1000,
            channel=self.channel_filter.currentData(),
        )
        self.rows = {int(item["id"]): item for item in rows}
        with populate(self.table):
            self.table.setRowCount(len(rows))
            for row, item in enumerate(rows):
                set_check_cell(self.table, row)
                unread = item["read_at"] is None
                set_text_cell(self.table, row, 1, item["id"], tooltip=False)
                channel_name = {
                    "opinion_review": "舆情提醒",
                    "runtime_alert": "系统运行",
                    "manual": "人工播报",
                }.get(str(item["kind"]), str(item["kind"]))
                set_text_cell(self.table, row, 2, channel_name, tooltip=False)
                self.table.setItem(
                    row, 3, severity_item(str(item["severity"]), str(item["severity"]))
                )
                title_cell = set_text_cell(self.table, row, 4, item["title"])
                if unread:
                    font = title_cell.font()
                    font.setBold(True)
                    title_cell.setFont(font)
                set_text_cell(self.table, row, 5, item["message"])
                set_text_cell(
                    self.table, row, 6, format_timestamp(item["created_at"]), tooltip=False
                )
                status_cell = set_text_cell(
                    self.table, row, 7, "未读" if unread else "已读", tooltip=False
                )
                if unread:
                    status_cell.setForeground(QColor(COLORS["primary"]))
                    status_font = status_cell.font()
                    status_font.setBold(True)
                    status_cell.setFont(status_font)
                else:
                    status_cell.setForeground(QColor("#8A94A6"))
        self.panel.show_count(len(rows))

    def selected_notification(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        cell = self.table.item(row, 1) if row >= 0 else None
        if cell is None:
            return None
        return self.rows.get(int(cell.text()))

    def select_all(self) -> None:
        should_check = any(
            (cell := self.table.item(row, 0)) is not None
            and cell.checkState() != Qt.CheckState.Checked
            for row in range(self.table.rowCount())
        )
        toggle_all(self.table, should_check)
        self.select_all_button.setText("取消全选" if should_check else "全选")

    def _notification_form(
        self, title_text: str, item: dict[str, Any] | None = None
    ) -> tuple[QDialog, QComboBox, QLineEdit, QTextEdit, QComboBox]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title_text)
        dialog.resize(560, 360)
        form = QFormLayout(dialog)
        severity = QComboBox()
        for value in ("P0", "P1", "P2", "P3", "warning", "info"):
            severity.addItem(value)
        severity.setCurrentText(str(item.get("severity", "info")) if item else "info")
        title_field = QLineEdit(str(item.get("title", "")) if item else "")
        message = QTextEdit(str(item.get("message", "")) if item else "")
        message.setMaximumHeight(130)
        status = QComboBox()
        status.addItem("未读", False)
        status.addItem("已读", True)
        status.setCurrentIndex(1 if item and item.get("read_at") else 0)
        form.addRow("等级", severity)
        form.addRow("标题", title_field)
        form.addRow("内容", message)
        form.addRow("状态", status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        return dialog, severity, title_field, message, status

    def add_notification(self) -> None:
        dialog, severity, title_field, message, status = self._notification_form("新增应用播报")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.storage.create_notification(
                severity=severity.currentText(),
                title=title_field.text(),
                message=message.toPlainText(),
                read=bool(status.currentData()),
            )
        except Exception as exc:
            show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def edit_notification(self) -> None:
        item = self.selected_notification()
        if item is None:
            return
        dialog, severity, title_field, message, status = self._notification_form(
            "编辑应用播报", item
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.storage.update_notification(
                int(item["id"]),
                severity=severity.currentText(),
                title=title_field.text(),
                message=message.toPlainText(),
                read=bool(status.currentData()),
            )
        except Exception as exc:
            show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def mark_current_read(self) -> None:
        item = self.selected_notification()
        if item is not None:
            self.storage.mark_notification_read(int(item["id"]))
            self.refresh()
            self.changed.emit()

    def delete_current(self) -> None:
        item = self.selected_notification()
        if item is not None and confirm_destructive(
            self, "删除应用播报", f"确定删除“{item['title']}”吗？"
        ):
            self.storage.delete_notifications([int(item["id"])])
            self.refresh()
            self.changed.emit()

    def delete_selected(self) -> None:
        ids = checked_ids(self.table, 1)
        if not ids:
            return
        if confirm_destructive(self, "删除应用播报", f"确定删除选中的 {len(ids)} 条播报吗？"):
            self.storage.delete_notifications(ids)
            self.refresh()
            self.changed.emit()
            show_toast(self, f"已删除 {len(ids)} 条播报")

    def mark_selected_read(self) -> None:
        ids = checked_ids(self.table, 1)
        if not ids:
            return
        count = self.storage.mark_notifications_read(ids)
        self.refresh()
        self.changed.emit()
        show_toast(self, f"已标记 {count} 条为已读")

    def mark_all_read(self) -> None:
        count = self.storage.mark_all_notifications_read()
        self.refresh()
        self.changed.emit()
        show_toast(self, f"已标记 {count} 条为已读" if count else "没有未读播报")
