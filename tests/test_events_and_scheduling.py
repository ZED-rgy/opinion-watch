from datetime import datetime

from opinion_watch.events import parse_event, serialize_event
from opinion_watch.scheduling import next_scheduled_datetime


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
