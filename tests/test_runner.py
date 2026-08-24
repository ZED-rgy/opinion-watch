import asyncio
from pathlib import Path

import pytest
from fakes import OfflineCollector
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from opinion_watch.browser import BrowserProfileLocked
from opinion_watch.collectors.base import CollectorRuntimeError
from opinion_watch.config import Settings
from opinion_watch.llm import LLMAssessment
from opinion_watch.models import (
    CollectedContent,
    OpinionCategory,
    Platform,
    RiskSeverity,
    SessionStatus,
)
from opinion_watch.runner import (
    ScanOptions,
    _baseline_detail_limit,
    _heartbeat_lease,
    _prepare_account_status_page,
    _screen_items_for_admission,
    _screen_items_for_detail,
    run_scan,
)
from opinion_watch.storage import Storage


class FakePage:
    url = "https://example.test/search"


def test_lost_lease_cancels_the_owning_scan_task(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()

    async def scenario() -> str:
        owner_task = asyncio.current_task()
        assert owner_task is not None
        heartbeat = asyncio.create_task(
            _heartbeat_lease(
                storage,
                "scan",
                "missing-owner",
                cancel_task=owner_task,
                heartbeat_seconds=0.001,
            )
        )
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError as exc:
            reason = str(exc)
        else:
            raise AssertionError("lease loss did not cancel the owner task")
        await heartbeat
        return reason

    assert asyncio.run(scenario()) == "lease_lost:scan"
    assert storage.list_alerts(limit=1)[0]["kind"] == "lease_lost"


def test_transient_heartbeat_error_retries_before_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    calls = 0

    def flaky_heartbeat(*_args, **_kwargs) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is temporarily busy")
        return False

    monkeypatch.setattr(storage, "heartbeat_task_lease", flaky_heartbeat)

    async def scenario() -> str:
        owner_task = asyncio.current_task()
        assert owner_task is not None
        heartbeat = asyncio.create_task(
            _heartbeat_lease(
                storage,
                "scan",
                "owner",
                cancel_task=owner_task,
                heartbeat_seconds=0.001,
            )
        )
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError as exc:
            reason = str(exc)
        else:
            raise AssertionError("lease loss did not cancel the owner task")
        await heartbeat
        return reason

    assert asyncio.run(scenario()) == "lease_lost:scan"
    assert calls == 2
    assert {item["kind"] for item in storage.list_alerts(limit=5)} == {
        "lease_heartbeat_error",
        "lease_lost",
    }


def test_baseline_detail_limit_scales_but_remains_bounded() -> None:
    assert _baseline_detail_limit(0) == 0
    assert _baseline_detail_limit(1) == 2
    assert _baseline_detail_limit(20) == 5
    assert _baseline_detail_limit(50) == 10
    assert _baseline_detail_limit(100) == 10


def test_baseline_sampling_prefers_never_checked_cards(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["速探长"], options={}
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id, platform="douyin", keyword="速探长", attempt_no=1
    )
    checked = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="checked",
        url="https://www.douyin.com/video/checked",
        title="速探长日常介绍一",
        source_keyword="速探长",
        brand_name="速探长",
    )
    unseen = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="unseen",
        url="https://www.douyin.com/video/unseen",
        title="速探长日常介绍二",
        source_keyword="速探长",
        brand_name="速探长",
    )
    storage.save_scan_candidates(run_id=run_id, attempt_id=attempt_id, items=[checked])
    storage.mark_scan_candidates(
        attempt_id=attempt_id,
        admitted_content_ids=[],
        detail_audits={
            "checked": {
                "detail_status": "succeeded",
                "detail_checked_at": "2026-08-20T00:00:00+00:00",
            }
        },
    )

    admitted, candidates, _stats = asyncio.run(
        _screen_items_for_admission(
            storage,
            [checked, unseen],
            brand="速探长",
            baseline_limit=1,
        )
    )

    assert [item.content_id for item in admitted] == ["unseen"]
    assert candidates == {"unseen"}


def test_account_preflight_opens_home_from_blank_page() -> None:
    class BlankPage:
        url = "about:blank"

        async def goto(self, url: str, **_kwargs: object) -> None:
            self.url = url

    class Collector:
        home_url = "https://www.douyin.com/"

    page = BlankPage()
    asyncio.run(_prepare_account_status_page(page, Collector()))

    assert page.url == Collector.home_url


def test_account_preflight_tolerates_timeout_once_the_page_has_navigated() -> None:
    """首页挂着推荐流，domcontentloaded 超时是常态。

    只要浏览器已经离开 about:blank，页面就足以判定登录态；把异常放出去会被外层
    当成 browser_error，整个平台的关键词一个都不检索。
    """

    class SlowPage:
        url = "about:blank"

        async def goto(self, url: str, **_kwargs: object) -> None:
            self.url = url
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded.")

    class Collector:
        home_url = "https://www.douyin.com/"

    page = SlowPage()
    asyncio.run(_prepare_account_status_page(page, Collector()))

    assert page.url == Collector.home_url


def test_account_preflight_still_raises_when_page_never_left_blank() -> None:
    # 停在空白页时不能咽下异常：拿空白页去判登录态会把账号误标成已退出。
    class DeadPage:
        url = "about:blank"

        async def goto(self, _url: str, **_kwargs: object) -> None:
            raise PlaywrightTimeoutError("Timeout 30000ms exceeded.")

    class Collector:
        home_url = "https://www.douyin.com/"

    with pytest.raises(PlaywrightTimeoutError):
        asyncio.run(_prepare_account_status_page(DeadPage(), Collector()))


class FakeBrowserSession:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.active_context = object()

    async def __aenter__(self) -> "FakeBrowserSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def page(self) -> FakePage:
        return FakePage()

    async def capture_diagnostic(self, page: FakePage, label: str) -> None:
        return None


class LockedBrowserSession(FakeBrowserSession):
    async def __aenter__(self) -> "LockedBrowserSession":
        raise BrowserProfileLocked("测试档案仍被 Chrome 使用")


class RetryThenSucceedCollector(OfflineCollector):
    calls = 0

    def __init__(self) -> None:
        super().__init__()
        self.keywords: list[str] = []

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
        self.calls += 1
        self.keywords.append(keyword)
        if self.calls == 1:
            raise CollectorRuntimeError(SessionStatus.ERROR, "临时页面错误")
        return [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="123",
                url="https://www.douyin.com/video/123",
                title="速探长投诉不退款",
                source_keyword=keyword,
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


class VerificationCollector(RetryThenSucceedCollector):
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
        raise CollectorRuntimeError(SessionStatus.VERIFICATION_REQUIRED, "搜索页需要确认")


def test_list_level_screening_marks_only_suspected_content_for_details() -> None:
    items, candidates = _screen_items_for_detail(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="ordinary",
                url="https://www.douyin.com/video/ordinary",
                title="速探长日常介绍",
                source_keyword="速探长",
            ),
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="complaint",
                url="https://www.douyin.com/video/complaint",
                title="速探长投诉后不退款",
                source_keyword="速探长",
            ),
        ]
    )

    assert candidates == {"complaint"}
    assert items[0].raw_data["precheck"]["suspected"] is False
    assert items[1].raw_data["precheck"]["suspected"] is True


def test_model_screening_skips_clean_content_before_storage(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="ordinary-model-result",
        url="https://www.douyin.com/video/ordinary-model-result",
        title="品牌日常介绍",
        source_keyword="示例品牌",
    )

    async def fake_screen(_storage, _contents):
        raise AssertionError("clean content must not call the model")

    monkeypatch.setattr("opinion_watch.runner.screen_items_with_llm", fake_screen)
    admitted, candidates, stats = asyncio.run(
        _screen_items_for_admission(storage, [item], brand="示例品牌")
    )

    assert admitted == []
    assert candidates == set()
    assert stats["filtered"] == 1


def test_search_noise_without_brand_mention_is_not_admitted(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="unrelated-negative",
        url="https://www.douyin.com/video/unrelated-negative",
        title="达人说骗子跑路了，大家避雷",
        source_keyword="配达人",
        brand_name="配达人",
    )

    admitted, detail_candidates, stats = asyncio.run(
        _screen_items_for_admission(storage, [item], brand="配达人")
    )

    assert admitted == []
    assert detail_candidates == set()
    assert stats["filtered"] == 1


def test_model_screening_only_sends_uncertain_content(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    items = [
        CollectedContent(
            platform=Platform.DOUYIN,
            content_id="clean",
            url="https://www.douyin.com/video/clean",
            title="示例品牌日常介绍",
            source_keyword="示例品牌",
        ),
        CollectedContent(
            platform=Platform.DOUYIN,
            content_id="uncertain",
            url="https://www.douyin.com/video/uncertain",
            title="示例品牌投诉后客服不理",
            source_keyword="示例品牌",
        ),
    ]
    received: list[str] = []

    async def fake_screen(_storage, contents):
        received.extend(str(item["platform_content_id"]) for item in contents)
        return (
            {
                "douyin:uncertain": LLMAssessment(
                    category=OpinionCategory.REASONABLE_CONSUMER_COMPLAINT,
                    severity=RiskSeverity.P2,
                    confidence=0.9,
                    rationale="需要复核",
                    matched_signals=["投诉"],
                    requires_review=True,
                )
            },
            [],
            1,
        )

    monkeypatch.setattr("opinion_watch.runner.screen_items_with_llm", fake_screen)
    admitted, candidates, stats = asyncio.run(
        _screen_items_for_admission(storage, items, brand="示例品牌")
    )

    assert received == ["uncertain"]
    assert [item.content_id for item in admitted] == ["uncertain"]
    assert candidates == {"uncertain"}
    assert stats["model_candidates"] == 1


def test_scan_runner_retries_transient_error_and_records_attempts(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    storage.add_keyword("速探长", "速探长物流")
    account_id = storage.add_account(Platform.DOUYIN.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "test.db",
        artifact_dir=tmp_path / "artifacts",
    )
    collector = RetryThenSucceedCollector()
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", FakeBrowserSession)
    monkeypatch.setattr("opinion_watch.runner.collector_for", lambda platform: collector)

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.DOUYIN],
            options=ScanOptions(
                limit=20,
                detail_limit=0,
                comments_limit=0,
                retries=1,
                retry_delay_seconds=0,
                brand_delay_seconds=0,
            ),
        )
    )

    assert exit_code == 0
    run = storage.list_scan_runs(limit=1)[0]
    assert run["status"] == "succeeded"
    detail = storage.get_scan_run(int(run["id"]))
    assert detail is not None
    assert [item["status"] for item in detail["attempts"]] == [
        "failed",
        "succeeded",
        "succeeded",
    ]
    assert collector.keywords == ["速探长", "速探长", "速探长物流"]
    alerts = storage.list_alerts()
    # 同类告警合并成一条播报，明细保留在摘要正文里。
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "coverage_shortfall"
    assert "本轮巡检共 2 项检索结果不足" in alerts[0]["message"]
    assert alerts[0]["message"].count("低于目标 20 条") == 2
    assert "速探长物流" in alerts[0]["message"]
    assert "实际检索 1 条" in str(run["note"])
    assert run["options"]["llm_enabled_at_start"] is False
    assessments = storage.list_assessments()
    assert len(assessments) == 1
    assert assessments[0]["content_item_id"] == 1
    assert assessments[0]["source"] == "rules"
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_run_contents").fetchone()[0] == 2


def test_browser_profile_failure_does_not_downgrade_login_status(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    account_id = storage.add_account(Platform.DOUYIN.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "test.db",
        artifact_dir=tmp_path / "artifacts",
    )
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", LockedBrowserSession)
    monkeypatch.setattr("opinion_watch.runner.collector_for", lambda platform: object())

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.DOUYIN],
            options=ScanOptions(brand_delay_seconds=0),
        )
    )

    account = next(item for item in storage.list_accounts() if int(item["id"]) == account_id)
    assert exit_code == 2
    assert account["status"] == "ready"
    assert storage.list_alerts()[0]["kind"] == "account_busy"


def test_locked_profile_during_multi_account_probe_keeps_account_scannable(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """探测阶段撞上占用的档案，不能把账号标成 error。

    list_scan_accounts 只返回 status == 'ready' 的账号，一旦标成 error，这个账号
    会从后续所有巡检里消失，直到人工重新登录——而实际原因只是用户自己开着那个
    Chrome 窗口。
    """
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    first_id = storage.add_account(Platform.DOUYIN.value, "账号一")
    second_id = storage.add_account(Platform.DOUYIN.value, "账号二")
    storage.update_account_status(first_id, "ready")
    storage.update_account_status(second_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "test.db",
        artifact_dir=tmp_path / "artifacts",
    )

    # 第一个打开的档案被占用，之后的正常：巡检应当跳过它继续跑完。
    class FirstProfileLockedSession(FakeBrowserSession):
        opened = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            FirstProfileLockedSession.opened += 1
            self.locked = FirstProfileLockedSession.opened == 1

        async def __aenter__(self) -> "FirstProfileLockedSession":
            if self.locked:
                raise BrowserProfileLocked("测试档案仍被 Chrome 使用")
            return self

    collector = RetryThenSucceedCollector()
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", FirstProfileLockedSession)
    monkeypatch.setattr("opinion_watch.runner.collector_for", lambda platform: collector)

    asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.DOUYIN],
            options=ScanOptions(brand_delay_seconds=0),
        )
    )

    accounts = {int(item["id"]): str(item["status"]) for item in storage.list_accounts()}
    assert accounts[first_id] == "ready"
    kinds = {str(item["kind"]) for item in storage.list_alerts()}
    assert "account_busy" in kinds
    assert "account_probe_error" not in kinds


def test_search_route_verification_does_not_downgrade_login_status(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    account_id = storage.add_account(Platform.DOUYIN.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "test.db",
        artifact_dir=tmp_path / "artifacts",
    )
    collector = VerificationCollector()
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", FakeBrowserSession)
    monkeypatch.setattr("opinion_watch.runner.collector_for", lambda platform: collector)

    exit_code = asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.DOUYIN],
            options=ScanOptions(retries=0, brand_delay_seconds=0),
        )
    )

    account = next(item for item in storage.list_accounts() if int(item["id"]) == account_id)
    assert exit_code == 2
    assert account["status"] == "ready"
    assert storage.list_alerts()[0]["kind"] == "verification_required"


class FixedCountCollector(RetryThenSucceedCollector):
    """返回固定条数搜索结果的采集器，用于覆盖率告警口径测试。"""

    count = 1

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
        self.keywords.append(keyword)
        return [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id=str(index),
                url=f"https://www.douyin.com/video/{index}",
                title=f"速探长投诉不退款 {index}",
                source_keyword=keyword,
            )
            for index in range(self.count)
        ]


def _run_with_collector(
    tmp_path: Path,
    monkeypatch: object,
    collector: FixedCountCollector,
) -> Storage:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    account_id = storage.add_account(Platform.DOUYIN.value, "测试账号")
    storage.update_account_status(account_id, "ready")
    settings = Settings(
        runtime_dir=tmp_path / "runtime",
        database_path=tmp_path / "test.db",
        artifact_dir=tmp_path / "artifacts",
    )
    monkeypatch.setattr("opinion_watch.runner.BrowserSession", FakeBrowserSession)
    monkeypatch.setattr("opinion_watch.runner.collector_for", lambda platform: collector)
    asyncio.run(
        run_scan(
            settings,
            storage,
            [Platform.DOUYIN],
            options=ScanOptions(
                limit=20,
                detail_limit=0,
                comments_limit=0,
                retries=0,
                retry_delay_seconds=0,
                brand_delay_seconds=0,
            ),
        )
    )
    return storage


def test_nearly_full_search_page_does_not_raise_coverage_warning(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """平台首屏掺入噪声卡片被跳过后仍算覆盖正常，不应每个关键词告警一次。"""
    collector = FixedCountCollector()
    collector.count = 16
    storage = _run_with_collector(tmp_path, monkeypatch, collector)

    assert not [alert for alert in storage.list_alerts() if alert["kind"] == "coverage_shortfall"]


def test_severe_coverage_shortfall_still_warns_once(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    collector = FixedCountCollector()
    collector.count = 3
    storage = _run_with_collector(tmp_path, monkeypatch, collector)

    alerts = [alert for alert in storage.list_alerts() if alert["kind"] == "coverage_shortfall"]
    assert len(alerts) == 1
    assert "实际检索 3 条" in alerts[0]["message"]


def test_run_note_records_each_shortfall_once(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """告警文案只在 record_run_warning 内部落一次，run.note 不应出现重复。"""
    collector = FixedCountCollector()
    collector.count = 3
    storage = _run_with_collector(tmp_path, monkeypatch, collector)

    note = str(storage.list_scan_runs(limit=1)[0]["note"])
    assert note.count("实际检索 3 条") == 1
