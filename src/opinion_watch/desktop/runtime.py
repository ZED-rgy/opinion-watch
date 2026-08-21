"""桌面端运行时引导：参数解析、迁移和运行环境组装。"""

from __future__ import annotations

import argparse
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSettings

from opinion_watch.config import DEFAULT_BRANDS, Settings
from opinion_watch.storage import Storage

DESKTOP_LEASE_SECONDS = 900
DESKTOP_HEARTBEAT_INTERVAL_MS = 300_000


@dataclass(frozen=True, slots=True)
class DesktopRuntime:
    """桌面进程的运行环境；lease_owner 用于心跳与退出释放。"""

    settings: Settings
    storage: Storage
    lease_owner: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="品牌舆情监控桌面应用")
    parser.add_argument("--smoke-test", action="store_true", help="初始化界面后立即退出")
    parser.add_argument("--screenshot", type=Path, help="保存桌面界面截图后退出")
    parser.add_argument("--page", type=int, choices=range(6), default=0, help="启动时显示的页面")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="运行数据目录；开机自启时用于固定数据库位置，默认取环境变量或当前目录下 runtime",
    )
    return parser


def create_runtime(runtime_dir: Path | None = None) -> DesktopRuntime:
    if runtime_dir is not None:
        # 写入环境变量而不是只改本进程配置：巡检 QProcess 子进程继承同一个值，
        # 才能保证读写同一个数据库。
        os.environ["OPINION_WATCH_RUNTIME_DIR"] = str(runtime_dir.resolve())
    settings = Settings.from_environment()
    settings.ensure_directories()
    storage = Storage(settings.database_path)
    storage.initialize()
    storage.recover_stale_scan_runs()
    migrate_legacy_schedule(storage)
    owner = str(uuid.uuid4())
    # 短租约 + 定时心跳：桌面进程崩溃后最多几分钟即可重新启动，
    # 而不是等一个超长租约自然过期。
    if not storage.acquire_task_lease("desktop", owner, lease_seconds=DESKTOP_LEASE_SECONDS):
        raise RuntimeError("品牌舆情监控已经在运行，请先关闭已有窗口。")
    if not storage.list_brands():
        for brand in DEFAULT_BRANDS:
            storage.add_brand(brand)
    return DesktopRuntime(settings=settings, storage=storage, lease_owner=owner)


def migrate_legacy_schedule(storage: Storage) -> None:
    """把旧版 QSettings 定时配置一次性导入 SQLite。

    应用生命周期动作放在启动引导里执行，而不是页面构造函数里。
    """
    from opinion_watch.services import ScheduleService

    service = ScheduleService(storage)
    config = service.load()
    if bool(config.get("legacy_imported")):
        return
    app_settings = QSettings("opinion-watch", "desktop")
    legacy_frequency = str(app_settings.value("schedule_frequency", "daily"))
    legacy_time = str(app_settings.value("schedule_time", "09:00"))
    legacy_weekday = int(str(app_settings.value("schedule_weekday", 0)))
    legacy_interval = int(str(app_settings.value("interval_minutes", 60)))
    legacy_mode = str(app_settings.value("scan_mode", "quick"))
    legacy_concurrency = int(str(app_settings.value("scan_concurrency", 1)))
    legacy_enabled = str(app_settings.value("auto_enabled", "false")).lower() == "true"
    service.save(
        enabled=legacy_enabled,
        frequency=(
            legacy_frequency if legacy_frequency in {"daily", "weekly", "interval"} else "daily"
        ),
        schedule_time=legacy_time,
        weekday=max(0, min(6, legacy_weekday)),
        interval_minutes=max(5, min(1440, legacy_interval)),
        scan_mode=legacy_mode if legacy_mode in {"quick", "deep"} else "quick",
        concurrency=max(1, min(4, legacy_concurrency)),
        legacy_imported=True,
    )
