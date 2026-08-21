"""桌面端主题令牌与样式表构建。

所有颜色、间距、圆角集中在这里；组件通过 objectName 或动态属性挂接
QSS 规则。运行时用 setProperty 改变外观后必须调用 repolish()，否则
Qt 不会重新计算样式。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZE_PT = 10

COLORS = {
    "primary": "#2F5BEA",
    "primary_hover": "#244DD0",
    "primary_pressed": "#1F43B8",
    "primary_soft": "#EAF0FF",
    "primary_text": "#244BC5",
    "focus_ring": "#8DA8FF",
    "selection_bg": "#DCE7FF",
    "selection_text": "#102A72",
    "bg": "#F7F8FA",
    "surface": "#FFFFFF",
    "surface_alt": "#FAFBFC",
    "border": "#E5E8EF",
    "border_soft": "#EDF0F4",
    "border_input": "#D8DDE7",
    "text": "#182033",
    "text_strong": "#10182B",
    "text_muted": "#697386",
    "text_faint": "#8B96AA",
    "danger": "#C93642",
    "danger_soft": "#FFF2F3",
    "danger_border": "#F0C8CC",
    "warning": "#D18A18",
    "warning_text": "#A06B00",
    "warning_soft": "#FFF9E9",
    "warning_border": "#F4E4AF",
    "success": "#18845B",
    "success_soft": "#EAF8F1",
    "success_border": "#CDEEDD",
    "badge": "#D9363E",
}

SEVERITY_COLORS = {
    "P0": "#C93642",
    "P1": "#D46B22",
    "P2": "#A06B00",
    "P3": "#667085",
    "error": "#C93642",
    "warning": "#A06B00",
    "info": "#2F5BEA",
}

STATE_COLORS = {
    "ok": COLORS["success"],
    "warn": COLORS["warning"],
    "muted": "#8A6270",
}

SPACING = {"xs": 4, "sm": 8, "md": 14, "lg": 18, "xl": 30}
RADII = {"sm": 8, "md": 10, "lg": 14, "pill": 16}


def repolish(widget: QWidget) -> None:
    """在运行时修改动态属性后强制 QSS 重新求值。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def build_stylesheet() -> str:
    c = COLORS
    return f"""
QMainWindow, QWidget {{
    background: {c["bg"]};
    color: {c["text"]};
    font-size: 14px;
}}
QWidget#page {{ background: {c["bg"]}; }}
QWidget#emptyState, QWidget#surfaceRow, QLabel, QCheckBox {{ background: transparent; }}
QStackedWidget#surfaceStack {{ background: transparent; }}
QSplitter#opinionSplit {{ background: transparent; }}
QSplitter::handle {{ background: {c["border"]}; width: 1px; }}
QFrame#sidebar {{ background: {c["surface"]}; border-right: 1px solid #E8EAF0; }}
QLabel#brandName {{ color: #14213D; font-size: 17px; font-weight: 700; }}
QLabel#eyebrow {{ color: #71798A; font-size: 12px; font-weight: 600; }}
QLabel#pageTitle {{ color: {c["text_strong"]}; font-size: 28px; font-weight: 700; }}
QLabel#pageSubtitle {{ color: {c["text_muted"]}; font-size: 14px; }}
QLabel#sectionTitle {{ color: #172033; font-size: 17px; font-weight: 700; }}
QLabel#muted {{ color: #778095; }}
QLabel#heroTime {{ color: {c["text_strong"]}; font-size: 34px; font-weight: 700; }}
QLabel#metric {{ color: {c["text_strong"]}; font-size: 24px; font-weight: 700; }}
QLabel#emptyTitle {{ font-size: 16px; font-weight: 700; color: #30394C; }}
QLabel#alertText {{ color: {c["warning_text"]}; }}
QLabel#noticeText {{ color: #725214; font-size: 12px; }}
QFrame#statusPill QLabel {{ color: #176A4C; font-weight: 600; }}
QLabel#heroCalendar {{
    background: {c["success_soft"]};
    border-radius: 28px;
}}
QLabel#readyDot {{ font-size: 10px; }}
QLabel#readyDot[state="ok"] {{ color: {c["success"]}; }}
QLabel#readyDot[state="warn"] {{ color: {c["warning"]}; }}
QLabel[state="ok"] {{ color: {c["success"]}; font-weight: 600; }}
QLabel[state="warn"] {{ color: {c["warning"]}; font-weight: 600; }}
QLabel[state="muted"] {{ color: {STATE_COLORS["muted"]}; }}
QLabel#toast {{
    background: #2B3245;
    color: #FFFFFF;
    border-radius: {RADII["md"]}px;
    padding: 10px 18px;
    font-weight: 600;
}}
QLabel[pill="severity"] {{
    border-radius: 10px;
    padding: 2px 10px;
    font-weight: 700;
    font-size: 12px;
}}
QLabel[pill="severity"][level="P0"] {{ background: #FDECEC; color: {SEVERITY_COLORS["P0"]}; }}
QLabel[pill="severity"][level="P1"] {{ background: #FDF0E4; color: {SEVERITY_COLORS["P1"]}; }}
QLabel[pill="severity"][level="P2"] {{
    background: {c["warning_soft"]}; color: {SEVERITY_COLORS["P2"]};
}}
QLabel[pill="severity"][level="P3"] {{ background: #F0F2F5; color: {SEVERITY_COLORS["P3"]}; }}
QFrame#surface, QFrame#hero {{
    background: {c["surface"]};
    border: 1px solid {c["border"]};
    border-radius: {RADII["lg"]}px;
}}
QFrame#hero {{ border: 1px solid #DFE5F2; }}
QFrame#detailPanel {{
    background: {c["surface"]};
    border: 1px solid {c["border"]};
    border-radius: {RADII["lg"]}px;
}}
QFrame#statusPill {{
    background: {c["success_soft"]};
    border: 1px solid {c["success_border"]};
    border-radius: {RADII["pill"]}px;
}}
QFrame#notice {{
    background: {c["warning_soft"]};
    border: 1px solid {c["warning_border"]};
    border-radius: {RADII["md"]}px;
}}
QFrame#divider {{ background: #E7EAF0; border: 0; }}
QPushButton {{
    min-height: 36px;
    padding: 0 15px;
    background: {c["primary"]};
    color: #FFFFFF;
    border: 1px solid {c["primary"]};
    border-radius: {RADII["sm"]}px;
    font-weight: 600;
}}
QPushButton:hover {{ background: {c["primary_hover"]}; border-color: {c["primary_hover"]}; }}
QPushButton:pressed {{ background: {c["primary_pressed"]}; }}
QPushButton:focus {{ border: 2px solid {c["focus_ring"]}; }}
QPushButton:disabled {{
    background: #E9ECF2;
    color: #A6AEBF;
    border-color: #E9ECF2;
}}
QPushButton[role="secondary"] {{
    background: {c["surface"]};
    color: #293248;
    border-color: #D9DEE8;
}}
QPushButton[role="secondary"]:hover {{ background: #F2F4F8; }}
QPushButton[role="secondary"]:disabled {{
    background: #F5F6F9;
    color: #B3BAC8;
    border-color: #E9ECF2;
}}
QPushButton[role="danger"] {{
    background: {c["surface"]};
    color: {c["danger"]};
    border-color: {c["danger_border"]};
}}
QPushButton[role="danger"]:hover {{ background: {c["danger_soft"]}; }}
QPushButton[role="danger"]:disabled {{
    background: #F5F6F9;
    color: #B3BAC8;
    border-color: #E9ECF2;
}}
QComboBox, QSpinBox, QTimeEdit, QLineEdit, QTextEdit {{
    min-height: 36px;
    background: {c["surface"]};
    color: #20283B;
    border: 1px solid {c["border_input"]};
    border-radius: {RADII["sm"]}px;
    padding: 0 10px;
    selection-background-color: #DDE6FF;
}}
QComboBox:focus, QSpinBox:focus, QTimeEdit:focus, QLineEdit:focus,
QTextEdit:focus {{ border: 2px solid {c["primary"]}; }}
QTextEdit {{ padding: 8px; }}
QCheckBox {{ spacing: 8px; color: #313A4D; }}
QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: #C9CFDA; }}
QCheckBox::indicator:checked {{ background: {c["primary"]}; }}
QTableWidget {{
    background: {c["surface"]};
    alternate-background-color: {c["surface_alt"]};
    border: 0;
    gridline-color: transparent;
    selection-background-color: #EEF3FF;
    selection-color: {c["text"]};
    outline: 0;
}}
QTableWidget::item {{ border-bottom: 1px solid {c["border_soft"]}; padding: 8px; }}
QTableWidget::item:selected {{
    background: {c["selection_bg"]};
    color: {c["selection_text"]};
    border-left: 3px solid {c["primary"]};
    font-weight: 600;
}}
QHeaderView::section {{
    background: #F6F7F9;
    color: #5D667A;
    border: 0;
    border-bottom: 1px solid #E5E8EE;
    padding: 10px 8px;
    font-weight: 600;
}}
QToolTip {{
    background: #2B3245;
    color: #FFFFFF;
    border: 0;
    padding: 6px 10px;
    font-size: 13px;
}}
QMenu {{
    background: {c["surface"]};
    border: 1px solid {c["border"]};
    border-radius: {RADII["sm"]}px;
    padding: 6px;
}}
QMenu::item {{ padding: 8px 22px; border-radius: 6px; color: {c["text"]}; }}
QMenu::item:selected {{ background: {c["primary_soft"]}; color: {c["primary_text"]}; }}
QListWidget#navigation {{
    background: {c["surface"]};
    border: 0;
    outline: 0;
    padding: 6px 12px;
}}
QListWidget#navigation::item {{
    color: #596277; padding: 11px 12px; border-radius: {RADII["sm"]}px; margin: 2px 0;
}}
QListWidget#navigation::item:hover {{ background: #F3F5F9; color: #26334D; }}
QListWidget#navigation::item:selected {{
    background: {c["primary_soft"]};
    color: {c["primary_text"]};
    font-weight: 600;
}}
QListWidget#timeline {{ background: transparent; border: 0; outline: 0; }}
QListWidget#timeline::item {{
    background: #F8F9FB; border: 1px solid #EAEDF2;
    border-radius: {RADII["sm"]}px; margin: 3px 0; padding: 10px;
}}
QListWidget#timeline::item:selected {{
    background: {c["selection_bg"]};
    color: {c["selection_text"]};
    border: 2px solid {c["primary"]};
    font-weight: 600;
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C9CFDA; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""
