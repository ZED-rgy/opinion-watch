import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import OfflineCollector

from opinion_watch.collectors.base import CollectorRuntimeError
from opinion_watch.config import Settings
from opinion_watch.models import CollectedContent, Platform, SessionStatus
from opinion_watch.runner import (
    ScanOptions,
    _recheck_detail_items,
    _screen_items_for_admission,
    _screen_items_for_detail,
    _select_detail_candidates,
    run_scan,
)
from opinion_watch.storage import Storage


def make_item(
    content_id: str,
    title: str,
    *,
    brand: str = "配达人",
    card_text: str | None = None,
    raw_data: dict[str, object] | None = None,
) -> CollectedContent:
    data = dict(raw_data or {})
    data.setdefault("search_card_text", card_text if card_text is not None else title)
    return CollectedContent(
        platform=Platform.XIAOHONGSHU,
        content_id=content_id,
        url=f"https://www.xiaohongshu.com/explore/{content_id}",
        title=title,
        source_keyword=brand,
        brand_name=brand,
        raw_data=data,
    )


def test_brand_warning_expression_enters_detail() -> None:
    item = make_item("warning", "后续，配达人海外仓，大家要擦亮眼睛！")

    screened, candidates = _screen_items_for_detail([item])

    assert candidates == {"warning"}
    assert screened[0].raw_data["precheck"]["requires_review"] is True
    assert "擦亮眼睛" in screened[0].raw_data["precheck"]["matched_signals"]


def test_generic_negative_without_brand_does_not_enter_detail() -> None:
    item = make_item("noise", "达人说骗子跑路了，大家避雷", card_text="达人说骗子跑路了，大家避雷")

    screened, candidates = _screen_items_for_detail([item])

    assert candidates == set()
    assert screened[0].raw_data["precheck"]["brand_matched"] is False


def test_card_screening_keeps_suspect_and_two_baseline_cards(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    items = [
        make_item("suspect", "配达人投诉后不退款"),
        make_item("normal-1", "配达人服务介绍"),
        make_item("normal-2", "配达人仓配日常"),
        make_item("normal-3", "配达人行业分享"),
    ]

    admitted, detail_candidates, stats = asyncio.run(
        _screen_items_for_admission(storage, items, brand="配达人", baseline_limit=2)
    )

    assert [item.content_id for item in admitted] == ["suspect", "normal-1", "normal-2"]
    assert detail_candidates == {"suspect", "normal-1", "normal-2"}
    assert [item.raw_data["screening"]["decision"] for item in admitted] == [
        "规则疑似",
        "保底抽查",
        "保底抽查",
    ]
    assert stats["filter_reasons"]["normal-3"] == "品牌相关但未命中风险"


def test_detail_limit_caps_selection_and_prioritizes_suspects(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    items = [
        make_item("suspect-1", "配达人投诉不退款"),
        make_item("suspect-2", "配达人跑路了"),
        make_item("normal-1", "配达人服务介绍"),
        make_item("normal-2", "配达人仓配日常"),
    ]
    admitted, candidates, _ = asyncio.run(
        _screen_items_for_admission(storage, items, brand="配达人", baseline_limit=2)
    )

    selected = _select_detail_candidates(admitted, candidates, detail_limit=3)

    assert selected == {"suspect-1", "suspect-2", "normal-1"}
    assert len(selected) == 3


def test_clean_baseline_detail_is_not_admitted() -> None:
    item = make_item(
        "clean",
        "配达人服务介绍",
        raw_data={
            "detail_collected": True,
            "description": "今天分享仓配行业常识。",
            "comments": ["学习了"],
            "screening": {"decision": "保底抽查", "admitted": True},
        },
    )

    admitted, reasons, suspected_count = _recheck_detail_items([item], brand="配达人")

    assert admitted == []
    assert reasons == {"clean": "保底抽查后无风险"}
    assert suspected_count == 0


def test_model_disabled_still_runs_baseline_sample(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    item = make_item("baseline", "配达人服务介绍")

    admitted, _, _ = asyncio.run(
        _screen_items_for_admission(storage, [item], brand="配达人", baseline_limit=2)
    )

    assert [item.content_id for item in admitted] == ["baseline"]


def test_model_failure_does_not_cancel_baseline_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()

    async def fail_model(*args: object, **kwargs: object) -> object:
        raise TimeoutError("模型超时")

    monkeypatch.setattr("opinion_watch.runner.screen_items_with_llm", fail_model)
    admitted, _, stats = asyncio.run(
        _screen_items_for_admission(
            storage, [make_item("baseline", "配达人服务介绍")], brand="配达人", baseline_limit=2
        )
    )

    assert [item.content_id for item in admitted] == ["baseline"]
    assert stats["model_errors"] == ["模型超时"]


class ZeroDetailPage:
    url = "https://example.test/search"


class ZeroDetailBrowserSession:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.active_context = object()

    async def __aenter__(self) -> "ZeroDetailBrowserSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def page(self) -> ZeroDetailPage:
        return ZeroDetailPage()

    async def capture_diagnostic(self, page: ZeroDetailPage, label: str) -> None:
        return None


class ZeroDetailCollector(OfflineCollector):
    async def session_status(self, page: object, context: object) -> SessionStatus:
        return SessionStatus.HEALTHY

    async def search(
        self,
        page: object,
        context: object,
        keyword: str,
        *,
        limit: int = 20,
        artifact_dir: Path | None = None,
        diagnostic_key: str | None = None,
    ) -> list[CollectedContent]:
        return [
            CollectedContent(
                platform=Platform.XIAOHONGSHU,
                content_id="card-only",
                url="https://www.xiaohongshu.com/explore/card-only",
                title="配达人服务介绍",
                source_keyword=keyword,
                raw_data={"search_card_text": "配达人服务介绍"},
            )
        ]

    async def enrich_items(
        self,
        context: object,
        items: list[CollectedContent],
        *,
        detail_limit: int = 5,
        comments_limit: int = 20,
        detail_candidate_ids: set[str] | None = None,
        artifact_dir: Path | None = None,
        search_page: object = None,
    ) -> list[CollectedContent]:
        return items


class CleanDetailCollector(ZeroDetailCollector):
    async def enrich_items(
        self,
        context: object,
        items: list[CollectedContent],
        *,
        detail_limit: int = 5,
        comments_limit: int = 20,
        detail_candidate_ids: set[str] | None = None,
        artifact_dir: Path | None = None,
        search_page: object = None,
    ) -> list[CollectedContent]:
        return [
            replace(
                item,
                raw_data={
                    **item.raw_data,
                    "detail_collected": True,
                    "description": "今天分享仓配行业常识。",
                    "comments": ["学习了"],
                },
            )
            for item in items
        ]


class PartialSuspectCollector(ZeroDetailCollector):
    async def search(
        self,
        page: object,
        context: object,
        keyword: str,
        *,
        limit: int = 20,
        artifact_dir: Path | None = None,
        diagnostic_key: str | None = None,
    ) -> list[CollectedContent]:
        items = await super().search(page, context, keyword, limit=limit)
        return [
            replace(
                items[0],
                title="配达人投诉后不退款",
                raw_data={"search_card_text": "配达人投诉后不退款"},
            )
        ]


class ConcurrentPage:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.url = f"https://example.test/search/{marker}"
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class ConcurrentContext:
    def __init__(self) -> None:
        self.next_marker = 0

    async def new_page(self) -> ConcurrentPage:
        self.next_marker += 1
        return ConcurrentPage(f"prefetch-{self.next_marker}")


class ConcurrentBrowserSession:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.active_context = ConcurrentContext()
        self.main_page = ConcurrentPage("main")

    async def __aenter__(self) -> "ConcurrentBrowserSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def page(self) -> ConcurrentPage:
        return self.main_page

    async def capture_diagnostic(self, page: ConcurrentPage, label: str) -> Path | None:
        return None


class ConcurrentCollector(ZeroDetailCollector):
    def __init__(self) -> None:
        super().__init__()
        self.detail_bindings: list[tuple[str, str]] = []

    async def search(
        self,
        page: object,
        context: object,
        keyword: str,
        *,
        limit: int = 20,
        artifact_dir: Path | None = None,
        diagnostic_key: str | None = None,
    ) -> list[CollectedContent]:
        await asyncio.sleep(0)
        return [
            make_item(
                f"card-{keyword}",
                f"配达人投诉 {keyword}",
                raw_data={"search_card_text": f"配达人投诉 {keyword}", "page": page.marker},
            )
        ]

    async def enrich_items(
        self,
        context: object,
        items: list[CollectedContent],
        *,
        detail_limit: int = 5,
        comments_limit: int = 20,
        detail_candidate_ids: set[str] | None = None,
        artifact_dir: Path | None = None,
        search_page: object = None,
    ) -> list[CollectedContent]:
        assert search_page is not None
        for item in items:
            self.detail_bindings.append((str(item.raw_data["page"]), search_page.marker))
        return [
            replace(item, raw_data={**item.raw_data, "detail_collected": True}) for item in items
        ]


class PrefetchGatewayBrowserSession(ConcurrentBrowserSession):
    diagnostic_path: Path | None = None

    async def capture_diagnostic(self, page: ConcurrentPage, label: str) -> Path | None:
        return self.diagnostic_path


class PrefetchGatewayCollector(ConcurrentCollector):
    def __init__(self, *, always_fail: bool) -> None:
        super().__init__()
        self.always_fail = always_fail
        self.search_calls: dict[str, int] = {}

    async def search(
        self,
        page: object,
        context: object,
        keyword: str,
        *,
        limit: int = 20,
        artifact_dir: Path | None = None,
        diagnostic_key: str | None = None,
    ) -> list[CollectedContent]:
        calls = self.search_calls.get(keyword, 0) + 1
        self.search_calls[keyword] = calls
        if keyword == "502关键词" and (self.always_fail or calls == 1):
            raise CollectorRuntimeError(SessionStatus.ERROR, "HTTP 502 Bad Gateway")
        return await super().search(page, context, keyword, limit=limit)


def _prepare_concurrent_gateway_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    always_fail: bool,
) -> tuple[Storage, PrefetchGatewayCollector, Path]:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    storage.add_brand("配达人")
    default_keyword_id = storage.list_keywords(brand_name="配达人")[0]["id"]
    storage.set_keyword_enabled(int(default_keyword_id), False)
    storage.add_keyword("配达人", "正常关键词")
    storage.add_keyword("配达人", "502关键词")
    account_id = storage.add_account(Platform.XIAOHONGSHU.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    diagnostic_path = tmp_path / "artifacts" / "gateway.png"
    PrefetchGatewayBrowserSession.diagnostic_path = diagnostic_path
    collector = PrefetchGatewayCollector(always_fail=always_fail)
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", PrefetchGatewayBrowserSession)
    monkeypatch.setattr("opinion_watch.runner.collector_for", lambda platform: collector)
    return storage, collector, diagnostic_path


def test_concurrent_prefetch_502_retry_success_persists_diagnostic_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, collector, diagnostic_path = _prepare_concurrent_gateway_scan(
        tmp_path, monkeypatch, always_fail=False
    )
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "scan.db",
        artifact_dir=tmp_path / "artifacts",
    )

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.XIAOHONGSHU],
            options=ScanOptions(
                limit=20,
                detail_limit=0,
                comments_limit=0,
                retries=1,
                retry_delay_seconds=0,
                brand_delay_seconds=0,
                concurrency=2,
            ),
        )
    )

    run = storage.get_scan_run(1)
    assert exit_code == 0
    assert run is not None
    assert run["status"] == "succeeded"
    assert collector.search_calls["502关键词"] == 2
    failed_attempt = next(item for item in run["attempts"] if item["status"] == "failed")
    assert failed_attempt["screenshot_path"] == str(diagnostic_path)
    assert all(
        isinstance(item["screenshot_path"], str)
        for item in run["attempts"]
        if item["screenshot_path"]
    )


def test_concurrent_prefetch_502_retry_failure_persists_attempt_and_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, collector, diagnostic_path = _prepare_concurrent_gateway_scan(
        tmp_path, monkeypatch, always_fail=True
    )
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "scan.db",
        artifact_dir=tmp_path / "artifacts",
    )

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.XIAOHONGSHU],
            options=ScanOptions(
                limit=20,
                detail_limit=0,
                comments_limit=0,
                retries=1,
                retry_delay_seconds=0,
                brand_delay_seconds=0,
                concurrency=2,
            ),
        )
    )

    run = storage.get_scan_run(1)
    assert exit_code == 2
    assert run is not None
    assert run["status"] == "partial"
    assert collector.search_calls["502关键词"] == 2
    failed_attempts = [item for item in run["attempts"] if item["status"] == "failed"]
    assert len(failed_attempts) == 2
    assert all(item["screenshot_path"] == str(diagnostic_path) for item in failed_attempts)
    alerts = storage.list_alerts(run_id=1)
    assert any(alert["kind"] == "error" and "HTTP 502" in alert["message"] for alert in alerts)


def test_concurrency_two_keeps_each_search_page_for_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    storage.add_brand("配达人")
    default_keyword_id = storage.list_keywords(brand_name="配达人")[0]["id"]
    storage.set_keyword_enabled(int(default_keyword_id), False)
    storage.add_keyword("配达人", "速探长")
    storage.add_keyword("配达人", "优速卖")
    account_id = storage.add_account(Platform.XIAOHONGSHU.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "scan.db",
        artifact_dir=tmp_path / "artifacts",
    )
    collector = ConcurrentCollector()
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", ConcurrentBrowserSession)
    monkeypatch.setattr("opinion_watch.runner.collector_for", lambda platform: collector)

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.XIAOHONGSHU],
            options=ScanOptions(
                limit=20,
                detail_limit=2,
                comments_limit=0,
                retries=0,
                brand_delay_seconds=0,
                concurrency=2,
            ),
        )
    )

    run = storage.get_scan_run(1)
    assert exit_code == 0
    assert run is not None
    assert run["status"] == "succeeded"
    assert run["detailed_count"] == 2
    assert all(expected == actual for expected, actual in collector.detail_bindings)
    assert all(actual != "main" for _, actual in collector.detail_bindings)


def test_partial_run_aggregates_repeated_runtime_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    storage.add_brand("配达人")
    default_keyword_id = storage.list_keywords(brand_name="配达人")[0]["id"]
    storage.set_keyword_enabled(int(default_keyword_id), False)
    for keyword in ("速探长", "优速卖", "配达人"):
        storage.add_keyword("配达人", keyword)
    account_id = storage.add_account(Platform.XIAOHONGSHU.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "scan.db",
        artifact_dir=tmp_path / "artifacts",
    )
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", ZeroDetailBrowserSession)
    monkeypatch.setattr(
        "opinion_watch.runner.collector_for", lambda platform: ZeroDetailCollector()
    )

    asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.XIAOHONGSHU],
            options=ScanOptions(
                limit=20,
                detail_limit=2,
                comments_limit=0,
                retries=0,
                brand_delay_seconds=0,
            ),
        )
    )

    warnings = [alert for alert in storage.list_alerts() if alert["kind"] == "zero_detail_coverage"]
    assert len(warnings) == 1
    assert "本轮巡检共 3 项" in warnings[0]["message"]
    assert "详情覆盖不足" in warnings[0]["message"]
    assert "zero_detail_coverage" not in warnings[0]["message"]


def test_brand_match_with_zero_details_is_partial_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    storage.add_brand("配达人")
    storage.add_keyword("配达人", "配达人")
    account_id = storage.add_account(Platform.XIAOHONGSHU.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "scan.db",
        artifact_dir=tmp_path / "artifacts",
    )
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", ZeroDetailBrowserSession)
    monkeypatch.setattr(
        "opinion_watch.runner.collector_for", lambda platform: ZeroDetailCollector()
    )

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.XIAOHONGSHU],
            options=ScanOptions(
                limit=20,
                detail_limit=2,
                comments_limit=0,
                retries=0,
                brand_delay_seconds=0,
            ),
        )
    )

    run = storage.get_scan_run(1)
    assert exit_code == 2
    assert run is not None
    assert run["attempts"][0]["status"] == "partial"
    assert any(alert["kind"] == "zero_detail_coverage" for alert in storage.list_alerts())
    assert run["attempts"][0]["scanned_count"] == 1
    assert run["attempts"][0]["detailed_count"] == 0


def test_partial_run_keeps_shallow_risk_as_candidate_without_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    storage.add_brand("配达人")
    storage.add_keyword("配达人", "配达人")
    account_id = storage.add_account(Platform.XIAOHONGSHU.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "scan.db",
        artifact_dir=tmp_path / "artifacts",
    )
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", ZeroDetailBrowserSession)
    monkeypatch.setattr(
        "opinion_watch.runner.collector_for", lambda platform: PartialSuspectCollector()
    )

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.XIAOHONGSHU],
            options=ScanOptions(
                limit=20,
                detail_limit=2,
                comments_limit=0,
                retries=0,
                brand_delay_seconds=0,
            ),
        )
    )

    run = storage.get_scan_run(1)
    assert exit_code == 2
    assert run is not None
    assert run["status"] == "partial"
    assert run["partial_count"] == 1
    assert run["failed_count"] == 0
    assert run["classification"]["processed"] == 1
    assessments = storage.list_assessments()
    assert len(assessments) == 1
    assert assessments[0]["requires_review"] is True
    assert assessments[0]["severity"] == "P3"
    notifications = storage.list_notifications(unread_only=True)
    assert not any(item["kind"] == "opinion_review" for item in notifications)


def test_clean_detail_success_counts_before_filtered_from_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    storage.add_brand("配达人")
    storage.add_keyword("配达人", "配达人")
    account_id = storage.add_account(Platform.XIAOHONGSHU.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "scan.db",
        artifact_dir=tmp_path / "artifacts",
    )
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", ZeroDetailBrowserSession)
    monkeypatch.setattr(
        "opinion_watch.runner.collector_for", lambda platform: CleanDetailCollector()
    )

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.XIAOHONGSHU],
            options=ScanOptions(
                limit=20,
                detail_limit=2,
                comments_limit=0,
                retries=0,
                brand_delay_seconds=0,
            ),
        )
    )

    run = storage.get_scan_run(1)
    alerts = storage.list_alerts()
    assert exit_code == 0
    assert run is not None
    assert run["status"] == "succeeded"
    assert run["attempts"][0]["status"] == "succeeded"
    assert run["attempts"][0]["detailed_count"] == 1
    assert not any(alert["kind"] == "zero_detail_coverage" for alert in alerts)
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0] == 0


def test_filter_reasons_are_specific_and_not_placeholder(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "scan.db")
    storage.initialize()
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["xiaohongshu"], brands=["配达人"], options={}
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id, platform="xiaohongshu", keyword="配达人", attempt_no=1
    )
    items = [
        make_item("brand-no-risk", "配达人服务介绍"),
        make_item("unrelated", "骗子避雷"),
    ]
    storage.save_scan_candidates(run_id=run_id, attempt_id=attempt_id, items=items)

    storage.mark_scan_candidates(
        attempt_id=attempt_id,
        admitted_content_ids=[],
        filter_reasons={
            "brand-no-risk": "保底抽查后无风险",
            "unrelated": "未出现目标品牌",
        },
    )

    with storage.connect() as connection:
        rows = connection.execute(
            "SELECT platform_content_id, filter_reason FROM scan_candidates "
            "WHERE attempt_id = ? ORDER BY platform_content_id",
            (attempt_id,),
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("brand-no-risk", "保底抽查后无风险"),
        ("unrelated", "未出现目标品牌"),
    ]
    assert all(row[1] != "未达到入库条件" for row in rows)
