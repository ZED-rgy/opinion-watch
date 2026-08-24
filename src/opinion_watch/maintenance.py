"""Bound long-running audit storage without deleting live evidence."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opinion_watch.storage import Storage

CANDIDATE_RETENTION_DAYS = 90
ARTIFACT_RETENTION_DAYS = 30
ARTIFACT_MAX_BYTES = 1_073_741_824  # 1 GiB


@dataclass(frozen=True, slots=True)
class MaintenanceStats:
    candidates_deleted: int = 0
    artifacts_deleted: int = 0
    bytes_deleted: int = 0


def run_maintenance(
    storage: Storage,
    artifact_dir: Path,
    *,
    now: datetime | None = None,
    candidate_retention_days: int = CANDIDATE_RETENTION_DAYS,
    artifact_retention_days: int = ARTIFACT_RETENTION_DAYS,
    artifact_max_bytes: int = ARTIFACT_MAX_BYTES,
) -> MaintenanceStats:
    """Prune old filtered candidates and unreferenced diagnostic artifacts."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("维护时间必须包含时区")
    if candidate_retention_days < 1 or artifact_retention_days < 1:
        raise ValueError("保留天数必须大于 0")
    if artifact_max_bytes < 0:
        raise ValueError("文件容量上限不能为负数")

    candidates_deleted = storage.prune_filtered_scan_candidates(
        before=current - timedelta(days=candidate_retention_days)
    )
    deleted_files, deleted_bytes = _prune_artifacts(
        artifact_dir,
        storage.list_artifact_references(),
        cutoff=current - timedelta(days=artifact_retention_days),
        max_bytes=artifact_max_bytes,
    )
    return MaintenanceStats(candidates_deleted, deleted_files, deleted_bytes)


def _prune_artifacts(
    artifact_dir: Path,
    references: set[str],
    *,
    cutoff: datetime,
    max_bytes: int,
) -> tuple[int, int]:
    root = artifact_dir.resolve()
    if not root.exists():
        return 0, 0

    protected: set[Path] = set()
    for value in references:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved.is_relative_to(root):
            protected.add(resolved)

    files: list[tuple[Path, int, float]] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        stat = path.stat()
        files.append((path, stat.st_size, stat.st_mtime))

    deleted_count = 0
    deleted_bytes = 0
    cutoff_timestamp = cutoff.timestamp()
    remaining_bytes = sum(size for _path, size, _mtime in files)
    for path, size, mtime in sorted(files, key=lambda item: item[2]):
        if path.resolve() in protected:
            continue
        expired = mtime < cutoff_timestamp
        over_budget = remaining_bytes > max_bytes
        if not expired and not over_budget:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted_count += 1
        deleted_bytes += size
        remaining_bytes -= size

    # Only remove empty directories below the configured artifact root.
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        with suppress(OSError):
            directory.rmdir()
    return deleted_count, deleted_bytes
