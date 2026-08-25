"""Safe migration from legacy app-local runtime directories to stable user storage."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opinion_watch.storage import Storage

_DATABASE_FILES = {"opinion-watch.db", "opinion-watch.db-shm", "opinion-watch.db-wal"}
_MIGRATED_DIRECTORIES = {"agent-inbox", "backups", "browser-profiles"}
_CACHE_DIRECTORIES = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "ShaderCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
}


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in _CACHE_DIRECTORIES}
    ignored.update(name for name in names if name.startswith("Singleton"))
    return ignored


def _active_leases(storage: Storage) -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    with storage.connect() as connection:
        rows = connection.execute(
            "SELECT name, owner, expires_at FROM task_leases WHERE expires_at > ? ORDER BY name",
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def migrate_runtime(source_dir: Path, target_dir: Path) -> dict[str, Any]:
    """Copy a complete runtime without overwriting either source or destination."""
    source = source_dir.resolve()
    target = target_dir.resolve()
    if source == target:
        raise ValueError("来源目录与目标目录相同，无需迁移")
    source_db = source / "opinion-watch.db"
    if not source_db.is_file():
        raise ValueError(f"来源目录中没有 opinion-watch.db：{source}")
    if target.exists():
        raise ValueError(f"目标运行目录已经存在，拒绝覆盖：{target}")
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("来源目录与目标目录不能互相包含")

    source_storage = Storage(source_db)
    leases = _active_leases(source_storage)
    if leases:
        names = "、".join(str(item["name"]) for item in leases)
        raise RuntimeError(f"来源运行目录仍有活动任务（{names}），请先退出桌面程序和巡检")

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    backup_path = source / "backups" / f"before-runtime-migration-{stamp}.db"
    source_storage.backup_to(backup_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".opinion-watch-migrate-", dir=target.parent))
    try:
        for child in source.iterdir():
            if child.name in _DATABASE_FILES:
                continue
            if child.name not in _MIGRATED_DIRECTORIES:
                continue
            destination = temporary / child.name
            if child.is_dir():
                shutil.copytree(child, destination, ignore=_copy_ignore)
            elif child.is_file():
                shutil.copy2(child, destination)

        migrated_db = temporary / "opinion-watch.db"
        source_storage.backup_to(migrated_db)
        migrated_storage = Storage(migrated_db)
        migrated_storage.initialize()
        with migrated_storage.connect() as connection:
            connection.execute("DELETE FROM task_leases")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    file_stats = [path.stat().st_size for path in target.rglob("*") if path.is_file()]
    return {
        "source": str(source),
        "target": str(target),
        "backup": str(backup_path),
        "files": len(file_stats),
        "bytes": sum(file_stats),
        "cache_directories_skipped": sorted(_CACHE_DIRECTORIES),
    }
