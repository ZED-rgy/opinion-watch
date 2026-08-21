"""品牌与关键词管理页。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
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
from opinion_watch.desktop.dialogs import confirm_destructive, show_error
from opinion_watch.desktop.utils import format_timestamp
from opinion_watch.storage import Storage


class KeywordsPage(QWidget):
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
        heading.addWidget(title("品牌与关键词"))
        subtitle = QLabel("维护自动巡检使用的品牌主体和搜索词")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(button("新增关键词", self.add_keyword, icon_name="fa6s.plus"))
        root.addLayout(header)
        card, layout = surface()
        brand_actions = QHBoxLayout()
        brand_actions.addWidget(QLabel("当前品牌"))
        self.brand_combo = QComboBox()
        brand_actions.addWidget(self.brand_combo, 1)
        brand_actions.addWidget(
            button("新增品牌", self.add_brand, icon_name="fa6s.plus", role="secondary")
        )
        brand_actions.addWidget(button("重命名", self.rename_brand, role="secondary"))
        brand_actions.addWidget(button("启用 / 停用", self.toggle_brand, role="secondary"))
        brand_actions.addWidget(button("删除品牌", self.delete_brand, role="danger"))
        layout.addLayout(brand_actions)
        keyword_actions = QHBoxLayout()
        keyword_actions.addWidget(QLabel("选中关键词："))
        self.rename_keyword_button = button("重命名", self.rename_keyword, role="secondary")
        self.toggle_keyword_button = button("启用 / 停用", self.toggle_keyword, role="secondary")
        self.delete_keyword_button = button("删除", self.delete_keyword, role="danger")
        keyword_actions.addWidget(self.rename_keyword_button)
        keyword_actions.addWidget(self.toggle_keyword_button)
        keyword_actions.addWidget(self.delete_keyword_button)
        keyword_actions.addStretch()
        layout.addLayout(keyword_actions)
        self.table = make_table(["ID", "品牌", "关键词", "状态", "更新时间"], sortable=True)
        self.table.setColumnHidden(0, True)
        bind_selection(
            self.table,
            self.rename_keyword_button,
            self.toggle_keyword_button,
            self.delete_keyword_button,
        )
        add_context_menu(
            self.table,
            [
                ("重命名关键词", self.rename_keyword),
                ("启用 / 停用", self.toggle_keyword),
                ("删除关键词", self.delete_keyword),
            ],
        )
        self.table.itemDoubleClicked.connect(lambda _item: self.rename_keyword())
        self.panel = TablePanel(
            self.table,
            EmptyState("暂无关键词", "为当前品牌添加一个关键词后，就可以参与自动巡检。"),
        )
        layout.addWidget(self.panel, 1)
        root.addWidget(card, 1)
        self.brand_combo.currentIndexChanged.connect(self.refresh_keywords)

    def refresh(self) -> None:
        current = self.brand_combo.currentText()
        self.brand_combo.blockSignals(True)
        self.brand_combo.clear()
        for brand in self.storage.list_brands():
            self.brand_combo.addItem(str(brand["name"]), brand)
        index = self.brand_combo.findText(current)
        self.brand_combo.setCurrentIndex(max(0, index))
        self.brand_combo.blockSignals(False)
        self.refresh_keywords()

    def refresh_keywords(self) -> None:
        brand = self.brand_combo.currentText() or None
        rows = self.storage.list_keywords(brand_name=brand)
        self.rows = {int(item["id"]): item for item in rows}
        with populate(self.table):
            self.table.setRowCount(len(rows))
            for row, item in enumerate(rows):
                values = (
                    item["id"],
                    item["brand_name"],
                    item["keyword"],
                    "启用" if item["enabled"] else "停用",
                    format_timestamp(item["updated_at"]),
                )
                for column, value in enumerate(values):
                    set_text_cell(self.table, row, column, value, tooltip=column == 2)
        self.panel.show_count(len(rows))

    def _after_change(self) -> None:
        self.refresh()
        self.changed.emit()

    def add_brand(self) -> None:
        name, ok = QInputDialog.getText(self, "新增品牌", "品牌或主体名称")
        if ok:
            try:
                self.storage.add_brand(name)
                self._after_change()
                self.brand_combo.setCurrentText(name.strip())
                show_toast(self, f"已添加品牌“{name.strip()}”")
            except Exception as exc:
                show_error(self, exc)

    def rename_brand(self) -> None:
        old_name = self.brand_combo.currentText()
        if not old_name:
            return
        new_name, ok = QInputDialog.getText(self, "重命名品牌", "新名称", text=old_name)
        if ok and new_name.strip():
            try:
                self.storage.rename_brand(old_name, new_name)
                self._after_change()
                self.brand_combo.setCurrentText(new_name.strip())
            except Exception as exc:
                show_error(self, exc)

    def toggle_brand(self) -> None:
        brand = self.brand_combo.currentData()
        if brand:
            self.storage.set_brand_enabled(str(brand["name"]), not bool(brand["enabled"]))
            self._after_change()

    def delete_brand(self) -> None:
        name = self.brand_combo.currentText()
        if name and confirm_destructive(
            self, "删除品牌", f"确认删除“{name}”及其关键词关联？此操作不可恢复。"
        ):
            self.storage.delete_brand(name)
            self._after_change()
            show_toast(self, f"已删除品牌“{name}”")

    def add_keyword(self) -> None:
        brand = self.brand_combo.currentText()
        if not brand:
            QMessageBox.information(self, "提示", "请先创建品牌。")
            return
        keyword, ok = QInputDialog.getText(self, "新增关键词", f"添加到“{brand}”")
        if ok:
            try:
                self.storage.add_keyword(brand, keyword)
                self._after_change()
                show_toast(self, f"已添加关键词“{keyword.strip()}”")
            except Exception as exc:
                show_error(self, exc)

    def rename_keyword(self) -> None:
        keyword_id = selected_id(self.table)
        if keyword_id is None:
            return
        current = str(self.rows[keyword_id]["keyword"])
        value, ok = QInputDialog.getText(self, "重命名关键词", "新关键词", text=current)
        if ok:
            try:
                self.storage.rename_keyword(keyword_id, value)
                self._after_change()
            except Exception as exc:
                show_error(self, exc)

    def toggle_keyword(self) -> None:
        keyword_id = selected_id(self.table)
        if keyword_id is not None:
            self.storage.set_keyword_enabled(keyword_id, not bool(self.rows[keyword_id]["enabled"]))
            self._after_change()

    def delete_keyword(self) -> None:
        keyword_id = selected_id(self.table)
        if keyword_id is None:
            return
        item = self.rows[keyword_id]
        if confirm_destructive(self, "删除关键词", f"确认删除“{item['keyword']}”？"):
            self.storage.delete_keyword(keyword_id)
            self._after_change()
            show_toast(self, f"已删除关键词“{item['keyword']}”")
