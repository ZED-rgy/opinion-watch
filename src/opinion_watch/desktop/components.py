"""桌面端共享 UI 组件与交互辅助。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import qtawesome as qta
from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.desktop.constants import ASSET_DIR
from opinion_watch.desktop.theme import SEVERITY_COLORS


def icon(name: str, color: str = "#5D667A") -> QIcon:
    return qta.icon(name, color=color)


def button(
    text: str,
    callback: Callable[[], object],
    *,
    icon_name: str | None = None,
    role: str = "primary",
) -> QPushButton:
    widget = QPushButton(text)
    widget.setProperty("role", role)
    if icon_name:
        widget.setIcon(icon(icon_name, "#FFFFFF" if role == "primary" else "#596277"))
        widget.setIconSize(QSize(15, 15))
    widget.clicked.connect(callback)
    return widget


def title(text: str, object_name: str = "pageTitle") -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    return label


def surface() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("surface")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(14)
    return frame, layout


def make_table(
    headers: list[str],
    *,
    multi_select: bool = False,
    sortable: bool = False,
) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(
        QAbstractItemView.SelectionMode.ExtendedSelection
        if multi_select
        else QAbstractItemView.SelectionMode.SingleSelection
    )
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(44)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setSortingEnabled(sortable)
    return table


@contextmanager
def populate(table: QTableWidget) -> Iterator[QTableWidget]:
    """填充期间禁用排序并屏蔽信号，避免行被重排、也避免 O(行数²) 的重算。

    bind_checked 挂在 itemChanged 上，而它每次都要遍历全表统计勾选状态。填充时
    每个 setItem 都会触发一轮全表遍历，千行表格能把主线程卡住几秒。填充结束后
    统一发一次 layoutChanged，让监听方补上最后一次刷新。
    """
    sorting = table.isSortingEnabled()
    table.setSortingEnabled(False)
    blocked = table.blockSignals(True)
    try:
        yield table
    finally:
        table.blockSignals(blocked)
        table.setSortingEnabled(sorting)
        table.model().layoutChanged.emit()


def set_text_cell(
    table: QTableWidget,
    row: int,
    column: int,
    value: object,
    *,
    tooltip: bool = True,
) -> QTableWidgetItem:
    text = str(value)
    item = QTableWidgetItem(text)
    if tooltip and text:
        item.setToolTip(text)
    table.setItem(row, column, item)
    return item


def severity_item(text: str, severity: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    color = SEVERITY_COLORS.get(severity)
    if color:
        item.setForeground(QColor(color))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


def selected_id(table: QTableWidget) -> int | None:
    row = table.currentRow()
    cell = table.item(row, 0) if row >= 0 else None
    return int(cell.text()) if cell is not None else None


def set_check_cell(table: QTableWidget, row: int, checked: bool = False) -> None:
    cell = QTableWidgetItem()
    cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    cell.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    table.setItem(row, 0, cell)


def checked_ids(table: QTableWidget, id_column: int) -> list[int]:
    values: list[int] = []
    for row in range(table.rowCount()):
        check_cell = table.item(row, 0)
        id_cell = table.item(row, id_column)
        if (
            check_cell is not None
            and check_cell.checkState() == Qt.CheckState.Checked
            and id_cell is not None
        ):
            values.append(int(id_cell.text()))
    return values


def toggle_all(table: QTableWidget, checked: bool) -> None:
    state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
    for row in range(table.rowCount()):
        cell = table.item(row, 0)
        if cell is not None:
            cell.setCheckState(state)


def bind_selection(table: QTableWidget, *widgets: QWidget) -> None:
    """选中行时才启用控件，替代“请先选择”提示弹窗。"""

    def apply() -> None:
        enabled = bool(table.selectedItems())
        for widget in widgets:
            widget.setEnabled(enabled)

    table.itemSelectionChanged.connect(apply)
    apply()


def bind_checked(table: QTableWidget, *widgets: QWidget) -> None:
    """任意行勾选时才启用控件（批量操作按钮）。"""

    def apply() -> None:
        enabled = any(
            (cell := table.item(row, 0)) is not None and cell.checkState() == Qt.CheckState.Checked
            for row in range(table.rowCount())
        )
        for widget in widgets:
            widget.setEnabled(enabled)

    table.itemChanged.connect(lambda _item: apply())
    table.model().rowsRemoved.connect(lambda *_args: apply())
    # populate() 填充期间屏蔽了 itemChanged，结束时补发 layoutChanged；
    # 不接这个信号的话，重新填充后按钮会停在上一批数据的启用状态。
    table.model().layoutChanged.connect(lambda *_args: apply())
    apply()


def add_context_menu(
    table: QTableWidget,
    actions: list[tuple[str, Callable[[], object]]],
) -> None:
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def open_menu(position: QPoint) -> None:
        if table.itemAt(position) is None:
            return
        menu = QMenu(table)
        for label, callback in actions:
            menu.addAction(label, callback)
        menu.exec(table.viewport().mapToGlobal(position))

    table.customContextMenuRequested.connect(open_menu)


def show_toast(parent: QWidget, text: str, *, duration_ms: int = 2_500) -> None:
    """主窗口底部的非阻断式提示，替代打断操作的信息弹窗。"""
    window = parent.window()
    toast = QLabel(text, window)
    toast.setObjectName("toast")
    toast.adjustSize()
    toast.move(
        (window.width() - toast.width()) // 2,
        window.height() - toast.height() - 46,
    )
    toast.show()
    toast.raise_()
    QTimer.singleShot(duration_ms, toast.deleteLater)


def run_busy(target: QPushButton, text: str = "处理中…") -> Callable[[], None]:
    """把按钮切换到忙碌态，返回恢复函数。"""
    original_text = target.text()
    original_icon = target.icon()
    target.setEnabled(False)
    target.setText(text)
    target.setIcon(qta.icon("fa6s.rotate", color="#A6AEBF", animation=qta.Spin(target)))

    def restore() -> None:
        target.setEnabled(True)
        target.setText(original_text)
        target.setIcon(original_icon)

    return restore


class EmptyState(QWidget):
    def __init__(
        self,
        heading: str,
        description: str,
        *,
        action_text: str | None = None,
        action: Callable[[], None] | None = None,
        image_name: str | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("emptyState")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(24, 28, 24, 28)
        layout.setSpacing(10)
        illustrated = False
        if image_name:
            pixmap = QPixmap(str(ASSET_DIR / image_name))
            if not pixmap.isNull():
                image = QLabel()
                image.setAlignment(Qt.AlignmentFlag.AlignCenter)
                image.setPixmap(
                    pixmap.scaled(
                        210,
                        150,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                layout.addWidget(image)
                illustrated = True
        if not illustrated:
            fallback = QLabel()
            fallback.setPixmap(icon("fa6s.inbox", "#8B96AA").pixmap(QSize(28, 28)))
            fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(fallback)
        headline = QLabel(heading)
        headline.setObjectName("emptyTitle")
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(headline)
        body = QLabel(description)
        body.setObjectName("muted")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        body.setMaximumWidth(430)
        layout.addWidget(body)
        self.action_button: QPushButton | None = None
        if action_text and action:
            self.action_button = button(action_text, action, icon_name="fa6s.play")
            self.action_button.setMaximumWidth(150)
            layout.addWidget(self.action_button, alignment=Qt.AlignmentFlag.AlignCenter)


class TablePanel(QStackedWidget):
    def __init__(self, table: QTableWidget, empty: EmptyState) -> None:
        super().__init__()
        self.setObjectName("surfaceStack")
        self.table = table
        self.empty = empty
        self.addWidget(table)
        self.addWidget(empty)

    def show_count(self, count: int) -> None:
        self.setCurrentWidget(self.table if count else self.empty)
