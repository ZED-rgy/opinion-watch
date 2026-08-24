"""离线测试用的采集器基类。

测试替身继承 BaseCollector 而不是自由裸类，这样 runner 调用的每个方法
（search / enrich_items / search_quality / pop_search_diagnostic）都由
真实契约约束；采集器新增关键字参数时，测试会立刻暴露而不是靠 runner
在运行时探测签名来兼容。
"""

from opinion_watch.collectors.base import BaseCollector
from opinion_watch.models import Platform


class OfflineCollector(BaseCollector):
    """不触碰 Playwright 的采集器替身；子类只需覆写 search/enrich_items。"""

    platform = Platform.DOUYIN
    home_url = "https://example.test/"
    authenticated_cookie_names = frozenset({"sessionid"})

    def build_search_url(self, keyword: str) -> str:
        return f"https://example.test/search?q={keyword}"

    def parse_content_id(self, url: str) -> str | None:
        return url.rsplit("/", 1)[-1] or None

    def accepts_url(self, url: str) -> bool:
        return url.startswith("https://example.test/")
