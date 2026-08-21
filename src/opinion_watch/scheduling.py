"""Pure schedule calculations used by desktop and future headless workers."""

from __future__ import annotations

from datetime import datetime, timedelta


def next_scheduled_datetime(
    now: datetime,
    *,
    frequency: str,
    schedule_time: str,
    weekday: int = 0,
    interval_minutes: int = 60,
) -> datetime:
    """Return the next occurrence for a persisted schedule configuration."""
    if frequency not in {"daily", "weekly", "interval"}:
        raise ValueError("定时频次必须是 daily、weekly 或 interval")
    if not 0 <= weekday <= 6:
        raise ValueError("执行日必须在周一到周日之间")
    if not 5 <= interval_minutes <= 1440:
        raise ValueError("巡检间隔必须在 5 到 1440 分钟之间")
    try:
        hour, minute = (int(value) for value in schedule_time.split(":", 1))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (ValueError, TypeError):
        raise ValueError("执行时间必须是 HH:MM") from None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("执行时间必须是 HH:MM")
    if frequency == "interval":
        return now + timedelta(minutes=interval_minutes)
    if frequency == "weekly":
        target += timedelta(days=(weekday - now.weekday()) % 7)
        if target <= now:
            target += timedelta(days=7)
        return target
    if target <= now:
        target += timedelta(days=1)
    return target
