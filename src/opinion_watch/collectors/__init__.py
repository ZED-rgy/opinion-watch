from opinion_watch.collectors.douyin import DouyinCollector
from opinion_watch.collectors.xiaohongshu import XiaohongshuCollector
from opinion_watch.models import Platform


def collector_for(platform: Platform) -> DouyinCollector | XiaohongshuCollector:
    if platform is Platform.DOUYIN:
        return DouyinCollector()
    if platform is Platform.XIAOHONGSHU:
        return XiaohongshuCollector()
    raise ValueError(f"不支持的平台：{platform}")


__all__ = ["DouyinCollector", "XiaohongshuCollector", "collector_for"]
