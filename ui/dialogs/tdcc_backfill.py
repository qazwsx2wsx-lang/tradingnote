"""集保戶股權分散表歷史回補對話框。

非強制回應（用 show()，不擋住主視窗）：背景執行緒逐檔逐週查集保官網，每筆查完就寫入
history.db，所以可以隨時「暫停」、關閉視窗甚至結束程式，下次按「開始」從沒查過的地方
繼續，進度條也接續原本的位置。"""

import threading

from PySide6 import QtCore, QtWidgets

import tradingnote_chips as chips
from tradingnote_http import PriceFetchError
from tradingnote_tasks import run_background_task

from ui import app_paths
from ui.widgets import DialogBase

SCOPE_TOP = "top"
SCOPE_ALL = "all"
REQUEST_DELAY_SECONDS = 1.0


class TdccBackfillDialog(DialogBase):
    def __init__(self, parent, top_tickers, all_tickers):
        super().__init__(parent)
        self.setWindowTitle("補集保大戶歷史資料")
        self.setMinimumWidth(420)
        self._scopes = {SCOPE_TOP: list(top_tickers), SCOPE_ALL: list(all_tickers)}
        self._task = None
        self._stop_event = None

        intro = QtWidgets.QLabel(
            "從集保官網逐檔逐週補約一年的大戶持股分布（每筆間隔 1 秒）。可隨時暫停或關閉，"
            "下次按「開始」會從沒查過的地方繼續；補資料時其他功能照常使用。"
        )
        intro.setWordWrap(True)
        intro.setProperty("muted", True)

        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.addItem(f"目前資金流入前 50 名（{len(self._scopes[SCOPE_TOP])} 檔）", SCOPE_TOP)
        self.scope_combo.addItem(f"全市場個股（{len(self._scopes[SCOPE_ALL])} 檔，需數小時以上）", SCOPE_ALL)

        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.status_label = QtWidgets.QLabel("尚未開始")
        self.status_label.setWordWrap(True)

        self.start_button = QtWidgets.QPushButton("開始")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QtWidgets.QPushButton("暫停")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._pause)
        self.close_button = QtWidgets.QPushButton("關閉")
        self.close_button.clicked.connect(self.close)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        for button in (self.start_button, self.stop_button, self.close_button):
            buttons.addWidget(button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addWidget(intro)
        layout.addSpacing(8)
        layout.addWidget(self.scope_combo)
        layout.addSpacing(8)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addSpacing(8)
        layout.addLayout(buttons)

    # ---------- 動作 ----------

    def _running(self):
        return self._task is not None

    def _set_running(self, running):
        self.start_button.setEnabled(not running)
        self.scope_combo.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _start(self):
        if self._running():
            return
        tickers = self._scopes[self.scope_combo.currentData()]
        if not tickers:
            self.status_label.setText("沒有可補的股票（資金流向資料尚未載入）")
            return
        self._stop_event = stop = threading.Event()
        db_path = app_paths.HISTORY_DB_PATH

        def work(_cancel_event, emit):
            # 取消只用自己的 stop 事件：BackgroundTask.cancel 會連「完成」回呼一起丟掉，
            # 這樣就沒辦法在暫停後正確還原按鈕。
            return chips.backfill_tdcc_history(
                db_path, tickers, cancel_event=stop, progress=emit,
                delay_seconds=REQUEST_DELAY_SECONDS)

        self._set_running(True)
        self.status_label.setText("連線集保官網…")
        # 回傳值要留住，否則 BackgroundTask 被回收、回呼永遠不會觸發
        self._task = run_background_task(
            self, work, self._on_done, self._on_error, on_progress=self._on_progress)

    def _pause(self):
        if self._stop_event is not None:
            self._stop_event.set()
            self.stop_button.setEnabled(False)
            self.status_label.setText("暫停中…（等這一筆查完）")

    def _on_progress(self, done, total, label):
        self.progress_bar.setValue(int(done * 100 / total) if total else 0)
        self.status_label.setText(f"{done:,} / {total:,}　{label}")

    def _on_done(self, result):
        self._task = None
        self._set_running(False)
        done, total = result["done"], result["total"]
        self.progress_bar.setValue(int(done * 100 / total) if total else 100)
        if result["stopped"]:
            text = f"已暫停：{done:,} / {total:,}。按「開始」可繼續。"
        else:
            text = f"完成：{done:,} / {total:,}。"
        if result["failed"]:
            text += f"（{result['failed']} 筆失敗已略過，重新開始會再試）"
        self.status_label.setText(text)

    def _on_error(self, message):
        self._task = None
        self._set_running(False)
        self.status_label.setText(f"已停止：{message}　已完成的部分都已保存，稍後按「開始」繼續。")

    def closeEvent(self, event):
        # 關視窗＝暫停；執行緒會在下一筆查完後自行結束，進度已存。
        if self._stop_event is not None:
            self._stop_event.set()
        super().closeEvent(event)
