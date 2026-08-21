import json
from pathlib import Path

from opinion_watch.classification import classify_batch
from opinion_watch.models import CollectedContent, OpinionCategory, Platform, RiskSeverity
from opinion_watch.storage import Storage


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    return storage


def test_brand_crud(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("速探长")
    storage.add_brand("优速卖")

    assert [item["name"] for item in storage.list_brands()] == ["速探长", "优速卖"]
    assert storage.rename_brand("优速卖", "优速卖新名")
    assert storage.set_brand_enabled("优速卖新名", False)
    assert [item["name"] for item in storage.list_brands(enabled_only=True)] == ["速探长"]
    assert storage.delete_brand("优速卖新名")


def test_brand_keywords_are_configurable_scan_targets(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("速探长")
    keyword_id = storage.add_keyword("速探长", "速探长物流")

    targets = storage.list_scan_targets()
    assert [(item["brand_name"], item["keyword"]) for item in targets] == [
        ("速探长", "速探长"),
        ("速探长", "速探长物流"),
    ]

    assert storage.rename_keyword(keyword_id, "速探长货代")
    assert storage.set_keyword_enabled(keyword_id, False)
    assert [item["keyword"] for item in storage.list_scan_targets()] == ["速探长"]
    assert storage.delete_keyword(keyword_id)


def test_platform_accounts_are_isolated_records(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    douyin_id = storage.add_account("douyin", "运营一号")
    xiaohongshu_id = storage.add_account("xiaohongshu", "运营一号")

    assert douyin_id != xiaohongshu_id
    assert storage.update_account_status(douyin_id, "ready")
    assert storage.set_account_enabled(xiaohongshu_id, False)
    accounts = storage.list_accounts()
    assert [(item["platform"], item["status"], item["enabled"]) for item in accounts] == [
        ("douyin", "ready", True),
        ("xiaohongshu", "not_logged_in", False),
    ]


def test_content_upsert_deduplicates_and_tracks_multiple_brands(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    first = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="123",
        url="https://www.douyin.com/video/123",
        title="第一次发现",
        source_keyword="速探长",
    )
    second = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="123",
        url="https://www.douyin.com/video/123",
        title="更新后的标题",
        source_keyword="配达人",
    )

    first_stats = storage.upsert_contents([first])
    second_stats = storage.upsert_contents([second])

    assert first_stats.inserted == 1
    assert second_stats.updated == 1
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM content_matches").fetchone()[0] == 2
        assert connection.execute("SELECT title FROM content_items").fetchone()[0] == "更新后的标题"


def test_content_match_uses_brand_separately_from_search_keyword(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("速探长")
    storage.add_keyword("速探长", "速探长物流")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="keyword-result",
                url="https://www.douyin.com/video/keyword-result",
                title="搜索结果",
                source_keyword="速探长物流",
                brand_name="速探长",
            )
        ]
    )

    with storage.connect() as connection:
        match = connection.execute(
            """
            SELECT b.name, cm.source_keyword
            FROM content_matches cm JOIN brands b ON b.id = cm.brand_id
            """
        ).fetchone()
    assert tuple(match) == ("速探长", "速探长物流")


def test_content_match_keeps_multiple_keywords_for_one_brand(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("速探长")
    storage.add_keyword("速探长", "速探长物流")
    for keyword in ("速探长", "速探长物流"):
        storage.upsert_contents(
            [
                CollectedContent(
                    platform=Platform.DOUYIN,
                    content_id="same-content",
                    url="https://example.test/same-content",
                    title="同一条内容",
                    source_keyword=keyword,
                    brand_name="速探长",
                )
            ]
        )

    with storage.connect() as connection:
        rows = connection.execute(
            "SELECT source_keyword FROM content_matches ORDER BY source_keyword"
        ).fetchall()
    assert [row[0] for row in rows] == ["速探长", "速探长物流"]


def test_shallow_rescan_preserves_existing_detail_snapshot(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    detailed = CollectedContent(
        platform=Platform.XIAOHONGSHU,
        content_id="abc",
        url="https://www.xiaohongshu.com/search_result/abc",
        title="详情标题",
        source_keyword="速探长",
        raw_data={
            "source": "browser_dom",
            "detail_collected": True,
            "description": "已有正文",
            "comments": ["已有评论"],
        },
    )
    shallow = CollectedContent(
        platform=Platform.XIAOHONGSHU,
        content_id="abc",
        url="https://www.xiaohongshu.com/search_result/abc",
        title="搜索标题",
        source_keyword="速探长",
        raw_data={"source": "browser_dom"},
    )

    storage.upsert_contents([detailed])
    storage.upsert_contents([shallow])

    with storage.connect() as connection:
        raw_data = json.loads(
            connection.execute("SELECT raw_json FROM content_items").fetchone()[0]
        )
    assert raw_data["detail_collected"] is True
    assert raw_data["description"] == "已有正文"
    assert raw_data["comments"] == ["已有评论"]


def test_scan_run_attempt_and_alert_lifecycle(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run_id = storage.create_scan_run(
        trigger="manual",
        platforms=["douyin"],
        brands=["速探长"],
        options={"limit": 5},
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id,
        platform="douyin",
        keyword="速探长",
        attempt_no=1,
    )
    storage.finish_scan_attempt(
        attempt_id,
        status="failed",
        error_status="verification_required",
        error_message="需要人工验证",
    )
    alert_id = storage.create_alert(
        run_id=run_id,
        attempt_id=attempt_id,
        platform="douyin",
        keyword="速探长",
        kind="verification_required",
        severity="warning",
        message="需要人工验证",
    )
    storage.finish_scan_run(
        run_id,
        status="failed",
        collected=0,
        inserted=0,
        updated=0,
        succeeded=0,
        failed=1,
    )

    run = storage.get_scan_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["platforms"] == ["douyin"]
    assert run["brands"] == ["速探长"]
    assert run["attempts"][0]["error_status"] == "verification_required"
    assert [item["id"] for item in storage.list_alerts(unacknowledged_only=True)] == [alert_id]
    assert storage.acknowledge_alert(alert_id)
    assert storage.list_alerts(unacknowledged_only=True) == []
    notification = storage.list_notifications()[0]
    assert notification["kind"] == "runtime_alert"
    assert notification["entity_id"] == str(alert_id)


def test_pending_assessment_creates_notification_and_review_marks_it_read(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("配达人")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.XIAOHONGSHU,
                content_id="review-me",
                url="https://www.xiaohongshu.com/explore/review-me",
                title="配达人被质疑虚假宣传",
                source_keyword="配达人",
            )
        ]
    )
    content_id = int(storage.list_contents_for_assessment()[0]["id"])
    storage.upsert_assessment(
        content_item_id=content_id,
        category=OpinionCategory.SUSPECTED_FALSE_INFORMATION.value,
        severity=RiskSeverity.P1.value,
        confidence=0.7,
        rationale="需要人工复核",
        matched_signals=["虚假宣传"],
        requires_review=True,
    )

    notification = storage.list_notifications(unread_only=True)[0]
    assert notification["severity"] == "P1"
    assert storage.review_assessment(
        content_id,
        category=OpinionCategory.ORDINARY_GRIEVANCE.value,
        severity=RiskSeverity.P3.value,
        note="人工确认",
        reviewer="tester",
    )
    assert storage.list_notifications(unread_only=True) == []


def test_notifications_and_manual_assessments_support_crud_and_bulk_actions(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    first = storage.create_notification(severity="warning", title="第一条", message="需要处理")
    second = storage.create_notification(severity="info", title="第二条", message="已知悉")
    assert storage.count_unread_notifications() == 2
    assert storage.update_notification(
        first,
        severity="P1",
        title="已更新",
        message="已补充处理意见",
        read=True,
    )
    assert storage.count_unread_notifications() == 1
    assert storage.mark_notifications_read([second]) == 1
    assert storage.count_unread_notifications() == 0
    assert storage.delete_notifications([first, second]) == 2

    content_id = storage.create_manual_assessment(
        platform=Platform.DOUYIN.value,
        title="人工录入的舆情",
        url="https://example.test/manual-opinion",
        brand_name="示例品牌",
        category=OpinionCategory.OTHER.value,
        severity=RiskSeverity.P2.value,
        rationale="人工补录待跟进事项",
    )
    assert storage.get_assessment(content_id)["source"] == "manual"
    assert storage.update_assessment(
        content_id,
        category=OpinionCategory.ORDINARY_GRIEVANCE.value,
        severity=RiskSeverity.P3.value,
        rationale="已完成复核",
        review_status="reviewed",
    )
    assert storage.get_assessment(content_id)["review_status"] == "reviewed"
    assert storage.delete_assessments([content_id]) == 1
    assert storage.get_assessment(content_id) is None
    assert storage.list_contents_for_assessment()[0]["title"] == "人工录入的舆情"


def test_operational_reset_is_backed_up_and_preserves_configuration(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("速探长")
    storage.add_keyword("速探长", "速探长物流")
    storage.add_account("douyin", "运营一号")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="old-content",
                url="https://www.douyin.com/video/old-content",
                title="旧验收数据",
                source_keyword="速探长",
            )
        ]
    )
    backup_path = tmp_path / "backups" / "before-reset.db"

    storage.backup_to(backup_path)
    removed = storage.reset_operational_data()

    assert backup_path.exists()
    assert removed["content_items"] == 1
    assert all(count == 0 for count in storage.operational_counts().values())
    assert [item["name"] for item in storage.list_brands()] == ["速探长"]
    assert [item["keyword"] for item in storage.list_keywords()] == [
        "速探长",
        "速探长物流",
    ]
    assert [item["display_name"] for item in storage.list_accounts()] == ["运营一号"]


def test_scan_run_links_contents_and_assessment_filter(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("示例品牌")
    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="run-content-1",
        url="https://example.test/run-content-1",
        title="示例品牌售后争议",
        source_keyword="示例品牌",
    )
    storage.upsert_contents([item])
    run_id = storage.create_scan_run(
        trigger="manual",
        platforms=[Platform.DOUYIN.value],
        brands=["示例品牌"],
        options={"limit": 5},
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id,
        platform=Platform.DOUYIN.value,
        keyword="示例品牌",
        attempt_no=1,
    )
    storage.link_scan_contents(run_id=run_id, attempt_id=attempt_id, items=[item])
    classify_batch(storage)
    storage.finish_scan_run(
        run_id,
        status="succeeded",
        collected=1,
        inserted=1,
        updated=0,
        succeeded=1,
        failed=0,
        classification_summary={"processed": 1},
        model_summary={"enabled": False},
    )

    rows = storage.list_assessments(run_id=run_id)
    assert len(rows) == 1
    assert rows[0]["latest_run_id"] == run_id
    run = storage.get_scan_run(run_id)
    assert run is not None
    assert run["content_count"] == 1
    assert run["classification"] == {"processed": 1}


def test_scan_run_metadata_can_be_updated_and_deleted_without_losing_content(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    item = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="keep-after-run-delete",
        url="https://example.test/keep-after-run-delete",
        title="保留的历史内容",
        source_keyword="示例品牌",
    )
    storage.upsert_contents([item])
    run_id = storage.create_scan_run(
        trigger="manual",
        platforms=[Platform.DOUYIN.value],
        brands=["示例品牌"],
        options={},
        title="第一次巡检",
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id,
        platform=Platform.DOUYIN.value,
        keyword="示例品牌",
        attempt_no=1,
    )
    storage.link_scan_contents(run_id=run_id, attempt_id=attempt_id, items=[item])
    assert storage.update_scan_run_metadata(run_id, title="复盘后的巡检", note="需要跟进")
    assert storage.get_scan_run(run_id)["title"] == "复盘后的巡检"
    assert storage.get_scan_run(run_id)["note"] == "需要跟进"
    assert storage.delete_scan_run(run_id)
    assert storage.get_scan_run(run_id) is None
    assert storage.list_contents_for_assessment()[0]["title"] == "保留的历史内容"
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM scan_run_contents").fetchone()[0] == 0


def test_finish_scan_run_persists_summary_fields_in_their_named_columns(
    tmp_path: Path,
) -> None:
    storage = make_storage(tmp_path)
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["示例品牌"], options={}
    )
    storage.finish_scan_run(
        run_id,
        status="partial",
        collected=7,
        scanned=20,
        filtered=13,
        inserted=4,
        updated=3,
        succeeded=1,
        failed=2,
        suspected=5,
        detailed=4,
        media_items=8,
        error_message="平台需要验证",
    )

    run = storage.get_scan_run(run_id)
    assert run is not None
    assert {
        key: run[key]
        for key in (
            "collected_count",
            "scanned_count",
            "filtered_count",
            "inserted_count",
            "updated_count",
            "succeeded_count",
            "failed_count",
            "suspected_count",
            "detailed_count",
            "media_count",
            "error_message",
        )
    } == {
        "collected_count": 7,
        "scanned_count": 20,
        "filtered_count": 13,
        "inserted_count": 4,
        "updated_count": 3,
        "succeeded_count": 1,
        "failed_count": 2,
        "suspected_count": 5,
        "detailed_count": 4,
        "media_count": 8,
        "error_message": "平台需要验证",
    }


def test_initialize_preserves_deleted_keywords_and_disabled_brands(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("可配置品牌")
    keyword_id = storage.add_keyword("可配置品牌", "品牌物流")
    storage.set_brand_enabled("可配置品牌", False)
    storage.delete_keyword(keyword_id)

    storage.initialize()

    assert storage.list_brands()[0]["enabled"] is False
    assert [item["keyword"] for item in storage.list_keywords()] == ["可配置品牌"]


def test_scan_candidates_keep_filtered_search_cards_auditable(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["示例品牌"], options={}
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id, platform="douyin", keyword="示例品牌", attempt_no=1
    )
    items = [
        CollectedContent(
            platform=Platform.DOUYIN,
            content_id="candidate-1",
            url="https://example.test/1",
            title="疑似投诉",
            source_keyword="示例品牌",
        ),
        CollectedContent(
            platform=Platform.DOUYIN,
            content_id="candidate-2",
            url="https://example.test/2",
            title="普通内容",
            source_keyword="示例品牌",
        ),
    ]
    assert storage.save_scan_candidates(run_id=run_id, attempt_id=attempt_id, items=items) == 2
    assert (
        storage.mark_scan_candidates(attempt_id=attempt_id, admitted_content_ids=["candidate-1"])
        == 2
    )

    with storage.connect() as connection:
        rows = connection.execute(
            "SELECT platform_content_id, status, filter_reason "
            "FROM scan_candidates ORDER BY platform_content_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("candidate-1", "admitted", ""),
        ("candidate-2", "filtered", "未达到入库条件"),
    ]


def test_scan_lease_and_stale_run_recovery(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    assert storage.acquire_task_lease("scan", "owner-a", lease_seconds=60)
    assert not storage.acquire_task_lease("scan", "owner-b", lease_seconds=60)
    assert storage.heartbeat_task_lease("scan", "owner-a", lease_seconds=60)
    assert storage.release_task_lease("scan", "owner-a")
    assert storage.acquire_task_lease("scan", "owner-b", lease_seconds=60)

    run_id = storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["示例品牌"], options={}
    )
    with storage.connect() as connection:
        connection.execute(
            "UPDATE scan_runs SET started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (run_id,),
        )
    # 只要还有未过期的巡检租约，说明有进程在跑，不能把慢任务误判成中断。
    assert storage.recover_stale_scan_runs(timeout_minutes=1) == 0
    assert storage.get_scan_run(run_id)["status"] == "running"
    assert storage.release_task_lease("scan", "owner-b")
    assert storage.recover_stale_scan_runs(timeout_minutes=1) == 1
    assert storage.get_scan_run(run_id)["status"] == "interrupted"


def test_external_llm_http_endpoint_is_rejected(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    try:
        storage.save_llm_config(
            enabled=True,
            provider="openai-compatible",
            base_url="http://api.example.test/v1",
            model="demo",
        )
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("external HTTP endpoint should be rejected")


def test_storage_context_closes_connections_after_commit(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    with storage.connect() as connection:
        connection.execute("SELECT 1")

    try:
        connection.execute("SELECT 1")
    except Exception as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("storage context should close the connection")


def test_schedule_config_is_persisted_across_storage_instances(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.save_schedule_config(
        enabled=True,
        frequency="weekly",
        schedule_time="18:30",
        weekday=4,
        interval_minutes=60,
        scan_mode="deep",
        concurrency=2,
        last_scheduled_at="2026-08-21T09:00:00+00:00",
        next_run_at="2026-08-28T18:30:00+00:00",
    )

    reopened = Storage(tmp_path / "test.db")
    reopened.initialize()
    config = reopened.get_schedule_config()
    assert config["enabled"] is True
    assert config["frequency"] == "weekly"
    assert config["schedule_time"] == "18:30"
    assert config["weekday"] == 4
    assert config["scan_mode"] == "deep"
    assert config["concurrency"] == 2
    assert config["last_scheduled_at"] == "2026-08-21T09:00:00+00:00"
    assert config["next_run_at"] == "2026-08-28T18:30:00+00:00"


def test_event_clusters_only_include_suspected_content(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("示例品牌")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="cluster-1",
                url="https://example.test/cluster-1",
                title="示例品牌售后拒绝退款引发争议",
                source_keyword="示例品牌",
                brand_name="示例品牌",
            ),
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="cluster-2",
                url="https://example.test/cluster-2",
                title="示例品牌售后拒绝退款引发争议",
                source_keyword="示例品牌",
                brand_name="示例品牌",
            ),
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="ordinary-cluster",
                url="https://example.test/ordinary-cluster",
                title="示例品牌日常介绍视频",
                source_keyword="示例品牌",
                brand_name="示例品牌",
            ),
        ]
    )
    content_rows = {
        row["platform_content_id"]: int(row["id"]) for row in storage.list_contents_for_assessment()
    }
    for content_id, category in (
        ("cluster-1", OpinionCategory.SUSPECTED_FALSE_INFORMATION.value),
        ("cluster-2", OpinionCategory.SUSPECTED_FALSE_INFORMATION.value),
        ("ordinary-cluster", OpinionCategory.IRRELEVANT.value),
    ):
        storage.upsert_assessment(
            content_item_id=content_rows[content_id],
            category=category,
            severity=RiskSeverity.P1.value
            if content_id != "ordinary-cluster"
            else RiskSeverity.P3.value,
            confidence=0.9,
            rationale="测试判断",
            matched_signals=["售后"],
            requires_review=content_id != "ordinary-cluster",
        )

    assert storage.rebuild_event_clusters() == 1
    clusters = storage.list_event_clusters()
    assert clusters[0]["content_count"] == 2


def test_event_cluster_keeps_brand_name_containing_comma(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("示例,品牌")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="comma-1",
                url="https://example.test/comma-1",
                title="示例品牌售后拒绝退款引发争议",
                source_keyword="示例,品牌",
                brand_name="示例,品牌",
            ),
        ]
    )
    content_rows = {
        row["platform_content_id"]: int(row["id"]) for row in storage.list_contents_for_assessment()
    }
    storage.upsert_assessment(
        content_item_id=content_rows["comma-1"],
        category=OpinionCategory.SUSPECTED_FALSE_INFORMATION.value,
        severity=RiskSeverity.P1.value,
        confidence=0.9,
        rationale="测试判断",
        matched_signals=["售后"],
        requires_review=True,
    )

    assert storage.rebuild_event_clusters() == 1
    clusters = storage.list_event_clusters()
    assert clusters[0]["brand_names"] == ["示例,品牌"]


def test_model_assessment_is_not_downgraded_by_rule_reclassification(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    storage.add_brand("示例品牌")
    storage.upsert_contents(
        [
            CollectedContent(
                platform=Platform.DOUYIN,
                content_id="model-guard",
                url="https://example.test/model-guard",
                title="示例品牌虚假宣传曝光",
                source_keyword="示例品牌",
                brand_name="示例品牌",
            ),
        ]
    )
    content_id = int(storage.list_contents_for_assessment()[0]["id"])
    storage.upsert_assessment(
        content_item_id=content_id,
        category=OpinionCategory.SUSPECTED_FALSE_INFORMATION.value,
        severity=RiskSeverity.P1.value,
        confidence=0.9,
        rationale="模型复判结论",
        matched_signals=["虚假宣传"],
        requires_review=True,
        source="model",
    )
    storage.upsert_assessment(
        content_item_id=content_id,
        category=OpinionCategory.OTHER.value,
        severity=RiskSeverity.P3.value,
        confidence=0.6,
        rationale="规则重刷",
        matched_signals=[],
        requires_review=False,
        source="rules",
    )

    assessment = storage.get_assessment(content_id)
    assert assessment is not None
    assert assessment["source"] == "model"
    assert assessment["severity"] == RiskSeverity.P1.value

    storage.upsert_assessment(
        content_item_id=content_id,
        category=OpinionCategory.SUSPECTED_DEFAMATION.value,
        severity=RiskSeverity.P2.value,
        confidence=0.8,
        rationale="新一轮模型复判",
        matched_signals=[],
        requires_review=True,
        source="model",
    )
    assessment = storage.get_assessment(content_id)
    assert assessment is not None
    assert assessment["severity"] == RiskSeverity.P2.value


def test_finish_scan_run_preserves_manual_note_when_no_new_note(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["示例品牌"], options={}
    )
    assert storage.update_scan_run_metadata(run_id, title="手动巡检", note="人工备注")
    storage.finish_scan_run(run_id, status="succeeded", collected=1)
    assert storage.get_scan_run(run_id)["note"] == "人工备注"

    storage.finish_scan_run(run_id, status="succeeded", collected=1, note="覆盖备注")
    assert storage.get_scan_run(run_id)["note"] == "覆盖备注"


def test_mark_all_notifications_read_is_a_single_batch_update(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    for index in range(3):
        storage.create_notification(severity="P3", title=f"播报 {index}", message="测试内容")
    storage.create_notification(severity="P3", title="已读", message="测试内容", read=True)

    assert storage.count_unread_notifications() == 3
    assert storage.mark_all_notifications_read() == 3
    assert storage.count_unread_notifications() == 0
    assert storage.mark_all_notifications_read() == 0


def test_initialize_recovers_from_interrupted_migration(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    with storage.connect() as connection:
        # 模拟上一次迁移中途崩溃：中间表已提交，但迁移版本未写入。
        connection.execute("DELETE FROM schema_migrations WHERE version = 2")
        connection.execute("CREATE TABLE content_matches_v2 (id INTEGER PRIMARY KEY)")

    reopened = Storage(tmp_path / "test.db")
    reopened.initialize()
    with reopened.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert int(version) == 2
