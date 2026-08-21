from datetime import datetime
from pathlib import Path

from opinion_watch.events import parse_event, serialize_event
from opinion_watch.scheduling import next_scheduled_datetime
from opinion_watch.services import ScheduleService
from opinion_watch.storage import Storage


def test_worker_event_protocol_is_versioned_and_ignores_logs() -> None:
    line = serialize_event("scan.finished", {"run_id": 3, "status": "succeeded"})
    assert parse_event(line) == {
        "version": 1,
        "type": "scan.finished",
        "run_id": 3,
        "status": "succeeded",
    }
    assert parse_event("普通日志") is None
    assert parse_event('{"version": 0, "type": "scan.finished"}') is None


def test_schedule_calculation_supports_daily_weekly_and_interval() -> None:
    now = datetime(2026, 8, 21, 10, 30)
    assert next_scheduled_datetime(now, frequency="daily", schedule_time="09:00") == datetime(
        2026, 8, 22, 9, 0
    )
    assert next_scheduled_datetime(
        now, frequency="weekly", schedule_time="11:00", weekday=0
    ) == datetime(2026, 8, 24, 11, 0)
    assert next_scheduled_datetime(
        now, frequency="interval", schedule_time="09:00", interval_minutes=30
    ) == datetime(2026, 8, 21, 11, 0)


def test_schedule_service_persists_and_calculates_from_one_config(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "schedule.db")
    storage.initialize()
    service = ScheduleService(storage)
    service.save(
        enabled=True,
        frequency="daily",
        schedule_time="18:00",
        weekday=0,
        interval_minutes=60,
        scan_mode="quick",
        next_run_at=None,
    )
    config = service.load()
    assert config["enabled"] is True
    assert service.next_run(config, datetime(2026, 8, 21, 19, 0)) == datetime(2026, 8, 22, 18, 0)
