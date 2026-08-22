"""舆情中心页：筛选、复核、趋势与事件聚合。"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.desktop.charts import SeverityTrendChart
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
    selected_id,
    set_check_cell,
    set_text_cell,
    severity_item,
    show_toast,
    surface,
    title,
    toggle_all,
)
from opinion_watch.desktop.constants import (
    CATEGORY_NAMES,
    PLATFORM_NAMES,
    REVIEW_STATUS_NAMES,
    RUN_STATUS_NAMES,
    assessment_source_name,
)
from opinion_watch.desktop.dialogs import (
    confirm_destructive,
    delete_scan_run_with_confirmation,
    edit_scan_run_metadata,
    long_text_dialog,
    show_error,
)
from opinion_watch.desktop.tasks import StorageTaskRunner
from opinion_watch.desktop.theme import COLORS, SEVERITY_COLORS, repolish
from opinion_watch.desktop.utils import format_timestamp
from opinion_watch.models import OpinionCategory, RiskSeverity
from opinion_watch.storage import Storage


class OpinionsPage(QWidget):
    changed = Signal()

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.rows: dict[int, dict[str, Any]] = {}
        self.cluster_rows: dict[int, dict[str, Any]] = {}
        self._run_filter_signature: tuple[int, ...] = ()
        self.tasks = StorageTaskRunner(storage.database_path, self)
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(title("舆情中心"))
        subtitle = QLabel("集中查看、判断和复核巡检发现的内容")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(
            button("导出 CSV", self.export_csv, icon_name="fa6s.file-csv", role="secondary")
        )
        header.addWidget(button("刷新", self.refresh, icon_name="fa6s.rotate", role="secondary"))
        root.addLayout(header)
        trend_card, trend_layout = surface()
        trend_header = QHBoxLayout()
        trend_header.addWidget(title("最近 7 天风险趋势", "sectionTitle"))
        trend_header.addStretch()
        legend = QLabel(
            f'<span style="color:{COLORS["danger"]};">■</span> P1　'
            f'<span style="color:{SEVERITY_COLORS["P2"]};">■</span> P2　'
            f'<span style="color:{SEVERITY_COLORS["P3"]};">■</span> P3'
        )
        legend.setObjectName("muted")
        trend_header.addWidget(legend)
        trend_layout.addLayout(trend_header)
        self.trend_chart = SeverityTrendChart(days=7)
        trend_layout.addWidget(self.trend_chart)
        root.addWidget(trend_card)
        card, layout = surface()
        view_row = QHBoxLayout()
        view_row.addWidget(QLabel("视图"))
        self.view_mode = QComboBox()
        self.view_mode.addItem("内容明细", "items")
        self.view_mode.addItem("聚合事件", "clusters")
        self.view_mode.currentIndexChanged.connect(self._switch_view)
        view_row.addWidget(self.view_mode)
        view_row.addStretch()
        layout.addLayout(view_row)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("巡检批次"))
        self.run_filter = QComboBox()
        self.run_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.run_filter, 1)
        self.edit_run_button = button(
            "编辑记录", self.edit_selected_run, icon_name="fa6s.pen-to-square", role="secondary"
        )
        self.delete_run_button = button(
            "删除记录", self.delete_selected_run, icon_name="fa6s.trash", role="danger"
        )
        filters.addWidget(self.edit_run_button)
        filters.addWidget(self.delete_run_button)
        filters.addWidget(QLabel("平台"))
        self.platform_filter = QComboBox()
        self.platform_filter.addItem("全部平台", "")
        for value, name in PLATFORM_NAMES.items():
            self.platform_filter.addItem(name, value)
        self.platform_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.platform_filter)
        filters.addWidget(QLabel("来源"))
        self.source_filter = QComboBox()
        self.source_filter.addItem("全部来源", "")
        self.source_filter.addItem("规则", "rules")
        self.source_filter.addItem("大模型", "model")
        self.source_filter.addItem("人工", "manual")
        self.source_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.source_filter)
        filters.addWidget(QLabel("等级"))
        self.severity_filter = QComboBox()
        self.severity_filter.addItem("全部等级", "")
        for value in RiskSeverity:
            self.severity_filter.addItem(value.value, value.value)
        self.severity_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.severity_filter)
        layout.addLayout(filters)
        actions = QHBoxLayout()
        actions.addWidget(button("新增舆情", self.add_assessment, icon_name="fa6s.plus"))
        self.open_button = button(
            "打开原帖",
            self.open_source,
            icon_name="fa6s.arrow-up-right-from-square",
            role="secondary",
        )
        self.review_button = button("人工复核", self.review, role="secondary")
        self.edit_button = button("编辑选中", self.edit_assessment, role="secondary")
        self.delete_button = button("删除选中", self.delete_selected, role="danger")
        self.select_all_button = button("全选", self.select_all, role="secondary")
        actions.addWidget(self.open_button)
        actions.addWidget(self.review_button)
        actions.addWidget(self.edit_button)
        actions.addStretch()
        actions.addWidget(self.select_all_button)
        actions.addWidget(self.delete_button)
        layout.addLayout(actions)
        self.scope_label = QLabel()
        self.scope_label.setObjectName("muted")
        layout.addWidget(self.scope_label)
        self.table = make_table(
            [
                "选择",
                "内容ID",
                "等级",
                "类型",
                "平台",
                "品牌",
                "标题",
                "首次发现",
                "最近发现",
                "判定来源",
                "复核状态",
            ],
            multi_select=True,
        )
        self.table.setColumnHidden(1, True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        bind_selection(self.table, self.open_button, self.review_button, self.edit_button)
        bind_checked(self.table, self.delete_button)
        add_context_menu(
            self.table,
            [
                ("打开原帖", self.open_source),
                ("人工复核", self.review),
                ("编辑舆情", self.edit_assessment),
            ],
        )
        self.table.itemDoubleClicked.connect(lambda _item: self.open_source())
        self.table.itemSelectionChanged.connect(self._update_detail_panel)
        self.panel = TablePanel(
            self.table,
            EmptyState(
                "暂未发现舆情",
                "完成巡检后，命中品牌关键词的公开内容会在这里归档。",
                image_name="empty-scan.png",
            ),
        )
        self.detail_panel = self._build_detail_panel()
        # 聚合事件视图：同一事件（平台+品牌+相近标题）的多条内容合并展示。
        self.cluster_table = make_table(
            ["ID", "平台", "品牌", "代表标题", "关联内容", "最近发现"], sortable=True
        )
        self.cluster_table.setColumnHidden(0, True)
        self.cluster_table.itemDoubleClicked.connect(lambda _item: self.show_cluster_members())
        add_context_menu(self.cluster_table, [("查看事件内容", self.show_cluster_members)])
        self.cluster_panel = TablePanel(
            self.cluster_table,
            EmptyState(
                "暂无聚合事件",
                "当多条疑似或高风险内容指向同一事件时，会在这里自动聚合。",
            ),
        )
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.panel)
        self.view_stack.addWidget(self.cluster_panel)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("opinionSplit")
        split.setChildrenCollapsible(False)
        split.addWidget(self.view_stack)
        split.addWidget(self.detail_panel)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        layout.addWidget(split, 1)
        root.addWidget(card, 1)

    def _switch_view(self) -> None:
        clusters = self.view_mode.currentData() == "clusters"
        self.view_stack.setCurrentWidget(self.cluster_panel if clusters else self.panel)
        self.detail_panel.setVisible(not clusters and self.selected() is not None)
        if clusters:
            self._refresh_clusters()

    def _refresh_clusters(self) -> None:
        def load(storage: Storage) -> list[dict[str, Any]]:
            return storage.list_event_clusters(limit=100)

        def apply(rows: object) -> None:
            clusters = rows if isinstance(rows, list) else []
            self.cluster_rows = {int(item["id"]): item for item in clusters}
            with populate(self.cluster_table):
                self.cluster_table.setRowCount(len(clusters))
                for row, item in enumerate(clusters):
                    set_text_cell(self.cluster_table, row, 0, item["id"], tooltip=False)
                    set_text_cell(
                        self.cluster_table,
                        row,
                        1,
                        PLATFORM_NAMES.get(str(item["platform"]), item["platform"]),
                        tooltip=False,
                    )
                    set_text_cell(
                        self.cluster_table,
                        row,
                        2,
                        "、".join(str(value) for value in item.get("brand_names", [])),
                    )
                    set_text_cell(self.cluster_table, row, 3, item["representative_title"])
                    set_text_cell(
                        self.cluster_table, row, 4, f"{item['content_count']} 条", tooltip=False
                    )
                    set_text_cell(
                        self.cluster_table,
                        row,
                        5,
                        format_timestamp(item.get("last_seen_at")),
                        tooltip=False,
                    )
            self.cluster_panel.show_count(len(clusters))

        self.tasks.submit(load, apply, cancel_key="clusters")

    def show_cluster_members(self) -> None:
        cluster_id = selected_id(self.cluster_table)
        if cluster_id is None:
            return
        cluster = self.cluster_rows.get(cluster_id)
        if cluster is None:
            return
        members = self.storage.list_cluster_members(cluster_id)
        lines = [
            f"事件：{cluster['representative_title']}",
            f"平台：{PLATFORM_NAMES.get(str(cluster['platform']), cluster['platform'])}　"
            f"关联内容：{len(members)} 条",
            "",
        ]
        for member in members:
            lines.append(
                f"[{member.get('severity') or '—'}] "
                f"{str(member.get('title') or '（无标题）')[:60]}\n"
                f"    {member.get('url')}\n"
                f"    最近发现：{format_timestamp(member.get('last_seen_at'))}　"
                f"复核：{REVIEW_STATUS_NAMES.get(str(member.get('review_status')), '未判定')}"
            )
        long_text_dialog(self, f"聚合事件 · #{cluster_id}", "\n".join(lines))

    def export_csv(self) -> None:
        """把当前筛选条件下的舆情台账导出为 CSV（Excel 可直接打开）。"""
        if not self.rows:
            show_toast(self, "当前没有可导出的数据")
            return
        default_name = f"舆情台账-{date.today().isoformat()}.csv"
        path, _selected = QFileDialog.getSaveFileName(
            self, "导出舆情台账", default_name, "CSV 文件 (*.csv)"
        )
        if not path:
            return
        headers = [
            "内容ID",
            "等级",
            "类型",
            "平台",
            "品牌",
            "标题",
            "链接",
            "首次发现",
            "最近发现",
            "判定来源",
            "复核状态",
            "判断依据",
            "命中信号",
        ]
        try:
            # utf-8-sig 让 Excel 正确识别中文编码。
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                for item in self.rows.values():
                    writer.writerow(
                        [
                            item["content_item_id"],
                            item["severity"],
                            CATEGORY_NAMES.get(str(item["category"]), item["category"]),
                            PLATFORM_NAMES.get(str(item["platform"]), item["platform"]),
                            "、".join(str(value) for value in item["brand_names"]),
                            item["title"] or "",
                            item["url"] or "",
                            format_timestamp(item.get("discovered_at")),
                            format_timestamp(item.get("last_seen_at")),
                            assessment_source_name(str(item["source"])),
                            REVIEW_STATUS_NAMES.get(
                                str(item["review_status"]), item["review_status"]
                            ),
                            item["rationale"] or "",
                            "、".join(item["matched_signals"]),
                        ]
                    )
        except OSError as exc:
            show_error(self, exc)
            return
        show_toast(self, f"已导出 {len(self.rows)} 条到 {Path(path).name}")

    def _build_detail_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("detailPanel")
        panel.setMinimumWidth(320)
        panel.setMaximumWidth(420)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        badge_row = QHBoxLayout()
        self.detail_severity = QLabel()
        self.detail_severity.setProperty("pill", "severity")
        badge_row.addWidget(self.detail_severity)
        self.detail_category = QLabel()
        self.detail_category.setObjectName("sectionTitle")
        badge_row.addWidget(self.detail_category)
        badge_row.addStretch()
        layout.addLayout(badge_row)
        self.detail_title = QLabel()
        self.detail_title.setWordWrap(True)
        self.detail_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_title)
        self.detail_meta = QLabel()
        self.detail_meta.setObjectName("muted")
        self.detail_meta.setWordWrap(True)
        layout.addWidget(self.detail_meta)
        layout.addWidget(title("判断依据", "sectionTitle"))
        self.detail_rationale = QLabel()
        self.detail_rationale.setWordWrap(True)
        self.detail_rationale.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_rationale)
        self.detail_signals = QLabel()
        self.detail_signals.setObjectName("muted")
        self.detail_signals.setWordWrap(True)
        layout.addWidget(self.detail_signals)
        layout.addStretch()
        buttons = QHBoxLayout()
        buttons.addWidget(
            button("打开原帖", self.open_source, icon_name="fa6s.arrow-up-right-from-square")
        )
        buttons.addWidget(button("人工复核", self.review, role="secondary"))
        buttons.addStretch()
        layout.addLayout(buttons)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        panel.setVisible(False)
        return panel

    def _update_detail_panel(self) -> None:
        item = self.selected()
        if item is None:
            self.detail_panel.setVisible(False)
            return
        severity = str(item["severity"])
        self.detail_severity.setText(severity)
        level = severity if severity in {"P0", "P1", "P2"} else "P3"
        self.detail_severity.setProperty("level", level)
        repolish(self.detail_severity)
        category = CATEGORY_NAMES.get(str(item["category"]), str(item["category"]))
        self.detail_category.setText(category)
        self.detail_title.setText(str(item["title"] or "（无标题）"))
        keywords = "、".join(item.get("observed_keywords", [])) or "未记录"
        review_status = REVIEW_STATUS_NAMES.get(str(item["review_status"]), item["review_status"])
        self.detail_meta.setText(
            f"平台：{PLATFORM_NAMES.get(str(item['platform']), item['platform'])}　"
            f"品牌：{'、'.join(str(value) for value in item['brand_names']) or '未归属'}\n"
            f"首次发现：{format_timestamp(item.get('discovered_at'))}\n"
            f"最近发现：{format_timestamp(item.get('last_seen_at'))}\n"
            f"判定来源：{assessment_source_name(str(item['source']))}　"
            f"复核状态：{review_status}\n"
            f"命中关键词：{keywords}"
        )
        self.detail_rationale.setText(str(item["rationale"] or "（无）"))
        signals = "、".join(item["matched_signals"]) or "无"
        self.detail_signals.setText(f"命中信号：{signals}")
        self.detail_panel.setVisible(True)

    def refresh(self) -> None:
        previous_run = self.run_filter.currentData() if self.run_filter.count() else None
        runs = self.storage.list_scan_runs(limit=30)
        linked_runs = [run for run in runs if int(run.get("linked_content_count") or 0) > 0]
        signature = tuple(int(run["id"]) for run in linked_runs)
        if signature != self._run_filter_signature:
            self._run_filter_signature = signature
            self.run_filter.blockSignals(True)
            self.run_filter.clear()
            self.run_filter.addItem("全部历史", -1)
            for run in linked_runs:
                run_title = str(run.get("title") or f"巡检记录 #{run['id']}")
                self.run_filter.addItem(
                    f"{run_title} · #{run['id']} · {format_timestamp(run.get('started_at'))} · "
                    f"{RUN_STATUS_NAMES.get(str(run['status']), run['status'])}",
                    int(run["id"]),
                )
            if previous_run is None and linked_runs:
                self.run_filter.setCurrentIndex(1)
            else:
                index = self.run_filter.findData(previous_run)
                self.run_filter.setCurrentIndex(index if index >= 0 else 0)
            self.run_filter.blockSignals(False)
        selected_run = self.run_filter.currentData()
        run_id = int(selected_run) if selected_run not in (None, -1) else None
        source = self.source_filter.currentData() or None
        platform = self.platform_filter.currentData() or None
        severity = self.severity_filter.currentData() or None

        # 台账主查询（limit=1000，含每行多个相关子查询）在后台线程执行，
        # 巡检写库期间界面不再卡顿。
        def load(storage: Storage) -> list[dict[str, Any]]:
            return storage.list_assessments(
                limit=1000,
                run_id=run_id,
                source=source,
                platform=platform,
                severity=severity,
            )

        self.scope_label.setText("正在加载…")
        self.tasks.submit(
            load,
            lambda rows: self._apply_rows(rows if isinstance(rows, list) else [], run_id),
            on_error=lambda message: self.scope_label.setText(f"加载失败：{message[:80]}"),
            cancel_key="assessments",
        )
        self.tasks.submit(
            lambda storage: storage.severity_trend(days=7),
            lambda rows: self.trend_chart.set_data(rows if isinstance(rows, list) else []),
            cancel_key="trend",
        )
        if self.view_mode.currentData() == "clusters":
            self._refresh_clusters()
        has_selected_run = run_id is not None
        self.edit_run_button.setEnabled(has_selected_run)
        self.delete_run_button.setEnabled(has_selected_run)

    def _apply_rows(self, rows: list[dict[str, Any]], run_id: int | None) -> None:
        self.rows = {int(item["content_item_id"]): item for item in rows}
        self.scope_label.setText(
            f"当前显示巡检 #{run_id} 的数据，共 {len(rows)} 条"
            if run_id is not None
            else f"当前显示全部历史数据，共 {len(rows)} 条"
        )
        with populate(self.table):
            self.table.setRowCount(len(rows))
            for row, item in enumerate(rows):
                set_check_cell(self.table, row)
                set_text_cell(self.table, row, 1, item["content_item_id"], tooltip=False)
                self.table.setItem(
                    row, 2, severity_item(str(item["severity"]), str(item["severity"]))
                )
                set_text_cell(
                    self.table,
                    row,
                    3,
                    CATEGORY_NAMES.get(str(item["category"]), item["category"]),
                    tooltip=False,
                )
                set_text_cell(
                    self.table,
                    row,
                    4,
                    PLATFORM_NAMES.get(str(item["platform"]), item["platform"]),
                    tooltip=False,
                )
                set_text_cell(
                    self.table, row, 5, "、".join(str(value) for value in item["brand_names"])
                )
                set_text_cell(self.table, row, 6, item["title"] or "（无标题）")
                set_text_cell(
                    self.table, row, 7, format_timestamp(item.get("discovered_at")), tooltip=False
                )
                set_text_cell(
                    self.table, row, 8, format_timestamp(item.get("last_seen_at")), tooltip=False
                )
                set_text_cell(
                    self.table, row, 9, assessment_source_name(str(item["source"])), tooltip=False
                )
                review_status = str(item["review_status"])
                review_cell = set_text_cell(
                    self.table,
                    row,
                    10,
                    REVIEW_STATUS_NAMES.get(review_status, review_status),
                    tooltip=False,
                )
                review_color = {
                    "pending": COLORS["warning_text"],
                    "reviewed": COLORS["success"],
                }.get(review_status, "#8A94A6")
                review_cell.setForeground(QColor(review_color))
                if review_status == "pending":
                    font = review_cell.font()
                    font.setBold(True)
                    review_cell.setFont(font)
        self.panel.show_count(len(rows))
        self._update_detail_panel()

    def selected_run_id(self) -> int | None:
        value = self.run_filter.currentData()
        return int(value) if value not in (None, -1) else None

    def edit_selected_run(self) -> None:
        run_id = self.selected_run_id()
        if run_id is not None and edit_scan_run_metadata(self.storage, run_id, self):
            self.refresh()
            self.changed.emit()

    def delete_selected_run(self) -> None:
        run_id = self.selected_run_id()
        if run_id is None:
            return
        if delete_scan_run_with_confirmation(self.storage, run_id, self):
            self.refresh()
            self.changed.emit()

    def selected(self) -> dict[str, Any] | None:
        content_id = self._selected_assessment_id()
        return self.rows.get(content_id) if content_id is not None else None

    def _selected_assessment_id(self) -> int | None:
        row = self.table.currentRow()
        cell = self.table.item(row, 1) if row >= 0 else None
        return int(cell.text()) if cell is not None else None

    def select_all(self) -> None:
        should_check = any(
            (cell := self.table.item(row, 0)) is not None
            and cell.checkState() != Qt.CheckState.Checked
            for row in range(self.table.rowCount())
        )
        toggle_all(self.table, should_check)
        self.select_all_button.setText("取消全选" if should_check else "全选")

    def add_assessment(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("新增舆情记录")
        dialog.resize(560, 460)
        form = QFormLayout(dialog)
        platform = QComboBox()
        for value, name in PLATFORM_NAMES.items():
            platform.addItem(name, value)
        brand = QLineEdit()
        brand.setPlaceholderText("例如：速探长")
        title_field = QLineEdit()
        url = QLineEdit()
        url.setPlaceholderText("原帖链接")
        category = QComboBox()
        for option in OpinionCategory:
            category.addItem(CATEGORY_NAMES[option.value], option.value)
        severity = QComboBox()
        for option in RiskSeverity:
            severity.addItem(option.value)
        rationale = QTextEdit()
        rationale.setPlaceholderText("填写人工判断依据")
        rationale.setMaximumHeight(110)
        form.addRow("平台", platform)
        form.addRow("品牌", brand)
        form.addRow("标题", title_field)
        form.addRow("链接", url)
        form.addRow("分类", category)
        form.addRow("等级", severity)
        form.addRow("判断依据", rationale)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.storage.create_manual_assessment(
                platform=str(platform.currentData()),
                title=title_field.text(),
                url=url.text(),
                brand_name=brand.text(),
                category=str(category.currentData()),
                severity=severity.currentText(),
                rationale=rationale.toPlainText(),
            )
        except Exception as exc:
            show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def edit_assessment(self) -> None:
        item = self.selected()
        if item is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑舆情记录")
        dialog.resize(560, 420)
        form = QFormLayout(dialog)
        category = QComboBox()
        for option in OpinionCategory:
            category.addItem(CATEGORY_NAMES[option.value], option.value)
        category.setCurrentIndex(category.findData(str(item["category"])))
        severity = QComboBox()
        for option in RiskSeverity:
            severity.addItem(option.value)
        severity.setCurrentText(str(item["severity"]))
        status = QComboBox()
        for value, name in REVIEW_STATUS_NAMES.items():
            status.addItem(name, value)
        status.setCurrentIndex(status.findData(str(item["review_status"])))
        reviewer = QLineEdit(str(item.get("reviewed_by") or "运营人员"))
        rationale = QTextEdit(str(item.get("rationale") or ""))
        rationale.setMaximumHeight(120)
        form.addRow("分类", category)
        form.addRow("等级", severity)
        form.addRow("复核状态", status)
        form.addRow("审核人", reviewer)
        form.addRow("判断依据", rationale)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.storage.update_assessment(
                int(item["content_item_id"]),
                category=str(category.currentData()),
                severity=severity.currentText(),
                rationale=rationale.toPlainText(),
                review_status=str(status.currentData()),
                reviewer=reviewer.text(),
            )
        except Exception as exc:
            show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def delete_selected(self) -> None:
        ids = checked_ids(self.table, 1)
        if not ids:
            return
        if confirm_destructive(
            self,
            "删除舆情记录",
            f"确定删除选中的 {len(ids)} 条舆情记录吗？原始采集内容仍会保留。",
        ):
            self.storage.delete_assessments(ids)
            self.refresh()
            self.changed.emit()
            show_toast(self, f"已删除 {len(ids)} 条舆情记录")

    def open_source(self) -> None:
        item = self.selected()
        if item:
            QDesktopServices.openUrl(QUrl(str(item["url"])))

    def review(self) -> None:
        item = self.selected()
        if item is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("人工复核舆情")
        dialog.resize(560, 420)
        form = QFormLayout(dialog)
        category = QComboBox()
        for option in OpinionCategory:
            category.addItem(CATEGORY_NAMES[option.value], option.value)
        category.setCurrentIndex(category.findData(str(item["category"])))
        severity = QComboBox()
        for option in RiskSeverity:
            severity.addItem(option.value)
        severity.setCurrentText(str(item["severity"]))
        reviewer = QLineEdit("运营人员")
        note = QTextEdit()
        note.setPlaceholderText("填写人工核查结论和依据")
        note.setMaximumHeight(120)
        form.addRow("分类", category)
        form.addRow("等级", severity)
        form.addRow("审核人", reviewer)
        form.addRow("审核备注", note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.storage.review_assessment(
            int(item["content_item_id"]),
            category=str(category.currentData()),
            severity=severity.currentText(),
            note=note.toPlainText().strip(),
            reviewer=reviewer.text().strip() or "运营人员",
        )
        self.refresh()
        self.changed.emit()
        show_toast(self, "人工复核结论已保存")
