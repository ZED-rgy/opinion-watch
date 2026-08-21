"""桌面页面构建与刷新用例（离屏 Qt）。"""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from opinion_watch.desktop.pages import (  # noqa: E402
    AccountsPage,
    KeywordsPage,
    NotificationsPage,
    OpinionsPage,
    SchedulerPage,
    SettingsPage,
)
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
