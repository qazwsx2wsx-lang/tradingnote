"""Shared Qt background-task plumbing.

Workers never touch widgets. They publish progress and results to a queue,
which is drained by a QTimer on the Qt thread.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable

from PySide6 import QtCore


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
