"""主题令牌与样式表构建的基础校验（无需 Qt 事件循环）。"""

import re

from opinion_watch.desktop.theme import (
    COLORS,
    SEVERITY_COLORS,
    STATE_COLORS,
    build_stylesheet,
)

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def test_stylesheet_builds_from_tokens() -> None:
    qss = build_stylesheet()
    assert qss.strip()
    assert COLORS["primary"] in qss
    assert "QPushButton:disabled" in qss
    assert qss.count("{") == qss.count("}")


def test_color_tokens_are_hex() -> None:
    for mapping in (COLORS, SEVERITY_COLORS, STATE_COLORS):
        for name, value in mapping.items():
            assert _HEX.match(value), f"{name}={value}"
