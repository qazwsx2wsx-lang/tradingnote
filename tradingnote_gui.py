#!/usr/bin/env python3
"""tradingnote - 圖形介面版本（PySide6 + pyqtgraph）"""

import html
import queue
import threading
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_core import (
    add_position,
    compute_pnl,
    find_position,
    get_market_snapshot,
    load_positions,
    load_settings,
    lookup_price,
    remove_position,
    save_positions,
    save_settings,
    snapshot_staleness_warnings,
    update_position,
)
from tradingnote_history import (
    DEFAULT_BACKFILL_TARGET_DAYS,
    VOLUME_RATIO_TIERS,
    _change_pct,
    backfill_twse_history,
    compute_industry_flow,
    compute_volume_ratio_outliers,
    default_start_date_for_days,
    get_available_dates,
    get_history_status,
    get_industry_directory,
    get_industry_map,
    get_industry_top_stocks_range,
    get_latest_ticker_record,
    record_snapshot,
    trading_days_between,
)
from tradingnote_finmind import (
    FINMIND_HOURLY_LIMIT,
    backfill_tpex_history_via_finmind,
    fetch_institutional_investors,
    fetch_position_detail,
    fetch_valuation,
    get_call_count,
    load_position_detail_cache,
    save_position_detail_cache,
)
from tradingnote_concepts import build_ticker_concept_map, load_concepts
from tradingnote_taifex import (
    DEFAULT_FUTURES_PRODUCTS,
    get_cached_daily_futures_report,
    get_futures_snapshot,
    list_all_products,
)
from tradingnote_ai_agent import GEMINI_RPM_HINT, run_agent_turn
from tradingnote_ai_agent import get_call_count as get_gemini_call_count
from tradingnote_http import PriceFetchError

DATA_DIR = Path(__file__).parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
CACHE_PATH = DATA_DIR / "price_cache.json"
FUTURES_CACHE_PATH = DATA_DIR / "futures_cache.json"
POSITION_DETAIL_CACHE_PATH = DATA_DIR / "position_detail_cache.json"
HISTORY_DB_PATH = DATA_DIR / "history.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

# 背景檢查「今天的資料是否已發布」的輪詢間隔。get_market_snapshot 本身有 30 分鐘
# 快取（tradingnote_core.CACHE_TTL_SECONDS），所以就算這裡設得比 30 分鐘短，實際
# 打 TWSE／TPEX 的頻率仍受快取保護，不會因為輪詢變密就增加對外部 API 的負擔。
NEW_DATA_CHECK_INTERVAL_MS = 5 * 60 * 1000


COLUMNS = [
    ("id", "ID", 90),
    ("ticker", "代號", 60),
    ("name", "名稱", 100),
    ("industry", "族群", 90),
    ("concepts", "概念股", 160),
    ("shares", "股數", 70),
    ("entry_price", "成本價", 80),
    ("current_price", "現價", 80),
    ("equity_cost", "權益成本", 90),
    ("equity_value", "權益現值", 90),
    ("pnl", "損益", 90),
    ("pnl_pct", "損益%", 80),
    ("note", "備註", 200),
    ("updated_at", "更新日期", 90),
]

# ---------- 視覺主題（黑白灰極簡風格；漲跌與產業分類色刻意不受此影響，見下方） ----------
COLOR_BG = "#FFFFFF"
COLOR_SURFACE = "#FFFFFF"
COLOR_TEXT = "#1A1A1A"
COLOR_MUTED = "#767676"
COLOR_ACCENT = "#1A1A1A"
COLOR_ACCENT_ACTIVE = "#000000"
COLOR_ACCENT_TEXT = "#FFFFFF"
COLOR_BORDER = "#DCDCDC"
COLOR_ROW_ALT = "#F7F7F7"
COLOR_HOVER = "#EDEDED"
# 漲跌（損益）刻意保留紅綠上色——功能性色彩，用來一眼辨識盈虧方向，不算裝飾用色。
COLOR_GAIN = "#1E7A3E"
COLOR_LOSS = "#C0392B"

# matplotlib 的 tab20 定性配色表（手動內嵌，避免 pyqtgraph 為了取這組色再偷偷依賴 matplotlib）。
# 黑白主題刻意不套用到這裡——這是資金流向頁唯一需要區分約35個產業類別的地方，改灰階會讓類別難以辨識。
TAB20_COLORS = [
    "#1f77b4", "#aec7e8", "#ff7f0e", "#ffbb78", "#2ca02c", "#98df8a",
    "#d62728", "#ff9896", "#9467bd", "#c5b0d5", "#8c564b", "#c49c94",
    "#e377c2", "#f7b6d2", "#7f7f7f", "#c7c7c7", "#bcbd22", "#dbdb8d",
    "#17becf", "#9edae5",
]

FONT_FAMILY = "PingFang TC"

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-family: "{FONT_FAMILY}";
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    background: {COLOR_BG};
    border-radius: 6px;
    top: -1px;
}}
QTabBar::tab {{
    background: {COLOR_BORDER};
    color: {COLOR_TEXT};
    padding: 9px 18px;
    margin-right: 3px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{
    background: {COLOR_ACCENT};
    color: {COLOR_ACCENT_TEXT};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{
    background: {COLOR_HOVER};
}}
QPushButton {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    background: {COLOR_BORDER};
}}
QPushButton:disabled {{
    color: {COLOR_MUTED};
}}
QPushButton[accent="true"] {{
    background: {COLOR_ACCENT};
    color: {COLOR_ACCENT_TEXT};
    border: none;
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background: {COLOR_ACCENT_ACTIVE};
}}
QLineEdit {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 5px 7px;
    color: {COLOR_TEXT};
}}
QSpinBox {{
    background: {COLOR_SURFACE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 5px 7px;
    color: {COLOR_TEXT};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background: {COLOR_BORDER};
    border: none;
    width: 16px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background: {COLOR_ACCENT};
}}
QLabel {{
    background: transparent;
}}
QLabel[muted="true"] {{
    color: {COLOR_MUTED};
    font-size: 12px;
}}
QLabel[header="true"] {{
    font-size: 15px;
    font-weight: 700;
}}
QCheckBox {{
    spacing: 8px;
}}
QTableWidget {{
    background: {COLOR_SURFACE};
    alternate-background-color: {COLOR_ROW_ALT};
    gridline-color: {COLOR_BORDER};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    selection-background-color: {COLOR_ACCENT};
    selection-color: {COLOR_ACCENT_TEXT};
}}
QHeaderView::section {{
    background: {COLOR_BORDER};
    color: {COLOR_TEXT};
    padding: 6px;
    border: none;
    font-weight: 600;
}}
QStatusBar {{
    background: {COLOR_SURFACE};
    color: {COLOR_MUTED};
    border-top: 1px solid {COLOR_BORDER};
    font-size: 12px;
}}
"""


def accent_button(text, slot=None):
    btn = QtWidgets.QPushButton(text)
    btn.setProperty("accent", True)
    if slot is not None:
        btn.clicked.connect(slot)
    return btn


def _snapshot_date(snapshot):
    """回傳快照裡最多股票共用的交易日（多數 PriceInfo.date 會是同一天，用眾數
    避免少數個股資料延遲／異常日期影響判斷）；快照是空的就回傳 None。"""
    dates = [p.date for p in snapshot.values() if p.date]
    if not dates:
        return None
    return Counter(dates).most_common(1)[0][0]


def _futures_snapshot_date(futures_snapshot):
    """回傳期貨盤後快照（get_futures_snapshot 的回傳值）裡最新的資料日期，格式轉成
    跟 _snapshot_date／history.db 一致的 "YYYY-MM-DD"（TAIFEX 原始欄位是 "YYYYMMDD"）；
    快照是空的就回傳 None。"""
    dates = [
        data["date"]
        for sessions in futures_snapshot.values()
        for data in sessions.values()
        if data.get("date")
    ]
    if not dates:
        return None
    raw = max(dates)
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


_MARKET_LABELS = {"TWSE": "上市 TWSE", "TPEX": "上櫃 TPEX"}


def _format_history_status(status):
    overall = status["overall"]
    if not overall["days"]:
        return "資料庫目前無歷史資料，請按「回補歷史資料」或等待下次自動同步。"

    lines = [
        f"整體：{overall['days']} 個交易日｜{overall['min_date']} ～ {overall['max_date']}"
        f"｜{overall['tickers']:,} 檔｜{overall['rows']:,} 筆",
    ]
    for market, label in _MARKET_LABELS.items():
        m = status["by_market"].get(market)
        if m is None:
            continue
        lines.append(
            f"{label}：{m['days']} 個交易日｜{m['min_date']} ～ {m['max_date']}"
            f"｜{m['tickers']:,} 檔｜{m['rows']:,} 筆"
        )

    size_mb = status["file_size_bytes"] / (1024 * 1024)
    lines.append(f"資料庫檔案大小：{size_mb:.1f} MB")
    return "\n".join(lines)


def run_backfill_in_thread(parent, target_days, progress_cb, done_cb, error_cb):
    """在背景執行緒跑 backfill_twse_history，透過 queue + QTimer 把結果安全地送回 Qt 主執行緒。
    BackfillDialog（手動回補）與啟動時的自動資料連續性同步共用同一套機制，
    target_days 由呼叫端傳入（設定頁「回補天數」，未設定則沿用預設 120）。"""
    q = queue.Queue()

    def worker():
        try:
            done = backfill_twse_history(
                HISTORY_DB_PATH,
                target_days=target_days,
                on_progress=lambda d, t: q.put(("progress", d, t)),
            )
            q.put(("done", done, None))
        except Exception as e:  # noqa: BLE001 - surface any failure to the caller
            q.put(("error", str(e), None))

    timer = QtCore.QTimer(parent)

    def poll():
        try:
            while True:
                kind, a, b = q.get_nowait()
                if kind == "progress":
                    progress_cb(a, b)
                elif kind == "done":
                    timer.stop()
                    done_cb(a)
                    return
                elif kind == "error":
                    timer.stop()
                    error_cb(a)
                    return
        except queue.Empty:
            pass

    timer.timeout.connect(poll)
    timer.start(150)
    threading.Thread(target=worker, daemon=True).start()
    return timer


def run_tpex_finmind_backfill_in_thread(parent, token, target_days, progress_cb, done_cb, error_cb):
    """在背景執行緒跑 backfill_tpex_history_via_finmind，機制跟 run_backfill_in_thread
    相同（背景執行緒＋queue＋QTimer 輪詢送回 Qt 主執行緒）。跟 TWSE 版不同的是
    done_cb 收到的是一個 dict（done/total/newly_fetched/stopped_reason），不是單一
    整數，因為額度可能用完提早停止，呼叫端需要分開處理「跑完」跟「額度用完」兩種
    情況（見 TpexBackfillDialog._on_done）。"""
    q = queue.Queue()

    def worker():
        try:
            result = backfill_tpex_history_via_finmind(
                HISTORY_DB_PATH,
                token,
                target_days=target_days,
                on_progress=lambda done, total, ticker: q.put(("progress", done, total, ticker)),
            )
            q.put(("done", result, None, None))
        except Exception as e:  # noqa: BLE001 - surface any failure to the caller
            q.put(("error", str(e), None, None))

    timer = QtCore.QTimer(parent)

    def poll():
        try:
            while True:
                kind, a, b, c = q.get_nowait()
                if kind == "progress":
                    progress_cb(a, b, c)
                elif kind == "done":
                    timer.stop()
                    done_cb(a)
                    return
                elif kind == "error":
                    timer.stop()
                    error_cb(a)
                    return
        except queue.Empty:
            pass

    timer.timeout.connect(poll)
    timer.start(150)
    threading.Thread(target=worker, daemon=True).start()
    return timer


def _format_fetched_at(iso_string):
    """position_detail_cache.json 存的 fetched_at 是 isoformat 字串，這裡轉成
    畫面上顯示用的「YYYY-MM-DD HH:MM」，解析失敗（格式意外跑掉）就原字串照印，
    不要為了美化格式讓整個部位詳細資訊區塊噴錯。"""
    try:
        return datetime.fromisoformat(iso_string).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso_string


def run_task_in_thread(parent, work_fn, on_done, on_error):
    """跑一個沒有進度回報、只有「完成／失敗」兩種結果的背景工作（例如查一次 FinMind
    API），機制跟 run_backfill_in_thread 相同（背景執行緒＋queue＋QTimer 輪詢把結果
    送回 Qt 主執行緒），但不需要 progress_cb，給 StockDetailDialog 這種一次性查詢用。"""
    q = queue.Queue()

    def worker():
        try:
            result = work_fn()
            q.put(("done", result))
        except Exception as e:  # noqa: BLE001 - surface any failure to the caller
            q.put(("error", str(e)))

    timer = QtCore.QTimer(parent)

    def poll():
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            return
        timer.stop()
        if kind == "done":
            on_done(payload)
        else:
            on_error(payload)

    timer.timeout.connect(poll)
    timer.start(150)
    threading.Thread(target=worker, daemon=True).start()
    return timer


def run_refresh_in_thread(parent, progress_cb, done_cb, error_cb):
    """在背景執行緒重新整理即時報價＋寫入歷史資料庫，機制跟 run_backfill_in_thread／
    run_task_in_thread 相同（背景執行緒＋queue＋QTimer 輪詢送回 Qt 主執行緒）。
    progress_cb(done, total, label) 傳數字進度＋階段文字，給「重新整理」彈窗畫進度條用：
    共 4 個階段（TWSE、TPEX、寫入快取——這 3 個轉發自 get_market_snapshot 的
    on_progress——再加上寫入歷史資料庫），避免 TWSE/TPEX 網路延遲時整個視窗看起來像當掉。"""
    q = queue.Queue()
    total_steps = 4

    def worker():
        try:
            snapshot = get_market_snapshot(
                CACHE_PATH,
                force_refresh=True,
                on_progress=lambda done, _total, label: q.put(
                    ("progress", done, total_steps, label)
                ),
            )
            q.put(("progress", 3, total_steps, "正在寫入歷史資料庫..."))
            record_snapshot(HISTORY_DB_PATH, snapshot)
            q.put(("progress", 4, total_steps, "完成"))
            q.put(("done", snapshot, None, None))
        except PriceFetchError as e:  # noqa: BLE001 - surface any failure to the caller
            q.put(("error", str(e), None, None))

    timer = QtCore.QTimer(parent)

    def poll():
        try:
            while True:
                kind, a, b, c = q.get_nowait()
                if kind == "progress":
                    progress_cb(a, b, c)
                elif kind == "done":
                    timer.stop()
                    done_cb(a)
                    return
                elif kind == "error":
                    timer.stop()
                    error_cb(a)
                    return
        except queue.Empty:
            pass

    timer.timeout.connect(poll)
    timer.start(150)
    threading.Thread(target=worker, daemon=True).start()
    return timer


def run_startup_preload_in_thread(parent, progress_cb, done_cb, error_cb):
    """App 啟動時、TradingNoteWindow 建立前，在背景執行緒抓取即時報價、寫入歷史
    資料庫、更新產業分類（機制跟 run_refresh_in_thread 相同）。原本這三步是在
    TradingNoteWindow.__init__ 裡同步做，視窗要等全部做完才 show()，網路慢時
    畫面會完全沒反應；改成背景執行緒＋StartupProgressDialog 讓使用者看得到進度。
    多做「更新產業分類」是因為 __init__ 後續的 refresh_flow_tab／refresh_stocks_tab
    也需要它，先在這裡連網更新好，__init__ 裡才不用再連一次網。"""
    q = queue.Queue()
    total_steps = 4

    def worker():
        try:
            snapshot = get_market_snapshot(
                CACHE_PATH,
                on_progress=lambda done, _total, label: q.put(
                    ("progress", done, total_steps, label)
                ),
            )
            q.put(("progress", 3, total_steps, "正在寫入歷史資料庫..."))
            record_snapshot(HISTORY_DB_PATH, snapshot)
            q.put(("progress", 3, total_steps, "正在更新產業分類..."))
            get_industry_map(HISTORY_DB_PATH)
            q.put(("progress", 4, total_steps, "完成"))
            q.put(("done", snapshot, None, None))
        except PriceFetchError as e:  # noqa: BLE001 - surface any failure to the caller
            q.put(("error", str(e), None, None))

    timer = QtCore.QTimer(parent)

    def poll():
        try:
            while True:
                kind, a, b, c = q.get_nowait()
                if kind == "progress":
                    progress_cb(a, b, c)
                elif kind == "done":
                    timer.stop()
                    done_cb(a)
                    return
                elif kind == "error":
                    timer.stop()
                    error_cb(a)
                    return
        except queue.Empty:
            pass

    timer.timeout.connect(poll)
    timer.start(150)
    threading.Thread(target=worker, daemon=True).start()
    return timer


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


class PositionFormDialog(QtWidgets.QDialog):
    """新增／編輯部位共用同一個表單。position=None 是新增模式（欄位空白，日期
    預設今天）；傳入現有 Position 就是編輯模式（欄位預先帶入目前的值，代號欄位
    唯讀——改代號等於換了一檔股票，語意上該用刪除+新增，不是編輯既有部位）。"""

    def __init__(self, parent, on_submit, position=None):
        super().__init__(parent)
        self.setWindowTitle("編輯部位" if position is not None else "新增部位")
        self.on_submit = on_submit

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)

        fields = [
            ("代號", "ticker"),
            ("股數", "shares"),
            ("成本價", "entry_price"),
            ("日期 (YYYY-MM-DD)", "entry_date"),
            ("備註", "note"),
        ]
        self.inputs = {}
        for label, key in fields:
            edit = QtWidgets.QLineEdit()
            edit.setMinimumWidth(200)
            if position is not None:
                edit.setText(str(getattr(position, key)))
                if key == "ticker":
                    edit.setReadOnly(True)
            elif key == "entry_date":
                edit.setText(date.today().isoformat())
            form.addRow(label, edit)
            self.inputs[key] = edit

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = accent_button("確認", self._submit)
        cancel_btn = QtWidgets.QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addLayout(btn_row)

    def _submit(self):
        try:
            shares = int(self.inputs["shares"].text())
            entry_price = float(self.inputs["entry_price"].text())
        except ValueError:
            QtWidgets.QMessageBox.critical(self, "錯誤", "股數需為整數、成本價需為數字。")
            return
        try:
            self.on_submit(
                self.inputs["ticker"].text(),
                shares,
                entry_price,
                self.inputs["entry_date"].text(),
                self.inputs["note"].text(),
            )
        except ValueError as e:
            QtWidgets.QMessageBox.critical(self, "錯誤", str(e))
            return
        self.accept()


class PriceLookupDialog(QtWidgets.QDialog):
    def __init__(self, parent, snapshot):
        super().__init__(parent)
        self.setWindowTitle("查價")
        self.snapshot = snapshot

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("股票代號"))
        self.edit = QtWidgets.QLineEdit()
        self.edit.setMinimumWidth(140)
        self.edit.returnPressed.connect(self._lookup)
        row.addWidget(self.edit)

        self.result_label = QtWidgets.QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setMinimumWidth(320)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(row)
        layout.addWidget(self.result_label)
        layout.addWidget(accent_button("查詢", self._lookup))

        self.edit.setFocus()

    def _lookup(self):
        ticker = self.edit.text().strip()
        price = lookup_price(ticker, self.snapshot)
        if price is not None:
            self.result_label.setText(
                f"{price.ticker} {price.name}（{price.market}）\n"
                f"收盤 {price.close}  漲跌 {price.change}\n"
                f"開 {price.open}  高 {price.high}  低 {price.low}\n"
                f"日期 {price.date}"
            )
            return

        # LIFO 備援：即時快照沒有這檔股票時，改向歷史資料庫要最新一筆（date DESC）。
        record = get_latest_ticker_record(HISTORY_DB_PATH, ticker.upper())
        if record is None:
            self.result_label.setText(f"查無此股票代號：{ticker}")
            return
        self.result_label.setText(
            f"{ticker.upper()} {record['name']}（{record['market']}，取自歷史資料）\n"
            f"收盤 {record['close']}  成交量 {record['volume']}\n"
            f"日期 {record['date']}（非即時快照，來自歷史資料庫最新一筆）"
        )


class StockDetailDialog(QtWidgets.QDialog):
    """雙擊「個股」分頁裡的股票時彈出：查本益比／殖利率／股價淨值比，以及最新一個
    交易日的三大法人買賣超。上櫃（TPEX）股票的本益比／殖利率改查 TPEX 官方端點
    （不吃 FinMind 額度，見 tradingnote_finmind.fetch_valuation）；上市（TWSE）
    股票、以及不分市場的三大法人買賣超，仍查 FinMind。查詢在背景執行緒跑
    （run_task_in_thread），避免網路延遲卡住整個視窗。「顯示完整籌碼面資訊」
    按鈕另外提供跟「部位紀錄」頁選取部位時同一份資料（融資融券／外資持股／
    借券／停資停券／VPT／MFI，見 _on_show_full_detail），共用同一份
    position_detail_cache.json（key 是 ticker，不分是從部位紀錄還是這裡查
    的）、也共用 _render_detail_block 畫面邏輯；不點按鈕就不會多打那六支
    FinMind API，避免瀏覽「個股」頁清單時無謂燒額度。"""

    def __init__(self, parent, ticker, name, finmind_token, market=None):
        super().__init__(parent)
        self.ticker = ticker
        self.name = name
        self.finmind_token = finmind_token
        self.market = market
        self.setWindowTitle(f"{ticker} {name}")
        self.setMinimumWidth(360)

        self.status_label = QtWidgets.QLabel("查詢中...")
        self.status_label.setWordWrap(True)

        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(20, 20, 20, 16)
        self._layout.addWidget(self.status_label)

        self.full_detail_button = QtWidgets.QPushButton("顯示完整籌碼面資訊（同部位紀錄）")
        self.full_detail_button.clicked.connect(self._on_show_full_detail)
        self._layout.addWidget(self.full_detail_button)
        self._full_detail_widgets_built = False

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        self._layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)

        def fetch_both():
            return (
                fetch_valuation(ticker, finmind_token, market=market),
                fetch_institutional_investors(ticker, finmind_token),
            )

        self._timer = run_task_in_thread(self, fetch_both, self._on_done, self._on_error)

    def _on_done(self, result):
        valuation, institutional = result
        lines = []

        if valuation is None:
            lines.append("本益比／殖利率：查無資料")
        else:
            per = valuation["per"]
            pbr = valuation["pbr"]
            yield_pct = valuation["dividend_yield"]
            lines.append(f"日期：{valuation['date']}")
            lines.append(
                f"本益比 PER：{per if per is not None else 'N/A'}　"
                f"股價淨值比 PBR：{pbr if pbr is not None else 'N/A'}"
            )
            lines.append(f"殖利率：{yield_pct if yield_pct is not None else 'N/A'}%")

        lines.append("")
        if institutional is None:
            lines.append("三大法人買賣超：查無資料")
        else:
            lines.append(f"三大法人買賣超（{institutional['date']}，單位：股）")
            for row in institutional["breakdown"]:
                lines.append(f"　{row['label']}：淨買超 {row['net']:+,}")

        self.status_label.setText("\n".join(lines))
        self._notify_finmind_call()

    def _on_error(self, message):
        self.status_label.setText(
            f"查詢失敗：{message}\n\n"
            "可能原因：FinMind token 未設定或已失效、"
            "已超過免費額度，或該股票暫無此資料。"
        )
        self._notify_finmind_call()

    def _notify_finmind_call(self):
        update = getattr(self.parent(), "update_finmind_count_label", None)
        if update is not None:
            update()

    def _build_full_detail_widgets(self):
        """第一次按下「顯示完整籌碼面資訊」時才建立這些 widget，插在按鈕跟
        關閉鈕之間。跟「部位紀錄」頁 _build_position_detail_section 同一招用
        QScrollArea 包住文字摘要＋五張圖表，避免視窗一次要塞下五張圖撐爆
        畫面；同時把視窗放大到看得下內容的尺寸（初始只有一行狀態文字時不需要
        這麼大）。"""
        self.full_detail_label = QtWidgets.QLabel("")
        self.full_detail_label.setWordWrap(True)

        self.full_detail_price_chart = pg.PlotWidget()
        self.full_detail_flow_chart = pg.PlotWidget()
        self.full_detail_margin_chart = pg.PlotWidget()
        self.full_detail_vpt_chart = pg.PlotWidget()
        self.full_detail_mfi_chart = pg.PlotWidget()
        for chart in (
            self.full_detail_price_chart,
            self.full_detail_flow_chart,
            self.full_detail_margin_chart,
            self.full_detail_vpt_chart,
            self.full_detail_mfi_chart,
        ):
            chart.setBackground(COLOR_BG)
            chart.showGrid(x=True, y=True, alpha=0.15)
            chart.setMinimumHeight(200)
            chart.addLegend()

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self.full_detail_label)
        content_layout.addWidget(self.full_detail_price_chart)
        content_layout.addWidget(self.full_detail_flow_chart)
        content_layout.addWidget(self.full_detail_margin_chart)
        content_layout.addWidget(self.full_detail_vpt_chart)
        content_layout.addWidget(self.full_detail_mfi_chart)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        self._layout.insertWidget(self._layout.indexOf(self.full_detail_button) + 1, scroll)
        self._full_detail_widgets_built = True
        self.setMinimumSize(640, 300)
        self.resize(700, 900)

    def _on_show_full_detail(self):
        if not self._full_detail_widgets_built:
            self._build_full_detail_widgets()

        self.full_detail_button.setEnabled(False)
        self.full_detail_button.setText("查詢中...")
        header = f"{self.ticker} {self.name or ''}"

        # 跟「部位紀錄」頁 _load_position_detail 同一招：先顯示上次永久存下來
        # 的結果（有的話），背景照樣重打一次 FinMind 拿最新資料。
        cached = load_position_detail_cache(POSITION_DETAIL_CACHE_PATH, self.ticker)
        if cached is not None:
            note = f"（上次查詢：{_format_fetched_at(cached['fetched_at'])}，背景更新中...）"
            _render_detail_block(
                self.full_detail_label,
                self.full_detail_price_chart,
                self.full_detail_flow_chart,
                self.full_detail_margin_chart,
                self.full_detail_vpt_chart,
                self.full_detail_mfi_chart,
                header,
                cached,
                note,
            )
        else:
            self.full_detail_label.setText(f"{header}\n\nFinMind 查詢中...")

        def fetch():
            data = fetch_position_detail(self.ticker, self.finmind_token, market=self.market)
            save_position_detail_cache(POSITION_DETAIL_CACHE_PATH, self.ticker, data)
            return data

        self._full_detail_timer = run_task_in_thread(
            self,
            fetch,
            lambda result: self._on_full_detail_done(header, result),
            lambda message: self._on_full_detail_error(header, message, cached),
        )

    def _on_full_detail_done(self, header, data):
        self.full_detail_button.setEnabled(True)
        self.full_detail_button.setText("重新整理完整籌碼面資訊")
        _render_detail_block(
            self.full_detail_label,
            self.full_detail_price_chart,
            self.full_detail_flow_chart,
            self.full_detail_margin_chart,
            self.full_detail_vpt_chart,
            self.full_detail_mfi_chart,
            header,
            data,
        )
        self._notify_finmind_call()

    def _on_full_detail_error(self, header, message, cached):
        self.full_detail_button.setEnabled(True)
        # 跟部位紀錄頁一樣：有上次快取就繼續顯示它＋「背景更新失敗」提示，
        # 不要把已經在畫面上的舊資料清空。
        if cached is not None:
            self.full_detail_button.setText("重新整理完整籌碼面資訊")
            note = (
                f"（背景更新失敗：{message}；顯示上次查詢結果 "
                f"{_format_fetched_at(cached['fetched_at'])}）"
            )
            _render_detail_block(
                self.full_detail_label,
                self.full_detail_price_chart,
                self.full_detail_flow_chart,
                self.full_detail_margin_chart,
                self.full_detail_vpt_chart,
                self.full_detail_mfi_chart,
                header,
                cached,
                note,
            )
        else:
            self.full_detail_button.setText("顯示完整籌碼面資訊（同部位紀錄）")
            self.full_detail_label.setText(
                f"{header}\n\nFinMind 查詢失敗：{message}\n\n"
                "可能原因：FinMind token 未設定或已失效、已超過免費額度，或該股票暫無此資料。"
            )
        self._notify_finmind_call()


class IndustryTopStocksDialog(QtWidgets.QDialog):
    """點擊「資金流向分析」頁的產業泡泡／資金動向清單時彈出的小視窗，顯示該產業
    近 days 個交易日累積成交金額前 N 大成分股（市值資料的代理指標，見
    get_industry_top_stocks_range；N 不足時全部顯示）。純本地資料，不打任何
    API，開啟即顯示。泡泡圖跟清單各自有獨立的「資料區間」設定，這裡的 days
    就是觸發點擊當下那邊的區間天數，讓彈出視窗的口徑跟畫面上看到的一致。"""

    def __init__(self, parent, industry, top_stocks, days):
        super().__init__(parent)
        self.setWindowTitle(f"{industry}　近 {days} 日成交金額前 {len(top_stocks)} 大成分股")
        self.setMinimumWidth(420)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        table = QtWidgets.QTableWidget(len(top_stocks), 5)
        table.setHorizontalHeaderLabels(["代號", "名稱", "現價", "累積漲跌%", "累積成交金額(億)"])
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for row, stock in enumerate(top_stocks):
            change_pct = stock["change_pct"]
            change_str = f"{change_pct:+.2f}%" if change_pct is not None else "N/A"
            color = COLOR_GAIN if (change_pct or 0) >= 0 else COLOR_LOSS
            values = [
                stock["ticker"],
                stock["name"] or "-",
                f"{stock['close']:.2f}",
                change_str,
                f"{stock['trading_value'] / 1e8:,.2f}",
            ]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if col in (0, 2, 3, 4):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                if col == 3 and change_pct is not None:
                    item.setForeground(QtGui.QColor(color))
                table.setItem(row, col, item)
        table.resizeColumnsToContents()
        table.setFixedHeight(min(760, 36 + table.rowCount() * 30))
        layout.addWidget(table)

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)


class BackfillDialog(QtWidgets.QDialog):
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


class TpexBackfillDialog(QtWidgets.QDialog):
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


class RefreshDialog(QtWidgets.QDialog):
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


class FlowChartWidget(pg.PlotWidget):
    """產業資金流向泡泡圖，內建滑鼠滾輪縮放與觸控板手勢縮放（macOS pinch-to-zoom）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground(COLOR_BG)
        self.showGrid(x=True, y=True, alpha=0.15)
        self.getPlotItem().getViewBox().setMouseMode(pg.ViewBox.RectMode)
        # RectMode 讓滑鼠左鍵拖曳做框選縮放；平移改用滾輪縮放 + 觸控板手勢，
        # 保留右鍵拖曳可平移（pyqtgraph 預設行為）。
        self.setMouseEnabled(x=True, y=True)
        self.getPlotItem().getViewBox().setMouseMode(pg.ViewBox.PanMode)

    def event(self, ev):
        # macOS 觸控板雙指捏合（pinch）在 Qt 上是 NativeGesture 事件，
        # 跟滑鼠滾輪縮放（wheelEvent，pyqtgraph 已內建支援）分開處理，
        # 這樣兩指滾動、雙指捏合都能觸發平滑縮放。
        if ev.type() == QtCore.QEvent.NativeGesture:
            if ev.gestureType() == QtCore.Qt.NativeGestureType.ZoomNativeGesture:
                vb = self.getPlotItem().getViewBox()
                scale = 1.0 - ev.value()
                center_scene = self.mapToScene(ev.position().toPoint())
                center_view = vb.mapSceneToView(center_scene)
                vb.scaleBy((scale, scale), center=center_view)
                return True
        return super().event(ev)


def populate_flow_chart(chart, db_path, snapshot, avg_days=5, on_industry_click=None):
    """把產業資金流向資料畫進既有的 FlowChartWidget（保留使用者目前的縮放/平移狀態不做）。
    avg_days 決定 X／Y 兩軸的天數（5/10/20 日流向切換）。on_industry_click 若提供，
    點擊泡泡時會被呼叫並帶入該產業名稱（例如用來刷新下方的成分股面板）。"""
    chart.clear()
    flow = compute_industry_flow(db_path, snapshot, avg_days=avg_days)
    plotted = [f for f in flow if f.volume_ratio is not None and f.avg_change_pct is not None]
    skipped = len(flow) - len(plotted)

    vb = chart.getPlotItem().getViewBox()

    if not plotted:
        text = pg.TextItem(
            "尚無足夠歷史資料可繪製（請先執行「回補歷史資料」，\n或等待逐日累積達到最小天數）",
            color=COLOR_MUTED,
            anchor=(0.5, 0.5),
        )
        chart.addItem(text)
        text.setPos(0, 0)
        chart.setTitle("產業資金流向", color=COLOR_TEXT, size="13pt")
        vb.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
        return

    # 泡泡大小用「資金比重(%)」（該產業成交金額 ÷ 全市場今日總成交金額）而非絕對金額，
    # 讓數字換算成跟當日大盤規模脫鉤的相對占比，不再是每天隨大盤總量起伏的絕對值。
    max_share = max(f.capital_share_pct for f in plotted)

    for i, f in enumerate(plotted):
        size = max(14.0, (f.capital_share_pct / max_share) ** 0.5 * 55.0)
        color = QtGui.QColor(TAB20_COLORS[i % len(TAB20_COLORS)])
        scatter = pg.ScatterPlotItem(
            x=[f.avg_change_pct],
            y=[f.volume_ratio],
            size=size,
            brush=pg.mkBrush(color.red(), color.green(), color.blue(), 190),
            pen=pg.mkPen(COLOR_TEXT, width=0.6),
        )
        if on_industry_click is not None:
            # sigClicked.emit(self, points, ev) 帶 3 個位置參數，lambda 必須先吃滿這 3 個
            # 才能讓 industry=f.industry 這個預設值不被 ev 位置覆蓋掉（曾經因為少寫一個
            # 參數，導致每次點擊收到的都是 ev 而非產業名稱，已用 headless 測試抓出來）。
            scatter.sigClicked.connect(
                lambda _plot, _pts, _ev, industry=f.industry: on_industry_click(industry)
            )
        chart.addItem(scatter)
        label = pg.TextItem(
            f"{f.industry}\n{f.capital_share_pct:.1f}%", color=COLOR_TEXT, anchor=(0.5, 0.5)
        )
        label.setPos(f.avg_change_pct, f.volume_ratio)
        chart.addItem(label)

    chart.addLine(x=0, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.addLine(y=1, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("bottom", f"近{avg_days}日成交金額加權平均累積漲跌 %", color=COLOR_TEXT)
    chart.setLabel("left", f"今日成交量 / 近{avg_days}日均量", color=COLOR_TEXT)
    title = f"產業資金流向｜{avg_days}日（泡泡大小＝資金比重%，點擊可查看成分股）"
    if skipped:
        title += f"　（另有 {skipped} 個產業因歷史資料不足未顯示）"
    chart.setTitle(title, color=COLOR_TEXT, size="13pt")

    # 限制縮小（滾輪／觸控板捏合）的下限，最多縮到剛好看見全部泡泡為止，避免
    # 縮出一大片空白；邊界抓資料範圍的 15% 當緩衝，讓泡泡本身（半徑）與旁邊的
    # 產業名稱標籤不會被邊緣裁到。上限（放大）不受影響，仍可無限拉近。
    xs = [f.avg_change_pct for f in plotted]
    ys = [f.volume_ratio for f in plotted]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_pad = max((x_max - x_min) * 0.15, 1.0)
    y_pad = max((y_max - y_min) * 0.15, 0.2)
    vb.setLimits(
        xMin=x_min - x_pad,
        xMax=x_max + x_pad,
        yMin=y_min - y_pad,
        yMax=y_max + y_pad,
    )

    chart.enableAutoRange()


def _limit_zoom_to_data(chart, xs, ys, x_floor=1.0, y_floor=1.0):
    """限制往外縮的下限，最多縮到剛好看見全部資料為止，避免縮出一大片空白；
    放大則不受影響，仍可無限拉近。邊界抓資料範圍的 15% 當緩衝。"""
    vb = chart.getPlotItem().getViewBox()
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_pad = max((x_max - x_min) * 0.15, x_floor)
    y_pad = max((y_max - y_min) * 0.15, y_floor)
    vb.setLimits(
        xMin=x_min - x_pad,
        xMax=x_max + x_pad,
        yMin=y_min - y_pad,
        yMax=y_max + y_pad,
    )


def _populate_price_chart(chart, price_history):
    """歷史股價走勢圖：price_history 是 fetch_stock_price_history() 的回傳格式
    （list of {"date":, "close":, ...}，由舊到新排序），跟其他幾張圖吃的
    {"dates":, "series":/"vpt":/"mfi":} dict 格式不同，這裡直接吃 list。只畫
    收盤價（使用者要的是「歷史股價資訊圖」，不是另外疊漲跌%／成交量，避免跟
    其他幾張圖一樣的軸混在一起）。"""
    chart.clear()
    if not price_history:
        return

    dates = [row["date"] for row in price_history]
    closes = [row["close"] for row in price_history]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    chart.plot(x, closes, pen=pg.mkPen("#e377c2", width=2), name="收盤價")
    chart.setLabel("left", "收盤價", color=COLOR_TEXT)
    chart.setTitle(f"歷史股價｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, closes)
    # 跟 _populate_vpt_chart 同樣的理由：這張圖也可能一開始被 QScrollArea 捲到
    # 看不見的地方，enableAutoRange() 算出來的範圍在 widget 還沒有真正版面
    # 尺寸時不可靠，改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(closes), max(closes)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _populate_flow_chart(chart, history):
    chart.clear()
    if history is None or not history["dates"]:
        return

    dates = history["dates"]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    bucket_colors = {"外資": "#1f77b4", "投信": "#2ca02c", "自營商": "#d62728"}
    all_y = [0]  # 包含 0，讓下面的零軸參考線不會被縮出視野邊界外
    for label, series in history["series"].items():
        cumulative = []
        running_total = 0
        for net in series:
            running_total += net
            cumulative.append(running_total)
        all_y.extend(cumulative)
        chart.plot(
            x,
            cumulative,
            pen=pg.mkPen(bucket_colors.get(label, COLOR_TEXT), width=2),
            name=label,
        )

    chart.addLine(y=0, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("left", "累計淨買賣超（股）", color=COLOR_TEXT)
    chart.setTitle(f"三大法人累計買賣超｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, all_y)
    chart.enableAutoRange()


def _populate_margin_chart(chart, margin_history):
    """融資融券餘額趨勢圖：跟 _populate_flow_chart 不同，這裡的資料本來就是
    「餘額」（TodayBalance），不是逐日買賣超流量，所以直接畫原始值，不能再
    累加一次（累加會變成「餘額的餘額」，數字沒有意義）。"""
    chart.clear()
    if margin_history is None or not margin_history["dates"]:
        return

    dates = margin_history["dates"]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    series_colors = {"融資餘額": "#9467bd", "融券餘額": "#ff7f0e"}
    all_y = []
    for label, series in margin_history["series"].items():
        all_y.extend(series)
        chart.plot(
            x,
            series,
            pen=pg.mkPen(series_colors.get(label, COLOR_TEXT), width=2),
            name=label,
        )

    chart.setLabel("left", "餘額（張）", color=COLOR_TEXT)
    chart.setTitle(f"融資融券餘額｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, all_y)
    chart.enableAutoRange()


def _populate_vpt_chart(chart, vpt_mfi_history):
    chart.clear()
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return

    dates = vpt_mfi_history["dates"]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    series = vpt_mfi_history["vpt"]
    chart.plot(x, series, pen=pg.mkPen("#17becf", width=2), name="VPT")
    chart.setLabel("left", "VPT", color=COLOR_TEXT)
    chart.setTitle(f"VPT 量價趨勢｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, series)
    # 這個區塊可能一開始被 QScrollArea 捲到看不見的地方，widget 還沒有真正的
    # 版面尺寸時呼叫 enableAutoRange()／chart.autoRange() 算出來的範圍不可靠
    # （實測會卡在 ±1 附近的退化值）。直接用剛剛算好的資料範圍 setRange，不
    # 依賴 pyqtgraph 的自動偵測；y_pad 下限跟 _limit_zoom_to_data 的 y_floor
    # 同樣抓 1.0，避免 VPT 全程沒有變化（min==max）時 setYRange 收斂成高度 0
    # 的退化範圍。
    y_lo, y_hi = min(series), max(series)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _populate_mfi_chart(chart, vpt_mfi_history):
    """MFI 值域固定在 0～100，圖上加 80／20 兩條參考線（超買／超賣，MFI
    標準慣例），跟 _populate_flow_chart 的零軸參考線同一種畫法。"""
    chart.clear()
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return

    dates = vpt_mfi_history["dates"]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    series = vpt_mfi_history["mfi"]
    chart.plot(x, series, pen=pg.mkPen("#bcbd22", width=2), name="MFI")
    chart.addLine(y=80, pen=pg.mkPen(COLOR_LOSS, style=QtCore.Qt.DashLine, width=1))
    chart.addLine(y=20, pen=pg.mkPen(COLOR_GAIN, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("left", "MFI", color=COLOR_TEXT)
    chart.setTitle(f"MFI 資金流量｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, series + [0, 100])
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(0, 100, padding=0.02)


def _render_detail_block(
    label, price_chart, flow_chart, margin_chart, vpt_chart, mfi_chart, header, data, note=None
):
    """畫「個股籌碼面詳細資訊」文字摘要＋五張趨勢圖（歷史股價／三大法人／
    融資融券／VPT／MFI）。data 是 fetch_position_detail() 的回傳值（不管是剛
    查到的，還是 position_detail_cache.json 讀出來的上次結果，shape 都相同，見
    tradingnote_finmind.POSITION_DETAIL_FIELDS）；「部位紀錄」頁跟「個股」頁的
    StockDetailDialog 共用這份畫面邏輯，畫在各自傳入的 label／圖表 widget 上。
    note 非 None 時插在 header 下面一行，用來標示「這是上次的快取，背景更新中」
    或「背景更新失敗，顯示上次結果」。"""
    valuation = data["valuation"]
    history = data["institutional_history"]
    margin_history = data["margin_history"]
    foreign_shareholding = data["foreign_shareholding"]
    lending = data["lending"]
    suspension = data["suspension"]

    lines = [header]
    if note:
        lines.append(note)
    lines.append("")

    if valuation is None:
        lines.append("FinMind 基本面：查無資料")
    else:
        per = valuation["per"]
        pbr = valuation["pbr"]
        yield_pct = valuation["dividend_yield"]
        lines.append(
            f"FinMind 基本面（{valuation['date']}）　本益比：{per if per is not None else 'N/A'}　"
            f"股價淨值比：{pbr if pbr is not None else 'N/A'}　"
            f"殖利率：{yield_pct if yield_pct is not None else 'N/A'}%"
        )

    if history is None or not history["dates"]:
        lines.append("三大法人買賣超：查無資料")
    else:
        latest_date = history["dates"][-1]
        lines.append(f"三大法人買賣超（最新 {latest_date}，單位：股）")
        for label_name, series in history["series"].items():
            lines.append(f"　{label_name}：淨買超 {series[-1]:+,}")

    if margin_history is None:
        lines.append("融資融券：查無資料")
    else:
        latest = margin_history["latest"]
        lines.append(
            f"融資融券（最新 {latest['date']}，單位：張）　"
            f"融資餘額：{latest['margin_balance']:,}（{latest['margin_change']:+,}）　"
            f"融券餘額：{latest['short_balance']:,}（{latest['short_change']:+,}）"
        )

    if foreign_shareholding is None or foreign_shareholding["ratio"] is None:
        lines.append("外資持股比例：查無資料")
    else:
        change = foreign_shareholding["change"]
        change_text = f"（{change:+.2f}pp）" if change is not None else ""
        lines.append(
            f"外資持股比例（{foreign_shareholding['date']}）："
            f"{foreign_shareholding['ratio']:.2f}%{change_text}"
        )

    if lending is None or not lending["volume"]:
        lines.append("借券成交：查無資料")
    else:
        fee_text = (
            f"，均費率 {lending['avg_fee_rate']:.2f}%"
            if lending["avg_fee_rate"] is not None
            else ""
        )
        lines.append(f"借券成交（{lending['date']}）：合計 {lending['volume']:,} 張{fee_text}")

    if suspension:
        for event in suspension:
            lines.append(
                f"⚠ 停資停券公告　{event['date']} ～ {event['end_date'] or '未提供'}　"
                f"原因：{event['reason'] or '未提供'}"
            )

    label.setText("\n".join(lines))
    _populate_price_chart(price_chart, data.get("price_history"))
    _populate_flow_chart(flow_chart, history)
    _populate_margin_chart(margin_chart, margin_history)
    _populate_vpt_chart(vpt_chart, data.get("vpt_mfi_history"))
    _populate_mfi_chart(mfi_chart, data.get("vpt_mfi_history"))


class TradingCalendarWidget(QtWidgets.QCalendarWidget):
    """只允許選取「有歷史資料」的日期（valid_dates_iso，來自
    tradingnote_history.get_available_dates）：沒資料的日期文字反白（灰階），
    點下去也不會真的被選取——QCalendarWidget 本身沒有「單一日期停用」的原生
    API（setMinimumDate／setMaximumDate 只能框住整段區間頭尾），所以改成監聽
    clicked(QDate) 訊號，點到不在 valid_dates_iso 裡的日期時，把選取狀態復原回
    上一個有效日期，點到有效日期才會真的送出 dateChosen 訊號。"""

    dateChosen = QtCore.Signal(str)  # 選到有效日期時 emit ISO 字串（yyyy-MM-dd）

    def __init__(self, valid_dates_iso, parent=None):
        super().__init__(parent)
        self.setGridVisible(True)
        self.setVerticalHeaderFormat(QtWidgets.QCalendarWidget.NoVerticalHeader)

        valid_qdates = sorted(
            QtCore.QDate.fromString(d, "yyyy-MM-dd") for d in valid_dates_iso
        )
        self._valid_set = set(valid_qdates)
        self._last_valid = valid_qdates[-1] if valid_qdates else QtCore.QDate.currentDate()

        if valid_qdates:
            self.setMinimumDate(valid_qdates[0])
            self.setMaximumDate(valid_qdates[-1])
            muted_format = QtGui.QTextCharFormat()
            muted_format.setForeground(QtGui.QColor(COLOR_MUTED))
            d = valid_qdates[0]
            while d <= valid_qdates[-1]:
                if d not in self._valid_set:
                    self.setDateTextFormat(d, muted_format)
                d = d.addDays(1)
            self.setSelectedDate(self._last_valid)

        self.clicked.connect(self._on_clicked)

    def _on_clicked(self, qdate):
        if qdate in self._valid_set:
            self._last_valid = qdate
            self.dateChosen.emit(qdate.toString("yyyy-MM-dd"))
        else:
            # 灰階（沒有資料）的日期：復原成上一個有效選取，等同「不能選」。
            self.setSelectedDate(self._last_valid)


class TradingDateDialog(QtWidgets.QDialog):
    """資金流向頁「流向天數」／「資料區間」的日曆式起始日期選擇器，取代原本直接
    輸入天數的 QSpinBox。結束日固定是今天／最新資料（跟 compute_industry_flow 的
    avg_days 語意一致，永遠是「今天以前 N 個交易日」），使用者只選起始日；點到有
    資料的日期立刻套用並關閉視窗，不需要另外按確定。"""

    def __init__(self, parent, valid_dates_iso, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.selected_date = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        if not valid_dates_iso:
            layout.addWidget(QtWidgets.QLabel("尚無足夠歷史資料可選擇日期，請先回補歷史資料。"))
        else:
            calendar = TradingCalendarWidget(valid_dates_iso, self)
            calendar.dateChosen.connect(self._on_date_chosen)
            layout.addWidget(calendar)
            hint = QtWidgets.QLabel("反白（灰階）日期沒有歷史資料，無法選取；區間結束日固定是今天／最新資料。")
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            layout.addWidget(hint)

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.reject)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)

    def _on_date_chosen(self, date_iso):
        self.selected_date = date_iso
        self.accept()


class TradingNoteWindow(QtWidgets.QMainWindow):
    def __init__(self, snapshot, last_error):
        """snapshot／last_error 由 main() 的啟動前置作業（run_startup_preload_in_thread）
        算好傳入——報價抓取、寫入歷史資料庫、產業分類更新這三個會連網／連DB的步驟
        都已經在背景執行緒做完，這裡不再重做一次，避免視窗建立過程又卡一次網路。"""
        super().__init__()
        self.setWindowTitle("tradingnote - 台股資金流向分析")
        self.resize(1040, 720)
        self.setMinimumSize(860, 600)

        self.settings = load_settings(SETTINGS_PATH)
        self.positions = load_positions(POSITIONS_PATH)
        # 概念股清單只在啟動時讀一次（跟 concepts.json 一樣是人工維護的靜態檔案，
        # 改完要重開程式才生效，見 tradingnote_concepts.py docstring）。
        self._ticker_concept_map = build_ticker_concept_map(load_concepts())
        self.snapshot = snapshot
        self.last_error = last_error
        self.staleness_warning = (
            "；".join(snapshot_staleness_warnings(self.snapshot)) if self.snapshot else ""
        )

        self.continuity_note = ""
        self._total_pnl = None
        self._sync_timer = None

        # 「有新資料可更新」提示：_known_data_date 是目前畫面上顯示的資料所屬交易日，
        # _dismissed_data_date 是使用者按過「✕」關閉、暫時不想再看到提示的那個交易日
        # （避免同一天的新資料每次背景檢查都重新跳出來吵）。
        self._known_data_date = _snapshot_date(self.snapshot)
        self._dismissed_data_date = None
        self._pending_data_date = None
        self._new_data_check_timer = None

        central = QtWidgets.QWidget()
        central_layout = QtWidgets.QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self._build_new_data_banner(central_layout)

        self.tabs = QtWidgets.QTabWidget()
        central_layout.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.flow_tab = QtWidgets.QWidget()
        self.positions_tab = QtWidgets.QWidget()
        self.stocks_tab = QtWidgets.QWidget()
        self.futures_tab = QtWidgets.QWidget()
        self.ai_agent_tab = QtWidgets.QWidget()
        self.settings_tab = QtWidgets.QWidget()
        # 資金流向分析先加入，成為預設頁；部位紀錄、個股、期貨、AI 助理、設定依序是第二～六個分頁。
        self.tabs.addTab(self.flow_tab, "資金流向分析")
        self.tabs.addTab(self.positions_tab, "部位紀錄")
        self.tabs.addTab(self.stocks_tab, "個股")
        self.tabs.addTab(self.futures_tab, "期貨")
        self.tabs.addTab(self.ai_agent_tab, "AI 助理")
        self.tabs.addTab(self.settings_tab, "設定")

        self._build_flow_tab()
        self._build_positions_tab()
        self._build_stocks_tab()
        self._build_futures_tab()
        self._build_ai_agent_tab()
        self._build_settings_tab()

        self.status_bar = QtWidgets.QStatusBar()
        self.setStatusBar(self.status_bar)

        self.refresh_table()
        self.refresh_flow_tab()
        self.refresh_stocks_tab()
        self.refresh_futures_tab()

        # 每次啟動是否自動確保 TWSE 歷史資料連續（跟上到昨天）由「設定」分頁的
        # 「每次啟動自動檢測」開關控制，預設開啟。資料已經連續時在背景執行緒
        # 幾乎瞬間完成且不會有任何可見動作；真的有缺口時才會更新狀態列並在
        # 完成後重繪資金流向圖。
        if self.settings.get("auto_check_continuity", True):
            self._sync_history_continuity()

        self._new_data_check_timer = QtCore.QTimer(self)
        self._new_data_check_timer.timeout.connect(self._check_for_new_data)
        self._new_data_check_timer.start(NEW_DATA_CHECK_INTERVAL_MS)

    # ---------- 「有新資料可更新」提示 ----------
    # 背景每隔 NEW_DATA_CHECK_INTERVAL_MS 用不強制的 get_market_snapshot 檢查一次
    # TWSE／TPEX 目前發布的資料屬於哪個交易日；發現比畫面上顯示的還新（且不是使用者
    # 剛關掉過的那個交易日）就在分頁上方跳出可關閉的提示列，由使用者決定要不要按
    # 「立即更新」——不會背著使用者悄悄把畫面上的價格／損益換掉。

    def _build_new_data_banner(self, container_layout):
        self.new_data_banner = QtWidgets.QWidget()
        self.new_data_banner.setProperty("accent", True)
        self.new_data_banner.setVisible(False)

        banner_layout = QtWidgets.QHBoxLayout(self.new_data_banner)
        banner_layout.setContentsMargins(14, 8, 14, 8)

        self.new_data_banner_label = QtWidgets.QLabel("")
        banner_layout.addWidget(self.new_data_banner_label)
        banner_layout.addStretch(1)
        banner_layout.addWidget(
            QtWidgets.QPushButton("立即更新", clicked=self._apply_new_data_now)
        )
        banner_layout.addWidget(
            QtWidgets.QPushButton("✕", clicked=self._dismiss_new_data_banner)
        )
        container_layout.addWidget(self.new_data_banner)

    def _check_for_new_data(self):
        def work():
            return get_market_snapshot(CACHE_PATH, force_refresh=False)

        self._new_data_poll_timer = run_task_in_thread(
            self, work, self._on_new_data_check_done, self._on_new_data_check_error
        )

    def _on_new_data_check_done(self, snapshot):
        new_date = _snapshot_date(snapshot)
        if not new_date or not self._known_data_date:
            return
        if new_date <= self._known_data_date:
            return
        if new_date == self._dismissed_data_date:
            return
        self._pending_data_date = new_date
        self.new_data_banner_label.setText(f"🔔 {new_date} 新資料已可更新")
        self.new_data_banner.setVisible(True)

    def _on_new_data_check_error(self, _message):
        pass  # 背景檢查失敗不用打擾使用者，反正下個輪詢週期會再試一次

    def _dismiss_new_data_banner(self):
        self._dismissed_data_date = self._pending_data_date
        self.new_data_banner.setVisible(False)

    def _apply_new_data_now(self):
        self.new_data_banner.setVisible(False)
        self.force_refresh()

    # ---------- 資金流向分析頁 ----------

    def _build_flow_tab(self):
        # 外層固定包一層 QScrollArea：分頁本體（工具列＋泡泡圖＋資金動向清單）放進
        # 可捲動的內容區塊，內容高度／寬度超出視窗時改用捲軸瀏覽，而不是被硬擠壓
        # 變形——加了下方清單後整頁變高，這樣才不會在小視窗下看不到清單。
        outer_layout = QtWidgets.QVBoxLayout(self.flow_tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        outer_layout.addWidget(scroll)

        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)

        self.flow_avg_days = 5
        self.flow_start_date = default_start_date_for_days(HISTORY_DB_PATH, self.flow_avg_days)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("流向區間："))
        self.flow_date_button = QtWidgets.QPushButton()
        self.flow_date_button.clicked.connect(self._open_flow_date_picker)
        toolbar.addWidget(self.flow_date_button)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        self._update_flow_date_button()

        self.flow_chart = FlowChartWidget()
        self.flow_chart.setMinimumHeight(380)
        layout.addWidget(self.flow_chart)

        layout.addSpacing(20)
        self._build_flow_list_section(layout)

        layout.addSpacing(20)
        self._build_volume_outliers_section(layout)

    def _update_flow_date_button(self):
        self.flow_date_button.setText(
            f"{self.flow_start_date} 起（近 {self.flow_avg_days} 個交易日）　▾"
        )

    def _open_flow_date_picker(self):
        # 可選的起始日只到「今天以前」——avg_days 的語意本來就是「今天以前 N 個交易日」
        # （見 compute_industry_flow），選今天當起始日沒有意義（等同 0 天，永遠資料不足）。
        today_iso = date.today().isoformat()
        valid_dates = [d for d in get_available_dates(HISTORY_DB_PATH) if d < today_iso]
        dialog = TradingDateDialog(self, valid_dates, "選擇流向區間起始日期")
        if dialog.exec() == QtWidgets.QDialog.Accepted and dialog.selected_date:
            self.flow_start_date = dialog.selected_date
            self.flow_avg_days = max(1, trading_days_between(HISTORY_DB_PATH, self.flow_start_date))
            self._update_flow_date_button()
            self.refresh_flow_tab()

    def refresh_flow_tab(self):
        populate_flow_chart(
            self.flow_chart,
            HISTORY_DB_PATH,
            self.snapshot,
            avg_days=self.flow_avg_days,
            on_industry_click=self._on_industry_bubble_clicked,
        )
        self.refresh_flow_list()
        self.refresh_volume_outliers_list()

    def _show_industry_top_stocks(self, industry, days):
        top_stocks = get_industry_top_stocks_range(
            HISTORY_DB_PATH, self.snapshot, industry, days, top_n=30
        )
        IndustryTopStocksDialog(self, industry, top_stocks, days=days).exec()

    def _on_industry_bubble_clicked(self, industry):
        # 泡泡圖本身就是「近 flow_avg_days 日」流向的視覺化，點擊彈出的成分股
        # 清單也該用同一個區間的累積成交金額，跟泡泡代表的資料口徑一致。
        self._show_industry_top_stocks(industry, self.flow_avg_days)

    def _on_flow_list_item_clicked(self, industry):
        # 跟泡泡圖各自獨立：清單本身有自己的「資料區間」設定
        # （self.list_avg_days），點清單裡的產業列時，前十大成分股用清單那個
        # 區間的累積成交金額排序，兩邊口徑才會一致。
        self._show_industry_top_stocks(industry, self.list_avg_days)

    # ---------- 資金動向清單（跟泡泡圖同一份 compute_industry_flow，但區間可自由
    # 輸入天數，且不篩掉歷史資料不足的產業——不足的行直接標「資料不足」，而不是
    # 悄悄從清單消失，讓使用者看得到資料缺口） ----------

    def _build_flow_list_section(self, layout):
        header = QtWidgets.QLabel("資金動向清單")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        self.list_avg_days = 20
        self.list_start_date = default_start_date_for_days(HISTORY_DB_PATH, self.list_avg_days)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("資料區間："))
        self.list_date_button = QtWidgets.QPushButton()
        self.list_date_button.clicked.connect(self._open_list_date_picker)
        controls.addWidget(self.list_date_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self._update_list_date_button()

        hint = QtWidgets.QLabel("起始日以「今日」往回算，跟泡泡圖的「流向區間」各自獨立。")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)

        self.flow_list = QtWidgets.QTreeWidget()
        self.flow_list.setColumnCount(5)
        self.flow_list.setHeaderLabels(["產業", "檔數", "成交金額(億)", "加權漲跌%", "量比"])
        self.flow_list.setRootIsDecorated(False)
        self.flow_list.setAlternatingRowColors(True)
        self.flow_list.setSortingEnabled(True)
        self.flow_list.setMinimumHeight(280)
        self.flow_list.itemClicked.connect(
            lambda item, _col: self._on_flow_list_item_clicked(
                item.data(0, QtCore.Qt.UserRole)
            )
        )
        layout.addWidget(self.flow_list)

    def _update_list_date_button(self):
        self.list_date_button.setText(
            f"{self.list_start_date} 起（近 {self.list_avg_days} 個交易日）　▾"
        )

    def _open_list_date_picker(self):
        today_iso = date.today().isoformat()
        valid_dates = [d for d in get_available_dates(HISTORY_DB_PATH) if d < today_iso]
        dialog = TradingDateDialog(self, valid_dates, "選擇資料區間起始日期")
        if dialog.exec() == QtWidgets.QDialog.Accepted and dialog.selected_date:
            self.list_start_date = dialog.selected_date
            self.list_avg_days = max(1, trading_days_between(HISTORY_DB_PATH, self.list_start_date))
            self._update_list_date_button()
            self.refresh_flow_list()

    def refresh_flow_list(self):
        flow = compute_industry_flow(HISTORY_DB_PATH, self.snapshot, avg_days=self.list_avg_days)
        self.flow_list.setSortingEnabled(False)
        self.flow_list.clear()
        for f in flow:
            if f.avg_change_pct is None:
                change_text = "資料不足"
            else:
                change_text = f"{f.avg_change_pct:+.2f}%"
            volume_text = f"{f.volume_ratio:.2f}" if f.volume_ratio is not None else "資料不足"

            item = QtWidgets.QTreeWidgetItem(
                [
                    f.industry,
                    str(f.stock_count),
                    f"{f.total_trading_value / 1e8:,.1f}",
                    change_text,
                    volume_text,
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, f.industry)
            for col in (1, 2, 3, 4):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            if f.avg_change_pct is not None:
                color = COLOR_GAIN if f.avg_change_pct >= 0 else COLOR_LOSS
                item.setForeground(3, QtGui.QColor(color))
            else:
                item.setForeground(3, QtGui.QColor(COLOR_MUTED))
                item.setForeground(4, QtGui.QColor(COLOR_MUTED))
            self.flow_list.addTopLevelItem(item)
        self.flow_list.setSortingEnabled(True)
        self.flow_list.sortByColumn(2, QtCore.Qt.DescendingOrder)
        for col in range(self.flow_list.columnCount()):
            self.flow_list.resizeColumnToContents(col)

    # ---------- 個股量比異常清單（今日量 ÷ 近N日均量，逐檔股票各自比較自己的
    # 歷史均量，抓「個股」層級的爆量，不像資金動向清單是整個產業加總後的量比，
    # 單一檔股票爆量會被同產業其他股票稀釋掉） ----------

    def _build_volume_outliers_section(self, layout):
        header = QtWidgets.QLabel("個股量比異常清單")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("均量天數："))
        self.outlier_days_spin = QtWidgets.QSpinBox()
        self.outlier_days_spin.setRange(2, 60)
        self.outlier_days_spin.setValue(5)
        self.outlier_days_spin.setSuffix(" 天")
        controls.addWidget(self.outlier_days_spin)
        controls.addWidget(accent_button("套用", self._on_outlier_days_apply))

        controls.addSpacing(16)
        controls.addWidget(QtWidgets.QLabel("級距："))
        self.outlier_tier_combo = QtWidgets.QComboBox()
        self.outlier_tier_combo.addItem("全部", None)
        for tier in VOLUME_RATIO_TIERS:
            self.outlier_tier_combo.addItem(tier, tier)
        self.outlier_tier_combo.currentIndexChanged.connect(self._on_outlier_tier_filter_changed)
        controls.addWidget(self.outlier_tier_combo)
        controls.addStretch(1)
        layout.addLayout(controls)

        hint = QtWidgets.QLabel(
            "今日成交量 ÷ 近N日均量 ≥ 1.5 倍的個股，依比值分成 1.5～2倍／2～3倍／3倍以上；"
            "雙擊股票查詢本益比／殖利率／三大法人買賣超（同「個股」分頁，走 FinMind）。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)

        self.outlier_avg_days = self.outlier_days_spin.value()
        self.outlier_tier_filter = None
        self.volume_outliers_list = QtWidgets.QTreeWidget()
        self.volume_outliers_list.setColumnCount(7)
        self.volume_outliers_list.setHeaderLabels(
            ["級距", "代號", "名稱", "現價", "漲跌%", "量比", "今日量(張)"]
        )
        self.volume_outliers_list.setRootIsDecorated(False)
        self.volume_outliers_list.setAlternatingRowColors(True)
        self.volume_outliers_list.setSortingEnabled(True)
        self.volume_outliers_list.setMinimumHeight(280)
        self.volume_outliers_list.itemDoubleClicked.connect(
            self._on_volume_outlier_double_clicked
        )
        layout.addWidget(self.volume_outliers_list)

    def _on_outlier_days_apply(self):
        requested_days = self.outlier_days_spin.value()
        status = get_history_status(HISTORY_DB_PATH)
        available_days = status["overall"]["days"]
        if available_days and requested_days > available_days:
            QtWidgets.QMessageBox.warning(
                self,
                "資料可能不足",
                f"選擇的均量天數是 {requested_days} 天，但資料庫目前只有 {available_days} "
                f"個交易日資料，符合的個股可能會偏少。\n\n"
                "可至「設定」分頁調整回補天數，或按「回補歷史資料」補齊後再試。",
            )
        self.outlier_avg_days = requested_days
        self.refresh_volume_outliers_list()

    def _on_outlier_tier_filter_changed(self, _index):
        self.outlier_tier_filter = self.outlier_tier_combo.currentData()
        self.refresh_volume_outliers_list()

    def refresh_volume_outliers_list(self):
        outliers = compute_volume_ratio_outliers(
            HISTORY_DB_PATH, self.snapshot, avg_days=self.outlier_avg_days, min_ratio=1.5
        )
        if self.outlier_tier_filter:
            outliers = [o for o in outliers if o.tier == self.outlier_tier_filter]

        tier_colors = {
            "3倍以上": COLOR_LOSS,
            "2～3倍": COLOR_ACCENT,
            "1.5～2倍": COLOR_MUTED,
        }

        self.volume_outliers_list.setSortingEnabled(False)
        self.volume_outliers_list.clear()
        for o in outliers:
            change_text = f"{o.change_pct:+.2f}%" if o.change_pct is not None else "-"
            item = QtWidgets.QTreeWidgetItem(
                [
                    o.tier,
                    o.ticker,
                    o.name,
                    f"{o.close:.2f}" if o.close is not None else "-",
                    change_text,
                    f"{o.volume_ratio:.2f}",
                    f"{o.today_volume / 1000:,.0f}",
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, o.ticker)
            item.setData(0, QtCore.Qt.UserRole + 1, o.market)
            for col in (1, 2, 3, 4, 5, 6):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            item.setForeground(0, QtGui.QColor(tier_colors[o.tier]))
            if o.change_pct is not None:
                color = COLOR_GAIN if o.change_pct >= 0 else COLOR_LOSS
                item.setForeground(4, QtGui.QColor(color))
            self.volume_outliers_list.addTopLevelItem(item)
        self.volume_outliers_list.setSortingEnabled(True)
        self.volume_outliers_list.sortByColumn(5, QtCore.Qt.DescendingOrder)
        for col in range(self.volume_outliers_list.columnCount()):
            self.volume_outliers_list.resizeColumnToContents(col)

    def _on_volume_outlier_double_clicked(self, item, _column):
        ticker = item.data(0, QtCore.Qt.UserRole)
        if not ticker:
            return
        name = item.text(2)
        market = item.data(0, QtCore.Qt.UserRole + 1)
        token = self.settings.get("finmind_token", "")
        StockDetailDialog(self, ticker, name, token, market=market).exec()

    def open_backfill_dialog(self):
        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)

        def on_complete():
            self.refresh_flow_tab()
            self._refresh_history_status()

        dialog = BackfillDialog(self, target_days=target_days, on_complete=on_complete)
        dialog.exec()

    def open_tpex_backfill_dialog(self):
        token = self.settings.get("finmind_token", "")
        if not token:
            QtWidgets.QMessageBox.information(
                self,
                "提示",
                "請先在「設定」分頁填入 FinMind API Token 才能使用這個功能。",
            )
            return

        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)

        def on_complete():
            self.refresh_flow_tab()
            self._refresh_history_status()
            self.update_finmind_count_label()

        dialog = TpexBackfillDialog(self, token, target_days=target_days, on_complete=on_complete)
        dialog.exec()

    def _sync_history_continuity(self):
        def on_progress(done, target):
            self.continuity_note = f"同步歷史資料中... {done}/{target} 天"
            self._update_status_bar()

        def on_done(done):
            had_progress = bool(self.continuity_note)
            self.continuity_note = ""
            self._update_status_bar()
            if had_progress:
                self.refresh_flow_tab()
                self._refresh_history_status()

        def on_error(_message):
            self.continuity_note = ""
            self._update_status_bar()

        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)
        self._sync_timer = run_backfill_in_thread(
            self, target_days, on_progress, on_done, on_error
        )

    # ---------- 部位紀錄頁 ----------

    def _build_positions_tab(self):
        layout = QtWidgets.QVBoxLayout(self.positions_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(accent_button("新增部位", self.open_add_dialog))
        toolbar.addWidget(QtWidgets.QPushButton("編輯部位", clicked=self.open_edit_dialog))
        toolbar.addWidget(QtWidgets.QPushButton("刪除部位", clicked=self.delete_selected))
        toolbar.addWidget(QtWidgets.QPushButton("查價", clicked=self.open_price_dialog))
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        keys = [key for key, _, _ in COLUMNS]
        labels = [label for _, label, _ in COLUMNS]
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(labels)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        for i, (key, _, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, width)
        self._column_keys = keys
        self.table.itemSelectionChanged.connect(self._on_position_row_selected)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self._build_position_detail_section())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

    def _build_position_detail_section(self):
        """選取上方某筆部位時顯示的詳細資訊區塊：族群／概念股（本地資料，
        即時顯示，見 self._ticker_concept_map／get_industry_map）＋ FinMind
        本益比／殖利率／三大法人買賣超／融資融券餘額／外資持股／借券／停資停券
        （背景查詢，見 _load_position_detail）。跟「個股」頁的 StockDetailDialog
        不同，這裡不彈窗，直接嵌在部位紀錄頁裡。文字摘要＋兩張趨勢圖疊起來可能
        比下方分割區塊的可視高度還高，用 QScrollArea 包起來讓內容用捲軸瀏覽
        （跟「資金流向分析」頁 _build_flow_tab 同一招），而不是把圖硬擠壓變形。"""
        container = QtWidgets.QWidget()
        outer_layout = QtWidgets.QVBoxLayout(container)
        outer_layout.setContentsMargins(0, 10, 0, 0)

        header = QtWidgets.QLabel(
            "個股詳細資訊（選取上方部位查看，含 FinMind 歷史股價、三大法人120日"
            "資金流向、融資融券餘額、外資持股、借券與停資停券、VPT 量價趨勢、"
            "MFI 資金流量）"
        )
        header.setProperty("header", True)
        outer_layout.addWidget(header)
        outer_layout.addSpacing(4)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        outer_layout.addWidget(scroll)

        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)

        self.position_detail_label = QtWidgets.QLabel("尚未選取部位。")
        self.position_detail_label.setWordWrap(True)
        layout.addWidget(self.position_detail_label)

        self.position_price_chart = pg.PlotWidget()
        self.position_price_chart.setBackground(COLOR_BG)
        self.position_price_chart.showGrid(x=True, y=True, alpha=0.15)
        self.position_price_chart.setMinimumHeight(200)
        self.position_price_chart.addLegend()
        layout.addWidget(self.position_price_chart)

        self.position_flow_chart = pg.PlotWidget()
        self.position_flow_chart.setBackground(COLOR_BG)
        self.position_flow_chart.showGrid(x=True, y=True, alpha=0.15)
        self.position_flow_chart.setMinimumHeight(200)
        self.position_flow_chart.addLegend()
        layout.addWidget(self.position_flow_chart)

        self.position_margin_chart = pg.PlotWidget()
        self.position_margin_chart.setBackground(COLOR_BG)
        self.position_margin_chart.showGrid(x=True, y=True, alpha=0.15)
        self.position_margin_chart.setMinimumHeight(200)
        self.position_margin_chart.addLegend()
        layout.addWidget(self.position_margin_chart)

        self.position_vpt_chart = pg.PlotWidget()
        self.position_vpt_chart.setBackground(COLOR_BG)
        self.position_vpt_chart.showGrid(x=True, y=True, alpha=0.15)
        self.position_vpt_chart.setMinimumHeight(200)
        self.position_vpt_chart.addLegend()
        layout.addWidget(self.position_vpt_chart)

        self.position_mfi_chart = pg.PlotWidget()
        self.position_mfi_chart.setBackground(COLOR_BG)
        self.position_mfi_chart.showGrid(x=True, y=True, alpha=0.15)
        self.position_mfi_chart.setMinimumHeight(200)
        self.position_mfi_chart.addLegend()
        layout.addWidget(self.position_mfi_chart)

        return container

    def refresh_table(self):
        total_pnl = 0.0
        has_pnl = False
        self.table.setRowCount(len(self.positions))
        try:
            industry_map = get_industry_map(HISTORY_DB_PATH)
        except PriceFetchError:
            # 族群只是輔助資訊，抓不到（例如離線、快取剛好過期又連不上網）不該
            # 讓整個部位表格連損益都顯示不出來，各列的「族群」直接落回「-」。
            industry_map = {}

        for row_index, pos in enumerate(self.positions):
            price = lookup_price(pos.ticker, self.snapshot)
            pnl = compute_pnl(pos, price)
            if price is not None and price.name and price.name != pos.name:
                pos.name = price.name
                pos.market = price.market

            # 權益成本只跟股數／成本價有關，不需要即時報價，price 抓不到時仍然算得出來；
            # 權益現值（=股數×現價）則跟損益一樣依賴 price，抓不到時比照顯示 N/A。
            equity_cost = pos.shares * pos.entry_price

            if pnl is None:
                current_price, pnl_amt, pnl_pct, color = "N/A", "N/A", "N/A", None
                equity_value = "N/A"
            else:
                current_price = f"{pnl.current_price:.2f}"
                pnl_amt = f"{pnl.unrealized_pnl:+.0f}"
                pnl_pct = f"{pnl.pnl_pct:+.2f}%"
                color = COLOR_GAIN if pnl.unrealized_pnl >= 0 else COLOR_LOSS
                equity_value = f"{pnl.current_value:,.0f}"
                total_pnl += pnl.unrealized_pnl
                has_pnl = True

            concepts = self._ticker_concept_map.get(pos.ticker) or []

            values = {
                "id": pos.id,
                "ticker": pos.ticker,
                "name": pos.name or "-",
                "industry": industry_map.get(pos.ticker) or "-",
                "concepts": "、".join(concepts) if concepts else "-",
                "shares": str(pos.shares),
                "entry_price": f"{pos.entry_price:.2f}",
                "current_price": current_price,
                "equity_cost": f"{equity_cost:,.0f}",
                "equity_value": equity_value,
                "pnl": pnl_amt,
                "pnl_pct": pnl_pct,
                "note": pos.note,
                "updated_at": pos.updated_at or "-",
            }
            for col, key in enumerate(self._column_keys):
                item = QtWidgets.QTableWidgetItem(values[key])
                if key in ("id", "ticker"):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                if key in ("pnl", "pnl_pct") and color:
                    item.setForeground(QtGui.QColor(color))
                self.table.setItem(row_index, col, item)

        save_positions(POSITIONS_PATH, self.positions)
        self._total_pnl = total_pnl if has_pnl else None
        self._update_status_bar()

    # ---------- 部位詳細資訊區塊：族群／概念股（本地）＋ FinMind 基本面／
    # 三大法人120日趨勢（背景查詢，見 tradingnote_finmind.py） ----------

    def _on_position_row_selected(self):
        position_id = self._selected_position_id()
        if position_id is None:
            self.position_detail_label.setText("尚未選取部位。")
            self.position_price_chart.clear()
            self.position_flow_chart.clear()
            self.position_margin_chart.clear()
            self.position_vpt_chart.clear()
            self.position_mfi_chart.clear()
            return
        pos = find_position(self.positions, position_id)
        if pos is None:
            return
        self._load_position_detail(pos)

    def _load_position_detail(self, pos):
        try:
            industry = get_industry_map(HISTORY_DB_PATH).get(pos.ticker) or "未分類"
        except PriceFetchError:
            industry = "未分類"
        concepts = self._ticker_concept_map.get(pos.ticker) or []
        concept_text = "、".join(concepts) if concepts else "（尚無分類，可編輯 concepts.json 新增）"
        header = f"{pos.ticker} {pos.name or ''}　族群：{industry}　概念股：{concept_text}"

        # 先看 position_detail_cache.json 有沒有這檔股票上次查到、永久存下來的
        # 結果：有就先顯示（不用等這次背景查詢），沒有才顯示「查詢中」空白狀態。
        # 不管有沒有快取，下面都照樣背景重打一次 FinMind 拿最新資料、查到就覆寫
        # 快取檔——快取讓「隨時可以取用」，不是拿來取代查新資料。
        cached = load_position_detail_cache(POSITION_DETAIL_CACHE_PATH, pos.ticker)
        if cached is not None:
            note = f"（上次查詢：{_format_fetched_at(cached['fetched_at'])}，背景更新中...）"
            self._render_position_detail(header, cached, note)
        else:
            self.position_detail_label.setText(f"{header}\n\nFinMind 查詢中...")
            self.position_price_chart.clear()
            self.position_flow_chart.clear()
            self.position_margin_chart.clear()
            self.position_vpt_chart.clear()
            self.position_mfi_chart.clear()

        token = self.settings.get("finmind_token", "")
        market = pos.market

        def fetch():
            data = fetch_position_detail(pos.ticker, token, market=market)
            save_position_detail_cache(POSITION_DETAIL_CACHE_PATH, pos.ticker, data)
            return data

        self._position_detail_timer = run_task_in_thread(
            self,
            fetch,
            lambda result: self._on_position_detail_done(pos, header, result),
            lambda message: self._on_position_detail_error(pos, header, message, cached),
        )

    def _is_current_detail_target(self, pos):
        return self._selected_position_id() == pos.id

    def _on_position_detail_done(self, pos, header, result):
        if not self._is_current_detail_target(pos):
            return  # 使用者查詢途中已切換到別筆部位，這份結果過期了，不套用
        self._render_position_detail(header, result)
        self.update_finmind_count_label()

    def _render_position_detail(self, header, data, note=None):
        """畫「部位詳細資訊」文字摘要＋五張趨勢圖。實際畫面邏輯是模組層級的
        _render_detail_block（跟「個股」頁 StockDetailDialog 的「顯示完整籌碼
        面資訊」按鈕共用同一份），這裡只是把部位紀錄頁自己的 label／圖表
        widget 傳進去。"""
        _render_detail_block(
            self.position_detail_label,
            self.position_price_chart,
            self.position_flow_chart,
            self.position_margin_chart,
            self.position_vpt_chart,
            self.position_mfi_chart,
            header,
            data,
            note,
        )

    def _on_position_detail_error(self, pos, header, message, cached=None):
        if not self._is_current_detail_target(pos):
            return
        # 有上次的快取結果就繼續顯示它（只是把「背景更新中」的提示換成「更新
        # 失敗」），不要因為這次背景重查失敗就把已經在畫面上的舊資料清空——
        # 這正是「另外存放，隨時可以取用」要解決的情境：沒 token／超額度／
        # 沒網路時，至少還能看上次查到的結果，不是一片空白。
        if cached is not None:
            note = (
                f"（背景更新失敗：{message}；顯示上次查詢結果 "
                f"{_format_fetched_at(cached['fetched_at'])}）"
            )
            self._render_position_detail(header, cached, note)
            self.update_finmind_count_label()
            return
        self.position_detail_label.setText(
            f"{header}\n\nFinMind 查詢失敗：{message}\n\n"
            "可能原因：FinMind token 未設定或已失效、已超過免費額度，或該股票暫無此資料。"
        )
        self.position_price_chart.clear()
        self.position_flow_chart.clear()
        self.position_margin_chart.clear()
        self.position_vpt_chart.clear()
        self.position_mfi_chart.clear()
        self.update_finmind_count_label()

    def _update_status_bar(self):
        cache_note = ""
        if CACHE_PATH.exists():
            import json as _json

            with CACHE_PATH.open("r", encoding="utf-8") as f:
                fetched_at = _json.load(f).get("fetched_at", "")
            cache_note = f"價格更新時間：{fetched_at}"
        total_note = (
            f"總損益：{self._total_pnl:+.0f}" if self._total_pnl is not None else "總損益：N/A"
        )
        error_note = f"　⚠ {self.last_error}" if self.last_error else ""
        staleness_note = f"　⚠ {self.staleness_warning}" if self.staleness_warning else ""
        continuity_note = f"　{self.continuity_note}" if self.continuity_note else ""
        self.status_bar.showMessage(
            f"{cache_note}　{total_note}{error_note}{staleness_note}{continuity_note}"
        )

    def open_add_dialog(self):
        def on_submit(ticker, shares, entry_price, entry_date, note):
            add_position(
                self.positions, ticker, shares, entry_price, entry_date, note, self.snapshot
            )
            self.refresh_table()

        PositionFormDialog(self, on_submit).exec()

    def _selected_position_id(self):
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        row = selected[0].row()
        return self.table.item(row, self._column_keys.index("id")).text()

    def open_edit_dialog(self):
        position_id = self._selected_position_id()
        if position_id is None:
            QtWidgets.QMessageBox.information(self, "提示", "請先選擇要編輯的部位。")
            return
        pos = find_position(self.positions, position_id)
        if pos is None:
            QtWidgets.QMessageBox.warning(
                self, "錯誤", "找不到這個部位，畫面可能已過期，請至「設定」分頁按「重新整理所有資料」。"
            )
            return

        def on_submit(_ticker, shares, entry_price, entry_date, note):
            update_position(
                self.positions, position_id, shares, entry_price, entry_date, note, self.snapshot
            )
            self.refresh_table()

        PositionFormDialog(self, on_submit, position=pos).exec()

    def delete_selected(self):
        position_id = self._selected_position_id()
        if position_id is None:
            QtWidgets.QMessageBox.information(self, "提示", "請先選擇要刪除的部位。")
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "確認刪除",
            f"確定要刪除部位 {position_id} 嗎？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        remove_position(self.positions, position_id)
        self.refresh_table()

    def open_price_dialog(self):
        PriceLookupDialog(self, self.snapshot).exec()

    def force_refresh(self):
        RefreshDialog(self, self._apply_refresh_result).exec()

    def _apply_refresh_result(self, snapshot, error):
        if snapshot is not None:
            self.snapshot = snapshot
            self.last_error = None
            self.staleness_warning = "；".join(snapshot_staleness_warnings(snapshot))
            self._known_data_date = _snapshot_date(snapshot) or self._known_data_date
            self._dismissed_data_date = None
            self.new_data_banner.setVisible(False)
        else:
            self.last_error = error
        self.refresh_table()
        self.refresh_flow_tab()
        self.refresh_stocks_tab()
        self.refresh_futures_tab(force_refresh=True)
        self._refresh_data_freshness_label()
        self._refresh_history_status()

    # ---------- 個股頁 ----------

    def _build_stocks_tab(self):
        layout = QtWidgets.QVBoxLayout(self.stocks_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("搜尋"))
        self.stock_search_edit = QtWidgets.QLineEdit()
        self.stock_search_edit.setPlaceholderText("輸入代號或名稱")
        self.stock_search_edit.setMaximumWidth(200)
        self.stock_search_edit.textChanged.connect(self._filter_stocks_tree)
        toolbar.addWidget(self.stock_search_edit)

        hint = QtWidgets.QLabel(
            "雙擊股票查詢本益比／法人買賣（上市透過 FinMind API，上櫃本益比／殖利率"
            "改查 TPEX 官方端點；法人買賣不分市場皆為 FinMind）"
        )
        hint.setProperty("muted", True)
        toolbar.addWidget(hint)
        toolbar.addStretch(1)

        self.finmind_count_label = QtWidgets.QLabel()
        self.finmind_count_label.setProperty("muted", True)
        toolbar.addWidget(self.finmind_count_label)
        layout.addLayout(toolbar)
        self.update_finmind_count_label()

        self.stocks_tree = QtWidgets.QTreeWidget()
        self.stocks_tree.setColumnCount(4)
        self.stocks_tree.setHeaderLabels(["名稱", "代號", "現價", "漲跌%"])
        self.stocks_tree.setColumnWidth(0, 160)
        self.stocks_tree.setColumnWidth(1, 80)
        self.stocks_tree.setColumnWidth(2, 90)
        self.stocks_tree.itemDoubleClicked.connect(self._on_stock_double_clicked)
        layout.addWidget(self.stocks_tree)

    def refresh_stocks_tab(self):
        try:
            directory = get_industry_directory(HISTORY_DB_PATH)
        except PriceFetchError as e:
            QtWidgets.QMessageBox.warning(self, "錯誤", f"無法取得產業分類資料：{e}")
            return

        groups = {}
        for ticker, info in directory.items():
            groups.setdefault(info["industry"] or "未分類", []).append((ticker, info))

        expanded = {
            self.stocks_tree.topLevelItem(i).text(0).split("（")[0]
            for i in range(self.stocks_tree.topLevelItemCount())
            if self.stocks_tree.topLevelItem(i).isExpanded()
        }

        self.stocks_tree.clear()
        for industry in sorted(groups):
            tickers = groups[industry]
            industry_item = QtWidgets.QTreeWidgetItem([f"{industry}（{len(tickers)}）"])
            self.stocks_tree.addTopLevelItem(industry_item)
            industry_item.setExpanded(industry in expanded)

            for ticker, info in sorted(tickers, key=lambda t: t[0]):
                price = self.snapshot.get(ticker)
                if price is not None and price.close is not None:
                    close_text = f"{price.close:.2f}"
                    change_pct = _change_pct(price)
                    change_text = f"{change_pct:+.2f}%" if change_pct is not None else "-"
                else:
                    close_text = "-"
                    change_text = "-"
                name = (price.name if price and price.name else None) or info["name"] or "-"

                # 欄位順序對齊 header：名稱／代號／現價／漲跌%
                child = QtWidgets.QTreeWidgetItem([name, ticker, close_text, change_text])
                child.setData(0, QtCore.Qt.UserRole, ticker)
                # UserRole + 1 存市場別（TWSE／TPEX），雙擊查詢時用來決定本益比／
                # 殖利率要查 FinMind 還是 TPEX 官方端點（見 StockDetailDialog）。
                child.setData(0, QtCore.Qt.UserRole + 1, info["market"])
                industry_item.addChild(child)

        self._filter_stocks_tree(self.stock_search_edit.text())

    def _filter_stocks_tree(self, text):
        """依代號／名稱關鍵字（不分大小寫、子字串比對）過濾樹狀列表：符合的個股
        顯示，其餘隱藏；產業分組列則依底下是否還有符合的個股決定顯示／隱藏，
        有輸入關鍵字時自動展開有符合結果的分組。"""
        keyword = text.strip().lower()
        for i in range(self.stocks_tree.topLevelItemCount()):
            industry_item = self.stocks_tree.topLevelItem(i)
            visible_count = 0
            for j in range(industry_item.childCount()):
                child = industry_item.child(j)
                matched = (
                    not keyword
                    or keyword in child.text(0).lower()
                    or keyword in child.text(1).lower()
                )
                child.setHidden(not matched)
                if matched:
                    visible_count += 1
            industry_item.setHidden(visible_count == 0)
            if keyword:
                industry_item.setExpanded(visible_count > 0)

    def update_finmind_count_label(self):
        """更新「個股」／「AI 助理」頁共用的 FinMind 用量提醒；免費額度是
        600 次／小時，接近或超過時把文字變色提醒，避免使用者查到一半才發現被 FinMind 擋掉。"""
        count = get_call_count()
        text = f"FinMind API 過去 1 小時：{count} / {FINMIND_HOURLY_LIMIT} 次"
        if count >= FINMIND_HOURLY_LIMIT:
            text += "　已達免費額度上限，查詢可能會失敗"
            color = COLOR_LOSS
        elif count >= FINMIND_HOURLY_LIMIT * 0.8:
            text += "　接近上限，請留意"
            color = COLOR_ACCENT
        else:
            color = COLOR_MUTED

        for label_name in (
            "finmind_count_label",
            "ai_agent_finmind_count_label",
        ):
            label = getattr(self, label_name, None)
            if label is not None:
                label.setText(text)
                label.setStyleSheet(f"color: {color};")

    def update_gemini_count_label(self):
        """更新「AI 助理」頁的 Gemini API 用量提醒；GEMINI_RPM_HINT 只是粗略
        參考值（Google 不保證固定，依帳號層級／模型調整），接近或超過時把文字
        變色提醒，避免使用者連續發問到一半才發現被 Gemini 擋掉（429）。"""
        label = getattr(self, "ai_agent_gemini_count_label", None)
        if label is None:
            return
        count = get_gemini_call_count()
        text = f"Gemini API 過去 1 分鐘：{count} / 約 {GEMINI_RPM_HINT} 次"
        if count >= GEMINI_RPM_HINT:
            text += "　可能已達每分鐘額度上限，稍等再試"
            color = COLOR_LOSS
        elif count >= GEMINI_RPM_HINT * 0.8:
            text += "　接近上限，請留意"
            color = COLOR_ACCENT
        else:
            color = COLOR_MUTED
        label.setText(text)
        label.setStyleSheet(f"color: {color};")

    def _on_stock_double_clicked(self, item, _column):
        ticker = item.data(0, QtCore.Qt.UserRole)
        if not ticker:
            return  # 點到的是產業分組列，不是個股
        name = item.text(1)
        market = item.data(0, QtCore.Qt.UserRole + 1)
        token = self.settings.get("finmind_token", "")
        StockDetailDialog(self, ticker, name, token, market=market).exec()

    # ---------- 期貨頁（TAIFEX 官方盤後行情） ----------
    # 原本串接 Fugle 的 data-futopt 即時行情，但查證 developer.fugle.tw 定價文件
    # 後發現期貨/選擇權完全不在免費方案內（連歷史/盤後資料都沒有，只有 intraday
    # 且需要付費 Developer 方案）。改用 TAIFEX 官方公開、免金鑰的「期貨每日交易
    # 行情」，只反映盤後（EOD）資訊，不是即時報價——跟 TWSE/TPEX 股票走 EOD 的既有
    # 原則一致，見 tradingnote_taifex.py。一天只更新一次，不需要背景輪詢計時器，
    # 隨「重新整理」（見 _apply_refresh_result）跟股票資料一起刷新即可。

    FUTURES_COLUMNS = [
        "商品", "契約月份", "時段", "最後成交", "漲跌", "漲跌%",
        "結算價", "成交量", "未沖銷契約數",
    ]
    FUTURES_SESSION_ORDER = ["一般", "盤後"]

    def _build_futures_tab(self):
        layout = QtWidgets.QVBoxLayout(self.futures_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("搜尋"))
        self.futures_search_edit = QtWidgets.QLineEdit()
        self.futures_search_edit.setPlaceholderText("輸入期貨代號，如 TX／MTX／TE")
        self.futures_search_edit.setMaximumWidth(200)
        self.futures_search_edit.textChanged.connect(self._filter_futures_table)
        toolbar.addWidget(self.futures_search_edit)

        hint = QtWidgets.QLabel(
            "預設顯示近月指數期貨（TX 臺股期貨／MTX 小型臺指期貨）盤後資訊，輸入代號可"
            "搜尋 TAIFEX 全部期貨商品。資料來自 TAIFEX 官方「期貨每日交易行情」，免金鑰、"
            "每個交易日更新一次，非即時報價——「一般」為日盤收盤後的彙總、「盤後」為夜盤"
            "收盤後的彙總。跟其他資料一起在「設定」分頁按「重新整理所有資料」更新。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        toolbar.addWidget(hint)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.futures_status_label = QtWidgets.QLabel("")
        self.futures_status_label.setProperty("muted", True)
        layout.addWidget(self.futures_status_label)

        self.futures_table = QtWidgets.QTableWidget()
        self.futures_table.setColumnCount(len(self.FUTURES_COLUMNS))
        self.futures_table.setHorizontalHeaderLabels(self.FUTURES_COLUMNS)
        self.futures_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.futures_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        for col, width in enumerate([70, 90, 60, 90, 80, 70, 90, 90, 110]):
            self.futures_table.setColumnWidth(col, width)
        layout.addWidget(self.futures_table)

        self._futures_snapshot = {}
        self._futures_all_products = list(DEFAULT_FUTURES_PRODUCTS)
        self._rebuild_futures_table()

    def _rebuild_futures_table(self):
        rows = [
            (product, session)
            for product in self._futures_all_products
            for session in self.FUTURES_SESSION_ORDER
            if session in self._futures_snapshot.get(product, {})
        ]
        self._futures_rows = rows
        self.futures_table.setRowCount(len(rows))
        for index in range(len(rows)):
            self._paint_futures_row(index)
        self._filter_futures_table(self.futures_search_edit.text())

    def _paint_futures_row(self, index):
        product, session = self._futures_rows[index]
        data = self._futures_snapshot[product][session]

        contract_month = data.get("contract_month") or "-"
        last = data.get("last")
        last_text = f"{last:,.0f}" if last is not None else "-"
        change = data.get("change")
        change_text = f"{change:+,.0f}" if change is not None else "-"
        settlement = data.get("settlement_price")
        settlement_text = f"{settlement:,.0f}" if settlement is not None else "-"
        volume = data.get("volume")
        volume_text = f"{volume:,}" if volume is not None else "-"
        open_interest = data.get("open_interest")
        oi_text = f"{open_interest:,}" if open_interest is not None else "-"
        change_pct = data.get("change_pct")
        change_pct_text = f"{change_pct:+.2f}%" if change_pct is not None else "-"

        color = COLOR_GAIN if (change is not None and change >= 0) else (
            COLOR_LOSS if change is not None else None
        )

        values = [
            product, contract_month, session,
            last_text, change_text, change_pct_text,
            settlement_text, volume_text, oi_text,
        ]
        for col, text in enumerate(values):
            item = QtWidgets.QTableWidgetItem(text)
            if col in (3, 4) and color:
                item.setForeground(QtGui.QColor(color))
            self.futures_table.setItem(index, col, item)

    def refresh_futures_tab(self, force_refresh=False):
        def fetch():
            rows = get_cached_daily_futures_report(FUTURES_CACHE_PATH, force_refresh=force_refresh)
            all_products = list_all_products(rows)
            return all_products, get_futures_snapshot(all_products, rows=rows)

        def on_done(result):
            all_products, snapshot = result
            self._futures_all_products = all_products
            self._futures_snapshot = snapshot
            data_date = _futures_snapshot_date(snapshot)
            self.futures_status_label.setText(f"資料日期：{data_date}" if data_date else "")
            self._rebuild_futures_table()
            self._refresh_data_freshness_label()

        def on_error(message):
            self.futures_status_label.setText(f"期貨盤後資訊取得失敗：{message}")

        self._futures_task_timer = run_task_in_thread(self, fetch, on_done, on_error)

    def _filter_futures_table(self, text):
        """依商品代碼關鍵字（不分大小寫、子字串比對）篩選期貨表格：關鍵字為空
        時只顯示預設商品（DEFAULT_FUTURES_PRODUCTS，即 TX／MTX），輸入關鍵字
        後改成在全部商品中比對，符合的列顯示、其餘隱藏。"""
        keyword = text.strip().lower()
        for index, (product, _session) in enumerate(self._futures_rows):
            if keyword:
                matched = keyword in product.lower()
            else:
                matched = product in DEFAULT_FUTURES_PRODUCTS
            self.futures_table.setRowHidden(index, not matched)

    # ---------- AI 助理頁 ----------

    def _build_ai_agent_tab(self):
        """真正串接 Gemini API 的自然語言問答（google-genai 自動函式呼叫）：模型自行
        判斷要不要呼叫 search_ticker／get_price／get_valuation／
        get_institutional_investors 這幾個工具，底層查詢跟其他分頁共用同一批
        FinMind／歷史資料庫函式。跟「AI 查詢」（純關鍵字規則比對）不同，這裡
        每次對話都會打 Gemini API，需要在設定頁填入 Gemini API Key，超過免費額度會產生費用。"""
        layout = QtWidgets.QVBoxLayout(self.ai_agent_tab)
        layout.setContentsMargins(20, 20, 20, 20)

        header_row = QtWidgets.QHBoxLayout()
        header = QtWidgets.QLabel("AI 助理")
        header.setProperty("header", True)
        header_row.addWidget(header)
        header_row.addStretch(1)
        self.ai_agent_finmind_count_label = QtWidgets.QLabel()
        self.ai_agent_finmind_count_label.setProperty("muted", True)
        header_row.addWidget(self.ai_agent_finmind_count_label)
        self.ai_agent_gemini_count_label = QtWidgets.QLabel()
        self.ai_agent_gemini_count_label.setProperty("muted", True)
        header_row.addWidget(self.ai_agent_gemini_count_label)
        layout.addLayout(header_row)

        hint = QtWidgets.QLabel(
            "用自然語言提問，例如「台積電最近的本益比和法人買賣超」，"
            "由 Gemini 自行判斷要查什麼資料。需先在「設定」分頁填入 Gemini API Key，"
            "每次對話會呼叫 Gemini API（超過免費額度會產生費用）。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(10)

        self.ai_agent_chat = QtWidgets.QTextEdit()
        self.ai_agent_chat.setReadOnly(True)
        layout.addWidget(self.ai_agent_chat, stretch=1)

        self.ai_agent_status_label = QtWidgets.QLabel()
        self.ai_agent_status_label.setProperty("muted", True)
        layout.addWidget(self.ai_agent_status_label)

        input_row = QtWidgets.QHBoxLayout()
        self.ai_agent_input = QtWidgets.QLineEdit()
        self.ai_agent_input.setPlaceholderText("輸入問題...")
        self.ai_agent_input.returnPressed.connect(self._send_ai_agent_message)
        input_row.addWidget(self.ai_agent_input)
        input_row.addWidget(accent_button("送出", self._send_ai_agent_message))
        input_row.addWidget(
            QtWidgets.QPushButton("清除對話", clicked=self._clear_ai_agent_chat)
        )
        layout.addLayout(input_row)

        self.ai_agent_history = None
        self.update_finmind_count_label()
        self.update_gemini_count_label()

    def _append_ai_agent_chat(self, speaker, text):
        safe_text = html.escape(text).replace("\n", "<br>")
        self.ai_agent_chat.append(f"<b>{html.escape(speaker)}：</b>{safe_text}<br>")

    def _clear_ai_agent_chat(self):
        self.ai_agent_history = None
        self.ai_agent_chat.clear()
        self.ai_agent_status_label.setText("")

    def _send_ai_agent_message(self):
        text = self.ai_agent_input.text().strip()
        if not text:
            return
        self.ai_agent_input.clear()
        self._append_ai_agent_chat("你", text)

        try:
            directory = get_industry_directory(HISTORY_DB_PATH)
        except PriceFetchError as e:
            self._append_ai_agent_chat("系統", f"無法取得股票名冊：{e}")
            return

        api_key = self.settings.get("gemini_api_key", "")
        finmind_token = self.settings.get("finmind_token", "")
        history_snapshot = self.ai_agent_history

        self.ai_agent_status_label.setText("查詢中...")
        self.ai_agent_input.setEnabled(False)

        def work():
            return run_agent_turn(
                api_key, history_snapshot, text, directory, HISTORY_DB_PATH, finmind_token
            )

        self._ai_agent_timer = run_task_in_thread(
            self, work, self._on_ai_agent_done, self._on_ai_agent_error
        )

    def _on_ai_agent_done(self, result):
        reply, new_history = result
        self.ai_agent_history = new_history
        self._append_ai_agent_chat("助理", reply)
        self.ai_agent_status_label.setText("")
        self.ai_agent_input.setEnabled(True)
        self.ai_agent_input.setFocus()
        self.update_finmind_count_label()
        self.update_gemini_count_label()

    def _on_ai_agent_error(self, message):
        self._append_ai_agent_chat("系統", f"發生錯誤：{message}")
        self.ai_agent_status_label.setText("")
        self.ai_agent_input.setEnabled(True)
        self.update_finmind_count_label()
        self.update_gemini_count_label()

    # ---------- 設定頁 ----------

    def _build_settings_tab(self):
        layout = QtWidgets.QVBoxLayout(self.settings_tab)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setAlignment(QtCore.Qt.AlignTop)

        header = QtWidgets.QLabel("資料庫")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        # 全部的「重新整理」統一到這裡：原本資金流向分析／部位紀錄／期貨三個分頁
        # 各有一顆重複的按鈕，其實都是呼叫同一個 force_refresh（期貨在
        # _apply_refresh_result 裡跟著一起刷新），拆開放在各分頁反而讓人以為
        # 是三個獨立的資料來源；集中在這裡＋下方即時顯示各類資料實際的最新日期，
        # 使用者才看得出「重新整理」到底有沒有真的抓到新資料。
        layout.addWidget(
            accent_button("重新整理所有資料", self.force_refresh),
            alignment=QtCore.Qt.AlignLeft,
        )
        layout.addSpacing(4)

        self.data_freshness_label = QtWidgets.QLabel("")
        self.data_freshness_label.setProperty("muted", True)
        self.data_freshness_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.data_freshness_label)
        self._refresh_data_freshness_label()
        layout.addSpacing(20)

        self.auto_check_box = QtWidgets.QCheckBox(
            "每次啟動自動檢測（TWSE 歷史資料有缺口時自動補齊）"
        )
        self.auto_check_box.setChecked(self.settings.get("auto_check_continuity", True))
        self.auto_check_box.toggled.connect(self._on_auto_check_toggle)
        layout.addWidget(self.auto_check_box)

        hint = QtWidgets.QLabel("關閉後，仍可用下方按鈕手動回補。")
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        layout.addSpacing(10)

        backfill_days_row = QtWidgets.QHBoxLayout()
        backfill_days_row.addWidget(QtWidgets.QLabel("回補天數（上市 TWSE／上櫃 TPEX 共用）"))
        self.backfill_days_spin = QtWidgets.QSpinBox()
        self.backfill_days_spin.setRange(20, 500)
        self.backfill_days_spin.setSingleStep(10)
        self.backfill_days_spin.setSuffix(" 天")
        self.backfill_days_spin.setValue(
            self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)
        )
        self.backfill_days_spin.valueChanged.connect(self._on_backfill_days_changed)
        backfill_days_row.addWidget(self.backfill_days_spin)
        backfill_days_row.addStretch(1)
        layout.addLayout(backfill_days_row)

        backfill_days_hint = QtWidgets.QLabel(
            "上市（TWSE）按「回補歷史資料」，走官方免費端點，逐日回補。"
            "上櫃（TPEX）沒有官方免費歷史端點，改按「使用 FinMind 補上櫃缺口」，"
            "逐檔呼叫 FinMind（需先設定好 finmind_token）；免費額度 600 次／小時，"
            "全市場上櫃約 800 檔，一次通常補不完，額度用完會自動暫停並記錄進度，"
            "之後再按一次即可接續，不會重補。調整天數後，下次按按鈕或自動同步才會套用新天數。"
        )
        backfill_days_hint.setProperty("muted", True)
        backfill_days_hint.setWordWrap(True)
        layout.addWidget(backfill_days_hint)
        layout.addSpacing(10)

        backfill_buttons_row = QtWidgets.QHBoxLayout()
        backfill_buttons_row.addWidget(
            accent_button("回補歷史資料", self.open_backfill_dialog)
        )
        backfill_buttons_row.addWidget(
            QtWidgets.QPushButton("使用 FinMind 補上櫃缺口", clicked=self.open_tpex_backfill_dialog)
        )
        backfill_buttons_row.addStretch(1)
        layout.addLayout(backfill_buttons_row)

        layout.addSpacing(20)
        status_header = QtWidgets.QLabel("資料庫狀態")
        status_header.setProperty("header", True)
        layout.addWidget(status_header)
        layout.addSpacing(4)

        self.history_status_label = QtWidgets.QLabel("")
        self.history_status_label.setWordWrap(True)
        self.history_status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.history_status_label)
        self._refresh_history_status()

        layout.addSpacing(20)
        gemini_header = QtWidgets.QLabel("Gemini API Key")
        gemini_header.setProperty("header", True)
        layout.addWidget(gemini_header)
        layout.addSpacing(4)

        gemini_hint = QtWidgets.QLabel(
            "「AI 助理」分頁用自然語言問答時使用（呼叫 Gemini API，超過免費額度會產生費用）。"
            "至 aistudio.google.com/apikey 建立 API key；留空則該分頁無法使用。"
        )
        gemini_hint.setProperty("muted", True)
        gemini_hint.setWordWrap(True)
        layout.addWidget(gemini_hint)

        gemini_row = QtWidgets.QHBoxLayout()
        gemini_row.addWidget(QtWidgets.QLabel("API Key"))
        self.gemini_key_edit = QtWidgets.QLineEdit(
            self.settings.get("gemini_api_key", "")
        )
        self.gemini_key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.gemini_key_edit.setMinimumWidth(320)
        self.gemini_key_edit.editingFinished.connect(self._on_gemini_key_changed)
        gemini_row.addWidget(self.gemini_key_edit)

        self.gemini_key_show = QtWidgets.QCheckBox("顯示")
        self.gemini_key_show.toggled.connect(
            lambda checked: self.gemini_key_edit.setEchoMode(
                QtWidgets.QLineEdit.Normal if checked else QtWidgets.QLineEdit.Password
            )
        )
        gemini_row.addWidget(self.gemini_key_show)
        gemini_row.addStretch(1)
        layout.addLayout(gemini_row)

    def _on_auto_check_toggle(self, checked):
        self.settings["auto_check_continuity"] = checked
        save_settings(SETTINGS_PATH, self.settings)

    def _on_backfill_days_changed(self, value):
        self.settings["backfill_target_days"] = value
        save_settings(SETTINGS_PATH, self.settings)

    def _refresh_data_freshness_label(self):
        """顯示各類資料「實際擷取到的最新一筆資料日期」，不是「上次按重新整理的
        時間」——重新整理有可能因為連線失敗、或當天 TWSE/TPEX/TAIFEX 還沒發布新
        資料而拿到跟之前一樣的日期，這裡一律以資料本身的日期欄位為準（_snapshot_date
        取 PriceInfo.date 眾數、_futures_snapshot_date 取 TAIFEX 回傳的 Date 欄位）。"""
        stock_date = _snapshot_date(self.snapshot) or "尚無資料"
        futures_date = _futures_snapshot_date(getattr(self, "_futures_snapshot", {})) or "尚無資料"
        self.data_freshness_label.setText(
            f"台股即時報價（TWSE／TPEX）最新日期：{stock_date}\n"
            f"期貨盤後（TAIFEX 全商品）最新日期：{futures_date}"
        )

    def _refresh_history_status(self):
        """本地 SQLite 聚合查詢（COUNT／MIN／MAX，非逐列讀取），即使累積到 ~200 天、
        全市場資料量也只是毫秒等級，跟其他分頁的本地資料讀取一樣直接同步呼叫，
        不需要另外開背景執行緒。"""
        status = get_history_status(HISTORY_DB_PATH)
        self.history_status_label.setText(_format_history_status(status))

    def _on_gemini_key_changed(self):
        self.settings["gemini_api_key"] = self.gemini_key_edit.text().strip()
        save_settings(SETTINGS_PATH, self.settings)


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyleSheet(STYLESHEET)
    pg.setConfigOptions(antialias=True, background=COLOR_BG, foreground=COLOR_TEXT)

    splash = StartupProgressDialog()
    splash.show()

    # 保住 TradingNoteWindow 的參照：window 是在 on_ready 這個巢狀函式的區域變數
    # 建立的，on_ready 執行完就會結束，若不另外存進外層變數，Python 端的物件
    # 會被回收（即使已經 show() 出來），導致視窗一閃就消失。
    windows = []

    def on_ready(snapshot, last_error):
        splash.close()
        window = TradingNoteWindow(snapshot, last_error)
        windows.append(window)
        window.show()

    splash._timer = run_startup_preload_in_thread(
        splash,
        splash.set_progress,
        done_cb=lambda snapshot: on_ready(snapshot, None),
        error_cb=lambda message: on_ready({}, message),
    )

    app.exec()


if __name__ == "__main__":
    main()
