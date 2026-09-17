import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets

import tradingnote_tasks
from tradingnote_tasks import BackgroundTask, wait_for_background_tasks


class WaitForBackgroundTasksTests(unittest.TestCase):
    """2026-09-17：反覆重現的原生層級當機（Fatal Python error: Aborted／
    Windows 0xC0000409）根因是背景 daemon thread 在直譯器關閉/GC 時還在跑
    sqlite3 之類的 C extension；wait_for_background_tasks() 讓
    QApplication.aboutToQuit 等背景執行緒收尾，避免這個競爭。第一版只有
    bounded join，實測（用真正的 app＋PYTHONFAULTHANDLER 重現腳本）發現
    TWSE 歷史回補這類網路任務可能撐過逾時還沒結束，逾時後放行一樣會撞上
    當機——所以逾時後還有執行緒在跑時改成直接 os._exit(0)。這裡驗證兩種
    情況：準時結束不會走 force-exit、逾時未結束會走 force-exit。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _start(self, work_fn):
        parent = QtCore.QObject()
        task = BackgroundTask(parent, work_fn, lambda result: None, lambda message: None).start()
        return task, parent

    def test_waits_for_running_thread_to_finish_without_force_exit(self):
        finished = []

        def work(_cancel_event, _emit):
            time.sleep(0.2)
            finished.append(True)
            return None

        task, parent = self._start(work)
        self.assertTrue(task._thread.is_alive())
        with patch.object(tradingnote_tasks.os, "_exit") as mock_exit:
            wait_for_background_tasks(timeout=2.0)
        self.assertFalse(task._thread.is_alive())
        self.assertEqual(finished, [True])
        mock_exit.assert_not_called()  # 準時結束，不需要走 force-exit 那條路
        task.cancel()

    def test_force_exits_instead_of_racing_interpreter_shutdown_when_still_running(self):
        """實測重現過的情境：逾時後如果還有背景執行緒在跑（例如網路請求還
        沒回來），放行讓直譯器繼續正常關閉一定會撞上原生層級當機（見本檔案
        class docstring）；這裡驗證逾時後會呼叫 os._exit()，而不是讓呼叫端
        (main() 的 aboutToQuit) 就這樣返回、放任程式往下走進 GC。用
        patch.object 而不是真的呼叫 os._exit()，否則會直接砍掉這個測試行程。"""
        def work(_cancel_event, _emit):
            time.sleep(1.5)
            return None

        task, parent = self._start(work)
        start = time.monotonic()
        with patch.object(tradingnote_tasks.os, "_exit") as mock_exit:
            wait_for_background_tasks(timeout=0.2)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0)  # 逾時後應該馬上返回，不等滿 1.5 秒
        self.assertTrue(task._thread.is_alive())  # 執行緒還在跑，只是我們沒繼續等
        mock_exit.assert_called_once_with(0)
        task.cancel()
        task._thread.join(3.0)  # 測試收尾，避免殘留執行緒跨到下一個測試


if __name__ == "__main__":
    unittest.main()
