"""桌面端共享对话框与统一的确认/错误/长文本展示。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.desktop.components import button, make_table, title
from opinion_watch.desktop.constants import PLATFORM_NAMES, RUN_STATUS_NAMES
from opinion_watch.desktop.utils import format_timestamp
from opinion_watch.storage import Storage


def show_error(parent: QWidget, exc: Exception) -> None:
    QMessageBox.critical(parent, "操作失败", str(exc))


def confirm_destructive(parent: QWidget, heading: str, text: str) -> bool:
    """破坏性操作确认：默认按钮是“否”，回车不会误删。"""
    answer = QMessageBox.question(
        parent,
        heading,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def long_text_dialog(parent: QWidget, heading: str, text: str) -> None:
    """可缩放、可选中复制的只读文本查看器。"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(heading)
    dialog.resize(720, 480)
    layout = QVBoxLayout(dialog)
    viewer = QTextEdit()
    viewer.setReadOnly(True)
    viewer.setPlainText(text)
    layout.addWidget(viewer)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()


class RunDetailDialog(QDialog):
    def __init__(
        self,
        storage: Storage,
        run: dict[str, Any],
        diagnostic_output: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"巡检详情 · #{run['id']}")
        self.resize(1280, 760)
        self.setMinimumSize(980, 620)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)
        status = RUN_STATUS_NAMES.get(str(run["status"]), str(run["status"]))
        trigger = "定时巡检" if run.get("trigger") == "watch" else "手动巡检"
        platforms = "、".join(
            PLATFORM_NAMES.get(str(item), str(item)) for item in run.get("platforms", [])
        )
        model_summary = run.get("model_summary")
        if isinstance(model_summary, dict) and model_summary.get("enabled"):
            model_text = (
                f"大模型：已启用，处理 {model_summary.get('processed', 0)} 条，"
                f"失败 {model_summary.get('failed', 0)} 条"
            )
        else:
            model_text = "大模型：本次未启用"
        summary = QLabel(
            f"运行编号：#{run['id']}　标题：{run.get('title') or '未命名记录'}\n"
            f"状态：{status}　类型：{trigger}\n"
            f"开始：{format_timestamp(run.get('started_at'))}　"
            f"结束：{format_timestamp(run.get('finished_at'))}\n"
            f"平台：{platforms or '无'}　卡片扫描：{run.get('scanned_count', 0)} 条　"
            f"入库：{run.get('collected_count', 0)} 条　过滤：{run.get('filtered_count', 0)} 条　"
            f"关联内容：{run.get('content_count', run.get('linked_content_count', 0))} 条　"
            f"新增：{run.get('inserted_count', 0)}　更新：{run.get('updated_count', 0)}　"
            f"成功：{run.get('succeeded_count', 0)}　失败：{run.get('failed_count', 0)}\n"
            f"疑似：{run.get('suspected_count', 0)}　详情：{run.get('detailed_count', 0)}　"
            f"媒体证据：{run.get('media_count', 0)}\n"
            f"{model_text}"
        )
        summary.setWordWrap(True)
        root.addWidget(summary)
        if str(run.get("note") or "").strip():
            note_label = QLabel(f"备注：{run['note']}")
            note_label.setWordWrap(True)
            note_label.setObjectName("muted")
            root.addWidget(note_label)
        root.addWidget(title("关键词执行结果", "sectionTitle"))
        table = make_table(
            [
                "平台",
                "关键词",
                "状态",
                "耗时",
                "卡片扫描",
                "入库",
                "过滤",
                "疑似",
                "详情",
                "媒体",
                "新增",
                "更新",
                "错误",
            ]
        )
        attempts = run.get("attempts", [])
        table.setRowCount(len(attempts) if isinstance(attempts, list) else 0)
        for row, attempt in enumerate(attempts if isinstance(attempts, list) else []):
            values = (
                PLATFORM_NAMES.get(str(attempt.get("platform")), attempt.get("platform", "")),
                attempt.get("keyword", ""),
                RUN_STATUS_NAMES.get(str(attempt.get("status")), attempt.get("status", "")),
                f"{attempt.get('duration_seconds', 0):.1f} 秒"
                if attempt.get("duration_seconds") is not None
                else "—",
                attempt.get("scanned_count", 0),
                attempt.get("collected_count", 0),
                attempt.get("filtered_count", 0),
                attempt.get("suspected_count", 0),
                attempt.get("detailed_count", 0),
                attempt.get("media_count", 0),
                attempt.get("inserted_count", 0),
                attempt.get("updated_count", 0),
                attempt.get("error_message", "") or "—",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 12 and str(value) != "—":
                    item.setToolTip(str(value))
                table.setItem(row, column, item)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setMinimumHeight(160)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        header = table.horizontalHeader()
        for column in range(table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(12, QHeaderView.ResizeMode.Stretch)
        table.resizeRowsToContents()
        root.addWidget(table, 1)
        alerts = storage.list_alerts(run_id=int(run["id"]), unacknowledged_only=False)
        if alerts:
            root.addWidget(title("本次巡检告警", "sectionTitle"))
            alert_text = QTextEdit()
            alert_text.setObjectName("alertText")
            alert_text.setReadOnly(True)
            alert_text.setPlainText(
                "\n".join(
                    f"{PLATFORM_NAMES.get(str(item.get('platform')), item.get('platform', ''))}："
                    f"{item.get('message', '')}"
                    for item in alerts
                )
            )
            alert_text.setMinimumHeight(74)
            alert_text.setMaximumHeight(180)
            root.addWidget(alert_text)
        if diagnostic_output.strip():
            diagnostic = QTextEdit()
            diagnostic.setReadOnly(True)
            diagnostic.setPlainText(diagnostic_output)
            diagnostic.setVisible(False)
            diagnostic.setMaximumHeight(180)

            def toggle_diagnostic() -> None:
                diagnostic.setVisible(not diagnostic.isVisible())
                toggle.setText("隐藏原始诊断日志" if diagnostic.isVisible() else "显示原始诊断日志")

            toggle = button("显示原始诊断日志", toggle_diagnostic, role="secondary")
            root.addWidget(toggle)
            root.addWidget(diagnostic)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


def edit_scan_run_metadata(storage: Storage, run_id: int, parent: QWidget) -> bool:
    run = storage.get_scan_run(run_id)
    if run is None:
        return False
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"编辑巡检记录 · #{run_id}")
    dialog.resize(520, 260)
    form = QFormLayout(dialog)
    run_title = QLineEdit(str(run.get("title") or ""))
    note = QTextEdit(str(run.get("note") or ""))
    note.setPlaceholderText("记录本次巡检的背景、结论或后续动作（可选）")
    note.setMaximumHeight(110)
    form.addRow("记录标题", run_title)
    form.addRow("备注", note)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    try:
        updated = storage.update_scan_run_metadata(
            run_id, title=run_title.text(), note=note.toPlainText()
        )
    except ValueError as exc:
        QMessageBox.warning(parent, "保存失败", str(exc))
        return False
    return updated


def delete_scan_run_with_confirmation(storage: Storage, run_id: int, parent: QWidget) -> bool:
    run = storage.get_scan_run(run_id)
    if run is None:
        return False
    run_title = str(run.get("title") or f"巡检记录 #{run_id}")
    if not confirm_destructive(
        parent,
        "删除巡检记录",
        f"确定删除“{run_title}”吗？\n\n只会删除本次巡检批次及其关联关系，已采集内容仍会保留在全部历史中。",
    ):
        return False
    return storage.delete_scan_run(run_id)
