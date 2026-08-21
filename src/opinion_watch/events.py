"""Versioned JSON Lines events shared by the CLI worker and desktop shell."""

from __future__ import annotations

import json
from collections.abc import Mapping

EVENT_VERSION = 1


def serialize_event(
    event_type: str, payload: Mapping[str, object], *, ensure_ascii: bool = False
) -> str:
    """Serialize one worker event with stable protocol metadata."""
    if not event_type.strip():
        raise ValueError("事件类型不能为空")
    return json.dumps(
        {"version": EVENT_VERSION, "type": event_type, **dict(payload)},
        ensure_ascii=ensure_ascii,
    )


def parse_event(line: str) -> dict[str, object] | None:
    """Parse one stdout line, returning None for ordinary logs or old output."""
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if value.get("version") != EVENT_VERSION or not isinstance(value.get("type"), str):
        return None
    return value
