import asyncio
from pathlib import Path

import pytest

from opinion_watch.cli import build_parser, run
from opinion_watch.config import Settings, stable_runtime_dir
from opinion_watch.runtime_migration import migrate_runtime
from opinion_watch.storage import Storage


def test_runtime_resolution_prefers_stable_then_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opinion_watch.config.sys.platform", "win32")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPINION_WATCH_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    stable = stable_runtime_dir()

    assert Settings.from_environment().runtime_dir == stable

    legacy = tmp_path / "runtime"
    legacy_storage = Storage(legacy / "opinion-watch.db")
    legacy_storage.initialize()
    assert Settings.from_environment().runtime_dir == legacy.resolve()

    stable_storage = Storage(stable / "opinion-watch.db")
    stable_storage.initialize()
    assert Settings.from_environment().runtime_dir == stable


def test_runtime_migration_backs_up_database_and_skips_browser_caches(tmp_path: Path) -> None:
    source = tmp_path / "legacy-runtime"
    target = tmp_path / "stable-runtime"
    storage = Storage(source / "opinion-watch.db")
    storage.initialize()
    storage.add_brand("迁移品牌")
    profile = source / "browser-profiles" / "douyin" / "1" / "Default"
    (profile / "Cache").mkdir(parents=True)
    (profile / "Cache" / "cache.bin").write_bytes(b"cache")
    (profile / "Cookies").write_bytes(b"profile-state")

    result = migrate_runtime(source, target)

    migrated = Storage(target / "opinion-watch.db")
    migrated.initialize()
    assert [item["name"] for item in migrated.list_brands()] == ["迁移品牌"]
    assert (profile.relative_to(source) / "Cookies").as_posix() in {
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    }
    assert not (target / profile.relative_to(source) / "Cache").exists()
    assert Path(str(result["backup"])).is_file()
    assert (source / "opinion-watch.db").is_file()


def test_runtime_migration_refuses_existing_target_and_active_tasks(tmp_path: Path) -> None:
    source = tmp_path / "legacy-runtime"
    storage = Storage(source / "opinion-watch.db")
    storage.initialize()
    target = tmp_path / "stable-runtime"
    target.mkdir()

    with pytest.raises(ValueError, match="拒绝覆盖"):
        migrate_runtime(source, target)

    target.rmdir()
    assert storage.acquire_task_lease("desktop", "test-owner", lease_seconds=60)
    with pytest.raises(RuntimeError, match="活动任务"):
        migrate_runtime(source, target)


def test_runtime_migration_cli_uses_explicit_source_before_initializing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opinion_watch.config.sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.delenv("OPINION_WATCH_RUNTIME_DIR", raising=False)
    source = tmp_path / "selected-source"
    storage = Storage(source / "opinion-watch.db")
    storage.initialize()
    storage.add_brand("主数据库品牌")
    args = build_parser().parse_args(
        [
            "data",
            "migrate-runtime",
            "--from-dir",
            str(source),
            "--confirm",
            "MIGRATE",
        ]
    )

    assert asyncio.run(run(args)) == 0

    migrated = Storage(stable_runtime_dir() / "opinion-watch.db")
    migrated.initialize()
    assert [item["name"] for item in migrated.list_brands()] == ["主数据库品牌"]
