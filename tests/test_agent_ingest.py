import asyncio
import json
from pathlib import Path

import pytest

from opinion_watch.agent_ingest import ingest_agent_jsonl, load_agent_jsonl
from opinion_watch.cli import build_parser, run
from opinion_watch.config import Settings
from opinion_watch.storage import Storage


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )


def test_agent_import_uses_candidate_audit_and_only_notifies_detailed_risk(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    source = tmp_path / "agent-results.jsonl"
    _write_jsonl(
        source,
        [
            {
                "platform": "xiaohongshu",
                "brand": "速探长",
                "keyword": "速探长 投诉",
                "url": "https://www.xiaohongshu.com/explore/risk-1?token=temporary",
                "platform_content_id": "risk-1",
                "title": "速探长被质疑虚假宣传",
                "description": "正文明确指向速探长并提出虚假宣传质疑。",
                "relevance": "relevant",
                "detail_collected": True,
                "evidence": ["正文明确出现速探长和虚假宣传"],
            },
            {
                "platform": "douyin",
                "brand": "速探长",
                "url": "https://www.douyin.com/video/uncertain-1",
                "platform_content_id": "uncertain-1",
                "title": "速探长投诉物流延误",
                "relevance": "uncertain",
                "risk_signals": ["投诉", "延误"],
            },
            {
                "platform": "douyin",
                "brand": "速探长",
                "url": "https://www.douyin.com/video/noise-1",
                "platform_content_id": "noise-1",
                "title": "其他品牌的普通视频",
                "relevance": "irrelevant",
            },
        ],
    )

    result = ingest_agent_jsonl(storage, source)

    assert result["scanned"] == 3
    assert result["admitted"] == 2
    assert result["filtered"] == 1
    assert result["detailed"] == 1
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0] == 2
        rows = connection.execute(
            "SELECT status, filter_reason FROM scan_candidates ORDER BY id"
        ).fetchall()
        assert [row["status"] for row in rows] == ["admitted", "admitted", "filtered"]
        assert rows[-1]["filter_reason"] == "Agent 判断与品牌无关"
        assert (
            connection.execute(
                "SELECT url FROM content_items WHERE platform_content_id = 'risk-1'"
            ).fetchone()[0]
            == "https://www.xiaohongshu.com/explore/risk-1"
        )
    assessments = storage.list_assessments(limit=10)
    assert {item["severity"] for item in assessments} == {"P1", "P3"}
    uncertain = next(item for item in assessments if item["severity"] == "P3")
    assert uncertain["requires_review"] is True
    notifications = storage.list_notifications(channel="opinion")
    assert len(notifications) == 1
    assert notifications[0]["severity"] == "P1"
    assert "速探长被质疑虚假宣传" in notifications[0]["title"]


def test_agent_import_is_idempotent_for_content_and_validates_before_writes(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    source = tmp_path / "agent-results.jsonl"
    _write_jsonl(
        source,
        [
            {
                "platform": "douyin",
                "brand": "速探长",
                "url": "https://www.douyin.com/video/123?share_token=one",
                "title": "速探长服务体验",
                "relevance": "relevant",
            }
        ],
    )

    first = ingest_agent_jsonl(storage, source)
    second = ingest_agent_jsonl(storage, source)

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 2


def test_agent_rediscovery_reclassifies_existing_content_with_new_detail(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.add_brand("速探长")
    source = tmp_path / "agent-results.jsonl"
    base = {
        "platform": "xiaohongshu",
        "brand": "速探长",
        "url": "https://www.xiaohongshu.com/explore/risk-2",
        "platform_content_id": "risk-2",
        "title": "速探长服务体验",
        "relevance": "relevant",
    }
    _write_jsonl(source, [base])
    ingest_agent_jsonl(storage, source)
    first = storage.list_assessments(limit=1)[0]
    assert first["severity"] == "P3"
    assert first["requires_review"] is False

    _write_jsonl(
        source,
        [
            {
                **base,
                "description": "正文质疑速探长存在虚假宣传。",
                "detail_collected": True,
                "evidence": ["详情正文出现虚假宣传"],
            }
        ],
    )
    result = ingest_agent_jsonl(storage, source)

    assert result["inserted"] == 0
    assert result["updated"] == 1
    assessment = storage.list_assessments(limit=1)[0]
    assert assessment["severity"] == "P1"
    assert assessment["requires_review"] is True
    latest_run = storage.list_scan_runs(limit=1)[0]
    assert latest_run["classification"]["processed"] == 1
    assert latest_run["classification"]["requires_review"] == 1
    notifications = storage.list_notifications(channel="opinion")
    assert len(notifications) == 1
    assert notifications[0]["severity"] == "P1"

    invalid = tmp_path / "invalid.jsonl"
    _write_jsonl(
        invalid,
        [
            {
                "platform": "douyin",
                "brand": "未配置品牌",
                "url": "https://www.douyin.com/video/456",
                "title": "未配置品牌内容",
            }
        ],
    )
    with pytest.raises(ValueError, match="尚未在系统中配置"):
        ingest_agent_jsonl(storage, invalid)
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 2


def test_agent_jsonl_rejects_non_https_urls(tmp_path: Path) -> None:
    source = tmp_path / "invalid-url.jsonl"
    _write_jsonl(
        source,
        [
            {
                "platform": "douyin",
                "brand": "速探长",
                "url": "http://www.douyin.com/video/123",
                "title": "速探长",
            }
        ],
    )

    with pytest.raises(ValueError, match="HTTPS"):
        load_agent_jsonl(source)


def test_ingest_cli_accepts_agent_jsonl_path() -> None:
    args = build_parser().parse_args(["ingest", "agent-results.jsonl"])

    assert args.command == "ingest"
    assert args.source == "agent"
    assert args.path == Path("agent-results.jsonl")


def test_ingest_cli_sends_enabled_wecom_daily_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("OPINION_WATCH_RUNTIME_DIR", str(runtime_dir))
    settings = Settings.from_environment()
    settings.ensure_directories()
    storage = Storage(settings.database_path)
    storage.initialize()
    storage.add_brand("速探长")
    storage.save_wecom_config(enabled=True, bot_id="bot", chat_id="chat")
    source = tmp_path / "agent-results.jsonl"
    _write_jsonl(
        source,
        [
            {
                "platform": "douyin",
                "brand": "速探长",
                "url": "https://www.douyin.com/video/agent-daily",
                "title": "速探长服务体验",
                "relevance": "relevant",
            }
        ],
    )
    calls: list[int] = []

    async def fake_send_daily_report(
        _storage: Storage, *, scan_run_id: int, force: bool = False
    ) -> bool:
        assert force is False
        calls.append(scan_run_id)
        return True

    monkeypatch.setattr(
        "opinion_watch.cli.send_daily_report_if_due",
        fake_send_daily_report,
    )
    args = build_parser().parse_args(["ingest", str(source)])

    assert asyncio.run(run(args)) == 0
    assert len(calls) == 1
    assert calls[0] == storage.list_scan_runs(limit=1)[0]["id"]


def test_ingest_cli_records_wecom_failure_without_losing_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("OPINION_WATCH_RUNTIME_DIR", str(runtime_dir))
    settings = Settings.from_environment()
    settings.ensure_directories()
    storage = Storage(settings.database_path)
    storage.initialize()
    storage.add_brand("速探长")
    storage.save_wecom_config(enabled=True, bot_id="bot", chat_id="chat")
    source = tmp_path / "agent-results.jsonl"
    _write_jsonl(
        source,
        [
            {
                "platform": "douyin",
                "brand": "速探长",
                "url": "https://www.douyin.com/video/agent-report-error",
                "title": "速探长服务体验",
                "relevance": "relevant",
            }
        ],
    )

    async def fail_daily_report(
        _storage: Storage, *, scan_run_id: int, force: bool = False
    ) -> bool:
        raise RuntimeError("企微暂不可用")

    monkeypatch.setattr("opinion_watch.cli.send_daily_report_if_due", fail_daily_report)
    args = build_parser().parse_args(["ingest", str(source)])

    assert asyncio.run(run(args)) == 0
    assert storage.list_scan_runs(limit=1)[0]["status"] == "succeeded"
    alerts = storage.list_alerts(unacknowledged_only=True)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "wecom_report_error"
