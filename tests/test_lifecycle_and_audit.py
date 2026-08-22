import asyncio
from dataclasses import replace

from opinion_watch.collectors.douyin import DouyinCollector
from opinion_watch.collectors.xiaohongshu import XiaohongshuCollector
from opinion_watch.models import CollectedContent, Platform, SessionStatus
from opinion_watch.storage import Storage


def _item(*, title: str = "速探长物流投诉", content_id: str = "note-1") -> CollectedContent:
    return CollectedContent(
        platform=Platform.XIAOHONGSHU,
        content_id=content_id,
        url=f"https://www.xiaohongshu.com/explore/{content_id}",
        title=title,
        source_keyword="速探长",
        brand_name="速探长",
        raw_data={"screening": {"admitted": True}},
    )


def test_xhs_navigation_url_is_transient_and_persisted_url_is_canonical(tmp_path) -> None:
    collector = XiaohongshuCollector()
    item = collector.items_from_anchors(
        [
            collector_anchor(
                "https://www.xiaohongshu.com/explore/66fad51c000000001b0224b8"
                "?xsec_token=do-not-persist"
            )
        ],
        "速探长",
    )[0]
    assert item.navigation_url and "xsec_token" in item.navigation_url
    assert "xsec_token" not in item.url

    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.upsert_contents([item])
    with storage.connect() as connection:
        row = connection.execute("SELECT url, raw_json FROM content_items").fetchone()
    assert "xsec_token" not in str(row["url"])
    assert "do-not-persist" not in str(row["raw_json"])


def test_invalid_xhs_detail_text_falls_back_to_card_metadata() -> None:
    collector = XiaohongshuCollector()
    assert not collector._valid_detail_text("当前笔记无法浏览", "页面不见了")
    assert collector._valid_detail_text("速探长物流体验", "速探长物流体验")
    assert collector._unavailable_reason("页面不见了") == "页面不见了"


def test_xhs_detail_prefers_live_card_click_over_tokenized_href() -> None:
    class PopupExpectation:
        def __init__(self) -> None:
            self.value = asyncio.sleep(0, result=None)

        async def __aenter__(self) -> "PopupExpectation":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Locator:
        first = None

        async def count(self) -> int:
            return 1

        async def click(self, **_kwargs: object) -> None:
            page.url = "https://www.xiaohongshu.com/explore/0123456789abcdef"

    class SearchPage:
        url = "https://www.xiaohongshu.com/search_result?keyword=速探长"

        def locator(self, _selector: str) -> Locator:
            locator = Locator()
            locator.first = locator
            return locator

        def expect_popup(self, **_kwargs: object) -> PopupExpectation:
            return PopupExpectation()

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    page = SearchPage()
    item = _item(content_id="0123456789abcdef")
    item = replace(
        item,
        navigation_url=(
            "https://www.xiaohongshu.com/explore/0123456789abcdef?xsec_token=temporary"
        ),
    )
    target, restore_url, error = asyncio.run(
        XiaohongshuCollector()._open_detail_by_click(page, item)  # type: ignore[arg-type]
    )
    assert target is page
    assert restore_url == "https://www.xiaohongshu.com/search_result?keyword=速探长"
    assert error == ""


def test_xhs_detail_click_uses_visible_card_link() -> None:
    class PopupExpectation:
        def __init__(self) -> None:
            self.value = asyncio.sleep(0, result=None)

        async def __aenter__(self) -> "PopupExpectation":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Locator:
        first = None

        async def count(self) -> int:
            return 1

        async def click(self, **_kwargs: object) -> None:
            page.url = "https://www.xiaohongshu.com/search_result/0123456789abcdef"

    class SearchPage:
        url = "https://www.xiaohongshu.com/search_result?keyword=配达人"
        selector = ""

        def locator(self, selector: str) -> Locator:
            self.selector = selector
            locator = Locator()
            locator.first = locator
            return locator

        def expect_popup(self, **_kwargs: object) -> PopupExpectation:
            return PopupExpectation()

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    page = SearchPage()
    item = _item(content_id="0123456789abcdef")
    target, restore_url, error = asyncio.run(
        XiaohongshuCollector()._open_detail_by_click(page, item)  # type: ignore[arg-type]
    )
    assert target is page
    assert restore_url == "https://www.xiaohongshu.com/search_result?keyword=配达人"
    assert error == ""
    assert ":visible" in page.selector


def test_douyin_login_confirmation_overrides_cookie_health() -> None:
    class Locator:
        first = None

        async def inner_text(self, timeout: int) -> str:
            return "登录后即可搜索更多精彩视频，请在抖音APP确认登录"

        async def count(self) -> int:
            return 0

        async def is_visible(self) -> bool:
            return False

    class Page:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        def locator(self, selector: str) -> Locator:
            return Locator()

    class Context:
        async def cookies(self) -> list[dict[str, str]]:
            return [{"name": "sessionid"}]

    status = asyncio.run(DouyinCollector().session_status(Page(), Context()))  # type: ignore[arg-type]
    assert status is SessionStatus.VERIFICATION_REQUIRED


def test_douyin_detail_rejects_unrelated_recommendation_popup() -> None:
    expected_id = "7652677355172941066"
    unrelated_id = "7676355993751637283"

    class Popup:
        def __init__(self) -> None:
            self.url = f"https://www.douyin.com/jingxuan?modal_id={unrelated_id}"
            self.closed = False

        async def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    popup = Popup()

    class PopupExpectation:
        value = asyncio.sleep(0, result=popup)

        async def __aenter__(self) -> "PopupExpectation":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class CardLocator:
        first = None

        async def count(self) -> int:
            return 1

        async def click(self, **_kwargs: object) -> None:
            return None

    class SearchPage:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        def locator(self, _selector: str) -> CardLocator:
            locator = CardLocator()
            locator.first = locator
            return locator

        def expect_popup(self, **_kwargs: object) -> PopupExpectation:
            return PopupExpectation()

    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id=expected_id,
        url=f"https://www.douyin.com/video/{expected_id}",
        title="速探长空运小包",
        source_keyword="速探长",
    )

    target, restore_url, error = asyncio.run(
        DouyinCollector()._open_detail_by_click(SearchPage(), item)  # type: ignore[arg-type]
    )

    assert target is None
    assert restore_url is None
    assert popup.closed is True
    assert expected_id in error
    assert unrelated_id in error
    assert "错配" in error


def test_douyin_detail_accepts_matching_modal_popup() -> None:
    content_id = "7652677355172941066"

    class Popup:
        def __init__(self) -> None:
            self.url = f"https://www.douyin.com/jingxuan?modal_id={content_id}"
            self.closed = False

        async def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    popup = Popup()

    class PopupExpectation:
        value = asyncio.sleep(0, result=popup)

        async def __aenter__(self) -> "PopupExpectation":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class CardLocator:
        first = None

        async def count(self) -> int:
            return 1

        async def click(self, **_kwargs: object) -> None:
            return None

    class SearchPage:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        def locator(self, _selector: str) -> CardLocator:
            locator = CardLocator()
            locator.first = locator
            return locator

        def expect_popup(self, **_kwargs: object) -> PopupExpectation:
            return PopupExpectation()

    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id=content_id,
        url=f"https://www.douyin.com/video/{content_id}",
        title="速探长空运小包",
        source_keyword="速探长",
    )

    target, restore_url, error = asyncio.run(
        DouyinCollector()._open_detail_by_click(SearchPage(), item)  # type: ignore[arg-type]
    )

    assert target is popup
    assert restore_url is None
    assert popup.closed is False
    assert error == ""


def test_douyin_detail_prefers_direct_navigation_without_clicking_search_card() -> None:
    class SearchPage:
        url = "https://www.douyin.com/jingxuan/search/速探长"

        def locator(self, _selector: str) -> object:
            raise AssertionError("抖音详情不应点击搜索卡片")

    class BodyLocator:
        async def inner_text(self, timeout: int) -> str:
            return "速探长物流投诉详情"

    class DetailPage:
        url = "https://www.douyin.com/video/1234567890123456789"

        async def goto(self, _url: str, **_kwargs: object) -> None:
            return None

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        async def title(self) -> str:
            return "速探长物流投诉"

        def locator(self, selector: str) -> BodyLocator:
            assert selector == "body"
            return BodyLocator()

    class Context:
        def __init__(self) -> None:
            self.page = DetailPage()
            self.new_page_calls = 0

        async def new_page(self) -> DetailPage:
            self.new_page_calls += 1
            return self.page

    class TestableDouyinCollector(DouyinCollector):
        async def session_status(self, _page: object, _context: object) -> SessionStatus:
            return SessionStatus.HEALTHY

        @staticmethod
        async def _first_text(_page: object, selectors: tuple[str, ...]) -> str:
            return "速探长物流投诉" if selectors else ""

        @staticmethod
        async def _all_text(_page: object, _selectors: tuple[str, ...], *, limit: int) -> list[str]:
            return [] if limit >= 0 else []

        async def _extract_media_evidence(
            self, _page: object, *, artifact_dir: object, content_id: str
        ) -> list[dict[str, object]]:
            return []

    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="1234567890123456789",
        url="https://www.douyin.com/video/1234567890123456789",
        title="速探长物流投诉",
        source_keyword="速探长",
        brand_name="速探长",
        navigation_url="https://www.douyin.com/video/1234567890123456789",
        raw_data={"screening": {"admitted": True}},
    )
    context = Context()
    enriched = asyncio.run(
        TestableDouyinCollector().enrich_items(
            context,
            [item],
            detail_limit=1,
            comments_limit=1,
            detail_candidate_ids={item.content_id},
            search_page=SearchPage(),  # type: ignore[arg-type]
        )
    )

    assert context.new_page_calls == 1
    assert enriched[0].raw_data["detail_status"] == "succeeded"
    assert enriched[0].raw_data["detail_collected"] is True


def test_douyin_rejects_detail_page_that_redirects_after_initial_match() -> None:
    expected_id = "1234567890123456789"
    unrelated_id = "7674851605736459546"

    class DetailPage:
        url = f"https://www.douyin.com/video/{expected_id}"

        def __init__(self) -> None:
            self.closed = False
            self.guard_installed = False
            self.wait_calls = 0

        async def add_init_script(self, _script: str) -> None:
            self.guard_installed = True

        async def goto(self, _url: str, **_kwargs: object) -> None:
            return None

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            self.wait_calls += 1
            if self.wait_calls == 2:
                self.url = f"https://www.douyin.com/jingxuan?modal_id={unrelated_id}"

        async def close(self) -> None:
            self.closed = True

    class Context:
        def __init__(self) -> None:
            self.page = DetailPage()

        async def new_page(self) -> DetailPage:
            return self.page

    class TestableDouyinCollector(DouyinCollector):
        detail_identity_stability_checks = 4

        async def session_status(self, _page: object, _context: object) -> SessionStatus:
            raise AssertionError("错配详情页不应进入会话检查和数据抽取")

    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id=expected_id,
        url=f"https://www.douyin.com/video/{expected_id}",
        title="速探长物流投诉",
        source_keyword="速探长",
        brand_name="速探长",
        navigation_url=f"https://www.douyin.com/video/{expected_id}",
        raw_data={"screening": {"admitted": True}},
    )
    context = Context()
    enriched = asyncio.run(
        TestableDouyinCollector().enrich_items(
            context,  # type: ignore[arg-type]
            [item],
            detail_limit=1,
            comments_limit=1,
            detail_candidate_ids={item.content_id},
        )
    )

    assert context.page.guard_installed is True
    assert context.page.closed is True
    assert enriched[0].raw_data["detail_status"] == "failed"
    assert unrelated_id in enriched[0].raw_data["detail_error"]
    assert enriched[0].raw_data.get("detail_collected") is not True


def test_soft_delete_rediscovery_is_new_opinion_and_recreates_notification(tmp_path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    item = _item()
    first = storage.upsert_contents([item])
    assert first.content_inserted == 1 and first.new_opinion == 1
    with storage.connect() as connection:
        content_id = int(connection.execute("SELECT id FROM content_items").fetchone()[0])
    storage.upsert_assessment(
        content_item_id=content_id,
        category="other",
        severity="P2",
        confidence=0.8,
        rationale="待复核",
        matched_signals=["投诉"],
        requires_review=True,
    )
    assert storage.soft_delete_opinions([content_id]) == 1
    assert storage.list_assessments() == []

    rediscovered = storage.upsert_contents([item])
    assert rediscovered.updated == 1
    assert rediscovered.new_opinion == 1
    assert rediscovered.rediscovered == 1
    assert len(storage.list_assessments()) == 1
    with storage.connect() as connection:
        row = connection.execute(
            "SELECT deleted_at, rediscovered_count FROM content_items WHERE id = ?",
            (content_id,),
        ).fetchone()
    assert row["deleted_at"] is None
    assert row["rediscovered_count"] == 1


def test_permanent_ignore_blocks_re_admission(tmp_path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    item = _item(content_id="ignored-note")
    storage.upsert_contents([item])
    with storage.connect() as connection:
        content_id = int(connection.execute("SELECT id FROM content_items").fetchone()[0])
    assert storage.set_permanent_ignore([content_id]) == 1
    result = storage.upsert_contents([item])
    assert result.ignored == 1
    assert storage.list_assessments() == []


def test_scan_attempt_audit_stats_are_stored(tmp_path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["xiaohongshu"], brands=["速探长"], options={}
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id, platform="xiaohongshu", keyword="速探长", attempt_no=1
    )
    storage.finish_scan_attempt(
        attempt_id,
        status="succeeded",
        scanned=20,
        brand_matched=4,
        detail_attempted=3,
        detailed=2,
        detail_unavailable=1,
        content_inserted=1,
        new_opinion=1,
        rediscovered=1,
    )
    row = storage.get_scan_run(run_id)["attempts"][0]
    assert row["scanned_count"] == 20
    assert row["brand_matched_count"] == 4
    assert row["detail_attempted_count"] == 3
    assert row["detailed_count"] == 2
    assert row["detail_unavailable_count"] == 1
    assert row["new_opinion_count"] == 1


def test_v4_migration_repairs_existing_unavailable_detail_title(tmp_path) -> None:
    database = tmp_path / "test.db"
    storage = Storage(database)
    storage.initialize()
    item = _item(title="速探长海外仓提醒")
    storage.upsert_contents([item])
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["xiaohongshu"], brands=["速探长"], options={}
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id, platform="xiaohongshu", keyword="速探长", attempt_no=1
    )
    storage.save_scan_candidates(run_id=run_id, attempt_id=attempt_id, items=[item])
    with storage.connect() as connection:
        connection.execute(
            "UPDATE content_items SET title = ?",
            ("当前笔记暂时无法浏览",),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version >= 4")

    Storage(database).initialize()
    with storage.connect() as connection:
        title = connection.execute("SELECT title FROM content_items").fetchone()[0]
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert title == "速探长海外仓提醒"
    assert version == 5


def collector_anchor(url: str):
    from opinion_watch.models import AnchorCandidate

    return AnchorCandidate(url, "卡片标题", author_name="卡片作者")
