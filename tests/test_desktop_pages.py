"""桌面页面构建与刷新用例（离屏 Qt）。"""

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QByteArray, QProcess  # noqa: E402
from PySide6.QtWidgets import QTableWidget, QTextEdit  # noqa: E402

from opinion_watch.desktop.dialogs import RunDetailDialog  # noqa: E402
from opinion_watch.desktop.pages import (  # noqa: E402
    AccountsPage,
    KeywordsPage,
    NotificationsPage,
    OpinionsPage,
    SchedulerPage,
    SettingsPage,
)
from opinion_watch.events import EVENT_VERSION  # noqa: E402
from opinion_watch.storage import Storage  # noqa: E402


def _make_page(name: str, storage: Storage, tmp_path: Path):
    if name == "settings":
        from opinion_watch.config import Settings

        settings = Settings(
            runtime_dir=tmp_path / "runtime",
            database_path=storage.database_path,
            artifact_dir=tmp_path / "artifacts",
        )
        return SettingsPage(settings, storage)
    factory = {
        "scheduler": SchedulerPage,
        "keywords": KeywordsPage,
        "accounts": AccountsPage,
        "opinions": OpinionsPage,
        "notifications": NotificationsPage,
    }[name]
    return factory(storage)


@pytest.mark.parametrize(
    "name",
    ["scheduler", "keywords", "accounts", "opinions", "notifications", "settings"],
)
def test_page_builds_and_refreshes_on_empty_database(
    qtbot, desktop_storage: Storage, tmp_path: Path, name: str
) -> None:
    page = _make_page(name, desktop_storage, tmp_path)
    qtbot.addWidget(page)
    page.refresh()


def test_notifications_page_renders_rows_and_binds_selection(
    qtbot, desktop_storage: Storage
) -> None:
    desktop_storage.create_notification(severity="P1", title="测试播报", message="正文")
    page = NotificationsPage(desktop_storage)
    qtbot.addWidget(page)
    page.refresh()
    assert page.table.rowCount() == 1
    assert page.panel.currentWidget() is page.table
    assert not page.edit_button.isEnabled()
    page.table.selectRow(0)
    assert page.edit_button.isEnabled()
    assert page.selected_notification() is not None


def test_notifications_page_separates_system_and_manual_channels(
    qtbot, desktop_storage: Storage
) -> None:
    desktop_storage.create_notification(severity="info", title="人工播报", message="正文")
    desktop_storage.create_alert(kind="browser_error", severity="warning", message="运行异常")
    page = NotificationsPage(desktop_storage)
    qtbot.addWidget(page)
    page.refresh()
    assert page.table.rowCount() == 2

    page.channel_filter.setCurrentIndex(page.channel_filter.findData("system"))

    assert page.table.rowCount() == 1
    assert page.table.item(0, 2).text() == "系统运行"


def test_notifications_empty_state_shown_without_rows(qtbot, desktop_storage: Storage) -> None:
    page = NotificationsPage(desktop_storage)
    qtbot.addWidget(page)
    page.refresh()
    assert page.panel.currentWidget() is page.panel.empty


def test_accounts_page_disables_actions_without_selection(qtbot, desktop_storage: Storage) -> None:
    desktop_storage.add_account("douyin", "测试账号")
    page = AccountsPage(desktop_storage)
    qtbot.addWidget(page)
    page.refresh()
    assert not page.login_button.isEnabled()
    page.table.selectRow(0)
    assert page.login_button.isEnabled()
    assert page.selected_account() is not None


def test_settings_refresh_keeps_secret_input(qtbot, desktop_storage: Storage, tmp_path) -> None:
    page = _make_page("settings", desktop_storage, tmp_path)
    qtbot.addWidget(page)
    page.refresh()
    page.llm_api_key.setText("sk-typing-in-progress")
    page.refresh()
    assert page.llm_api_key.text() == "sk-typing-in-progress"


def test_scheduler_stop_button_starts_hidden(qtbot, desktop_storage: Storage) -> None:
    page = SchedulerPage(desktop_storage)
    qtbot.addWidget(page)
    assert not page.stop_button.isVisible()
    assert not page.stop_button.isEnabled()


class _ChunkedProcess:
    """只提供 read_output 需要的接口，用来喂任意切分的 stdout 块。"""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def readAllStandardOutput(self) -> QByteArray:  # noqa: N802 - Qt 接口命名
        return QByteArray(self._chunks.pop(0).encode("utf-8"))


def test_event_split_across_read_chunks_still_yields_run_id(
    qtbot, desktop_storage: Storage
) -> None:
    page = SchedulerPage(desktop_storage)
    qtbot.addWidget(page)
    event = json.dumps(
        {"version": EVENT_VERSION, "type": "scan.started", "run_id": 4242},
        ensure_ascii=False,
    )
    # 在 JSON 中间切开：readAllStandardOutput 的块边界和行边界无关。
    cut = len(event) // 2
    page.process = _ChunkedProcess([event[:cut], event[cut:] + "\n"])

    page.read_output()
    assert page.current_run_id is None  # 半条事件不能解析出任何东西

    page.read_output()
    assert page.current_run_id == 4242


def test_trailing_event_without_newline_is_flushed_on_exit(qtbot, desktop_storage: Storage) -> None:
    page = SchedulerPage(desktop_storage)
    qtbot.addWidget(page)
    # 子进程最后一行没来得及换行就退出，这条事件也必须被认领。
    page.process = _ChunkedProcess(
        [
            json.dumps(
                {"version": EVENT_VERSION, "type": "scan.started", "run_id": 77},
                ensure_ascii=False,
            )
        ]
    )
    page.read_output()
    assert page.current_run_id is None

    page.process_finished(0, QProcess.ExitStatus.NormalExit)
    assert page.latest_run_id == 77


def test_run_detail_table_keeps_columns_separated_by_horizontal_scroll(
    qtbot, desktop_storage: Storage
) -> None:
    run_id = desktop_storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["速探长"], options={}
    )
    run = desktop_storage.get_scan_run(run_id)
    assert run is not None
    dialog = RunDetailDialog(desktop_storage, run)
    qtbot.addWidget(dialog)
    table = dialog.findChild(QTableWidget)
    assert table is not None
    assert table.minimumWidth() >= 1400


def test_run_detail_summarizes_historical_browser_logs(qtbot, desktop_storage: Storage) -> None:
    run_id = desktop_storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["速探长"], options={}
    )
    desktop_storage.create_alert(
        run_id=run_id,
        platform="douyin",
        kind="browser_error",
        severity="error",
        message=(
            "BrowserType.launch_persistent_context: Target page, context or browser has been "
            "closed\nBrowser logs:\n<launching> chrome.exe --private-flags\n"
            "<process did exit: exitCode=0, signal=null>"
        ),
    )
    run = desktop_storage.get_scan_run(run_id)
    assert run is not None

    dialog = RunDetailDialog(desktop_storage, run)
    qtbot.addWidget(dialog)
    alert_text = dialog.findChild(QTextEdit, "alertText")

    assert alert_text is not None
    assert "抖音：Chrome 启动后立即退出" in alert_text.toPlainText()
    assert "Browser logs" not in alert_text.toPlainText()
    assert "--private-flags" not in alert_text.toPlainText()


def test_run_detail_localizes_grouped_alert_without_empty_platform_prefix(
    qtbot, desktop_storage: Storage
) -> None:
    run_id = desktop_storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["深圳逐影"], options={}
    )
    desktop_storage.create_alert(
        run_id=run_id,
        kind="zero_detail_coverage",
        severity="warning",
        message=(
            "本轮巡检共 1 项zero_detail_coverage：\n"
            "- douyin/深圳逐影 已命中品牌卡片，但详情核查为 0 条"
        ),
    )
    run = desktop_storage.get_scan_run(run_id)
    assert run is not None

    dialog = RunDetailDialog(desktop_storage, run)
    qtbot.addWidget(dialog)
    alert_text = dialog.findChild(QTextEdit, "alertText")

    assert alert_text is not None
    display = alert_text.toPlainText()
    assert display.startswith("本轮巡检共 1 项详情覆盖不足：")
    assert "抖音/深圳逐影" in display
    assert not display.startswith("：")
