"""桌面端运行时引导：参数解析、迁移和运行环境组装。

第 7 步会把 build_parser / create_runtime 也迁到这里。
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from opinion_watch.storage import Storage


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
