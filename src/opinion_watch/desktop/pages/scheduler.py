"""自动巡检页：调度配置、巡检执行与运行记录。"""

from __future__ import annotations

import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from PySide6.QtCore import QProcess, QSize, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from opinion_watch.desktop.components import (
    EmptyState,
    button,
    icon,
    run_busy,
    surface,
    title,
)
from opinion_watch.desktop.constants import (
    ACCOUNT_STATUS_NAMES,
    PLATFORM_NAMES,
    RUN_STATUS_NAMES,
)
from opinion_watch.desktop.dialogs import (
    RunDetailDialog,
    delete_scan_run_with_confirmation,
    edit_scan_run_metadata,
)
from opinion_watch.desktop.process import cli_command
from opinion_watch.desktop.theme import repolish
from opinion_watch.desktop.utils import decode_process_output, format_timestamp
from opinion_watch.events import parse_event
from opinion_watch.services import ScheduleService
from opinion_watch.storage import Storage

_TERMINAL_ATTEMPT_EVENTS = {"scan.attempt_succeeded", "scan.attempt_failed", "scan.attempt_error"}


class SchedulerPage(QWidget):
    scan_finished = Signal()
    manage_scope_requested = Signal()

    def __init__(self, storage: Storage) -> None:
        super().__init__()
        self.storage = storage
        self.schedule_service = ScheduleService(storage)
        schedule_config = self.schedule_service.load()
        self.process = QProcess(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.next_run_at: datetime | None = None
        self.output_buffer: list[str] = []
        self.latest_run_id: int | None = None
        self.current_run_id: int | None = None
        self._finished_keywords = 0
        self._pending_line = ""
        self._restore_scan_button: Any = None
        self._stop_requested = False

        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 28)
        root.setSpacing(18)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(title("自动巡检"))
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
        health_icon.setPixmap(icon("fa6s.shield-halved", "#18845B").pixmap(QSize(14, 14)))
        health_layout.addWidget(health_icon)
        self.health_text = QLabel("正在检查账号")
        health_layout.addWidget(self.health_text)
        header.addWidget(self.health_pill)
        header.addSpacing(8)
        self.scan_button = button("立即巡检", self.start_scan, icon_name="fa6s.play")
        header.addWidget(self.scan_button)
        self.stop_button = button("停止巡检", self.stop_scan, icon_name="fa6s.stop", role="danger")
        self.stop_button.setVisible(False)
        self.stop_button.setEnabled(False)
        header.addWidget(self.stop_button)
        root.addLayout(header)

        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumHeight(230)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(24, 22, 24, 22)
        hero_layout.setSpacing(22)
        calendar = QLabel()
        calendar.setObjectName("heroCalendar")
        calendar.setFixedSize(104, 104)
        calendar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        calendar.setPixmap(icon("fa6s.calendar-check", "#16936B").pixmap(QSize(46, 46)))
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
        activity, activity_layout = surface()
        activity_header = QHBoxLayout()
        activity_header.addWidget(title("巡检动态", "sectionTitle"))
        activity_header.addStretch()
        self.detail_button = button(
            "查看运行详情", self.toggle_details, icon_name="fa6s.terminal", role="secondary"
        )
        self.detail_button.setEnabled(False)
        activity_header.addWidget(self.detail_button)
        self.edit_run_button = button(
            "编辑记录", self.edit_selected_run, icon_name="fa6s.pen-to-square", role="secondary"
        )
        self.edit_run_button.setEnabled(False)
        activity_header.addWidget(self.edit_run_button)
        self.delete_run_button = button(
            "删除记录", self.delete_selected_run, icon_name="fa6s.trash", role="danger"
        )
        self.delete_run_button.setEnabled(False)
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

        scope, scope_layout = surface()
        scope.setMinimumWidth(300)
        scope.setMaximumWidth(380)
        scope_layout.addWidget(title("监控范围", "sectionTitle"))
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
        migration_icon.setPixmap(icon("fa6s.circle-info", "#A06B00").pixmap(QSize(15, 15)))
        migration_layout.addWidget(migration_icon, alignment=Qt.AlignmentFlag.AlignTop)
        migration_text = QLabel(
            "自动巡检会使用账号对应的独立 Chrome 登录档案；首次切换后需重新登录。"
        )
        migration_text.setObjectName("noticeText")
        migration_text.setWordWrap(True)
        migration_layout.addWidget(migration_text, 1)
        scope_layout.addStretch()
        scope_layout.addWidget(migration)
        scope_layout.addWidget(
            button(
                "管理监控范围",
                self.manage_scope_requested.emit,
                icon_name="fa6s.sliders",
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
        self.process.errorOccurred.connect(self._scan_process_error)
        self.timeline.itemSelectionChanged.connect(self._update_run_buttons)
        self.configure_timer(self.auto_enabled.isChecked())
        # 首次数据加载由主窗口的页面切换触发，避免启动时双重刷新。

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

    def _set_scan_busy(self, busy: bool) -> None:
        if busy and self._restore_scan_button is None:
            self._restore_scan_button = run_busy(self.scan_button, "巡检中…")
            self.stop_button.setVisible(True)
            self.stop_button.setEnabled(True)
            if self.empty_timeline.action_button is not None:
                self.empty_timeline.action_button.setEnabled(False)
        elif not busy and self._restore_scan_button is not None:
            self._restore_scan_button()
            self._restore_scan_button = None
            self.stop_button.setVisible(False)
            self.stop_button.setEnabled(False)
            if self.empty_timeline.action_button is not None:
                self.empty_timeline.action_button.setEnabled(True)

    def start_scan(self, *, trigger: str = "manual") -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.run_status.setText("巡检正在运行，请勿重复启动")
            return
        llm_enabled = bool(self.storage.get_llm_config().get("enabled"))
        self.current_run_id = None
        self._finished_keywords = 0
        self._pending_line = ""
        self.output_buffer.clear()
        self.output.clear()
        self.output.setVisible(False)
        self._stop_requested = False
        self._set_scan_busy(True)
        self.run_status.setText(
            "正在检索抖音和小红书…" + ("（大模型复判已启用）" if llm_enabled else "（规则筛选）")
        )
        program, arguments = cli_command(
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
        )
        self.process.setProgram(program)
        self.process.setArguments(arguments)
        self.process.start()

    def stop_scan(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._stop_requested = True
        self.timer.stop()
        self.stop_button.setEnabled(False)
        self.run_status.setText("正在停止巡检…")
        self.process.terminate()
        QTimer.singleShot(3_000, self._force_stop_scan)

    def _force_stop_scan(self) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        process_id = int(self.process.processId())
        if sys.platform == "win32" and process_id > 0:
            with suppress(Exception):
                subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        else:
            self.process.kill()

    def shutdown(self) -> None:
        """退出应用前收掉巡检子进程。

        QProcess 不是子进程组的父级守护者：直接关窗口会把 CLI 进程和它拉起的
        Chrome 一起留在后台，scan/account 租约要等到过期才释放，下次启动会直接
        撞上 BrowserProfileLocked。这里走和"停止巡检"一样的 terminate → 强杀
        路径，但必须同步等待，aboutToQuit 之后没有事件循环再跑 singleShot。
        """
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._stop_requested = True
        self.timer.stop()
        self.process.terminate()
        if not self.process.waitForFinished(3_000):
            self._force_stop_scan()
            self.process.waitForFinished(2_000)

    def _scan_process_error(self, error: QProcess.ProcessError) -> None:
        # 启动失败时 finished 不会触发，状态会一直停在“正在检索…”。
        if error is QProcess.ProcessError.FailedToStart:
            self._set_scan_busy(False)
            self._stop_requested = False
            self.run_status.setText("巡检子进程无法启动，请检查 Python 运行环境")

    def read_output(self) -> None:
        value = decode_process_output(self.process.readAllStandardOutput())
        self.output_buffer.append(value)
        self._append_output(value)
        # readAllStandardOutput 给的是字节块，边界和行边界无关：一条事件 JSON
        # 很容易被切成两半。直接 splitlines 会把两半都解析失败地丢掉，丢掉
        # scan.started 就意味着 current_run_id 一直为空，结束时回退到“最近一次
        # 运行”，可能指向别人的 run。这里把不完整的尾行留到下一块再拼。
        chunk = self._pending_line + value
        lines = chunk.splitlines(keepends=True)
        self._pending_line = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending_line = lines.pop()
        for line in lines:
            self._dispatch_event(line)

    def _dispatch_event(self, line: str) -> None:
        event = parse_event(line)
        if event is None:
            return
        event_type = str(event.get("type") or "")
        if event_type == "scan.started":
            with suppress(TypeError, ValueError):
                self.current_run_id = int(str(event.get("run_id")))
        elif event_type in _TERMINAL_ATTEMPT_EVENTS:
            # 重试中的尝试不计入进度，等到终态再更新。
            if str(event.get("status")) == "retrying":
                return
            self._finished_keywords += 1
            platform = PLATFORM_NAMES.get(str(event.get("platform")), "")
            keyword = str(event.get("keyword") or "")
            self.run_status.setText(
                f"巡检进行中 · 已完成 {self._finished_keywords} 个关键词检索"
                + (f"（最新：{platform} {keyword}）" if keyword else "")
            )
        elif event_type == "scan.finished":
            self.run_status.setText(
                "巡检已完成" if event.get("status") == "succeeded" else "巡检未完成"
            )
        elif event_type in {
            "scan.session_status",
            "scan.account_not_ready",
            "scan.browser_error",
        }:
            message = str(event.get("message") or "巡检遇到问题，请查看运行详情")
            self.run_status.setText(message[:120])

    def read_error(self) -> None:
        value = decode_process_output(self.process.readAllStandardError())
        self.output_buffer.append(value)
        self._append_output(value)

    def _append_output(self, value: str) -> None:
        # 增量追加；整段 setPlainText 会在长巡检里把每次刷新都变成全量重排。
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(value)

    def process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        # 子进程可能在最后一行没有换行就退出了，这里补投一次，否则末尾的
        # scan.finished 会连带 run_id 一起丢掉。
        if self._pending_line:
            trailing, self._pending_line = self._pending_line, ""
            self._dispatch_event(trailing)
        was_stopped = self._stop_requested
        cancelled = False
        if was_stopped:
            run_id = self.current_run_id
            if run_id is None:
                running = next(
                    (
                        item
                        for item in self.storage.list_scan_runs(limit=20)
                        if str(item.get("status")) == "running"
                    ),
                    None,
                )
                run_id = int(running["id"]) if running is not None else None
            if run_id is not None:
                cancelled = self.storage.cancel_scan_run(run_id)
        self._set_scan_busy(False)
        self.run_status.setText(
            "巡检已停止"
            if was_stopped and cancelled
            else "巡检已完成"
            if exit_code == 0
            else "巡检未完成，请查看运行详情"
        )
        if self.current_run_id is not None:
            # 用本次子进程在 scan.started 里上报的 run_id；取“最新一条记录”
            # 会在并发 CLI 巡检时指向别人的运行。
            self.latest_run_id = self.current_run_id
            self.current_run_id = None
        else:
            runs = self.storage.list_scan_runs(limit=1)
            self.latest_run_id = int(runs[0]["id"]) if runs else None
        self._stop_requested = False
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

    def _update_run_buttons(self) -> None:
        enabled = self.selected_run_id() is not None
        self.detail_button.setEnabled(enabled)
        self.edit_run_button.setEnabled(enabled)
        self.delete_run_button.setEnabled(enabled)

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
            layout_item = self.platform_rows.takeAt(0)
            row_widget = layout_item.widget() if layout_item is not None else None
            if row_widget is not None:
                row_widget.deleteLater()
        for platform, name in PLATFORM_NAMES.items():
            platform_accounts = [item for item in accounts if item["platform"] == platform]
            ready_count = sum(1 for item in platform_accounts if item["status"] == "ready")
            row = QWidget()
            row.setObjectName("surfaceRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            platform_icon = QLabel()
            platform_icon.setPixmap(icon("fa6s.circle", "#2F5BEA").pixmap(QSize(8, 8)))
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
            state.setProperty("state", "ok" if ready_count else "muted")
            repolish(state)
            row_layout.addWidget(state)
            self.platform_rows.addWidget(row)

        runs = self.storage.list_scan_runs(limit=12)
        self.timeline.clear()
        for run in runs:
            status = str(run["status"])
            when = format_timestamp(run.get("started_at"))
            collected = int(run.get("collected_count") or 0)
            brands_text = "、".join(str(value) for value in run.get("brands", [])) or "全部品牌"
            platforms_text = "、".join(
                PLATFORM_NAMES.get(str(value), str(value)) for value in run.get("platforms", [])
            )
            trigger = "定时" if run.get("trigger") == "watch" else "手动"
            run_title = str(run.get("title") or f"巡检记录 #{run['id']}")
            text = (
                f"{run_title}  ·  #{run['id']}  {RUN_STATUS_NAMES.get(status, status)}  ·  {when}\n"
                f"{trigger} · {platforms_text or '无平台'} · {brands_text} · "
                f"发现 {collected} 条 · 关联 {run.get('linked_content_count', 0)} 条"
            )
            item = QListWidgetItem(
                icon(
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
        self._update_run_buttons()
