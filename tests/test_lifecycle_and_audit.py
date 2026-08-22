import asyncio

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
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")

    Storage(database).initialize()
    with storage.connect() as connection:
        title = connection.execute("SELECT title FROM content_items").fetchone()[0]
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert title == "速探长海外仓提醒"
    assert version == 4


def collector_anchor(url: str):
    from opinion_watch.models import AnchorCandidate

    return AnchorCandidate(url, "卡片标题", author_name="卡片作者")
