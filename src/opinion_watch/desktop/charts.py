"""最近 N 天风险等级趋势的轻量柱状图（QPainter 绘制，无额外依赖）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from opinion_watch.desktop.theme import SEVERITY_COLORS

_SERIES = ("P1", "P2", "P3")
_FALLBACK_COLOR = "#B6BDCC"


class SeverityTrendChart(QWidget):
    """每根柱子是一天，按 P1/P2/P3 堆叠；P0 计入 P1（模型不产出 P0）。"""

    def __init__(self, *, days: int = 7, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.days = days
        self._daily: list[tuple[str, dict[str, int]]] = []
        self._max_total = 0
        self.setMinimumHeight(150)

    def set_data(self, rows: list[dict[str, Any]]) -> None:
        """rows 来自 Storage.severity_trend：[{day, severity, count}]。"""
        by_day: dict[str, dict[str, int]] = {}
        for row in rows:
            day = str(row.get("day") or "")
            severity = str(row.get("severity") or "P3")
            if severity == "P0":
                severity = "P1"
            if severity not in _SERIES:
                severity = "P3"
            bucket = by_day.setdefault(day, dict.fromkeys(_SERIES, 0))
            bucket[severity] += int(row.get("count") or 0)
        today = date.today()
        self._daily = []
        for offset in range(self.days - 1, -1, -1):
            key = (today - timedelta(days=offset)).isoformat()
            self._daily.append((key, by_day.get(key, dict.fromkeys(_SERIES, 0))))
        self._max_total = max((sum(bucket.values()) for _day, bucket in self._daily), default=0)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()
        label_height = 20
        chart_height = height - label_height - 6
        if not self._daily:
            painter.end()
            return
        if self._max_total == 0:
            painter.setPen(QColor("#9AA3B5"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "最近没有归档的舆情内容")
            painter.end()
            return
        slot_width = width / len(self._daily)
        bar_width = min(34.0, slot_width * 0.55)
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        for index, (day, bucket) in enumerate(self._daily):
            x = index * slot_width + (slot_width - bar_width) / 2
            y = float(chart_height)
            total = sum(bucket.values())
            for severity in reversed(_SERIES):
                count = bucket[severity]
                if count <= 0:
                    continue
                bar_height = chart_height * count / self._max_total
                y -= bar_height
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(SEVERITY_COLORS.get(severity, _FALLBACK_COLOR)))
                painter.drawRoundedRect(QRectF(x, y, bar_width, bar_height), 3, 3)
            painter.setPen(QColor("#5D667A"))
            if total:
                painter.drawText(
                    QRectF(x - 12, max(0.0, y - 16), bar_width + 24, 14),
                    Qt.AlignmentFlag.AlignCenter,
                    str(total),
                )
            painter.setPen(QColor("#9AA3B5"))
            painter.drawText(
                QRectF(index * slot_width, chart_height + 4, slot_width, label_height),
                Qt.AlignmentFlag.AlignCenter,
                day[5:],  # MM-DD
            )
        painter.end()
