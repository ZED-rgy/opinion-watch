import asyncio
from pathlib import Path

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
    _screen_items_for_admission,
    _screen_items_for_detail,
    run_scan,
)
from opinion_watch.storage import Storage


class FakePage:
    url = "https://example.test/search"


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


class RetryThenSucceedCollector:
    calls = 0

    def __init__(self) -> None:
        self.keywords: list[str] = []

    async def session_status(self, page: FakePage, context: object) -> SessionStatus:
        return SessionStatus.HEALTHY

    async def search(
        self,
        page: FakePage,
        context: object,
        keyword: str,
        *,
        limit: int,
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
        detail_limit: int,
        comments_limit: int,
        detail_candidate_ids: set[str] | None = None,
        artifact_dir: Path | None = None,
    ) -> list[CollectedContent]:
        return items


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


def test_model_screening_filters_ordinary_content_before_storage(
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

    async def fake_screen(_storage, contents):
        content = contents[0]
        return (
            {
                f"douyin:{content['platform_content_id']}": LLMAssessment(
                    category=OpinionCategory.OTHER,
                    severity=RiskSeverity.P3,
                    confidence=0.95,
                    rationale="普通内容",
                    matched_signals=[],
                    requires_review=False,
                )
            },
            [],
            1,
        )

    monkeypatch.setattr("opinion_watch.runner.screen_items_with_llm", fake_screen)
    admitted, candidates, stats = asyncio.run(
        _screen_items_for_admission(storage, [item], brand="示例品牌")
    )

    assert admitted == []
    assert candidates == set()
    assert stats["filtered"] == 1


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
    assert storage.list_alerts() == []
    assessments = storage.list_assessments()
    assert len(assessments) == 1
    assert assessments[0]["content_item_id"] == 1
    assert assessments[0]["source"] == "rules"
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_run_contents").fetchone()[0] == 2
