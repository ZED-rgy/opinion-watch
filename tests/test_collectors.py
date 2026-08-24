import asyncio
from dataclasses import replace

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.classification import brand_matches_card
from opinion_watch.collectors.base import CollectorRuntimeError
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
                    "href": "https://www.douyin.com/note/7512345678901234567",
                    "text": "图文\n结果",
                    "hasMedia": True,
                    "isRelatedSearch": False,
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
            raw_text="图文\n结果",
        )
    ]
    assert page.waits == [750]


def test_douyin_anchor_extraction_ignores_related_search_module() -> None:
    class FakePage:
        async def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        class Locator:
            async def evaluate_all(self, _expression: str) -> list[dict[str, object]]:
                return [
                    {
                        "id": "waterfall_item_7474402199671395628",
                        "href": "",
                        "text": "相关搜索\n深圳逐影国际\n逐影公司",
                        "hasMedia": False,
                        "isRelatedSearch": True,
                    },
                    {
                        "id": "waterfall_item_7652677355172941066",
                        "href": "https://www.douyin.com/video/7652677355172941066",
                        "text": "深圳逐影真实视频",
                        "hasMedia": True,
                        "isRelatedSearch": False,
                    },
                ]

        def locator(self, selector: str) -> "FakePage.Locator":
            assert selector == '[id^="waterfall_item_"]'
            return self.Locator()

    anchors = asyncio.run(DouyinCollector()._extract_anchors(FakePage()))  # type: ignore[arg-type]

    assert [anchor.href for anchor in anchors] == [
        "https://www.douyin.com/video/7652677355172941066"
    ]
    assert all("7474402199671395628" not in anchor.href for anchor in anchors)


def test_douyin_anchor_id_fallback_requires_media() -> None:
    class FakePage:
        async def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        class Locator:
            async def evaluate_all(self, _expression: str) -> list[dict[str, object]]:
                return [
                    {
                        "id": "waterfall_item_7474402199671395628",
                        "href": "",
                        "text": "深圳逐影国际",
                        "hasMedia": False,
                        "isRelatedSearch": False,
                    },
                    {
                        "id": "waterfall_item_7512345678901234567",
                        "href": "",
                        "text": "图文\n真实媒体卡片",
                        "hasMedia": True,
                        "isRelatedSearch": False,
                    },
                ]

        def locator(self, _selector: str) -> "FakePage.Locator":
            return self.Locator()

    anchors = asyncio.run(DouyinCollector()._extract_anchors(FakePage()))  # type: ignore[arg-type]

    assert anchors == [
        AnchorCandidate(
            "https://www.douyin.com/note/7512345678901234567",
            "真实媒体卡片",
            "image",
            raw_text="图文\n真实媒体卡片",
        )
    ]


def test_douyin_anchor_keeps_full_card_text_and_author() -> None:
    """标题清洗只留最长一行；品牌归属判断依赖整段卡片文本和作者名。"""

    class FakePage:
        async def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        class Locator:
            async def evaluate_all(self, _expression: str) -> list[dict[str, object]]:
                return [
                    {
                        "id": "waterfall_item_7512345678901234567",
                        "href": "https://www.douyin.com/video/7512345678901234567",
                        "text": "视频\n这家公司太坑了千万别去\n@速探长官方\n1.2万\n08-21",
                        "author": "速探长官方",
                        "hasMedia": True,
                        "isRelatedSearch": False,
                    }
                ]

        def locator(self, _selector: str) -> "FakePage.Locator":
            return self.Locator()

    anchors = asyncio.run(DouyinCollector()._extract_anchors(FakePage()))  # type: ignore[arg-type]

    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor.text == "这家公司太坑了千万别去"
    assert anchor.author_name == "速探长官方"
    # 品牌名只出现在作者行，必须仍然保留在可判断的文本里。
    assert "速探长" in anchor.raw_text


def test_douyin_card_author_falls_back_to_at_line_without_dom_node() -> None:
    """DOM 没有作者节点时，回退到 innerText 里的 "@作者" 行。"""

    class FakePage:
        async def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        class Locator:
            async def evaluate_all(self, _expression: str) -> list[dict[str, object]]:
                return [
                    {
                        "id": "waterfall_item_7512345678901234567",
                        "href": "https://www.douyin.com/video/7512345678901234567",
                        "text": "视频\n物流慢得离谱\n@优速卖跨境\n300",
                        "author": "",
                        "hasMedia": True,
                        "isRelatedSearch": False,
                    }
                ]

        def locator(self, _selector: str) -> "FakePage.Locator":
            return self.Locator()

    anchors = asyncio.run(DouyinCollector()._extract_anchors(FakePage()))  # type: ignore[arg-type]

    assert anchors[0].author_name == "优速卖跨境"


def test_douyin_card_text_reaches_brand_matching() -> None:
    """回归问题一：抖音卡片必须把整段文本带到 search_card_text。"""
    collector = DouyinCollector()
    items = collector.items_from_anchors(
        [
            AnchorCandidate(
                "https://www.douyin.com/video/7512345678901234567",
                "这家公司太坑了千万别去",
                "video",
                author_name="速探长官方",
                raw_text="视频\n这家公司太坑了千万别去\n@速探长官方\n1.2万",
            )
        ],
        "速探长",
    )

    assert len(items) == 1
    item = items[0]
    assert item.author_name == "速探长官方"
    assert "速探长" in item.raw_data["search_card_text"]
    assert brand_matches_card(
        {
            "title": item.title,
            "brand_names": ["速探长"],
            "raw_data": item.raw_data,
        }
    )


def test_douyin_verification_recovery_switches_to_classic_search_route() -> None:
    class Page:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        async def goto(self, url: str, **_kwargs: object) -> None:
            self.url = url

    recovered = asyncio.run(
        DouyinCollector()._recover_search_access(
            Page(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            "速探长",
            SessionStatus.VERIFICATION_REQUIRED,
        )
    )

    assert recovered is True


def test_search_rechecks_status_after_route_recovery() -> None:
    content_id = "7652677355172941066"

    class Page:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    class RecoveringCollector(DouyinCollector):
        status_checks = 0
        recovered = False

        async def _open_search_page(self, page: object, search_url: str, keyword: str) -> None:
            return None

        async def session_status(self, page: object, context: object) -> SessionStatus:
            self.status_checks += 1
            return SessionStatus.HEALTHY if self.recovered else SessionStatus.VERIFICATION_REQUIRED

        async def _dismiss_login_modal(self, page: object) -> bool:
            return False

        async def _recover_search_access(
            self,
            page: object,
            context: object,
            keyword: str,
            status: SessionStatus,
        ) -> bool:
            self.recovered = True
            return True

        async def _upstream_error(self, page: object) -> None:
            return None

        async def _wait_for_content_anchors(
            self, page: object, *, timeout_ms: int
        ) -> list[AnchorCandidate]:
            return [
                AnchorCandidate(
                    f"https://www.douyin.com/video/{content_id}",
                    "速探长公开内容",
                )
            ]

    collector = RecoveringCollector()
    items = asyncio.run(
        collector.search(
            Page(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            "速探长",
            limit=1,
        )
    )

    assert collector.status_checks == 2
    assert collector.recovered is True
    assert [item.content_id for item in items] == [content_id]


def test_verification_message_separates_route_access_from_account_health() -> None:
    message = DouyinCollector._search_access_message(SessionStatus.VERIFICATION_REQUIRED)

    assert "搜索页" in message
    assert "账号登录状态未改变" in message
    assert "verification_required" not in message


def test_captcha_overlay_outranks_login_wall_selector() -> None:
    """验证遮罩常复用登录弹窗的类名，必须先按文案判定为人机验证。

    否则会被归入 VERIFICATION_REQUIRED，触发关闭弹窗/切换备用路由的恢复
    动作——那等同于绕过验证，是 AGENTS.md 的合规红线。
    """

    class FakeLocator:
        first = None

        async def inner_text(self, timeout: int) -> str:
            return "安全验证\n请完成下列验证后继续\n拖动滑块完成拼图"

        async def count(self) -> int:
            return 1

        async def is_visible(self) -> bool:
            return True

    class FakePage:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator()

    class FakeContext:
        async def cookies(self) -> list[dict[str, str]]:
            return [{"name": "sessionid"}]

    status = asyncio.run(
        DouyinCollector().session_status(FakePage(), FakeContext())  # type: ignore[arg-type]
    )
    assert status is SessionStatus.CAPTCHA_REQUIRED


def test_search_aborts_on_captcha_without_dismissing_or_rerouting() -> None:
    attempts: list[str] = []

    class Page:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    class CaptchaCollector(DouyinCollector):
        async def _open_search_page(self, page: object, search_url: str, keyword: str) -> None:
            return None

        async def session_status(self, page: object, context: object) -> SessionStatus:
            return SessionStatus.CAPTCHA_REQUIRED

        async def _dismiss_login_modal(self, page: object) -> bool:
            attempts.append("dismiss")
            return True

        async def _recover_search_access(
            self, page: object, context: object, keyword: str, status: SessionStatus
        ) -> bool:
            attempts.append("reroute")
            return True

        async def _upstream_error(self, page: object) -> None:
            return None

    with pytest.raises(CollectorRuntimeError) as excinfo:
        asyncio.run(
            CaptchaCollector().search(
                Page(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                "速探长",
                limit=1,
            )
        )

    assert excinfo.value.status is SessionStatus.CAPTCHA_REQUIRED
    # 关键断言：命中人机验证后一次绕过尝试都不能发生。
    assert attempts == []


def test_captcha_recovery_is_refused_even_when_called_directly() -> None:
    class Page:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        async def goto(self, url: str, **_kwargs: object) -> None:
            self.url = url

    page = Page()
    recovered = asyncio.run(
        DouyinCollector()._recover_search_access(
            page,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            "速探长",
            SessionStatus.CAPTCHA_REQUIRED,
        )
    )

    assert recovered is False
    assert page.url == "https://www.douyin.com/jingxuan/search/速探长"


def test_captcha_message_states_no_bypass_will_be_attempted() -> None:
    message = DouyinCollector._search_access_message(SessionStatus.CAPTCHA_REQUIRED)

    assert "人机验证" in message
    assert "不会尝试绕过" in message
    assert "captcha_required" not in message


def test_douyin_card_text_cleaning_strips_metrics_and_dates() -> None:
    sample = "图文\n25 我是dy团长，速探长物流靠谱吗大家避雷 #物流\n@某某用户\n1.2万\n08-21"
    assert (
        DouyinCollector._clean_card_text(sample) == "25 我是dy团长，速探长物流靠谱吗大家避雷 #物流"
    )


def test_reached_search_page_tolerates_platform_appended_parameters() -> None:
    """平台一定会改写搜索 URL，全等比较会把正常页面误判成导航超时。"""
    reached = XiaohongshuCollector._reached_search_page
    target = "https://www.xiaohongshu.com/search_result?keyword=速探长"

    # 平台追加自己的参数、重排顺序、补上尾斜杠——都仍然是目标搜索页。
    assert reached("https://www.xiaohongshu.com/search_result?keyword=速探长&type=51", target)
    assert reached("https://www.xiaohongshu.com/search_result?type=51&keyword=速探长", target)
    assert reached("https://www.xiaohongshu.com/search_result/?keyword=速探长", target)
    # 关键词被换掉、路径不同、跨站——必须仍然判为没到目标页。
    assert not reached("https://www.xiaohongshu.com/search_result?keyword=别的品牌", target)
    assert not reached("https://www.xiaohongshu.com/explore?keyword=速探长", target)
    assert not reached("https://www.douyin.com/search_result?keyword=速探长", target)


def test_reached_search_page_matches_percent_encoded_keyword_in_path() -> None:
    """抖音把关键词放在路径里，浏览器回报的是百分号编码形式。"""
    target = DouyinCollector().build_search_url("速探长")

    assert "%" in target  # build_search_url 用 quote 编码
    assert DouyinCollector._reached_search_page(
        "https://www.douyin.com/jingxuan/search/速探长", target
    )
    assert not DouyinCollector._reached_search_page("https://www.douyin.com/jingxuan", target)


def test_failed_in_place_detail_click_returns_search_url_for_restore() -> None:
    """点击把搜索页路由到错配详情页时，必须交回 before_url 供调用方复位。

    否则搜索页永久停在错误详情页上，该关键词剩余候选会全部以
    "搜索页找不到对应卡片"失败，错因还被记成卡片问题。
    """
    search_url = "https://www.douyin.com/jingxuan/search/速探长"

    class CardLocator:
        first = None

        async def count(self) -> int:
            return 1

        async def click(self, **_kwargs: object) -> None:
            return None

    class PopupExpectation:
        async def __aenter__(self) -> "PopupExpectation":
            return self

        async def __aexit__(self, *_args: object) -> None:
            raise PlaywrightTimeoutError("no popup")

    class SearchPage:
        def __init__(self) -> None:
            self.url = search_url

        def locator(self, _selector: str) -> CardLocator:
            locator = CardLocator()
            locator.first = locator
            return locator

        def expect_popup(self, **_kwargs: object) -> PopupExpectation:
            return PopupExpectation()

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            # 点击在当前页改了路由，落到了另一条内容的详情页。
            self.url = "https://www.douyin.com/video/9999999999999999999"

    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="7652677355172941066",
        url="https://www.douyin.com/video/7652677355172941066",
        title="速探长空运小包",
        source_keyword="速探长",
    )

    target, restore_url, error = asyncio.run(
        DouyinCollector()._open_detail_by_click(SearchPage(), item)  # type: ignore[arg-type]
    )

    assert target is None
    assert restore_url == search_url
    assert "错配" in error


def test_missing_card_does_not_request_restore_to_a_stale_url() -> None:
    """找不到卡片时什么都没点，此时的 url 可能本身就是坏的，不能拿去复位。"""

    class CardLocator:
        first = None

        async def count(self) -> int:
            return 0

    class SearchPage:
        url = "https://www.douyin.com/video/9999999999999999999"

        def locator(self, _selector: str) -> CardLocator:
            locator = CardLocator()
            locator.first = locator
            return locator

    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="7652677355172941066",
        url="https://www.douyin.com/video/7652677355172941066",
        title="速探长空运小包",
        source_keyword="速探长",
    )

    target, restore_url, error = asyncio.run(
        DouyinCollector()._open_detail_by_click(SearchPage(), item)  # type: ignore[arg-type]
    )

    assert target is None
    assert restore_url is None
    assert error == "搜索页找不到对应卡片"


def test_media_evidence_falls_back_to_bare_img_and_video_elements() -> None:
    """容器选择器全部落空时，回退分支必须直接取 img/video 本身。

    原实现把回退定位器当成容器，去 img/video 内部再找 img/video，恒为空；
    多元素定位器触发的 strict 违规异常又被 suppress 吞掉，选择器一改版就
    静默退化成"这条内容没有媒体证据"。
    """
    requested: list[str] = []

    class Handle:
        async def evaluate(self, _expression: str) -> dict[str, object]:
            return {
                "kind": "img",
                "src": "https://p3.douyinpic.com/cover/large.jpeg",
                "poster": "",
                "alt": "速探长物流",
                "width": 1080,
                "height": 1920,
                "id": "",
                "className": "detail-cover",
                "parentClass": "video-player",
                "ancestorClass": "",
            }

        async def screenshot(self, **_kwargs: object) -> None:
            return None

    class Locator:
        def __init__(self, selector: str, handles: list[Handle]) -> None:
            self.selector = selector
            self.handles = handles
            self.first = self

        async def count(self) -> int:
            return 0  # 所有 media_container_selectors 都落空

        async def is_visible(self) -> bool:
            return False

        async def element_handles(self) -> list[Handle]:
            return self.handles

    class FakePage:
        url = "https://www.douyin.com/video/7652677355172941066"

        def locator(self, selector: str) -> Locator:
            requested.append(selector)
            return Locator(selector, [Handle()] if "main img" in selector else [])

    evidence = asyncio.run(
        DouyinCollector()._extract_media_evidence(
            FakePage(),  # type: ignore[arg-type]
            artifact_dir=None,
            content_id="7652677355172941066",
        )
    )

    assert "main img, main video" in requested
    assert [(node["kind"], node["url"]) for node in evidence] == [
        ("image", "https://p3.douyinpic.com/cover/large.jpeg")
    ]


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


def test_xiaohongshu_author_name_drops_trailing_timestamp() -> None:
    normalize = XiaohongshuCollector._normalize_author_name

    assert normalize("boss炸雷 04-24") == "boss炸雷"
    assert normalize("智己萌萌懂 2025-12-03") == "智己萌萌懂"
    assert normalize("每天分享一小点 昨天 13:10") == "每天分享一小点"
    assert normalize("旅行的意义 3天前") == "旅行的意义"
    assert normalize("速运日记 19小时前") == "速运日记"
    assert normalize("@momo 刚刚") == "momo"
    # 名字本身含数字或连字符时不能被误伤。
    assert normalize("00 07-31") == "00"
    assert normalize("2026计划") == "2026计划"


def test_xiaohongshu_author_echo_title_is_treated_as_empty() -> None:
    """回归问题二：标题回退到作者行时必须留空，让质量闸门看得见。"""

    class FakePage:
        async def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

        class Locator:
            async def evaluate_all(self, _expression: str) -> list[dict[str, object]]:
                return [
                    {
                        "href": "https://www.xiaohongshu.com/explore/69d702d00000000023010177",
                        "text": "boss炸雷 04-09 赞",
                        "author_name": "boss炸雷 04-09",
                        "raw_text": "boss炸雷 04-09 赞",
                        "media_kind": "image",
                    },
                    {
                        "href": "https://www.xiaohongshu.com/explore/6a6a1063000000001f01f661",
                        "text": "⚠️深圳这家公司快跑",
                        "author_name": "00 07-31",
                        "raw_text": "⚠️深圳这家公司快跑 00 07-31 30",
                        "media_kind": "image",
                    },
                ]

        def locator(self, _selector: str) -> "FakePage.Locator":
            return self.Locator()

    anchors = asyncio.run(XiaohongshuCollector()._extract_anchors(FakePage()))  # type: ignore[arg-type]

    assert anchors[0].text == ""
    assert anchors[0].author_name == "boss炸雷"
    # 真实标题不受影响。
    assert anchors[1].text == "⚠️深圳这家公司快跑"
    assert anchors[1].author_name == "00"


def test_xiaohongshu_author_echo_titles_reach_quality_gate() -> None:
    """伪标题必须计入 empty_title，否则选择器退化会静默通过。"""
    collector = XiaohongshuCollector()
    items = collector.items_from_anchors(
        [
            AnchorCandidate(
                "https://www.xiaohongshu.com/explore/69d702d00000000023010177",
                "",
                "image",
                "boss炸雷",
                "boss炸雷 04-09 赞",
            )
        ],
        "速探长",
    )

    quality = XiaohongshuCollector.search_quality(items)

    assert quality["empty_title"] == 1
    assert quality["needs_diagnostic"]
    # 卡片原文仍然保留，品牌匹配不会因为标题留空而丢证据。
    assert items[0].raw_data["search_card_text"] == "boss炸雷 04-09 赞"


def test_search_quality_flags_empty_titles() -> None:
    items = [collector_item("one", ""), collector_item("two", "")]

    quality = XiaohongshuCollector.search_quality(items)

    assert quality["all_titles_empty"]
    assert quality["needs_diagnostic"]
    assert quality["degrades_run"]
    assert quality["empty_title_ratio"] == 1.0


def test_search_quality_separates_title_loss_from_total_text_loss() -> None:
    """标题全空但卡片原文尚在时，不该被当成数据质量失败。"""
    with_card_text = replace(
        collector_item("one", ""),
        raw_data={"search_card_text": "boss炸雷 04-09 赞"},
    )
    without_any_text = replace(collector_item("two", ""), raw_data={"search_card_text": ""})

    salvageable = XiaohongshuCollector.search_quality([with_card_text])
    assert salvageable["all_titles_empty"]
    assert not salvageable["no_usable_text"]
    assert salvageable["needs_diagnostic"]
    assert salvageable["degrades_run"]

    hopeless = XiaohongshuCollector.search_quality([without_any_text])
    assert hopeless["no_usable_text"]
    assert hopeless["degrades_run"]


def test_search_quality_diagnoses_normal_untitled_cards_without_degrading_run() -> None:
    """小红书允许封面有字但正文标题为空，少量出现不能把巡检判成 partial。"""
    untitled = [
        replace(
            collector_item(f"untitled-{index}", ""),
            raw_data={"search_card_text": f"作者{index} 08-24 赞"},
        )
        for index in range(7)
    ]
    titled = [collector_item(f"titled-{index}", f"正常标题 {index}") for index in range(13)]

    quality = XiaohongshuCollector.search_quality([*untitled, *titled])

    assert quality["empty_title"] == 7
    assert quality["empty_title_ratio"] == 0.35
    assert quality["needs_diagnostic"]
    assert not quality["no_usable_text"]
    assert not quality["degrades_run"]


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
