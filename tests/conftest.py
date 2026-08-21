"""Shared pytest fixtures for the desktop test suite."""

import os
from pathlib import Path

import pytest

# 必须在任何 Qt 导入之前设置，CI 与本地无显示环境下都用离屏渲染。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from opinion_watch.storage import Storage  # noqa: E402


@pytest.fixture
def desktop_storage(tmp_path: Path) -> Storage:
    """独立的临时数据库，不经过 create_runtime（避免租约与环境变量污染）。"""
    storage = Storage(tmp_path / "desktop-test.db")
    storage.initialize()
    return storage
