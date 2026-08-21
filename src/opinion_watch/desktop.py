from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import qtawesome as qta
from PySide6.QtCore import QProcess, QSettings, QSize, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.config import DEFAULT_BRANDS, Settings
from opinion_watch.credentials import CredentialStore
from opinion_watch.events import parse_event
from opinion_watch.models import OpinionCategory, Platform, RiskSeverity
from opinion_watch.services import ScheduleService
from opinion_watch.storage import Storage

ASSET_DIR = Path(__file__).with_name("assets")
AUTOSTART_REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

PLATFORM_NAMES = {
    Platform.DOUYIN.value: "抖音",
    Platform.XIAOHONGSHU.value: "小红书",
}
HOME_URLS = {
    Platform.DOUYIN.value: "https://www.douyin.com/",
    Platform.XIAOHONGSHU.value: "https://www.xiaohongshu.com/",
}
ACCOUNT_STATUS_NAMES = {
    "not_logged_in": "未登录",
    "ready": "可用",
    "login_required": "需要登录",
    "verification_required": "需要验证",
    "rate_limited": "访问受限",
    "error": "异常",
}
RUN_STATUS_NAMES = {
    "running": "巡检中",
    "interrupted": "已中断",
    "succeeded": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
}
REVIEW_STATUS_NAMES = {
    "pending": "待复核",
    "reviewed": "已复核",
    "not_required": "无需复核",
}

ASSESSMENT_SOURCE_NAMES = {"rules": "规则", "model": "大模型", "manual": "人工"}


def _assessment_source_name(source: str) -> str:
    return ASSESSMENT_SOURCE_NAMES.get(source, source)


def _windows_autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REGISTRY_PATH) as key:
            winreg.QueryValueEx(key, "OpinionWatch")
    except (FileNotFoundError, OSError):
        return False
    return True


def _set_windows_autostart(enabled: bool) -> None:
    if os.name != "nt":
        if enabled:
            raise RuntimeError("开机启动仅支持 Windows。")
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REGISTRY_PATH) as key:
        if enabled:
            launcher = Path(sys.argv[0]).resolve()
            if launcher.suffix.lower() == ".py" or launcher.name.lower().startswith("python"):
                command = f'"{sys.executable}" -m opinion_watch.desktop'
            else:
                command = f'"{launcher}"'
            winreg.SetValueEx(key, "OpinionWatch", 0, winreg.REG_SZ, command)
        else:
            with suppress(FileNotFoundError):
                winreg.DeleteValue(key, "OpinionWatch")


CATEGORY_NAMES = {
    OpinionCategory.SUSPECTED_FALSE_INFORMATION.value: "疑似虚假信息",
    OpinionCategory.SUSPECTED_DEFAMATION.value: "疑似恶意诽谤",
    OpinionCategory.COORDINATED_COMPLAINT.value: "集中投诉",
    OpinionCategory.SUSPECTED_ASTROTURFING.value: "疑似水军攻击",
    OpinionCategory.REASONABLE_CONSUMER_COMPLAINT.value: "合理消费者投诉",
    OpinionCategory.ORDINARY_GRIEVANCE.value: "普通吐槽",
    OpinionCategory.IRRELEVANT.value: "无关内容",
    OpinionCategory.OTHER.value: "其他",
}

APP_STYLE = """
QMainWindow, QWidget {
    background: #F7F8FA;
    color: #182033;
    font-size: 14px;
}
QWidget#page { background: #F7F8FA; }
QWidget#emptyState, QWidget#surfaceRow, QLabel, QCheckBox { background: transparent; }
QStackedWidget#surfaceStack { background: transparent; }
QFrame#sidebar { background: #FFFFFF; border-right: 1px solid #E8EAF0; }
QLabel#brandName { color: #14213D; font-size: 17px; font-weight: 700; }
QLabel#eyebrow { color: #71798A; font-size: 12px; font-weight: 600; }
QLabel#pageTitle { color: #10182B; font-size: 28px; font-weight: 700; }
QLabel#pageSubtitle { color: #697386; font-size: 14px; }
QLabel#sectionTitle { color: #172033; font-size: 17px; font-weight: 700; }
QLabel#muted { color: #778095; }
QLabel#heroTime { color: #10182B; font-size: 34px; font-weight: 700; }
QLabel#metric { color: #10182B; font-size: 24px; font-weight: 700; }
QFrame#surface, QFrame#hero {
    background: #FFFFFF;
    border: 1px solid #E5E8EF;
    border-radius: 14px;
}
QFrame#hero { border: 1px solid #DFE5F2; }
QFrame#statusPill { background: #EAF8F1; border: 1px solid #CDEEDD; border-radius: 16px; }
QFrame#notice { background: #FFF9E9; border: 1px solid #F4E4AF; border-radius: 10px; }
QFrame#divider { background: #E7EAF0; border: 0; }
QPushButton {
    min-height: 36px;
    padding: 0 15px;
    background: #2F5BEA;
    color: #FFFFFF;
    border: 1px solid #2F5BEA;
    border-radius: 8px;
    font-weight: 600;
}
QPushButton:hover { background: #244DD0; border-color: #244DD0; }
QPushButton:pressed { background: #1F43B8; }
QPushButton:focus { border: 2px solid #8DA8FF; }
QPushButton[role="secondary"] { background: #FFFFFF; color: #293248; border-color: #D9DEE8; }
QPushButton[role="secondary"]:hover { background: #F2F4F8; }
QPushButton[role="danger"] { background: #FFFFFF; color: #C93642; border-color: #F0C8CC; }
QPushButton[role="danger"]:hover { background: #FFF2F3; }
QComboBox, QSpinBox, QTimeEdit, QLineEdit, QTextEdit {
    min-height: 36px;
    background: #FFFFFF;
    color: #20283B;
    border: 1px solid #D8DDE7;
    border-radius: 8px;
    padding: 0 10px;
    selection-background-color: #DDE6FF;
}
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus, QLineEdit:focus,
QTextEdit:focus { border: 2px solid #2F5BEA; }
QTextEdit { padding: 8px; }
QCheckBox { spacing: 8px; color: #313A4D; }
QCheckBox::indicator { width: 34px; height: 18px; border-radius: 9px; background: #C9CFDA; }
QCheckBox::indicator:checked { background: #2F5BEA; }
QTableWidget {
    background: #FFFFFF;
    alternate-background-color: #FAFBFC;
    border: 0;
    gridline-color: transparent;
    selection-background-color: #EEF3FF;
    selection-color: #182033;
    outline: 0;
}
QTableWidget::item { border-bottom: 1px solid #EDF0F4; padding: 8px; }
QTableWidget::item:selected {
    background: #DCE7FF; color: #102A72; border-left: 3px solid #2F5BEA;
    font-weight: 600;
}
QHeaderView::section {
    background: #F6F7F9;
    color: #5D667A;
    border: 0;
    border-bottom: 1px solid #E5E8EE;
    padding: 10px 8px;
    font-weight: 600;
}
QListWidget#navigation { background: #FFFFFF; border: 0; outline: 0; padding: 6px 12px; }
QListWidget#navigation::item {
    color: #596277; padding: 11px 12px; border-radius: 8px; margin: 2px 0;
}
QListWidget#navigation::item:hover { background: #F3F5F9; color: #26334D; }
QListWidget#navigation::item:selected { background: #EAF0FF; color: #244BC5; font-weight: 600; }
QWidget#notificationNavRow, QWidget#notificationNavRow QLabel { background: transparent; }
QListWidget#timeline { background: transparent; border: 0; outline: 0; }
QListWidget#timeline::item {
    background: #F8F9FB; border: 1px solid #EAEDF2;
    border-radius: 8px; margin: 3px 0; padding: 10px;
}
QListWidget#timeline::item:selected {
    background: #DCE7FF; color: #102A72; border: 2px solid #2F5BEA;
    font-weight: 600;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #C9CFDA; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def _icon(name: str, color: str = "#5D667A"):
    return qta.icon(name, color=color)


def _button(
    text: str,
    callback: Callable[[], None],
    *,
    icon: str | None = None,
    role: str = "primary",
) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("role", role)
    if icon:
        button.setIcon(_icon(icon, "#FFFFFF" if role == "primary" else "#596277"))
        button.setIconSize(QSize(15, 15))
    button.clicked.connect(callback)
    return button


def _title(text: str, object_name: str = "pageTitle") -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    return label


def _surface() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("surface")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(14)
    return frame, layout


def _new_table(headers: list[str], *, multi_select: bool = False) -> QTableWidget:
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
    return table


def _selected_id(table: QTableWidget) -> int | None:
    row = table.currentRow()
    if row < 0 or table.item(row, 0) is None:
        return None
    return int(table.item(row, 0).text())


def _set_check_cell(table: QTableWidget, row: int, checked: bool = False) -> None:
    cell = QTableWidgetItem()
    cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    cell.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    table.setItem(row, 0, cell)


def _checked_ids(table: QTableWidget, id_column: int) -> list[int]:
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


def _toggle_all(table: QTableWidget, checked: bool) -> None:
    state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
    for row in range(table.rowCount()):
        cell = table.item(row, 0)
        if cell is not None:
            cell.setCheckState(state)


def _format_timestamp(value: object) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def _show_error(parent: QWidget, exc: Exception) -> None:
    QMessageBox.critical(parent, "操作失败", str(exc))


def _process_json_result(output: str) -> dict[str, object] | None:
    for line in reversed(output.splitlines()):
        try:
            candidate = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("status"):
            return candidate
    decoder = json.JSONDecoder()
    for start in (index for index, value in enumerate(output) if value == "{"):
        try:
            candidate, _ = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("status"):
            return candidate
    return None


class EmptyState(QWidget):
    def __init__(
        self,
        title: str,
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
        if image_name:
            image = QLabel()
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = QPixmap(str(ASSET_DIR / image_name))
            if not pixmap.isNull():
                image.setPixmap(
                    pixmap.scaled(
                        210,
                        150,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                layout.addWidget(image)
        icon = QLabel()
        icon.setPixmap(_icon("fa6s.inbox", "#8B96AA").pixmap(QSize(28, 28)))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        headline = QLabel(title)
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        headline.setStyleSheet("font-size: 16px; font-weight: 700; color: #30394C;")
        layout.addWidget(headline)
        body = QLabel(description)
        body.setObjectName("muted")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        body.setMaximumWidth(430)
        layout.addWidget(body)
        if action_text and action:
            button = _button(action_text, action, icon="fa6s.play")
            button.setMaximumWidth(150)
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)


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


class RunDetailDialog(QDialog):
    def __init__(
        self,
        storage: Storage,
        run: dict[str, object],
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
            f"开始：{_format_timestamp(run.get('started_at'))}　"
            f"结束：{_format_timestamp(run.get('finished_at'))}\n"
            f"平台：{platforms or '无'}　检索：{run.get('scanned_count', 0)} 条　"
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
        root.addWidget(_title("关键词执行结果", "sectionTitle"))
        table = _new_table(
            [
                "平台",
                "关键词",
                "状态",
                "检索",
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
                table.setItem(row, column, QTableWidgetItem(str(value)))
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideNone)
        table.setMinimumHeight(160)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        header = table.horizontalHeader()
        for column in range(table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(11, QHeaderView.ResizeMode.Stretch)
        table.resizeRowsToContents()
        root.addWidget(table, 1)
        alerts = storage.list_alerts(run_id=int(run["id"]), unacknowledged_only=False)
        if alerts:
            root.addWidget(_title("本次巡检告警", "sectionTitle"))
            alert_text = QTextEdit()
            alert_text.setReadOnly(True)
            alert_text.setPlainText(
                "\n".join(
                    f"{PLATFORM_NAMES.get(str(item.get('platform')), item.get('platform', ''))}："
                    f"{item.get('message', '')}"
                    for item in alerts
                )
            )
            alert_text.setStyleSheet("color:#A06B00;")
            alert_text.setMinimumHeight(74)
            alert_text.setMaximumHeight(180)
            root.addWidget(alert_text)
        if diagnostic_output.strip():
            diagnostic = QTextEdit()
            diagnostic.setReadOnly(True)
            diagnostic.setPlainText(diagnostic_output)
            diagnostic.setVisible(False)
            diagnostic.setMaximumHeight(180)
            toggle = _button(
                "显示原始诊断日志",
                lambda: diagnostic.setVisible(not diagnostic.isVisible()),
                role="secondary",
            )
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
    title = QLineEdit(str(run.get("title") or ""))
    note = QTextEdit(str(run.get("note") or ""))
    note.setPlaceholderText("记录本次巡检的背景、结论或后续动作（可选）")
    note.setMaximumHeight(110)
    form.addRow("记录标题", title)
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
            run_id, title=title.text(), note=note.toPlainText()
        )
    except ValueError as exc:
        QMessageBox.warning(parent, "保存失败", str(exc))
        return False
    return updated


def delete_scan_run_with_confirmation(storage: Storage, run_id: int, parent: QWidget) -> bool:
    run = storage.get_scan_run(run_id)
    if run is None:
        return False
    title = str(run.get("title") or f"巡检记录 #{run_id}")
    answer = QMessageBox.question(
        parent,
        "删除巡检记录",
        f"确定删除“{title}”吗？\n\n只会删除本次巡检批次及其关联关系，已采集内容仍会保留在全部历史中。",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return False
    return storage.delete_scan_run(run_id)


class SchedulerPage(QWidget):
    scan_finished = Signal()
    manage_scope_requested = Signal()

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.schedule_service = ScheduleService(storage)
        self.app_settings = QSettings("opinion-watch", "desktop")
        schedule_config = self.schedule_service.load()
        if not bool(schedule_config.get("legacy_imported")):
            legacy_frequency = str(self.app_settings.value("schedule_frequency", "daily"))
            legacy_time = str(self.app_settings.value("schedule_time", "09:00"))
            legacy_weekday = int(self.app_settings.value("schedule_weekday", 0))
            legacy_interval = int(self.app_settings.value("interval_minutes", 60))
            legacy_mode = str(self.app_settings.value("scan_mode", "quick"))
            legacy_concurrency = int(self.app_settings.value("scan_concurrency", 1))
            legacy_enabled = str(self.app_settings.value("auto_enabled", "false")).lower() == "true"
            self.schedule_service.save(
                enabled=legacy_enabled,
                frequency=legacy_frequency
                if legacy_frequency in {"daily", "weekly", "interval"}
                else "daily",
                schedule_time=legacy_time,
                weekday=max(0, min(6, legacy_weekday)),
                interval_minutes=max(5, min(1440, legacy_interval)),
                scan_mode=legacy_mode if legacy_mode in {"quick", "deep"} else "quick",
                concurrency=max(1, min(4, legacy_concurrency)),
                legacy_imported=True,
            )
            schedule_config = self.schedule_service.load()
        self.process = QProcess(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.next_run_at: datetime | None = None
        self.output_buffer: list[str] = []
        self.latest_run_id: int | None = None

        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(_title("自动巡检"))
        subtitle = QLabel("按计划检索品牌关键词，发现值得关注的公开内容")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        self.health_pill = QFrame()
        self.health_pill.setObjectName("statusPill")
        health_layout = QHBoxLayout(self.health_pill)
        health_layout.setContentsMargins(11, 6, 11, 6)
        health_layout.setSpacing(7)
        health_icon = QLabel()
        health_icon.setPixmap(_icon("fa6s.shield-halved", "#18845B").pixmap(QSize(14, 14)))
        health_layout.addWidget(health_icon)
        self.health_text = QLabel("正在检查账号")
        self.health_text.setStyleSheet("color: #176A4C; font-weight: 600;")
        health_layout.addWidget(self.health_text)
        header.addWidget(self.health_pill)
        header.addSpacing(8)
        header.addWidget(_button("立即巡检", self.start_scan, icon="fa6s.play"))
        root.addLayout(header)

        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumHeight(230)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        hero_layout.setSpacing(22)
        calendar = QLabel()
        calendar.setFixedSize(104, 104)
        calendar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        calendar.setStyleSheet("background:#EAF8F1; border-radius:28px;")
        calendar.setPixmap(_icon("fa6s.calendar-check", "#16936B").pixmap(QSize(46, 46)))
        hero_layout.addWidget(calendar)
        next_block = QVBoxLayout()
        next_block.setSpacing(4)
        next_label = QLabel("下次巡检")
        next_label.setObjectName("eyebrow")
        next_block.addWidget(next_label)
        self.next_run_label = QLabel("尚未计划")
        self.next_run_label.setObjectName("heroTime")
        next_block.addWidget(self.next_run_label)
        self.run_status = QLabel("开启定时巡检后，将按设定频次自动运行")
        self.run_status.setObjectName("muted")
        next_block.addWidget(self.run_status)
        hero_layout.addLayout(next_block, 1)
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedSize(1, 76)
        hero_layout.addWidget(divider)
        controls = QGridLayout()
        controls.setHorizontalSpacing(12)
        controls.setVerticalSpacing(8)
        self.auto_enabled = QCheckBox("启用定时巡检")
        self.auto_enabled.setChecked(bool(schedule_config.get("enabled")))
        self.frequency = QComboBox()
        self.frequency.addItem("每日一次", "daily")
        self.frequency.addItem("每周一次", "weekly")
        self.frequency.addItem("按间隔", "interval")
        saved_frequency = str(schedule_config.get("frequency") or "daily")
        frequency_index = self.frequency.findData(saved_frequency)
        self.frequency.setCurrentIndex(frequency_index if frequency_index >= 0 else 0)
        self.schedule_time = QTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm")
        saved_time = QTime.fromString(str(schedule_config.get("schedule_time") or "09:00"), "HH:mm")
        self.schedule_time.setTime(saved_time if saved_time.isValid() else QTime(9, 0))
        self.weekday = QComboBox()
        for index, name in enumerate(("周一", "周二", "周三", "周四", "周五", "周六", "周日")):
            self.weekday.addItem(name, index)
        saved_weekday = int(schedule_config.get("weekday") or 0)
        self.weekday.setCurrentIndex(max(0, min(6, saved_weekday)))
        self.interval = QSpinBox()
        self.interval.setRange(5, 1440)
        self.interval.setSuffix(" 分钟")
        self.interval.setValue(int(schedule_config.get("interval_minutes") or 60))
        self.scan_mode = QComboBox()
        self.scan_mode.addItem("快速巡检（每关键词至少 20 条）", "quick")
        self.scan_mode.addItem("深度巡检（每关键词至少 50 条）", "deep")
        saved_scan_mode = str(schedule_config.get("scan_mode") or "quick")
        scan_mode_index = self.scan_mode.findData(saved_scan_mode)
        self.scan_mode.setCurrentIndex(scan_mode_index if scan_mode_index >= 0 else 0)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 4)
        self.concurrency.setValue(max(1, min(4, int(schedule_config.get("concurrency") or 1))))
        self.concurrency.setToolTip(
            "每个并发页面使用同一个已登录账号档案；建议先保持 1，过高可能触发平台验证。"
        )
        controls.addWidget(self.auto_enabled, 0, 0, 1, 2)
        frequency_label = QLabel("巡检频次")
        frequency_label.setObjectName("muted")
        controls.addWidget(frequency_label, 1, 0)
        controls.addWidget(self.frequency, 1, 1)
        self.schedule_time_label = QLabel("执行时间")
        self.schedule_time_label.setObjectName("muted")
        controls.addWidget(self.schedule_time_label, 2, 0)
        controls.addWidget(self.schedule_time, 2, 1)
        self.weekday_label = QLabel("执行日")
        self.weekday_label.setObjectName("muted")
        controls.addWidget(self.weekday_label, 3, 0)
        controls.addWidget(self.weekday, 3, 1)
        self.interval_label = QLabel("巡检间隔")
        self.interval_label.setObjectName("muted")
        controls.addWidget(self.interval_label, 4, 0)
        controls.addWidget(self.interval, 4, 1)
        scan_mode_label = QLabel("检索模式")
        scan_mode_label.setObjectName("muted")
        controls.addWidget(scan_mode_label, 5, 0)
        controls.addWidget(self.scan_mode, 5, 1)
        concurrency_label = QLabel("并发页面（默认 1）")
        concurrency_label.setObjectName("muted")
        controls.addWidget(concurrency_label, 6, 0)
        controls.addWidget(self.concurrency, 6, 1)
        self.update_schedule_controls()
        hero_layout.addLayout(controls)
        root.addWidget(hero)

        content = QHBoxLayout()
        content.setSpacing(18)
        activity, activity_layout = _surface()
        activity_header = QHBoxLayout()
        activity_header.addWidget(_title("巡检动态", "sectionTitle"))
        activity_header.addStretch()
        self.detail_button = _button(
            "查看运行详情", self.toggle_details, icon="fa6s.terminal", role="secondary"
        )
        self.detail_button.setVisible(False)
        activity_header.addWidget(self.detail_button)
        self.edit_run_button = _button(
            "编辑记录", self.edit_selected_run, icon="fa6s.pen-to-square", role="secondary"
        )
        self.edit_run_button.setVisible(False)
        activity_header.addWidget(self.edit_run_button)
        self.delete_run_button = _button(
            "删除记录", self.delete_selected_run, icon="fa6s.trash", role="danger"
        )
        self.delete_run_button.setVisible(False)
        activity_header.addWidget(self.delete_run_button)
        activity_layout.addLayout(activity_header)
        self.timeline_stack = QStackedWidget()
        self.timeline_stack.setObjectName("surfaceStack")
        self.timeline = QListWidget()
        self.timeline.setObjectName("timeline")
        self.timeline.itemDoubleClicked.connect(lambda _item: self.show_run_detail())
        self.empty_timeline = EmptyState(
            "还没有巡检记录",
            "点击“立即巡检”完成第一次搜索，运行结果会在这里按时间整理。",
            action_text="立即巡检",
            action=self.start_scan,
            image_name="empty-scan.png",
        )
        self.timeline_stack.addWidget(self.timeline)
        self.timeline_stack.addWidget(self.empty_timeline)
        activity_layout.addWidget(self.timeline_stack, 1)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setVisible(False)
        self.output.setMaximumHeight(180)
        activity_layout.addWidget(self.output)
        content.addWidget(activity, 2)

        scope, scope_layout = _surface()
        scope.setMinimumWidth(300)
        scope.setMaximumWidth(380)
        scope_layout.addWidget(_title("监控范围", "sectionTitle"))
        scope_note = QLabel("当前启用的品牌、关键词与平台账号")
        scope_note.setObjectName("muted")
        scope_layout.addWidget(scope_note)
        metrics = QGridLayout()
        metrics.setVerticalSpacing(15)
        self.brand_metric = QLabel("0")
        self.keyword_metric = QLabel("0")
        self.brand_metric.setObjectName("metric")
        self.keyword_metric.setObjectName("metric")
        metrics.addWidget(self.brand_metric, 0, 0)
        metrics.addWidget(self.keyword_metric, 0, 1)
        metrics.addWidget(QLabel("启用品牌"), 1, 0)
        metrics.addWidget(QLabel("检索关键词"), 1, 1)
        scope_layout.addLayout(metrics)
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        scope_layout.addWidget(line)
        self.platform_rows = QVBoxLayout()
        self.platform_rows.setSpacing(10)
        scope_layout.addLayout(self.platform_rows)
        self.model_status_label = QLabel()
        self.model_status_label.setObjectName("muted")
        scope_layout.addWidget(self.model_status_label)
        self.wecom_status_label = QLabel()
        self.wecom_status_label.setObjectName("muted")
        scope_layout.addWidget(self.wecom_status_label)
        migration = QFrame()
        migration.setObjectName("notice")
        migration_layout = QHBoxLayout(migration)
        migration_layout.setContentsMargins(11, 10, 11, 10)
        migration_icon = QLabel()
        migration_icon.setPixmap(_icon("fa6s.circle-info", "#A06B00").pixmap(QSize(15, 15)))
        migration_layout.addWidget(migration_icon, alignment=Qt.AlignmentFlag.AlignTop)
        migration_text = QLabel(
            "自动巡检会使用账号对应的独立 Chrome 登录档案；首次切换后需重新登录。"
        )
        migration_text.setWordWrap(True)
        migration_text.setStyleSheet("color:#725214; font-size:12px;")
        migration_layout.addWidget(migration_text, 1)
        scope_layout.addStretch()
        scope_layout.addWidget(migration)
        scope_layout.addWidget(
            _button(
                "管理监控范围",
                self.manage_scope_requested.emit,
                icon="fa6s.sliders",
                role="secondary",
            )
        )
        content.addWidget(scope, 1)
        root.addLayout(content, 1)

        self.auto_enabled.toggled.connect(self.configure_timer)
        self.frequency.currentIndexChanged.connect(lambda _index: self.save_schedule())
        self.schedule_time.timeChanged.connect(lambda _time: self.save_schedule())
        self.weekday.currentIndexChanged.connect(lambda _index: self.save_schedule())
        self.interval.valueChanged.connect(lambda _value: self.save_schedule())
        self.scan_mode.currentIndexChanged.connect(lambda _index: self.save_scan_mode())
        self.concurrency.valueChanged.connect(lambda _value: self.save_scan_mode())
        self.timer.timeout.connect(self.scheduled_scan)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.process_finished)
        self.configure_timer(self.auto_enabled.isChecked())
        self.refresh()

    def update_schedule_controls(self) -> None:
        mode = str(self.frequency.currentData())
        self.schedule_time_label.setVisible(mode in {"daily", "weekly"})
        self.schedule_time.setVisible(mode in {"daily", "weekly"})
        self.weekday_label.setVisible(mode == "weekly")
        self.weekday.setVisible(mode == "weekly")
        self.interval_label.setVisible(mode == "interval")
        self.interval.setVisible(mode == "interval")

    def save_schedule(self) -> None:
        self.update_schedule_controls()
        if self.auto_enabled.isChecked():
            self._persist_schedule(next_run_at=None)
            self.configure_timer(True)
        else:
            self._persist_schedule(next_run_at=None)

    def save_scan_mode(self) -> None:
        self._persist_schedule()

    def _persist_schedule(
        self,
        *,
        next_run_at: datetime | None | object = ...,
        last_scheduled_at: datetime | None | object = ...,
    ) -> None:
        current = self.schedule_service.load()
        next_value = current.get("next_run_at") if next_run_at is ... else next_run_at
        last_value = (
            current.get("last_scheduled_at") if last_scheduled_at is ... else last_scheduled_at
        )
        self.schedule_service.save(
            enabled=self.auto_enabled.isChecked(),
            frequency=str(self.frequency.currentData()),
            schedule_time=self.schedule_time.time().toString("HH:mm"),
            weekday=int(self.weekday.currentData()),
            interval_minutes=self.interval.value(),
            scan_mode=str(self.scan_mode.currentData()),
            concurrency=self.concurrency.value(),
            last_scheduled_at=(
                last_value.isoformat()
                if isinstance(last_value, datetime)
                else str(last_value)
                if last_value
                else None
            ),
            next_run_at=(
                next_value.isoformat()
                if isinstance(next_value, datetime)
                else str(next_value)
                if next_value
                else None
            ),
            legacy_imported=True,
        )

    def _next_scheduled_datetime(self) -> datetime:
        now = datetime.now().astimezone()
        return self.schedule_service.next_run(
            {
                "frequency": self.frequency.currentData(),
                "schedule_time": self.schedule_time.time().toString("HH:mm"),
                "weekday": self.weekday.currentData(),
                "interval_minutes": self.interval.value(),
                "concurrency": self.concurrency.value(),
            },
            now,
        )

    def _set_next_run(self) -> None:
        self.next_run_at = self._next_scheduled_datetime()
        self.next_run_label.setText(self.next_run_at.strftime("%Y-%m-%d %H:%M"))
        self._persist_schedule(next_run_at=self.next_run_at)

    def configure_timer(self, enabled: bool) -> None:
        if enabled:
            saved_next = self.schedule_service.load().get("next_run_at")
            try:
                parsed_next = datetime.fromisoformat(str(saved_next)) if saved_next else None
            except ValueError:
                parsed_next = None
            now = datetime.now().astimezone()
            if parsed_next is not None and parsed_next > now:
                self.next_run_at = parsed_next
                self.next_run_label.setText(self.next_run_at.strftime("%Y-%m-%d %H:%M"))
            else:
                # A missed schedule is compensated once after restart, then the
                # normal frequency calculation resumes after that run.
                self.next_run_at = now + timedelta(seconds=1)
                self.next_run_label.setText(self.next_run_at.strftime("%Y-%m-%d %H:%M"))
            self._persist_schedule(next_run_at=self.next_run_at)
            delay = max(
                1000, int((self.next_run_at - datetime.now().astimezone()).total_seconds() * 1000)
            )
            self.timer.start(delay)
            if self.process.state() == QProcess.ProcessState.NotRunning:
                self.run_status.setText(f"已启用，{self.schedule_description()}自动巡检")
        else:
            self.timer.stop()
            self.next_run_at = None
            self.next_run_label.setText("尚未计划")
            self._persist_schedule(next_run_at=None)
            if self.process.state() == QProcess.ProcessState.NotRunning:
                self.run_status.setText("开启定时巡检后，将按设定频次自动运行")

    def schedule_description(self) -> str:
        mode = str(self.frequency.currentData())
        if mode == "weekly":
            return (
                f"每周{self.weekday.currentText()} {self.schedule_time.time().toString('HH:mm')} "
            )
        if mode == "interval":
            return f"每 {self.interval.value()} 分钟"
        return f"每日 {self.schedule_time.time().toString('HH:mm')} "

    def scheduled_scan(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._persist_schedule(
                last_scheduled_at=datetime.now(UTC),
                next_run_at=self.next_run_at,
            )
            self.start_scan(trigger="watch")
        elif self.auto_enabled.isChecked():
            self.configure_timer(True)

    def start_scan(self, *, trigger: str = "manual") -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.run_status.setText("巡检正在运行，请勿重复启动")
            return
        self.output_buffer.clear()
        self.output.clear()
        self.output.setVisible(False)
        self.detail_button.setVisible(False)
        self.run_status.setText("正在检索抖音和小红书…")
        self.process.setProgram(sys.executable)
        self.process.setArguments(
            [
                "-m",
                "opinion_watch",
                "scan",
                "--mode",
                str(self.scan_mode.currentData()),
                "--limit",
                "50" if self.scan_mode.currentData() == "deep" else "20",
                "--detail-limit",
                "50" if self.scan_mode.currentData() == "deep" else "20",
                "--comments-limit",
                "5",
                "--brand-delay-seconds",
                "5",
                "--concurrency",
                str(self.concurrency.value()),
                "--trigger",
                trigger,
            ]
        )
        self.process.start()

    def read_output(self) -> None:
        value = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        self.output_buffer.append(value)
        self.output.setPlainText("".join(self.output_buffer))
        for line in value.splitlines():
            event = parse_event(line)
            if event is not None and event.get("type") == "scan.finished":
                self.run_status.setText(
                    "巡检已完成" if event.get("status") == "succeeded" else "巡检未完成"
                )

    def read_error(self) -> None:
        value = bytes(self.process.readAllStandardError()).decode("utf-8", "replace")
        self.output_buffer.append(value)
        self.output.setPlainText("".join(self.output_buffer))

    def process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self.run_status.setText("巡检已完成" if exit_code == 0 else "巡检未完成，请查看运行详情")
        runs = self.storage.list_scan_runs(limit=1)
        self.latest_run_id = int(runs[0]["id"]) if runs else None
        self.detail_button.setVisible(self.latest_run_id is not None)
        self.edit_run_button.setVisible(self.latest_run_id is not None)
        self.delete_run_button.setVisible(self.latest_run_id is not None)
        if self.auto_enabled.isChecked():
            self.configure_timer(True)
        self.refresh()
        self.scan_finished.emit()

    def toggle_details(self) -> None:
        self.show_run_detail()

    def show_run_detail(self) -> None:
        item = self.timeline.currentItem()
        run_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else self.latest_run_id
        if run_id is None:
            return
        run = self.storage.get_scan_run(int(run_id))
        if run is None:
            return
        RunDetailDialog(self.storage, run, "".join(self.output_buffer), self).exec()

    def selected_run_id(self) -> int | None:
        item = self.timeline.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else self.latest_run_id
        return int(value) if value is not None else None

    def edit_selected_run(self) -> None:
        run_id = self.selected_run_id()
        if run_id is not None and edit_scan_run_metadata(self.storage, run_id, self):
            self.refresh()

    def delete_selected_run(self) -> None:
        run_id = self.selected_run_id()
        if run_id is None:
            return
        if delete_scan_run_with_confirmation(self.storage, run_id, self):
            self.latest_run_id = None
            self.refresh()

    def refresh(self) -> None:
        brands = self.storage.list_brands(enabled_only=True)
        keywords = self.storage.list_keywords(enabled_only=True)
        accounts = self.storage.list_accounts(enabled_only=True)
        self.brand_metric.setText(str(len(brands)))
        self.keyword_metric.setText(str(len(keywords)))
        ready = sum(1 for account in accounts if account["status"] == "ready")
        self.health_text.setText(f"{ready} 个账号可用" if accounts else "待配置账号")
        llm_enabled = bool(self.storage.get_llm_config().get("enabled"))
        self.model_status_label.setText(
            "大模型：已启用（仅复判疑似舆情）" if llm_enabled else "大模型：未启用（使用规则）"
        )
        wecom_config = self.storage.get_wecom_config()
        wecom_enabled = bool(wecom_config.get("enabled"))
        self.wecom_status_label.setText("企微日报：已启用" if wecom_enabled else "企微日报：未启用")

        while self.platform_rows.count():
            item = self.platform_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for platform, name in PLATFORM_NAMES.items():
            platform_accounts = [item for item in accounts if item["platform"] == platform]
            ready_count = sum(1 for item in platform_accounts if item["status"] == "ready")
            row = QWidget()
            row.setObjectName("surfaceRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            platform_icon = QLabel()
            platform_icon.setPixmap(_icon("fa6s.circle", "#2F5BEA").pixmap(QSize(8, 8)))
            row_layout.addWidget(platform_icon)
            row_layout.addWidget(QLabel(name))
            row_layout.addStretch()
            if ready_count:
                state_text = f"{ready_count} 个可用"
            elif platform_accounts:
                state_text = ACCOUNT_STATUS_NAMES.get(
                    str(platform_accounts[0]["status"]), str(platform_accounts[0]["status"])
                )
            else:
                state_text = "待配置"
            state = QLabel(state_text)
            state.setStyleSheet(
                "color:#18845B; font-weight:600;" if ready_count else "color:#8A6270;"
            )
            row_layout.addWidget(state)
            self.platform_rows.addWidget(row)

        runs = self.storage.list_scan_runs(limit=12)
        self.timeline.clear()
        for run in runs:
            status = str(run["status"])
            when = _format_timestamp(run.get("started_at"))
            collected = int(run.get("collected_count") or 0)
            brands_text = "、".join(str(value) for value in run.get("brands", [])) or "全部品牌"
            platforms_text = "、".join(
                PLATFORM_NAMES.get(str(value), str(value)) for value in run.get("platforms", [])
            )
            trigger = "定时" if run.get("trigger") == "watch" else "手动"
            title = str(run.get("title") or f"巡检记录 #{run['id']}")
            text = (
                f"{title}  ·  #{run['id']}  {RUN_STATUS_NAMES.get(status, status)}  ·  {when}\n"
                f"{trigger} · {platforms_text or '无平台'} · {brands_text} · "
                f"发现 {collected} 条 · 关联 {run.get('linked_content_count', 0)} 条"
            )
            item = QListWidgetItem(
                _icon(
                    "fa6s.circle-check" if status == "succeeded" else "fa6s.circle-exclamation",
                    "#18845B" if status == "succeeded" else "#D18A18",
                ),
                text,
            )
            item.setData(Qt.ItemDataRole.UserRole, int(run["id"]))
            item.setSizeHint(QSize(0, 62))
            self.timeline.addItem(item)
        self.timeline_stack.setCurrentWidget(self.timeline if runs else self.empty_timeline)
        if runs:
            self.timeline.setCurrentRow(0)
            self.latest_run_id = int(runs[0]["id"])
        has_runs = bool(runs)
        self.detail_button.setVisible(has_runs)
        self.edit_run_button.setVisible(has_runs)
        self.delete_run_button.setVisible(has_runs)


class KeywordsPage(QWidget):
    changed = Signal()

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.rows: dict[int, dict[str, object]] = {}
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(_title("品牌与关键词"))
        subtitle = QLabel("维护自动巡检使用的品牌主体和搜索词")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(_button("新增关键词", self.add_keyword, icon="fa6s.plus"))
        root.addLayout(header)
        surface, layout = _surface()
        brand_actions = QHBoxLayout()
        brand_actions.addWidget(QLabel("当前品牌"))
        self.brand_combo = QComboBox()
        brand_actions.addWidget(self.brand_combo, 1)
        brand_actions.addWidget(
            _button("新增品牌", self.add_brand, icon="fa6s.plus", role="secondary")
        )
        brand_actions.addWidget(_button("重命名", self.rename_brand, role="secondary"))
        brand_actions.addWidget(_button("启用 / 停用", self.toggle_brand, role="secondary"))
        brand_actions.addWidget(_button("删除品牌", self.delete_brand, role="danger"))
        layout.addLayout(brand_actions)
        keyword_actions = QHBoxLayout()
        keyword_actions.addWidget(_button("重命名关键词", self.rename_keyword, role="secondary"))
        keyword_actions.addWidget(_button("启用 / 停用", self.toggle_keyword, role="secondary"))
        keyword_actions.addWidget(_button("删除关键词", self.delete_keyword, role="danger"))
        keyword_actions.addStretch()
        layout.addLayout(keyword_actions)
        self.table = _new_table(["ID", "品牌", "关键词", "状态", "更新时间"])
        self.table.setColumnHidden(0, True)
        self.panel = TablePanel(
            self.table,
            EmptyState("暂无关键词", "为当前品牌添加一个关键词后，就可以参与自动巡检。"),
        )
        layout.addWidget(self.panel, 1)
        root.addWidget(surface, 1)
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
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            values = (
                item["id"],
                item["brand_name"],
                item["keyword"],
                "启用" if item["enabled"] else "停用",
                _format_timestamp(item["updated_at"]),
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
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
            except Exception as exc:
                _show_error(self, exc)

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
                _show_error(self, exc)

    def toggle_brand(self) -> None:
        brand = self.brand_combo.currentData()
        if brand:
            self.storage.set_brand_enabled(str(brand["name"]), not bool(brand["enabled"]))
            self._after_change()

    def delete_brand(self) -> None:
        name = self.brand_combo.currentText()
        if (
            name
            and QMessageBox.question(self, "删除品牌", f"确认删除“{name}”及其关键词关联？")
            == QMessageBox.StandardButton.Yes
        ):
            self.storage.delete_brand(name)
            self._after_change()

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
            except Exception as exc:
                _show_error(self, exc)

    def rename_keyword(self) -> None:
        keyword_id = _selected_id(self.table)
        if keyword_id is None:
            return
        current = str(self.rows[keyword_id]["keyword"])
        value, ok = QInputDialog.getText(self, "重命名关键词", "新关键词", text=current)
        if ok:
            try:
                self.storage.rename_keyword(keyword_id, value)
                self._after_change()
            except Exception as exc:
                _show_error(self, exc)

    def toggle_keyword(self) -> None:
        keyword_id = _selected_id(self.table)
        if keyword_id is not None:
            self.storage.set_keyword_enabled(keyword_id, not bool(self.rows[keyword_id]["enabled"]))
            self._after_change()

    def delete_keyword(self) -> None:
        keyword_id = _selected_id(self.table)
        if keyword_id is None:
            return
        item = self.rows[keyword_id]
        if (
            QMessageBox.question(self, "删除关键词", f"确认删除“{item['keyword']}”？")
            == QMessageBox.StandardButton.Yes
        ):
            self.storage.delete_keyword(keyword_id)
            self._after_change()


class AccountsPage(QWidget):
    changed = Signal()
    login_requested = Signal(object)

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.rows: dict[int, dict[str, object]] = {}
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(_title("平台账号"))
        subtitle = QLabel("每个账号使用与自动巡检一致的登录档案")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(_button("新增账号", self.add_account, icon="fa6s.plus"))
        root.addLayout(header)
        surface, layout = _surface()
        actions = QHBoxLayout()
        actions.addWidget(_button("打开自动巡检浏览器", self.open_login, icon="fa6s.globe"))
        actions.addWidget(_button("启用 / 停用", self.toggle_account, role="secondary"))
        actions.addWidget(_button("删除账号", self.delete_account, role="danger"))
        actions.addStretch()
        layout.addLayout(actions)
        self.table = _new_table(["ID", "平台", "账号名称", "登录状态", "状态", "最近检查"])
        self.table.setColumnHidden(0, True)
        self.panel = TablePanel(
            self.table,
            EmptyState(
                "还没有平台账号", "新增账号并在自动巡检浏览器中完成登录，登录态会独立保存在本机。"
            ),
        )
        layout.addWidget(self.panel, 1)
        root.addWidget(surface, 1)

    def refresh(self) -> None:
        rows = self.storage.list_accounts()
        self.rows = {int(item["id"]): item for item in rows}
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            values = (
                item["id"],
                PLATFORM_NAMES.get(str(item["platform"]), item["platform"]),
                item["display_name"],
                ACCOUNT_STATUS_NAMES.get(str(item["status"]), item["status"]),
                "启用" if item["enabled"] else "停用",
                _format_timestamp(item["last_checked_at"]),
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
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
        except Exception as exc:
            _show_error(self, exc)

    def selected_account(self) -> dict[str, object] | None:
        account_id = _selected_id(self.table)
        return self.rows.get(account_id) if account_id is not None else None

    def open_login(self) -> None:
        account = self.selected_account()
        if account is None:
            QMessageBox.information(self, "提示", "请先选择一个账号。")
            return
        self.login_requested.emit(account)

    def toggle_account(self) -> None:
        account = self.selected_account()
        if account:
            self.storage.set_account_enabled(int(account["id"]), not bool(account["enabled"]))
            self.refresh()
            self.changed.emit()

    def delete_account(self) -> None:
        account = self.selected_account()
        if (
            account
            and QMessageBox.question(
                self, "删除账号", "只删除账号记录，不删除本地浏览器档案。确认继续？"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.storage.delete_account(int(account["id"]))
            self.refresh()
            self.changed.emit()


class BrowserLoginWindow(QMainWindow):
    account_updated = Signal()

    def __init__(self, settings: Settings, storage: Storage, account: dict[str, object]) -> None:
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
        layout.addWidget(_title("自动巡检登录", "sectionTitle"))
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
        self.complete_button = _button("登录完成并检查", self.mark_ready, icon="fa6s.check")
        actions.addWidget(self.complete_button)
        actions.addWidget(_button("取消", self.cancel_login, role="secondary"))
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
        value = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        if value.strip():
            self.output_buffer.append(value)
            self.status_label.setText(value.strip().splitlines()[-1])

    def read_error(self) -> None:
        value = bytes(self.process.readAllStandardError()).decode("utf-8", "replace")
        if value.strip():
            self.output_buffer.append(value)

    def process_finished(self, _exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._cancelled:
            return
        output = "".join(self.output_buffer)
        result = _process_json_result(output)
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

    def closeEvent(self, event) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._cancelled = True
            self.process.write(b"\n")
            self.process.waitForFinished(10_000)
        event.accept()


class OpinionsPage(QWidget):
    changed = Signal()

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.rows: dict[int, dict[str, object]] = {}
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(_title("舆情中心"))
        subtitle = QLabel("集中查看、判断和复核巡检发现的内容")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(_button("刷新", self.refresh, icon="fa6s.rotate", role="secondary"))
        root.addLayout(header)
        surface, layout = _surface()
        actions = QHBoxLayout()
        actions.addWidget(
            _button("打开原帖", self.open_source, icon="fa6s.arrow-up-right-from-square")
        )
        actions.addWidget(_button("新增舆情", self.add_assessment, icon="fa6s.plus"))
        actions.addWidget(_button("编辑选中", self.edit_assessment, role="secondary"))
        actions.addWidget(_button("删除选中", self.delete_selected, role="danger"))
        actions.addWidget(_button("查看判断依据", self.show_detail, role="secondary"))
        actions.addWidget(_button("人工复核", self.review, role="secondary"))
        self.select_all_button = _button("全选", self.select_all, role="secondary")
        actions.addWidget(self.select_all_button)
        actions.addStretch()
        layout.addLayout(actions)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("巡检批次"))
        self.run_filter = QComboBox()
        self.run_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.run_filter, 1)
        self.edit_run_button = _button(
            "编辑记录", self.edit_selected_run, icon="fa6s.pen-to-square", role="secondary"
        )
        self.delete_run_button = _button(
            "删除记录", self.delete_selected_run, icon="fa6s.trash", role="danger"
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
        self.scope_label = QLabel()
        self.scope_label.setObjectName("muted")
        layout.addWidget(self.scope_label)
        self.table = _new_table(
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
        self.panel = TablePanel(
            self.table,
            EmptyState(
                "暂未发现舆情",
                "完成巡检后，命中品牌关键词的公开内容会在这里归档。",
                image_name="empty-scan.png",
            ),
        )
        layout.addWidget(self.panel, 1)
        root.addWidget(surface, 1)

    def refresh(self) -> None:
        previous_run = self.run_filter.currentData() if self.run_filter.count() else None
        runs = self.storage.list_scan_runs(limit=30)
        self.run_filter.blockSignals(True)
        self.run_filter.clear()
        linked_runs = [run for run in runs if int(run.get("linked_content_count") or 0) > 0]
        self.run_filter.addItem("全部历史", -1)
        for run in linked_runs:
            title = str(run.get("title") or f"巡检记录 #{run['id']}")
            self.run_filter.addItem(
                f"{title} · #{run['id']} · {_format_timestamp(run.get('started_at'))} · "
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
        rows = self.storage.list_assessments(
            limit=1000,
            run_id=run_id,
            source=self.source_filter.currentData() or None,
            platform=self.platform_filter.currentData() or None,
            severity=self.severity_filter.currentData() or None,
        )
        self.rows = {int(item["content_item_id"]): item for item in rows}
        self.table.setRowCount(len(rows))
        self.scope_label.setText(
            f"当前显示巡检 #{run_id} 的数据，共 {len(rows)} 条"
            if run_id is not None
            else f"当前显示全部历史数据，共 {len(rows)} 条"
        )
        for row, item in enumerate(rows):
            values = (
                item["content_item_id"],
                item["severity"],
                CATEGORY_NAMES.get(str(item["category"]), item["category"]),
                PLATFORM_NAMES.get(str(item["platform"]), item["platform"]),
                "、".join(str(value) for value in item["brand_names"]),
                item["title"] or "（无标题）",
                _format_timestamp(item.get("discovered_at")),
                _format_timestamp(item.get("last_seen_at")),
                {"rules": "规则", "model": "大模型", "manual": "人工"}.get(
                    str(item["source"]), str(item["source"])
                ),
                REVIEW_STATUS_NAMES.get(str(item["review_status"]), item["review_status"]),
            )
            _set_check_cell(self.table, row)
            for column, value in enumerate(values, start=1):
                cell = QTableWidgetItem(str(value))
                if column == 2:
                    cell.setForeground(
                        QColor(
                            {"P0": "#C93642", "P1": "#D46B22", "P2": "#A06B00"}.get(
                                str(value), "#667085"
                            )
                        )
                    )
                self.table.setItem(row, column, cell)
        self.panel.show_count(len(rows))
        has_selected_run = run_id is not None
        self.edit_run_button.setEnabled(has_selected_run)
        self.delete_run_button.setEnabled(has_selected_run)

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

    def selected(self) -> dict[str, object] | None:
        content_id = self._selected_assessment_id()
        return self.rows.get(content_id) if content_id is not None else None

    def _selected_assessment_id(self) -> int | None:
        row = self.table.currentRow()
        cell = self.table.item(row, 1) if row >= 0 else None
        return int(cell.text()) if cell is not None else None

    def select_all(self) -> None:
        should_check = any(
            self.table.item(row, 0).checkState() != Qt.CheckState.Checked
            for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
        )
        _toggle_all(self.table, should_check)
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
        title = QLineEdit()
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
        form.addRow("标题", title)
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
                title=title.text(),
                url=url.text(),
                brand_name=brand.text(),
                category=str(category.currentData()),
                severity=severity.currentText(),
                rationale=rationale.toPlainText(),
            )
        except Exception as exc:
            _show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def edit_assessment(self) -> None:
        item = self.selected()
        if item is None:
            QMessageBox.information(self, "提示", "请先选择一条舆情记录。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑舆情记录")
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
            _show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def delete_selected(self) -> None:
        ids = _checked_ids(self.table, 1)
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要删除的舆情记录。")
            return
        answer = QMessageBox.question(
            self,
            "删除舆情记录",
            f"确定删除选中的 {len(ids)} 条舆情记录吗？原始采集内容仍会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.storage.delete_assessments(ids)
            self.refresh()
            self.changed.emit()

    def open_source(self) -> None:
        item = self.selected()
        if item:
            QDesktopServices.openUrl(QUrl(str(item["url"])))

    def show_detail(self) -> None:
        item = self.selected()
        if item:
            QMessageBox.information(
                self,
                (
                    f"{item['severity']} · "
                    f"{CATEGORY_NAMES.get(str(item['category']), item['category'])}"
                ),
                f"首次发现：{_format_timestamp(item.get('discovered_at'))}\n"
                f"最近发现：{_format_timestamp(item.get('last_seen_at'))}\n"
                f"判定来源：{_assessment_source_name(str(item['source']))}\n"
                f"命中关键词：{'、'.join(item.get('observed_keywords', [])) or '未记录'}\n\n"
                f"{item['rationale']}\n\n命中信号：{'、'.join(item['matched_signals']) or '无'}",
            )

    def review(self) -> None:
        item = self.selected()
        if item is None:
            QMessageBox.information(self, "提示", "请先选择一条舆情记录。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("人工复核舆情")
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


class NotificationsPage(QWidget):
    changed = Signal()

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(_title("应用内播报"))
        subtitle = QLabel("重要发现和巡检异常会集中出现在这里")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(_button("新增播报", self.add_notification, icon="fa6s.plus"))
        header.addWidget(_button("编辑选中", self.edit_notification, role="secondary"))
        header.addWidget(_button("删除选中", self.delete_selected, role="danger"))
        header.addWidget(
            _button("全部标记已读", self.mark_all_read, icon="fa6s.check-double", role="secondary")
        )
        root.addLayout(header)
        surface, layout = _surface()
        actions = QHBoxLayout()
        self.select_all_button = _button("全选", self.select_all, role="secondary")
        actions.addWidget(self.select_all_button)
        actions.addWidget(_button("标记选中已读", self.mark_selected_read, role="secondary"))
        actions.addStretch()
        layout.addLayout(actions)
        self.table = _new_table(
            ["选择", "ID", "等级", "标题", "内容", "时间", "状态"], multi_select=True
        )
        self.table.setColumnHidden(1, True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.panel = TablePanel(
            self.table,
            EmptyState("暂无播报", "出现需要关注的新舆情或巡检异常时，这里会第一时间提醒你。"),
        )
        layout.addWidget(self.panel, 1)
        root.addWidget(surface, 1)

    def refresh(self) -> None:
        rows = self.storage.list_notifications(limit=1000)
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            _set_check_cell(self.table, row)
            values = (
                item["id"],
                item["severity"],
                item["title"],
                item["message"],
                _format_timestamp(item["created_at"]),
                "未读" if item["read_at"] is None else "已读",
            )
            for column, value in enumerate(values, start=1):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.panel.show_count(len(rows))

    def selected_notification(self) -> dict[str, object] | None:
        row = self.table.currentRow()
        cell = self.table.item(row, 1) if row >= 0 else None
        if cell is None:
            return None
        notification_id = int(cell.text())
        return next(
            (
                item
                for item in self.storage.list_notifications(limit=1000)
                if int(item["id"]) == notification_id
            ),
            None,
        )

    def select_all(self) -> None:
        should_check = any(
            self.table.item(row, 0).checkState() != Qt.CheckState.Checked
            for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
        )
        _toggle_all(self.table, should_check)
        self.select_all_button.setText("取消全选" if should_check else "全选")

    def _notification_form(
        self, title_text: str, item: dict[str, object] | None = None
    ) -> tuple[QDialog, QComboBox, QLineEdit, QTextEdit, QComboBox]:
        dialog = QDialog(self)
        dialog.setWindowTitle(title_text)
        dialog.resize(560, 360)
        form = QFormLayout(dialog)
        severity = QComboBox()
        for value in ("P0", "P1", "P2", "P3", "warning", "info"):
            severity.addItem(value)
        severity.setCurrentText(str(item.get("severity", "info")) if item else "info")
        title = QLineEdit(str(item.get("title", "")) if item else "")
        message = QTextEdit(str(item.get("message", "")) if item else "")
        message.setMaximumHeight(130)
        status = QComboBox()
        status.addItem("未读", False)
        status.addItem("已读", True)
        status.setCurrentIndex(1 if item and item.get("read_at") else 0)
        form.addRow("等级", severity)
        form.addRow("标题", title)
        form.addRow("内容", message)
        form.addRow("状态", status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        return dialog, severity, title, message, status

    def add_notification(self) -> None:
        dialog, severity, title, message, status = self._notification_form("新增应用播报")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.storage.create_notification(
                severity=severity.currentText(),
                title=title.text(),
                message=message.toPlainText(),
                read=bool(status.currentData()),
            )
        except Exception as exc:
            _show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def edit_notification(self) -> None:
        item = self.selected_notification()
        if item is None:
            QMessageBox.information(self, "提示", "请先选择一条播报。")
            return
        dialog, severity, title, message, status = self._notification_form("编辑应用播报", item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.storage.update_notification(
                int(item["id"]),
                severity=severity.currentText(),
                title=title.text(),
                message=message.toPlainText(),
                read=bool(status.currentData()),
            )
        except Exception as exc:
            _show_error(self, exc)
            return
        self.refresh()
        self.changed.emit()

    def delete_selected(self) -> None:
        ids = _checked_ids(self.table, 1)
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要删除的播报。")
            return
        answer = QMessageBox.question(
            self,
            "删除应用播报",
            f"确定删除选中的 {len(ids)} 条播报吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.storage.delete_notifications(ids)
            self.refresh()
            self.changed.emit()

    def mark_selected_read(self) -> None:
        ids = _checked_ids(self.table, 1)
        if not ids:
            QMessageBox.information(self, "提示", "请先勾选要标记的播报。")
            return
        self.storage.mark_notifications_read(ids)
        self.refresh()
        self.changed.emit()

    def mark_all_read(self) -> None:
        for item in self.storage.list_notifications(unread_only=True, limit=1000):
            self.storage.mark_notification_read(int(item["id"]))
        self.refresh()
        self.changed.emit()


class SettingsPage(QWidget):
    def __init__(self, settings: Settings, storage: Storage) -> None:
        super().__init__()
        self.settings = settings
        self.storage = storage
        self.wecom_test_process = QProcess(self)
        self.wecom_test_process.finished.connect(self.wecom_test_finished)
        self.wecom_discover_process = QProcess(self)
        self.wecom_discover_process.finished.connect(self.wecom_discover_finished)
        self.llm_test_process = QProcess(self)
        self.llm_test_process.finished.connect(self.llm_test_finished)
        self.setObjectName("page")
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(30, 26, 30, 28)
        page_layout.setSpacing(18)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(18)
        root.addWidget(_title("设置"))
        subtitle = QLabel("查看本地数据、隐私边界和当前版本")
        subtitle.setObjectName("pageSubtitle")
        root.addWidget(subtitle)
        surface, layout = _surface()
        layout.addWidget(_title("数据与隐私", "sectionTitle"))
        privacy = QLabel(
            "账号登录态仅保存在本机独立浏览器档案中；应用不会导出 Cookie，也不会记录账号密码。"
        )
        privacy.setWordWrap(True)
        privacy.setObjectName("muted")
        layout.addWidget(privacy)
        self.counts_label = QLabel()
        self.counts_label.setWordWrap(True)
        layout.addWidget(self.counts_label)
        layout.addWidget(_title("企微智能机器人日报", "sectionTitle"))
        wecom_note = QLabel(
            "日报在定时巡检当天首次成功后发送一次。Secret 仅保存到 Windows 凭据管理器；"
            "群聊 ID 需要从企微智能机器人所在群聊的回调消息中获取。"
        )
        wecom_note.setWordWrap(True)
        wecom_note.setObjectName("muted")
        layout.addWidget(wecom_note)
        self.wecom_enabled = QCheckBox("启用企微日报")
        layout.addWidget(self.wecom_enabled)
        wecom_form = QFormLayout()
        self.wecom_bot_id = QLineEdit()
        self.wecom_bot_id.setPlaceholderText("企业微信智能机器人 Bot ID")
        self.wecom_secret = QLineEdit()
        self.wecom_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.wecom_secret.setPlaceholderText("留空表示保留已保存 Secret")
        self.wecom_chat_id = QLineEdit()
        self.wecom_chat_id.setPlaceholderText("目标群聊 chatid")
        wecom_form.addRow("Bot ID", self.wecom_bot_id)
        wecom_form.addRow("Secret", self.wecom_secret)
        wecom_form.addRow("群聊 ID", self.wecom_chat_id)
        layout.addLayout(wecom_form)
        wecom_actions = QHBoxLayout()
        wecom_actions.addWidget(_button("保存企微配置", self.save_wecom, icon="fa6s.floppy-disk"))
        self.wecom_test_button = _button(
            "发送测试消息", self.test_wecom, icon="fa6s.paper-plane", role="secondary"
        )
        wecom_actions.addWidget(self.wecom_test_button)
        self.wecom_discover_button = _button(
            "监听群聊 ID", self.discover_wecom, icon="fa6s.tower-broadcast", role="secondary"
        )
        wecom_actions.addWidget(self.wecom_discover_button)
        wecom_actions.addStretch()
        layout.addLayout(wecom_actions)
        self.autostart_enabled = QCheckBox("启用 Windows 开机启动")
        self.autostart_enabled.setToolTip(
            "仅写入当前 Windows 用户的启动项；关闭应用后不会继续驻留后台。"
        )
        self.autostart_enabled.toggled.connect(self.save_autostart)
        layout.addWidget(self.autostart_enabled)
        layout.addWidget(_title("大模型辅助研判", "sectionTitle"))
        llm_note = QLabel(
            "可选的文本二次复判：规则先筛选疑似舆情，再调用兼容 OpenAI chat/completions 的接口；"
            "当前仅发送标题、正文和评论等文本证据，不发送图片或视频。"
            "例如 DeepSeek 使用 https://api.deepseek.com，OpenAI 使用 https://api.openai.com/v1。"
            " API Key 仅保存到 Windows 凭据管理器；关闭后不影响规则巡检。"
        )
        llm_note.setWordWrap(True)
        llm_note.setObjectName("muted")
        layout.addWidget(llm_note)
        self.llm_enabled = QCheckBox("启用大模型复判")
        layout.addWidget(self.llm_enabled)
        llm_form = QFormLayout()
        self.llm_provider = QLineEdit()
        self.llm_provider.setPlaceholderText("openai-compatible")
        self.llm_base_url = QLineEdit()
        self.llm_base_url.setPlaceholderText("https://api.openai.com/v1")
        self.llm_model = QLineEdit()
        self.llm_model.setPlaceholderText("模型名称，例如 gpt-4o-mini")
        self.llm_api_key = QLineEdit()
        self.llm_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_api_key.setPlaceholderText("留空表示保留已保存 API Key")
        self.llm_limit = QSpinBox()
        self.llm_limit.setRange(1, 100)
        self.llm_limit.setSuffix(" 条")
        llm_form.addRow("提供商", self.llm_provider)
        llm_form.addRow("Base URL", self.llm_base_url)
        llm_form.addRow("模型", self.llm_model)
        llm_form.addRow("API Key", self.llm_api_key)
        llm_form.addRow("每次最多复判", self.llm_limit)
        llm_form.setVerticalSpacing(8)
        layout.addLayout(llm_form)
        llm_actions = QHBoxLayout()
        llm_actions.addWidget(
            _button("保存大模型配置", lambda: self.save_llm(), icon="fa6s.floppy-disk")
        )
        self.llm_test_button = _button(
            "测试连接", self.test_llm, icon="fa6s.plug", role="secondary"
        )
        llm_actions.addWidget(self.llm_test_button)
        llm_actions.addStretch()
        layout.addLayout(llm_actions)
        actions = QHBoxLayout()
        actions.addWidget(
            _button("打开运行目录", self.open_runtime, icon="fa6s.folder-open", role="secondary")
        )
        actions.addWidget(
            _button("打开备份目录", self.open_backups, icon="fa6s.box-archive", role="secondary")
        )
        actions.addStretch()
        layout.addLayout(actions)
        root.addWidget(surface)
        version, version_layout = _surface()
        version_layout.addWidget(_title("版本信息", "sectionTitle"))
        version_layout.addWidget(QLabel("品牌舆情监控  v0.2.0"))
        note = QLabel(
            "自动巡检与平台账号页使用同一个独立 Playwright 登录档案；请在“打开自动巡检浏览器”"
            "中完成登录，不要使用普通 Chrome 个人档案代替。"
        )
        note.setObjectName("muted")
        version_layout.addWidget(note)
        root.addWidget(version)
        root.addStretch()
        scroll.setWidget(content)
        page_layout.addWidget(scroll)

    def refresh(self) -> None:
        counts = self.storage.operational_counts()
        self.counts_label.setText(
            f"当前业务数据：舆情内容 {counts['content_items']} 条 · "
            f"巡检记录 {counts['scan_runs']} 次 · 应用播报 {counts['app_notifications']} 条"
        )
        config = self.storage.get_wecom_config()
        self.wecom_enabled.blockSignals(True)
        self.wecom_enabled.setChecked(bool(config.get("enabled")))
        self.wecom_enabled.blockSignals(False)
        self.wecom_bot_id.setText(str(config.get("bot_id") or ""))
        self.wecom_chat_id.setText(str(config.get("chat_id") or ""))
        self.wecom_secret.clear()
        self.autostart_enabled.blockSignals(True)
        self.autostart_enabled.setChecked(_windows_autostart_enabled())
        self.autostart_enabled.blockSignals(False)
        llm_config = self.storage.get_llm_config()
        self.llm_enabled.blockSignals(True)
        self.llm_enabled.setChecked(bool(llm_config.get("enabled")))
        self.llm_enabled.blockSignals(False)
        self.llm_provider.setText(str(llm_config.get("provider") or "openai-compatible"))
        self.llm_base_url.setText(str(llm_config.get("base_url") or "https://api.openai.com/v1"))
        self.llm_model.setText(str(llm_config.get("model") or ""))
        self.llm_limit.setValue(int(llm_config.get("max_candidates") or 20))
        self.llm_api_key.clear()

    def save_wecom(self, *, show_message: bool = True, force_disabled: bool = False) -> bool:
        try:
            secret = self.wecom_secret.text().strip()
            if secret:
                CredentialStore.set_wecom_secret(secret)
            enabled = self.wecom_enabled.isChecked() and not force_disabled
            if enabled and not CredentialStore.get_wecom_secret():
                raise ValueError("启用企微日报时必须填写 Secret")
            self.storage.save_wecom_config(
                enabled=enabled,
                bot_id=self.wecom_bot_id.text(),
                chat_id=self.wecom_chat_id.text(),
            )
            self.wecom_secret.clear()
            if show_message:
                QMessageBox.information(self, "企微配置", "企微日报配置已保存。")
            return True
        except Exception as exc:
            _show_error(self, exc)
            return False

    def save_autostart(self, enabled: bool) -> None:
        try:
            _set_windows_autostart(enabled)
        except Exception as exc:
            self.autostart_enabled.blockSignals(True)
            self.autostart_enabled.setChecked(not enabled)
            self.autostart_enabled.blockSignals(False)
            _show_error(self, exc)

    def test_wecom(self) -> None:
        if self.wecom_test_process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.save_wecom(show_message=False):
            return
        self.wecom_test_button.setEnabled(False)
        self.wecom_test_process.setProgram(sys.executable)
        self.wecom_test_process.setArguments(["-m", "opinion_watch", "wecom", "test"])
        self.wecom_test_process.start()

    def save_llm(self, *, show_message: bool = True) -> bool:
        try:
            api_key = self.llm_api_key.text().strip()
            if api_key:
                CredentialStore.set_llm_api_key(api_key)
            enabled = self.llm_enabled.isChecked()
            if enabled and not CredentialStore.get_llm_api_key():
                raise ValueError("启用大模型复判时必须填写 API Key")
            self.storage.save_llm_config(
                enabled=enabled,
                provider=self.llm_provider.text(),
                base_url=self.llm_base_url.text(),
                model=self.llm_model.text(),
                max_candidates=self.llm_limit.value(),
            )
            self.llm_api_key.clear()
            if show_message:
                QMessageBox.information(self, "大模型配置", "大模型配置已保存。")
            return True
        except Exception as exc:
            _show_error(self, exc)
            return False

    def test_llm(self) -> None:
        if self.llm_test_process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.save_llm(show_message=False):
            return
        self.llm_test_button.setEnabled(False)
        self.llm_test_process.setProgram(sys.executable)
        self.llm_test_process.setArguments(["-m", "opinion_watch", "llm", "test"])
        self.llm_test_process.start()

    def llm_test_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self.llm_test_button.setEnabled(True)
        output = bytes(self.llm_test_process.readAllStandardOutput()).decode("utf-8", "replace")
        error = bytes(self.llm_test_process.readAllStandardError()).decode("utf-8", "replace")
        message = (output or error).strip() or "大模型测试结束。"
        if exit_code == 0:
            QMessageBox.information(self, "大模型测试", message)
        else:
            QMessageBox.warning(self, "大模型测试失败", message)

    def discover_wecom(self) -> None:
        if self.wecom_discover_process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.save_wecom(
            show_message=False,
            force_disabled=not self.wecom_chat_id.text().strip(),
        ):
            return
        self.wecom_discover_button.setEnabled(False)
        self.wecom_discover_process.setProgram(sys.executable)
        self.wecom_discover_process.setArguments(
            ["-m", "opinion_watch", "wecom", "discover", "--timeout-seconds", "120"]
        )
        self.wecom_discover_process.start()

    def wecom_test_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self.wecom_test_button.setEnabled(True)
        output = bytes(self.wecom_test_process.readAllStandardOutput()).decode("utf-8", "replace")
        error = bytes(self.wecom_test_process.readAllStandardError()).decode("utf-8", "replace")
        message = (output or error).strip() or "企微测试结束。"
        if exit_code == 0:
            QMessageBox.information(self, "企微测试", message)
        else:
            QMessageBox.warning(self, "企微测试失败", message)

    def wecom_discover_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self.wecom_discover_button.setEnabled(True)
        output = bytes(self.wecom_discover_process.readAllStandardOutput()).decode(
            "utf-8", "replace"
        )
        error = bytes(self.wecom_discover_process.readAllStandardError()).decode("utf-8", "replace")
        if exit_code != 0:
            QMessageBox.warning(self, "监听失败", (error or output).strip() or "未发现群聊 ID。")
            return
        result = None
        for line in reversed(output.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("chat_id"):
                result = candidate
                break
        if result is None:
            QMessageBox.warning(self, "监听失败", "未能从企微响应中读取群聊 ID。")
            return
        self.wecom_chat_id.setText(str(result["chat_id"]))
        if self.save_wecom(show_message=False):
            QMessageBox.information(
                self,
                "群聊 ID 已获取",
                "已自动回填并保存群聊 ID。现在可以发送测试消息。",
            )

    def open_runtime(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.runtime_dir.resolve())))

    def open_backups(self) -> None:
        path = self.settings.runtime_dir / "backups"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))


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

    def open_account_browser(self, account: dict[str, object]) -> None:
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
    return parser


def create_runtime() -> tuple[Settings, Storage]:
    settings = Settings.from_environment()
    settings.ensure_directories()
    storage = Storage(settings.database_path)
    storage.initialize()
    storage.recover_stale_scan_runs()
    owner = str(uuid.uuid4())
    if not storage.acquire_task_lease("desktop", owner, lease_seconds=86_400):
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
    settings, storage = create_runtime()
    app.aboutToQuit.connect(
        lambda: storage.release_task_lease(
            "desktop", str(getattr(storage, "_desktop_lease_owner", ""))
        )
    )
    window = MainWindow(settings, storage)
    window.navigation.setCurrentRow(args.page)
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
