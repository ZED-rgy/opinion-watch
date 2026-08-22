"""趋势统计、后台任务执行器与趋势图控件用例。"""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from opinion_watch.desktop.charts import SeverityTrendChart  # noqa: E402
from opinion_watch.desktop.tasks import StorageTaskRunner  # noqa: E402
from opinion_watch.models import CollectedContent, Platform  # noqa: E402
from opinion_watch.storage import Storage  # noqa: E402


def _seed_assessment(storage: Storage, content_id: str, severity: str) -> None:
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id=content_id,
                url=f"https://example.test/{content_id}",
                title=f"示例内容 {content_id}",
                source_keyword="速探长",
                brand_name="速探长",
            )
        ]
    )
    rows = storage.list_contents_for_assessment(limit=100, include_assessed=True)
    item = next(row for row in rows if row["platform_content_id"] == content_id)
    storage.upsert_assessment(
        content_item_id=int(item["id"]),
        category="suspected_defamation",
        severity=severity,
        confidence=0.8,
        rationale="测试",
        matched_signals=[],
        requires_review=True,
    )


def test_severity_trend_counts_recent_days(desktop_storage: Storage) -> None:
    _seed_assessment(desktop_storage, "trend-1", "P1")
    _seed_assessment(desktop_storage, "trend-2", "P2")
    rows = desktop_storage.severity_trend(days=7)
    total = sum(int(row["count"]) for row in rows)
    assert total == 2
    assert {row["severity"] for row in rows} == {"P1", "P2"}


def test_list_cluster_members_returns_joined_rows(desktop_storage: Storage) -> None:
    desktop_storage.add_brand("速探长")
    for content_id in ("member-1", "member-2"):
        desktop_storage.upsert_contents(
            [
                CollectedContent(
                    platform=Platform.DOUYIN,
                    content_id=content_id,
                    url=f"https://example.test/{content_id}",
                    title="速探长虚假宣传曝光维权",
                    source_keyword="速探长",
                    brand_name="速探长",
                )
            ]
        )
    for row in desktop_storage.list_contents_for_assessment(limit=10, include_assessed=True):
        desktop_storage.upsert_assessment(
            content_item_id=int(row["id"]),
            category="suspected_false_information",
            severity="P1",
            confidence=0.9,
            rationale="测试",
            matched_signals=["虚假宣传"],
            requires_review=True,
        )
    assert desktop_storage.rebuild_event_clusters() == 1
    cluster = desktop_storage.list_event_clusters()[0]
    members = desktop_storage.list_cluster_members(int(cluster["id"]))
    assert len(members) == 2
    assert all(member["severity"] == "P1" for member in members)


def test_storage_task_runner_executes_off_thread(qtbot, tmp_path: Path) -> None:
    storage = Storage(tmp_path / "runner.db")
    storage.initialize()
    storage.add_brand("速探长")
    runner = StorageTaskRunner(storage.database_path)
    results: list[object] = []
    runner.submit(lambda s: [row["name"] for row in s.list_brands()], results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=5_000)
    assert results == [["速探长"]]
    errors: list[str] = []
    runner.submit(
        lambda s: (_ for _ in ()).throw(RuntimeError("boom")),
        results.append,
        on_error=errors.append,
    )
    qtbot.waitUntil(lambda: bool(errors), timeout=5_000)
    assert errors == ["boom"]
    runner.shutdown()


def test_trend_chart_accepts_rows_and_paints(qtbot) -> None:
    chart = SeverityTrendChart(days=7)
    qtbot.addWidget(chart)
    from datetime import date

    chart.set_data(
        [
            {"day": date.today().isoformat(), "severity": "P1", "count": 3},
            {"day": date.today().isoformat(), "severity": "P0", "count": 1},  # 归入 P1
        ]
    )
    assert chart._max_total == 4
    chart.set_data([])
    assert chart._max_total == 0
