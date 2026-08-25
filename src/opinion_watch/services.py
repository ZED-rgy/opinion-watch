"""Application services shared by the desktop shell and headless workers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from opinion_watch.scheduling import next_scheduled_datetime
from opinion_watch.storage import Storage


class ScheduleService:
    """Own persisted schedule state and the shared next-run calculation."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def load(self) -> dict[str, Any]:
        return self.storage.get_schedule_config()

    def save(self, **values: object) -> None:
        self.storage.save_schedule_config(
            enabled=bool(values["enabled"]),
            frequency=str(values["frequency"]),
            schedule_time=str(values["schedule_time"]),
            weekday=int(str(values["weekday"])),
            interval_minutes=int(str(values["interval_minutes"])),
            scan_mode=str(values["scan_mode"]),
            concurrency=int(str(values.get("concurrency") or 1)),
            last_scheduled_at=(
                str(values["last_scheduled_at"]) if values.get("last_scheduled_at") else None
            ),
            next_run_at=str(values["next_run_at"]) if values.get("next_run_at") else None,
            missed_run_policy=str(values.get("missed_run_policy") or "skip"),
            legacy_imported=bool(values.get("legacy_imported", True)),
        )

    def next_run(self, config: dict[str, Any], now: datetime) -> datetime:
        return next_scheduled_datetime(
            now,
            frequency=str(config.get("frequency") or "daily"),
            schedule_time=str(config.get("schedule_time") or "09:00"),
            weekday=int(str(config.get("weekday") or 0)),
            interval_minutes=int(str(config.get("interval_minutes") or 60)),
        )
