"""桌面端共享常量：页面身份、显示名映射和资源路径。"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path

from opinion_watch.models import OpinionCategory, Platform

# assets 位于 opinion_watch/assets，而不是 desktop/ 子包内。
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"


class Page(IntEnum):
    """主窗口导航页的稳定编号；smoke-test 契约要求共 6 页。"""

    SCHEDULER = 0
    KEYWORDS = 1
    ACCOUNTS = 2
    OPINIONS = 3
    NOTIFICATIONS = 4
    SETTINGS = 5


PLATFORM_NAMES = {
    Platform.DOUYIN.value: "抖音",
    Platform.XIAOHONGSHU.value: "小红书",
}
ACCOUNT_STATUS_NAMES = {
    "not_logged_in": "未登录",
    "ready": "可用",
    "login_required": "需要登录",
    "verification_required": "需要验证",
    "captcha_required": "需人工完成验证",
    "rate_limited": "访问受限",
    "error": "异常",
}
RUN_STATUS_NAMES = {
    "running": "巡检中",
    "interrupted": "已中断",
    "succeeded": "已完成",
    "partial": "部分完成",
    "failed": "失败",
    "cancelled": "已取消",
}
RUN_TRIGGER_NAMES = {
    "manual": "手动巡检",
    "watch": "定时巡检",
    "agent": "Agent 导入",
}
REVIEW_STATUS_NAMES = {
    "pending": "待复核",
    "reviewed": "已复核",
    "not_required": "无需复核",
}
ASSESSMENT_SOURCE_NAMES = {"rules": "规则", "model": "大模型", "manual": "人工"}
CATEGORY_NAMES = {
    OpinionCategory.SUSPECTED_FALSE_INFORMATION.value: "疑似虚假信息",
    OpinionCategory.SUSPECTED_DEFAMATION.value: "疑似恶意诽谤",
    OpinionCategory.COORDINATED_COMPLAINT.value: "集中投诉",
    OpinionCategory.SUSPECTED_ASTROTURFING.value: "疑似水军攻击",
    OpinionCategory.REASONABLE_CONSUMER_COMPLAINT.value: "合理消费者投诉",
    OpinionCategory.ORDINARY_GRIEVANCE.value: "普通吐槽",
    OpinionCategory.IRRELEVANT.value: "无关内容",
    OpinionCategory.OTHER.value: "其他",
}


def assessment_source_name(source: str) -> str:
    return ASSESSMENT_SOURCE_NAMES.get(source, source)
