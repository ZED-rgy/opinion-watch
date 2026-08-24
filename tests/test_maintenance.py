import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opinion_watch.maintenance import run_maintenance
from opinion_watch.models import CollectedContent, Platform
from opinion_watch.storage import Storage


def test_maintenance_prunes_old_filtered_candidates_and_unreferenced_artifacts(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    run_id = storage.create_scan_run(
        trigger="manual", platforms=["douyin"], brands=["速探长"], options={}
    )
    attempt_id = storage.create_scan_attempt(
        run_id=run_id, platform="douyin", keyword="速探长", attempt_no=1
    )
    candidate = CollectedContent(
        platform=Platform.DOUYIN,
        content_id="old-filtered",
        url="https://www.douyin.com/video/old-filtered",
        title="速探长普通内容",
        source_keyword="速探长",
    )
    storage.save_scan_candidates(run_id=run_id, attempt_id=attempt_id, items=[candidate])
    storage.mark_scan_candidates(attempt_id=attempt_id, admitted_content_ids=[])
    old_time = datetime(2026, 1, 1, tzinfo=UTC)
    with storage.connect() as connection:
        connection.execute(
            "UPDATE scan_candidates SET created_at = ? WHERE attempt_id = ?",
            (old_time.isoformat(), attempt_id),
        )

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    protected = artifacts / "protected.png"
    expired = artifacts / "expired.png"
    recent = artifacts / "recent.png"
    for path in (protected, expired, recent):
        path.write_bytes(b"evidence")
    timestamp = old_time.timestamp()
    os.utime(protected, (timestamp, timestamp))
    os.utime(expired, (timestamp, timestamp))
    storage.create_alert(
        run_id=run_id,
        kind="diagnostic",
        severity="warning",
        message="保留诊断",
        screenshot_path=str(protected),
    )

    stats = run_maintenance(
        storage,
        artifacts,
        now=datetime.now(UTC),
        candidate_retention_days=30,
        artifact_retention_days=30,
        artifact_max_bytes=1024,
    )

    assert stats.candidates_deleted == 1
    assert stats.artifacts_deleted == 1
    assert protected.exists()
    assert not expired.exists()
    assert recent.exists()
    with storage.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_candidates").fetchone()[0] == 0


def test_maintenance_due_is_recorded_only_after_success(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "test.db")
    storage.initialize()

    assert storage.maintenance_due("retention")
    storage.mark_maintenance_succeeded("retention")
    assert not storage.maintenance_due("retention")
    with storage.connect() as connection:
        connection.execute(
            "UPDATE maintenance_state SET last_succeeded_at = ? WHERE task = 'retention'",
            ((datetime.now(UTC) - timedelta(days=2)).isoformat(),),
        )
    assert storage.maintenance_due("retention")
