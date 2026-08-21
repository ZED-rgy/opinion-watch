import asyncio

from playwright.async_api import Error as PlaywrightError

from opinion_watch.collectors.douyin import DouyinCollector
from opinion_watch.collectors.xiaohongshu import XiaohongshuCollector
from opinion_watch.models import AnchorCandidate, Platform, SessionStatus


def test_douyin_extracts_and_deduplicates_at_storage_boundary() -> None:
    collector = DouyinCollector()
    items = collector.items_from_anchors(
        [
            AnchorCandidate("https://www.douyin.com/video/7512345678901234567", "  示例 视频 "),
            AnchorCandidate("https://example.com/video/7512345678901234567", "无关"),
        ],
        "速探长",
    )

    assert len(items) == 1
    assert items[0].platform is Platform.DOUYIN
    assert items[0].content_id == "7512345678901234567"
    assert items[0].title == "示例 视频"


def test_xiaohongshu_parses_explore_link() -> None:
    collector = XiaohongshuCollector()
    note_id = "66fad51c000000001b0224b8"

    assert (
        collector.parse_content_id(
            f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=secret"
        )
        == note_id
    )
    assert collector.accepts_url(f"https://www.xiaohongshu.com/explore/{note_id}")
    assert collector.accepts_url(
        f"https://www.xiaohongshu.com/search_result/{note_id}?xsec_token=public-link-token"
    )


def test_session_status_accepts_visible_authenticated_avatar_when_cookie_name_changes() -> None:
    class FakeLocator:
        def __init__(self, visible: bool) -> None:
            self.visible = visible
            self.first = self

        async def count(self) -> int:
            return 1 if self.visible else 0

        async def is_visible(self) -> bool:
            return self.visible

        async def inner_text(self, timeout: int) -> str:
            return ""

    class FakePage:
        url = "https://www.douyin.com/jingxuan"

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(selector == '[data-e2e="header-avatar"]')

    class FakeContext:
        async def cookies(self) -> list[dict[str, str]]:
            return [{"name": "new_session_cookie"}]

    status = asyncio.run(DouyinCollector().session_status(FakePage(), FakeContext()))  # type: ignore[arg-type]
    assert status is SessionStatus.HEALTHY


def test_canonical_url_removes_rotating_query_parameters() -> None:
    collector = XiaohongshuCollector()

    assert (
        collector.canonical_url("https://www.xiaohongshu.com/explore/abc?xsec_token=secret#section")
        == "https://www.xiaohongshu.com/explore/abc"
    )


def test_anchor_extraction_retries_when_navigation_replaces_context() -> None:
    class FakeLocator:
        calls = 0

        async def evaluate_all(self, _expression: str) -> list[dict[str, str]]:
            self.calls += 1
            if self.calls == 1:
                raise PlaywrightError(
                    "Execution context was destroyed, most likely because of a navigation"
                )
            return [
                {
                    "id": "waterfall_item_7512345678901234567",
                    "text": "图文\n结果",
                }
            ]

    class FakePage:
        def __init__(self) -> None:
            self.anchor_locator = FakeLocator()
            self.waits: list[int] = []

        def locator(self, selector: str) -> FakeLocator:
            assert selector == '[id^="waterfall_item_"]'
            return self.anchor_locator

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waits.append(timeout_ms)

    page = FakePage()
    anchors = asyncio.run(DouyinCollector()._extract_anchors(page))  # type: ignore[arg-type]

    assert anchors == [
        AnchorCandidate(
            "https://www.douyin.com/note/7512345678901234567",
            "图文\n结果",
            "image",
        )
    ]
    assert page.waits == [750]
