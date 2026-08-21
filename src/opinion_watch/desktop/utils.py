"""桌面端小工具：子进程输出解码、JSON 结果提取和时间格式化。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from PySide6.QtCore import QByteArray


def decode_process_output(data: QByteArray) -> str:
    return bytes(data.data()).decode("utf-8", "replace")


def format_timestamp(value: object) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def process_json_result(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        try:
            candidate = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("status"):
            return candidate
    decoder = json.JSONDecoder()
    for start in (index for index, value in enumerate(output) if value == "{"):
        try:
            candidate, _ = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("status"):
            return candidate
    return None
