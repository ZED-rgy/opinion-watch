import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

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


class ZeroDetailCollector:
    async def session_status(self, page: ZeroDetailPage, context: object) -> SessionStatus:
        return SessionStatus.HEALTHY

    async def search(
        self,
        page: ZeroDetailPage,
        context: object,
        keyword: str,
        *,
        limit: int,
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
        detail_limit: int,
        comments_limit: int,
        detail_candidate_ids: set[str] | None = None,
        artifact_dir: Path | None = None,
    ) -> list[CollectedContent]:
        return items


class CleanDetailCollector(ZeroDetailCollector):
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
