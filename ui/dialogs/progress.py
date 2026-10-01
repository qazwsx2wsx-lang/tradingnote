"""啟動預載、回補、重新整理等長時間作業的進度對話框。"""

from PySide6 import QtCore, QtWidgets

from tradingnote_history import DEFAULT_BACKFILL_TARGET_DAYS

from ui.workers import run_backfill_in_thread, run_refresh_in_thread, run_tpex_finmind_backfill_in_thread
from ui.widgets import DialogBase


class StartupProgressDialog(QtWidgets.QDialog):
    """App 啟動時顯示，讓使用者知道正在抓報價／更新產業分類，避免主視窗建立
    完成前完全沒有任何畫面（原本 TradingNoteWindow() 建構子跑完才 show()，
    網路慢時看起來像沒反應甚至像當掉）。由 main() 建立、驅動、關閉，本身不
    知道背景工作的細節，只負責顯示 run_startup_preload_in_thread 回報的進度。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("tradingnote")
        self.setFixedWidth(320)
        # 啟動階段還沒有主視窗可以退回去，故意拿掉關閉鈕，避免使用者手動關掉
        # 這個對話框後，背景執行緒做完卻沒有視窗可以顯示、app 卡住沒反應。
        self.setWindowFlags(QtCore.Qt.Dialog | QtCore.Qt.CustomizeWindowHint | QtCore.Qt.WindowTitleHint)

        self.status_label = QtWidgets.QLabel("正在啟動...")
        self.status_label.setWordWrap(True)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.addWidget(self.status_label)
        layout.addSpacing(6)
        layout.addWidget(self.progress_bar)

    def set_progress(self, done, total, label):
        self.progress_bar.setValue(int(done / total * 100))
        self.status_label.setText(label)


class BackfillDialog(DialogBase):
    def __init__(self, parent, target_days=DEFAULT_BACKFILL_TARGET_DAYS, on_complete=None):
        super().__init__(parent)
        self.setWindowTitle("回補歷史資料")
        self.setMinimumWidth(320)
        self.on_complete = on_complete

        self.status_label = QtWidgets.QLabel(
            f"準備回補上市股票近 {target_days} 天資料..."
        )
        self.status_label.setWordWrap(True)
        self.close_button = QtWidgets.QPushButton("關閉")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addWidget(self.status_label)
        layout.addSpacing(10)
        layout.addWidget(self.close_button, alignment=QtCore.Qt.AlignHCenter)

        self._timer = run_backfill_in_thread(
            self,
            target_days,
            progress_cb=lambda d, t: self.status_label.setText(f"已回補 {d}/{t} 天"),
            done_cb=self._on_done,
            error_cb=self._on_error,
        )

    def _on_done(self, done):
        self.status_label.setText(f"回補完成，共 {done} 個交易日。")
        self.close_button.setEnabled(True)
        if self.on_complete:
            self.on_complete()

    def _on_error(self, message):
        self.status_label.setText(f"回補失敗：{message}")
        self.close_button.setEnabled(True)


class TpexBackfillDialog(DialogBase):
    """用 FinMind 補上櫃（TPEX）歷史資料的進度視窗，跟 BackfillDialog（TWSE）
    UI 風格一致，但完成訊息要分兩種：真的補完，跟額度用完提早停止——後者不是
    錯誤，是預期中會發生的事（FinMind 免費額度 600 次／小時，全市場上櫃約 800
    檔，一次通常補不完），文案要讓使用者知道「之後再點一次會自動接續」，不要
    看起來像失敗。"""

    def __init__(self, parent, token, target_days=DEFAULT_BACKFILL_TARGET_DAYS, on_complete=None):
        super().__init__(parent)
        self.setWindowTitle("使用 FinMind 補上櫃缺口")
        self.setMinimumWidth(360)
        self.on_complete = on_complete

        self.status_label = QtWidgets.QLabel("準備檢查上櫃股票歷史資料缺口...")
        self.status_label.setWordWrap(True)
        self.close_button = QtWidgets.QPushButton("關閉")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addWidget(self.status_label)
        layout.addSpacing(10)
        layout.addWidget(self.close_button, alignment=QtCore.Qt.AlignHCenter)

        self._timer = run_tpex_finmind_backfill_in_thread(
            self,
            token,
            target_days,
            progress_cb=lambda done, total, ticker: self.status_label.setText(
                f"已檢查 {done}/{total} 檔（{ticker}）..."
            ),
            done_cb=self._on_done,
            error_cb=self._on_error,
        )

    def _on_done(self, result):
        done, total = result["done"], result["total"]
        newly = result["newly_fetched"]
        if result["stopped_reason"] == "quota_exhausted":
            self.status_label.setText(
                f"已達 FinMind 每小時額度上限，本次新補 {newly} 檔"
                f"（累計 {done}/{total} 檔已達標）。\n"
                "額度約 1 小時後重置，之後再按一次這個按鈕即可自動接續，"
                "不會重新從頭補。"
            )
        else:
            self.status_label.setText(
                f"上櫃歷史資料回補完成，{total} 檔全數達標（本次新補 {newly} 檔）。"
            )
        self.close_button.setEnabled(True)
        if self.on_complete:
            self.on_complete()

    def _on_error(self, message):
        self.status_label.setText(f"回補失敗：{message}")
        self.close_button.setEnabled(True)


class RefreshDialog(DialogBase):
    """點擊「重新整理」時彈出，顯示連線取得即時報價／寫入歷史資料庫的階段進度。
    on_complete(snapshot, error) 完成時被呼叫：成功時 snapshot 是新快照、error 是
    None；失敗時 snapshot 是 None、error 是錯誤訊息，呼叫端據此決定是否套用新快照。"""

    def __init__(self, parent, on_complete):
        super().__init__(parent)
        self.setWindowTitle("重新整理")
        self.setMinimumWidth(320)
        self.on_complete = on_complete

        self.status_label = QtWidgets.QLabel("準備連線取得最新報價...")
        self.status_label.setWordWrap(True)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.close_button = QtWidgets.QPushButton("關閉")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addWidget(self.status_label)
        layout.addSpacing(6)
        layout.addWidget(self.progress_bar)
        layout.addSpacing(10)
        layout.addWidget(self.close_button, alignment=QtCore.Qt.AlignHCenter)

        self._timer = run_refresh_in_thread(
            self,
            progress_cb=self._on_progress,
            done_cb=self._on_done,
            error_cb=self._on_error,
        )

    def _on_progress(self, done, total, label):
        self.progress_bar.setValue(int(done / total * 100))
        self.status_label.setText(label)

    def _on_done(self, snapshot):
        self.progress_bar.setValue(100)
        self.status_label.setText("重新整理完成。")
        self.close_button.setEnabled(True)
        self.on_complete(snapshot, None)

    def _on_error(self, message):
        self.status_label.setText(f"重新整理失敗：{message}")
        self.close_button.setEnabled(True)
        self.on_complete(None, message)
