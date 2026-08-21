"""设置页：数据边界、企微日报、大模型与开机自启。"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable

from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from opinion_watch import __version__
from opinion_watch.config import Settings
from opinion_watch.credentials import CredentialStore
from opinion_watch.desktop.autostart import set_windows_autostart, windows_autostart_enabled
from opinion_watch.desktop.components import button, run_busy, show_toast, surface, title
from opinion_watch.desktop.dialogs import long_text_dialog, show_error
from opinion_watch.desktop.utils import decode_process_output
from opinion_watch.storage import Storage


class SettingsPage(QWidget):
    def __init__(self, settings: Settings, storage: Storage) -> None:
        super().__init__()
        self.settings = settings
        self.storage = storage
        self._config_loaded = False
        self._busy_restores: dict[QProcess, Callable[[], None]] = {}
        self.wecom_test_process = QProcess(self)
        self.wecom_test_process.finished.connect(self.wecom_test_finished)
        self.wecom_test_process.errorOccurred.connect(
            lambda error: self._subprocess_failed("企微测试", error)
        )
        self.wecom_discover_process = QProcess(self)
        self.wecom_discover_process.finished.connect(self.wecom_discover_finished)
        self.wecom_discover_process.errorOccurred.connect(
            lambda error: self._subprocess_failed("群聊 ID 监听", error)
        )
        self.llm_test_process = QProcess(self)
        self.llm_test_process.finished.connect(self.llm_test_finished)
        self.llm_test_process.errorOccurred.connect(
            lambda error: self._subprocess_failed("大模型测试", error)
        )
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
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(title("设置"))
        subtitle = QLabel("查看本地数据、隐私边界和当前版本")
        subtitle.setObjectName("pageSubtitle")
        heading.addWidget(subtitle)
        header.addLayout(heading)
        header.addStretch()
        header.addWidget(
            button("重新加载配置", self.reload_config, icon_name="fa6s.rotate", role="secondary")
        )
        root.addLayout(header)
        card, layout = surface()
        layout.addWidget(title("数据与隐私", "sectionTitle"))
        privacy = QLabel(
            "账号登录态仅保存在本机独立浏览器档案中；应用不会导出 Cookie，也不会记录账号密码。"
        )
        privacy.setWordWrap(True)
        privacy.setObjectName("muted")
        layout.addWidget(privacy)
        self.counts_label = QLabel()
        self.counts_label.setWordWrap(True)
        layout.addWidget(self.counts_label)
        layout.addWidget(title("企微智能机器人日报", "sectionTitle"))
        wecom_note = QLabel(
            "日报在定时巡检当天首次成功后发送一次；手动点击立即巡检时每次成功或部分完成都会发送。"
            " Secret 仅保存到 Windows 凭据管理器；"
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
        wecom_actions.addWidget(
            button("保存企微配置", self.save_wecom, icon_name="fa6s.floppy-disk")
        )
        self.wecom_test_button = button(
            "发送测试消息", self.test_wecom, icon_name="fa6s.paper-plane", role="secondary"
        )
        wecom_actions.addWidget(self.wecom_test_button)
        self.wecom_discover_button = button(
            "监听群聊 ID", self.discover_wecom, icon_name="fa6s.tower-broadcast", role="secondary"
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
        layout.addWidget(title("大模型辅助研判", "sectionTitle"))
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
            button("保存大模型配置", lambda: self.save_llm(), icon_name="fa6s.floppy-disk")
        )
        self.llm_test_button = button(
            "测试连接", self.test_llm, icon_name="fa6s.plug", role="secondary"
        )
        llm_actions.addWidget(self.llm_test_button)
        llm_actions.addStretch()
        layout.addLayout(llm_actions)
        actions = QHBoxLayout()
        actions.addWidget(
            button(
                "打开运行目录", self.open_runtime, icon_name="fa6s.folder-open", role="secondary"
            )
        )
        actions.addWidget(
            button(
                "打开备份目录", self.open_backups, icon_name="fa6s.box-archive", role="secondary"
            )
        )
        actions.addStretch()
        layout.addLayout(actions)
        root.addWidget(card)
        version_card, version_layout = surface()
        version_layout.addWidget(title("版本信息", "sectionTitle"))
        version_layout.addWidget(QLabel(f"品牌舆情监控  v{__version__}"))
        note = QLabel(
            "自动巡检与平台账号页使用同一个独立 Playwright 登录档案；请在“打开自动巡检浏览器”"
            "中完成登录，不要使用普通 Chrome 个人档案代替。"
        )
        note.setObjectName("muted")
        version_layout.addWidget(note)
        root.addWidget(version_card)
        root.addStretch()
        scroll.setWidget(content)
        page_layout.addWidget(scroll)

    def refresh(self) -> None:
        # 统计信息每次都刷新；配置表单只在首次或显式重载时填充，
        # 避免其他页面触发的后台刷新清掉正在输入的密钥。
        counts = self.storage.operational_counts()
        self.counts_label.setText(
            f"当前业务数据：舆情内容 {counts['content_items']} 条 · "
            f"巡检记录 {counts['scan_runs']} 次 · 应用播报 {counts['app_notifications']} 条"
        )
        if not self._config_loaded:
            self._load_config()

    def reload_config(self) -> None:
        self._load_config()
        show_toast(self, "已重新加载配置")

    def _load_config(self) -> None:
        config = self.storage.get_wecom_config()
        self.wecom_enabled.blockSignals(True)
        self.wecom_enabled.setChecked(bool(config.get("enabled")))
        self.wecom_enabled.blockSignals(False)
        self.wecom_bot_id.setText(str(config.get("bot_id") or ""))
        self.wecom_chat_id.setText(str(config.get("chat_id") or ""))
        self.wecom_secret.clear()
        self.autostart_enabled.blockSignals(True)
        self.autostart_enabled.setChecked(windows_autostart_enabled())
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
        self._config_loaded = True

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
                show_toast(self, "企微日报配置已保存")
            return True
        except Exception as exc:
            show_error(self, exc)
            return False

    def save_autostart(self, enabled: bool) -> None:
        try:
            set_windows_autostart(enabled, runtime_dir=self.settings.runtime_dir)
        except Exception as exc:
            self.autostart_enabled.blockSignals(True)
            self.autostart_enabled.setChecked(not enabled)
            self.autostart_enabled.blockSignals(False)
            show_error(self, exc)

    def _set_busy(self, process: QProcess, target: QPushButton, text: str) -> None:
        self._busy_restores[process] = run_busy(target, text)

    def _restore_busy(self, process: QProcess) -> None:
        restore = self._busy_restores.pop(process, None)
        if restore is not None:
            restore()

    def _subprocess_failed(self, label: str, error: QProcess.ProcessError) -> None:
        # 只处理启动失败：进程根本没跑起来时 finished 不会触发，
        # 按钮会永久停留在忙碌状态且没有任何提示。
        if error is not QProcess.ProcessError.FailedToStart:
            return
        for process in list(self._busy_restores):
            self._restore_busy(process)
        QMessageBox.warning(
            self, "无法启动子进程", f"{label}子进程无法启动，请检查运行环境后重试。"
        )

    def test_wecom(self) -> None:
        if self.wecom_test_process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.save_wecom(show_message=False):
            return
        self._set_busy(self.wecom_test_process, self.wecom_test_button, "发送中…")
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
                show_toast(self, "大模型配置已保存")
            return True
        except Exception as exc:
            show_error(self, exc)
            return False

    def test_llm(self) -> None:
        if self.llm_test_process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.save_llm(show_message=False):
            return
        self._set_busy(self.llm_test_process, self.llm_test_button, "测试中…")
        self.llm_test_process.setProgram(sys.executable)
        self.llm_test_process.setArguments(["-m", "opinion_watch", "llm", "test"])
        self.llm_test_process.start()

    def llm_test_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._restore_busy(self.llm_test_process)
        output = decode_process_output(self.llm_test_process.readAllStandardOutput())
        error = decode_process_output(self.llm_test_process.readAllStandardError())
        message = (output or error).strip() or "大模型测试结束。"
        if exit_code == 0:
            show_toast(self, "大模型连接测试通过")
            if len(message) > 120 or "\n" in message:
                long_text_dialog(self, "大模型测试结果", message)
        else:
            long_text_dialog(self, "大模型测试失败", message)

    def discover_wecom(self) -> None:
        if self.wecom_discover_process.state() != QProcess.ProcessState.NotRunning:
            return
        if not self.save_wecom(
            show_message=False,
            force_disabled=not self.wecom_chat_id.text().strip(),
        ):
            return
        self._set_busy(self.wecom_discover_process, self.wecom_discover_button, "监听中…")
        self.wecom_discover_process.setProgram(sys.executable)
        self.wecom_discover_process.setArguments(
            ["-m", "opinion_watch", "wecom", "discover", "--timeout-seconds", "120"]
        )
        self.wecom_discover_process.start()

    def wecom_test_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._restore_busy(self.wecom_test_process)
        output = decode_process_output(self.wecom_test_process.readAllStandardOutput())
        error = decode_process_output(self.wecom_test_process.readAllStandardError())
        message = (output or error).strip() or "企微测试结束。"
        if exit_code == 0:
            show_toast(self, "企微测试消息已发送")
            if len(message) > 120 or "\n" in message:
                long_text_dialog(self, "企微测试结果", message)
        else:
            long_text_dialog(self, "企微测试失败", message)

    def wecom_discover_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._restore_busy(self.wecom_discover_process)
        output = decode_process_output(self.wecom_discover_process.readAllStandardOutput())
        error = decode_process_output(self.wecom_discover_process.readAllStandardError())
        if exit_code != 0:
            long_text_dialog(self, "监听失败", (error or output).strip() or "未发现群聊 ID。")
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
            show_toast(self, "已自动回填并保存群聊 ID，现在可以发送测试消息")

    def open_runtime(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.runtime_dir.resolve())))

    def open_backups(self) -> None:
        path = self.settings.runtime_dir / "backups"
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
