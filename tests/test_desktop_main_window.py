"""主窗口与运行时引导用例。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from opinion_watch.config import Settings  # noqa: E402
from opinion_watch.desktop.constants import Page  # noqa: E402
from opinion_watch.desktop.main_window import BADGE_ROLE, MainWindow  # noqa: E402
from opinion_watch.desktop.runtime import (  # noqa: E402
    build_parser,
    create_runtime,
    migrate_legacy_schedule,
)
from opinion_watch.storage import Storage  # noqa: E402


def _make_window(storage: Storage, tmp_path: Path) -> MainWindow:
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=storage.database_path,
        artifact_dir=tmp_path / "artifacts",
    )
    return MainWindow(settings, storage)


def test_main_window_has_six_pages_and_navigates(
    qtbot, desktop_storage: Storage, tmp_path: Path
) -> None:
    window = _make_window(desktop_storage, tmp_path)
    qtbot.addWidget(window)
    assert window.stack.count() == len(Page)
    for page in Page:
        window.navigation.setCurrentRow(page)
        assert window.stack.currentIndex() == page.value


def test_main_window_shutdown_stops_storage_worker(
    qtbot, desktop_storage: Storage, tmp_path: Path
) -> None:
    window = _make_window(desktop_storage, tmp_path)
    qtbot.addWidget(window)

    window.shutdown()

    assert not window.opinions.tasks._thread.isRunning()


def test_unread_notifications_show_in_nav_badge(
    qtbot, desktop_storage: Storage, tmp_path: Path
) -> None:
    desktop_storage.create_notification(severity="P2", title="未读", message="正文")
    window = _make_window(desktop_storage, tmp_path)
    qtbot.addWidget(window)
    window.update_global_status()
    assert int(window.nav_items[Page.NOTIFICATIONS].data(BADGE_ROLE)) == 1


def test_main_window_refreshes_after_external_database_write(
    qtbot, desktop_storage: Storage, tmp_path: Path
) -> None:
    window = _make_window(desktop_storage, tmp_path)
    qtbot.addWidget(window)
    refresh = Mock()
    window.refresh_current_page = refresh  # type: ignore[method-assign]

    desktop_storage.create_notification(severity="P2", title="后台新增", message="正文")
    window._poll_external_changes()

    refresh.assert_called_once_with()
    assert int(window.nav_items[Page.NOTIFICATIONS].data(BADGE_ROLE)) == 1


def test_build_parser_accepts_the_documented_flags(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["--smoke-test", "--page", "3", "--runtime-dir", str(tmp_path)])
    assert args.smoke_test is True
    assert args.page == 3
    assert args.runtime_dir == tmp_path
    with pytest.raises(SystemExit):
        parser.parse_args(["--page", "6"])


def test_create_runtime_returns_lease_and_seeds_brands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPINION_WATCH_RUNTIME_DIR", raising=False)
    runtime = create_runtime(tmp_path / "runtime-a")
    assert runtime.lease_owner
    assert runtime.storage.list_brands()
    with pytest.raises(RuntimeError):
        create_runtime(tmp_path / "runtime-a")
    runtime.storage.release_task_lease("desktop", runtime.lease_owner)


def test_migrate_legacy_schedule_marks_import_done(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "migrate.db")
    storage.initialize()
    migrate_legacy_schedule(storage)
    assert bool(storage.get_schedule_config().get("legacy_imported"))
