import asyncio

from playwright.async_api import Error as PlaywrightError

from opinion_watch.collectors.douyin import DouyinCollector
from opinion_watch.collectors.xiaohongshu import XiaohongshuCollector
from opinion_watch.models import AnchorCandidate, CollectedContent, Platform, SessionStatus


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


def test_douyin_parses_jingxuan_modal_content_id() -> None:
    collector = DouyinCollector()
    content_id = "7676355993751637283"

    assert (
        collector.parse_content_id(f"https://www.douyin.com/jingxuan?modal_id={content_id}")
        == content_id
    )
    assert collector.accepts_url(f"https://www.douyin.com/jingxuan?modal_id={content_id}")
    assert collector.parse_content_id(f"https://example.com/jingxuan?modal_id={content_id}") is None
    assert collector.parse_content_id(f"https://example.com/video/{content_id}") is None


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


def test_gateway_error_page_is_not_treated_as_empty_search() -> None:
    class BodyLocator:
        async def inner_text(self, timeout: int) -> str:
            return "502 Bad Gateway\\nTengine\\nupstream service unavailable"

    class FakePage:
        async def title(self) -> str:
            return "502 Bad Gateway"

        def locator(self, selector: str) -> BodyLocator:
            assert selector == "body"
            return BodyLocator()

    message = asyncio.run(DouyinCollector()._upstream_error(FakePage()))  # type: ignore[arg-type]

    assert message is not None
    assert "HTTP 502" in message
    assert "重试" in message


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


def test_xiaohongshu_search_login_modal_with_session_cookie_is_verification() -> None:
    class FakeLocator:
        first = None

        async def inner_text(self, timeout: int) -> str:
            return "登录后查看搜索结果"

        async def count(self) -> int:
            return 0

        async def is_visible(self) -> bool:
            return False

    class FakePage:
        url = "https://www.xiaohongshu.com/search_result?keyword=速探长"

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator()

    class FakeContext:
        async def cookies(self) -> list[dict[str, str]]:
            return [{"name": "web_session"}]

    status = asyncio.run(
        XiaohongshuCollector().session_status(FakePage(), FakeContext())  # type: ignore[arg-type]
    )
    assert status is SessionStatus.VERIFICATION_REQUIRED


def test_dismiss_login_modal_clicks_close_selector_before_escape() -> None:
    clicked: list[str] = []
    pressed: list[str] = []

    class FakeLocator:
        def __init__(self, selector: str, visible: bool) -> None:
            self.selector = selector
            self.visible = visible
            self.first = self

        async def count(self) -> int:
            return 1 if self.visible else 0

        async def is_visible(self) -> bool:
            return self.visible

        async def click(self, timeout: int) -> None:
            clicked.append(self.selector)

    class FakeKeyboard:
        async def press(self, key: str) -> None:
            pressed.append(key)

    class FakePage:
        keyboard = FakeKeyboard()

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(selector, visible="login-container" in selector)

    collector = XiaohongshuCollector()
    dismissed = asyncio.run(collector._dismiss_login_modal(FakePage()))  # type: ignore[arg-type]
    assert dismissed
    assert clicked and "login-container" in clicked[0]
    assert pressed == []  # 命中关闭按钮后不再按 Esc


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
            "结果",  # "图文" 媒体标记被标题清洗剔除
            "image",
        )
    ]
    assert page.waits == [750]


def test_douyin_card_text_cleaning_strips_metrics_and_dates() -> None:
    sample = "图文\n25 我是dy团长，速探长物流靠谱吗大家避雷 #物流\n@某某用户\n1.2万\n08-21"
    assert (
        DouyinCollector._clean_card_text(sample) == "25 我是dy团长，速探长物流靠谱吗大家避雷 #物流"
    )


def test_xiaohongshu_anchor_metadata_is_preserved_for_card_extraction() -> None:
    collector = XiaohongshuCollector()
    items = collector.items_from_anchors(
        [
            AnchorCandidate(
                "https://www.xiaohongshu.com/explore/66fad51c000000001b0224b8",
                "速探长物流体验",
                "image",
                "小红书用户",
                "速探长物流体验\n小红书用户\n收藏 12",
            )
        ],
        "速探长",
    )

    assert items[0].title == "速探长物流体验"
    assert items[0].author_name == "小红书用户"
    assert items[0].raw_data["search_card_text"].startswith("速探长物流体验")


def test_search_quality_flags_empty_titles() -> None:
    items = [collector_item("one", ""), collector_item("two", "")]

    quality = XiaohongshuCollector.search_quality(items)

    assert quality["all_titles_empty"]
    assert quality["needs_diagnostic"]
    assert quality["empty_title_ratio"] == 1.0


def collector_item(content_id: str, title: str) -> CollectedContent:
    return CollectedContent(
        platform=Platform.XIAOHONGSHU,
        content_id=content_id,
        url=f"https://www.xiaohongshu.com/explore/{content_id}",
        title=title,
        source_keyword="速探长",
        raw_data={"search_card_text": "卡片正文"},
    )


def test_media_filter_rejects_static_and_tiny_assets() -> None:
    assert not XiaohongshuCollector._is_relevant_media(
        {"width": 64, "height": 64, "className": "avatar"},
        "https://cdn.example.com/avatar.png",
    )
    assert not XiaohongshuCollector._is_relevant_media(
        {"width": 800, "height": 600, "className": "note-image"},
        "data:image/png;base64,abc",
    )
    assert XiaohongshuCollector._is_relevant_media(
        {"width": 800, "height": 600, "className": "note-image"},
        "https://cdn.example.com/note-image.jpg",
    )
