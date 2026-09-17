"""Shared Qt background-task plumbing.

Workers never touch widgets. They publish progress and results to a queue,
which is drained by a QTimer on the Qt thread.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable

from PySide6 import QtCore

# 2026-09-17：追蹤所有還在跑的背景執行緒，供 wait_for_background_tasks() 在
# app 即將結束時（QApplication.aboutToQuit）呼叫——執行緒本身是 daemon=True，
# CPython 不會在直譯器關閉前等它們，若剛好還在用 sqlite3 這類 C extension，
# 直譯器開始 finalize／GC 時可能跟還在跑的執行緒互撞，導致原生層級當機
# （反覆出現的 Fatal Python error: Aborted／Windows 0xC0000409，見
# CHANGELOG.md 2026-09-17（續4）；用 PYTHONFAULTHANDLER=1 重現時，抓到的
# 堆疊正好是背景執行緒在跑 FlowAnalysisService.analyze() 的 SQLite 查詢）。
_live_threads_lock = threading.Lock()
_live_threads: list[threading.Thread] = []


def wait_for_background_tasks(timeout: float = 5.0):
    """等待目前所有背景執行緒結束，最多等 timeout 秒（本地 SQLite 查詢通常
    遠低於這個時間）。**如果逾時後還有執行緒沒結束**（實測發生過：TWSE 歷史
    回補、資金流向分析這類任務可能還在等網路回應），代表放行讓 CPython 走
    正常的直譯器關閉流程一定會跟那個還在跑的執行緒互撞、導致原生層級當機
    ——這正是這次追查到、且已經用同一支重現腳本驗證過兩次的根因，不是臆測。
    與其等著撞上那個更難看的當機，這裡改成直接呼叫 os._exit(0) 跳過 Python
    的 GC／finalize 直接結束行程：sqlite3 用 WAL／rollback journal 設計成
    禁得起行程被砍掉，不會因此壞掉，效果不會比目前反覆發生的原生當機更差，
    只是跳過非必要的收尾步驟。使用者的資料（部位／設定／快取）都是各自
    操作當下就同步寫檔（見 save_positions／save_position_detail_cache 等），
    不是留到程式關閉才寫，所以這裡略過的收尾不影響已經完成的操作。"""
    with _live_threads_lock:
        threads = [t for t in _live_threads if t.is_alive()]
    deadline = time.monotonic() + timeout
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(remaining)
    if any(t.is_alive() for t in threads):
        os._exit(0)


class BackgroundTask:
    def __init__(
        self,
        parent,
        work_fn: Callable[[threading.Event, Callable[..., None]], object],
        on_done: Callable[[object], None],
        on_error: Callable[[str], None],
        on_progress: Callable[..., None] | None = None,
        poll_interval_ms: int = 100,
    ):
        self._queue = queue.Queue()
        self._cancel_event = threading.Event()
        self._work_fn = work_fn
        self._on_done = on_done
        self._on_error = on_error
        self._on_progress = on_progress
        self._timer = QtCore.QTimer(parent)
        self._timer.timeout.connect(self._poll)
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="tradingnote-background-task",
        )
        self._poll_interval_ms = poll_interval_ms

    def start(self):
        self._timer.start(self._poll_interval_ms)
        self._thread.start()
        with _live_threads_lock:
            _live_threads[:] = [t for t in _live_threads if t.is_alive()]
            _live_threads.append(self._thread)
        return self

    def cancel(self):
        """Request cancellation and stop delivering callbacks to the UI."""

        self._cancel_event.set()
        self._timer.stop()

    @property
    def cancelled(self):
        return self._cancel_event.is_set()

    def _emit_progress(self, *args):
        if not self.cancelled:
            self._queue.put(("progress", args))

    def _run(self):
        try:
            result = self._work_fn(self._cancel_event, self._emit_progress)
        except Exception as exc:  # noqa: BLE001 - return failures to the UI
            self._queue.put(("error", str(exc)))
        else:
            self._queue.put(("done", result))

    def _poll(self):
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return

            if self.cancelled:
                continue
            if kind == "progress":
                if self._on_progress is not None:
                    self._on_progress(*payload)
            elif kind == "done":
                self._timer.stop()
                self._on_done(payload)
                return
            elif kind == "error":
                self._timer.stop()
                self._on_error(payload)
                return


def run_background_task(parent, work_fn, on_done, on_error, on_progress=None):
    """Start a background task and return its task handle."""

    return BackgroundTask(
        parent,
        work_fn,
        on_done,
        on_error,
        on_progress=on_progress,
    ).start()
