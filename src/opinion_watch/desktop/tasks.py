"""把耗时的 storage 查询移出 GUI 线程的最小工作线程封装。

每个 Worker 持有独立的 Storage 实例（storage 每次调用都新开 SQLite
连接，天然线程安全）；结果通过信号回到 GUI 线程。同一个 Worker 上的
新请求会让旧结果作废（按序号丢弃），避免竞态覆盖。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from opinion_watch.storage import Storage


class _StorageWorker(QObject):
    finished = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, database_path: Path) -> None:
        super().__init__()
        self._database_path = database_path
        self._storage: Storage | None = None

    @Slot(int, object)
    def run(self, request_id: int, job: object) -> None:
        try:
            if self._storage is None:
                # Storage 在工作线程内创建，连接绝不跨线程共享。
                self._storage = Storage(self._database_path)
            result = job(self._storage)  # type: ignore[operator]
        except Exception as exc:
            self.failed.emit(request_id, str(exc))
            return
        self.finished.emit(request_id, result)


class StorageTaskRunner(QObject):
    """在后台线程执行 `job(storage) -> result`，回调回到 GUI 线程。"""

    _submit = Signal(int, object)

    def __init__(self, database_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _StorageWorker(database_path)
        self._worker.moveToThread(self._thread)
        self._submit.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._request_id = 0
        self._closing = False
        self._callbacks: dict[int, tuple[Callable[[Any], None], Callable[[str], None] | None]] = {}
        self._thread.start()
        # 应用退出时随线程一起收尾；destroyed 信号里 self 已不可用，
        # 因此提前捕获 thread 引用。
        thread = self._thread

        def _stop_thread() -> None:
            thread.quit()
            thread.wait()

        self.destroyed.connect(_stop_thread)

    def submit(
        self,
        job: Callable[[Storage], Any],
        on_done: Callable[[Any], None],
        *,
        on_error: Callable[[str], None] | None = None,
        cancel_key: str | None = None,
    ) -> None:
        """提交后台任务。同 cancel_key 的旧请求结果会被丢弃。"""
        if self._closing:
            if on_error is not None:
                on_error("应用正在退出，后台查询已取消")
            return
        self._request_id += 1
        request_id = self._request_id
        if cancel_key is not None:
            stale = [
                rid
                for rid, (_, _err) in self._callbacks.items()
                if getattr(self._callbacks[rid][0], "_cancel_key", None) == cancel_key
            ]
            for rid in stale:
                self._callbacks.pop(rid, None)
            on_done._cancel_key = cancel_key  # type: ignore[attr-defined]
        self._callbacks[request_id] = (on_done, on_error)
        self._submit.emit(request_id, job)

    @Slot(int, object)
    def _on_finished(self, request_id: int, result: object) -> None:
        entry = self._callbacks.pop(request_id, None)
        if entry is not None:
            entry[0](result)

    @Slot(int, str)
    def _on_failed(self, request_id: int, message: str) -> None:
        entry = self._callbacks.pop(request_id, None)
        if entry is not None and entry[1] is not None:
            entry[1](message)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._callbacks.clear()
        self._thread.quit()
        # SQLite 连接有 5 秒 busy_timeout。这里等待工作线程真正结束，不能让
        # QApplication 在查询仍执行时销毁 QThread，后者会导致原生层崩溃。
        self._thread.wait()
