#!/usr/bin/env python3
"""tradingnote - 圖形介面版本（PySide6 + pyqtgraph）"""

import html
import statistics
from collections import Counter
from datetime import date, datetime, timedelta

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_core import (
    add_position,
    compute_pnl,
    fetch_tpex_valuation_all,
    fetch_twse_valuation_all,
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
    compute_valuation_flow,
    default_start_date_for_days,
    get_available_dates,
    get_history_status,
    get_industry_directory,
    get_industry_map,
    get_latest_ticker_record,
    record_snapshot,
    get_data_revision,
    record_valuation_snapshot,
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
from tradingnote_concepts import (
    CLASSIFICATION_INDUSTRY,
    CLASSIFICATION_LABELS,
    CLASSIFICATION_VALUE_CHAIN_LEAF,
    build_classification_catalog,
    build_ticker_concept_map,
    load_concepts,
)
from tradingnote_taifex import (
    DEFAULT_FUTURES_PRODUCTS,
    LARGE_TRADERS_ALL_CONTRACTS_MONTH,
    LARGE_TRADERS_HISTORY_ALL_CONTRACTS_MONTH,
    backfill_large_traders_history,
    build_large_traders_name_map,
    build_ssf_map,
    get_cached_daily_futures_report,
    get_cached_large_traders_futures_report,
    get_cached_ssf_list,
    get_futures_snapshot,
    get_large_traders_history_series,
    group_large_traders_all,
    large_traders_code_for_product,
    list_all_products,
)
from tradingnote_flow import FlowAnalysisService, FlowPeriod
from tradingnote_institutional import get_cached_institutional_snapshot
from tradingnote_http import PriceFetchError
from tradingnote_paths import APP_PATHS
from tradingnote_tasks import run_background_task
from tradingnote_technical import load_local_technical, calculate_indicators
from tradingnote_journal import (
    initialize_journal,
    load_week,
    save_journal_entry,
    save_portfolio_snapshot,
)

DATA_DIR = APP_PATHS.data_dir
POSITIONS_PATH = APP_PATHS.positions
CACHE_PATH = APP_PATHS.price_cache
FUTURES_CACHE_PATH = APP_PATHS.futures_cache
FUTURES_LARGE_TRADERS_CACHE_PATH = APP_PATHS.futures_large_traders_cache
FUTURES_SSF_CACHE_PATH = APP_PATHS.futures_ssf_cache
INSTITUTIONAL_CACHE_PATH = APP_PATHS.institutional_cache
POSITION_DETAIL_CACHE_PATH = APP_PATHS.position_detail_cache
HISTORY_DB_PATH = APP_PATHS.history_db
SETTINGS_PATH = APP_PATHS.settings

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

# 視覺主題常數與全域 QSS 已搬到 ui/theme.py（2026-09-15，見 ARCHITECTURE.md／
# STATUS.md）；這裡改用 import，內容不變，往後要調色只改 ui/theme.py 一處。
from ui.theme import (  # noqa: E402
    COLOR_BG,
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_MUTED,
    COLOR_ACCENT,
    COLOR_ACCENT_ACTIVE,
    COLOR_ACCENT_TEXT,
    COLOR_BORDER,
    COLOR_ROW_ALT,
    COLOR_HOVER,
    COLOR_CARD_BG,
    COLOR_GAIN,
    COLOR_LOSS,
    COLOR_GAIN_TINT,
    COLOR_LOSS_TINT,
    COLOR_WARNING_TINT,
    COLOR_NEUTRAL_TINT,
    COLOR_SELECTED_BG,
    COLOR_DISABLED_BG,
    COLOR_DISABLED_TEXT,
    COLOR_SPECIAL,
    FONT_FAMILY,
    STYLESHEET,
)
from ui.format import gain_loss_color  # noqa: E402
from ui.components.stat_card import StatCard  # noqa: E402
from ui.components.signal_badge import SignalBadge  # noqa: E402
from ui.components.section_card import SectionCard  # noqa: E402
from ui.components.insight_card import InsightCard  # noqa: E402


def accent_button(text, slot=None):
    btn = QtWidgets.QPushButton(text)
    btn.setProperty("accent", True)
    if slot is not None:
        btn.clicked.connect(slot)
    return btn


def _set_standard_icon(button, standard_pixmap, tooltip=None):
    """套用會隨 DPI 清晰縮放的 Qt 系統圖示，避免用文字符號模擬小圖示。"""
    button.setIcon(button.style().standardIcon(standard_pixmap))
    button.setIconSize(QtCore.QSize(22, 22))
    if tooltip:
        button.setToolTip(tooltip)
    return button


def _compact_money(value):
    """把金額縮成適合摘要卡與提示框閱讀的億元格式。"""
    if value is None:
        return "—"
    return f"{value / 1e8:+,.1f} 億"


def _flow_momentum_insight(rows):
    """rule-based（不接 LLM）：從族群資金流向清單挑出今天最值得注意的一則量價觀察，
    給 InsightCard 用。只用 turnover_ratio（今日量比）與 daily_change_pct（當日
    漲跌%）——兩者都存在才夠格參與，門檻（量比 >= 1.5、漲跌 >= 0.5%）是為了避免拿
    普通、不特別的日子也硬生出一句「看起來煞有其事」的結論。回傳
    (headline, detail, tone)；沒有夠格的族群時回傳中性的「暫無訊號」文案，而不是
    留白或報錯——見規格「Loading/Error/Empty State」的一致性要求。
    """
    candidates = [
        row
        for row in rows
        if row.turnover_ratio is not None
        and row.daily_change_pct is not None
        and row.turnover_ratio >= 1.5
        and abs(row.daily_change_pct) >= 0.5
    ]
    if not candidates:
        return (
            "暫無明顯資金訊號",
            "今日各族群量能與價格變動都在正常範圍內。",
            "neutral",
        )
    top = max(candidates, key=lambda row: row.turnover_ratio)
    if top.daily_change_pct > 0:
        return (
            "資金動能增強",
            f"{top.industry}今日成交量為近期均量的 {top.turnover_ratio:.2f} 倍，"
            f"且價格同步走強（{top.daily_change_pct:+.2f}%）。",
            "positive",
        )
    return (
        "放量下跌需留意",
        f"{top.industry}今日成交量為近期均量的 {top.turnover_ratio:.2f} 倍，"
        f"但價格走弱（{top.daily_change_pct:+.2f}%），賣壓可能未完全釋放。",
        "negative",
    )


def _latest_valid(values):
    """從指標數列取最後一個非 None 的值（序列尾端通常是最新交易日）。"""
    for value in reversed(values or ()):
        if value is not None:
            return value
    return None


def _stock_trend_badges(technical):
    """rule-based（不接 LLM）：從既有的本地技術指標（tradingnote_technical.
    calculate_indicators 的既有輸出，不新增任何指標計算）萃取「趨勢／動能／量能」
    三個方向性標籤，給「個股概覽」的 SignalBadge 用。純粹是「怎麼解讀已經算好的
    數字」：
    - 趨勢：收盤價相對 MA20 的乖離（>=1% 偏多、<=-1% 偏空，用來過濾貼著均線
      上下的雜訊）。
    - 動能：RSI(14)（>=55 偏強、<=45 偏弱，50 上下不特別有意義所以留一段中性帶）。
    - 量能：今日成交量相對均量20 的倍數（>=1.2x 放大、<=0.8x 萎縮）；量能本身
      沒有天生的多空傾向（爆量可能是噴出也可能是出貨），tone 刻意不用
      positive/negative，避免暗示「量增=好事」。
    某一項資料不足（例如新股不到 20 個交易日）時該項直接跳過，不用預設值假裝
    有結論；三項全部不足時回傳空 list，呼叫端應顯示「資料不足」的空狀態文字。
    """
    if not technical:
        return []
    badges = []

    close = _latest_valid(technical.get("close"))
    ma20 = _latest_valid(technical.get("ma", {}).get("ma20"))
    if close is not None and ma20:
        diff_pct = (close / ma20 - 1) * 100
        if diff_pct >= 1:
            badges.append(("趨勢：偏多", "positive"))
        elif diff_pct <= -1:
            badges.append(("趨勢：偏空", "negative"))
        else:
            badges.append(("趨勢：中性", "neutral"))

    rsi = _latest_valid(technical.get("rsi"))
    if rsi is not None:
        if rsi >= 55:
            badges.append(("動能：偏強", "positive"))
        elif rsi <= 45:
            badges.append(("動能：偏弱", "negative"))
        else:
            badges.append(("動能：中性", "neutral"))

    volume_series = technical.get("charts", {}).get("volume", {}).get("series", {})
    volume = _latest_valid(volume_series.get("成交量"))
    volume_ma20 = _latest_valid(volume_series.get("均量20"))
    if volume is not None and volume_ma20:
        ratio = volume / volume_ma20
        if ratio >= 1.2:
            badges.append(("量能：放大", "warning"))
        elif ratio <= 0.8:
            badges.append(("量能：萎縮", "neutral"))
        else:
            badges.append(("量能：平穩", "neutral"))

    return badges


def _clear_layout(layout):
    """移除 layout 裡目前所有 widget，給要重複重繪同一列徽章／標籤的地方用
    （例如切換選取股票時），避免每次都往下疊加舊的 widget。`takeAt()` 只是讓
    layout 不再管這個 widget 的版面配置，widget 本身仍是同一個 parent 底下的
    子物件，還會留在原地疊圖，所以要先 `setParent(None)` 把它整個拔出父子關係
    立刻讓它從畫面上消失，`deleteLater()` 才是排隊真正刪除底層 Qt 物件。
    """
    while layout.count():
        child = layout.takeAt(0)
        widget = child.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


def _populate_trend_badge_row(layout, technical, empty_text="歷史資料不足，暫無法判斷趨勢／動能／量能。"):
    """清空並重繪一列「趨勢／動能／量能」SignalBadge；沒有夠格的資料時改顯示
    empty_text 的 muted 提示，維持跟其他空狀態一致的呈現方式。共用給
    `StockDetailDialog` 與「個股查詢」分頁的 `stock_preview` 摘要面板。
    """
    _clear_layout(layout)
    badges = _stock_trend_badges(technical)
    if badges:
        for text, tone in badges:
            layout.addWidget(SignalBadge(text, tone=tone))
    else:
        hint = QtWidgets.QLabel(empty_text)
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
    layout.addStretch(1)


def _screen_fit_size(widget, preferred_width=None, preferred_height=None, ratio=0.85):
    """算出 widget 開啟時的合理尺寸，讓視窗／對話框自動符合目前螢幕大小，不會
    在小螢幕（筆電、遠端桌面）上比可視範圍還大而被裁到看不見。有給
    preferred_width/height（對話框習慣的偏好尺寸）時取「偏好尺寸」與「螢幕可用
    工作區 * ratio」兩者較小值；沒給（例如主視窗）就直接用比例本身，讓尺寸隨
    螢幕大小等比縮放，大螢幕也能用到更多畫面。"""
    screen = widget.screen() or QtWidgets.QApplication.primaryScreen()
    avail = screen.availableGeometry()
    max_w = int(avail.width() * ratio)
    max_h = int(avail.height() * ratio)
    width = max_w if preferred_width is None else min(preferred_width, max_w)
    height = max_h if preferred_height is None else min(preferred_height, max_h)
    return width, height


def _center_on_screen(widget):
    screen = widget.screen() or QtWidgets.QApplication.primaryScreen()
    frame = widget.frameGeometry()
    frame.moveCenter(screen.availableGeometry().center())
    widget.move(frame.topLeft())


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


def _futures_date_to_iso(raw):
    """TAIFEX 原始日期欄位 "YYYYMMDD" → "YYYY-MM-DD"；格式不符或空值回傳空字串。"""
    if raw and len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return ""


def _format_settlement_month(month):
    """大額交易人未沖銷部位的 SettlementMonth 顯示文字：999912（TAIFEX 用來表示
    「所有契約合計」）轉成「所有契約」；正常的 6 碼西元年月轉成 "YYYY/MM"；
    其他非標準代碼（例如 TX 偶爾出現、全市場未沖銷僅 1 口的 666666 佔位資料）
    原樣顯示，不臆測其語意。"""
    if month == LARGE_TRADERS_ALL_CONTRACTS_MONTH:
        return "所有契約"
    if month and len(month) == 6 and month.isdigit() and "01" <= month[4:6] <= "12":
        return f"{month[0:4]}/{month[4:6]}"
    return month or "-"


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


def _record_valuation_snapshot_best_effort():
    """盡力而為記錄今天的本益比／股價淨值比快照（TWSE／TPEX 官方 bulk 端點各打
    一次），供泡泡圖「估值百分位」新模式逐日累積用（見 tradingnote_history.
    compute_valuation_flow）。刻意用寬鬆的 `except Exception`（不是專案慣例的窄範圍
    例外）：這是輔助性的背景累積動作，任何失敗（離線、端點暫時掛掉、格式意外跑掉）
    都不應該讓啟動或「重新整理」流程跟著失敗——當天只是沒新增一筆 valuation_history，
    不影響其他既有功能，之後正常連線時自然會補上。"""
    try:
        record_valuation_snapshot(HISTORY_DB_PATH, fetch_twse_valuation_all(), "TWSE")
    except Exception:
        pass
    try:
        record_valuation_snapshot(HISTORY_DB_PATH, fetch_tpex_valuation_all(), "TPEX")
    except Exception:
        pass


def run_backfill_in_thread(parent, target_days, progress_cb, done_cb, error_cb):
    def work(_cancel_event, emit):
        return backfill_twse_history(
            HISTORY_DB_PATH,
            target_days=target_days,
            on_progress=lambda done, total: emit(done, total),
        )

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )


def run_tpex_finmind_backfill_in_thread(
    parent, token, target_days, progress_cb, done_cb, error_cb
):
    def work(_cancel_event, emit):
        return backfill_tpex_history_via_finmind(
            HISTORY_DB_PATH,
            token,
            target_days=target_days,
            on_progress=lambda done, total, ticker: emit(done, total, ticker),
        )

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )


def run_task_in_thread(parent, work_fn, on_done, on_error):
    return run_background_task(
        parent,
        lambda _cancel_event, _emit: work_fn(),
        on_done,
        on_error,
    )


def _format_fetched_at(iso_string):
    """把 position_detail_cache.json 的時間戳格式化成畫面可讀的文字。

    舊快取若有異常格式，不應該讓整個部位詳細資訊區塊無法顯示，直接保留
    原字串作為 fallback。
    """
    try:
        return datetime.fromisoformat(iso_string).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso_string


def run_refresh_in_thread(parent, progress_cb, done_cb, error_cb):
    total_steps = 5

    def work(_cancel_event, emit):
        snapshot = get_market_snapshot(
            CACHE_PATH,
            force_refresh=True,
            on_progress=lambda done, _total, label: emit(done, total_steps, label),
        )
        emit(3, total_steps, "正在取得三大法人方向...")
        try:
            get_cached_institutional_snapshot(
                INSTITUTIONAL_CACHE_PATH, force_refresh=True
            )
        except PriceFetchError:
            pass
        emit(4, total_steps, "正在寫入歷史資料庫...")
        record_snapshot(HISTORY_DB_PATH, snapshot)
        _record_valuation_snapshot_best_effort()
        emit(5, total_steps, "重新整理完成。")
        return snapshot

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )


def run_startup_preload_in_thread(parent, progress_cb, done_cb, error_cb):
    total_steps = 5

    def work(_cancel_event, emit):
        snapshot = get_market_snapshot(
            CACHE_PATH,
            on_progress=lambda done, _total, label: emit(done, total_steps, label),
        )
        emit(3, total_steps, "正在取得三大法人方向...")
        try:
            get_cached_institutional_snapshot(INSTITUTIONAL_CACHE_PATH)
        except PriceFetchError:
            pass
        emit(4, total_steps, "正在寫入歷史資料庫...")
        record_snapshot(HISTORY_DB_PATH, snapshot)
        _record_valuation_snapshot_best_effort()
        emit(4, total_steps, "正在更新產業分類...")
        get_industry_map(HISTORY_DB_PATH)
        emit(5, total_steps, "啟動準備完成。")
        return snapshot

    return run_background_task(
        parent,
        work,
        done_cb,
        error_cb,
        on_progress=progress_cb,
    )


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
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
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
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
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
    借券／停資停券／VPT／MFI／KD／MACD／均線／RSI，見 _on_show_full_detail），共用同一份
    position_detail_cache.json（key 是 ticker，不分是從部位紀錄還是這裡查
    的）、也共用 _render_detail_block 畫面邏輯；不點按鈕就不會多打完整籌碼查詢，
    避免瀏覽「個股」頁清單時無謂燒額度。"""

    def __init__(self, parent, ticker, name, finmind_token, market=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
        self.ticker = ticker
        self.name = name
        self.finmind_token = finmind_token
        self.market = market
        self.setWindowTitle(f"{ticker} {name}")
        self.setMinimumWidth(360)

        self.hero = QtWidgets.QFrame()
        self.hero.setProperty("stockHero", True)
        hero_layout = QtWidgets.QHBoxLayout(self.hero)
        hero_layout.setContentsMargins(16, 12, 16, 12)
        identity_layout = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel(f"{name}　{ticker}")
        title.setProperty("header", True)
        market_label = QtWidgets.QLabel(market or "市場未明")
        market_label.setProperty("muted", True)
        identity_layout.addWidget(title)
        identity_layout.addWidget(market_label)
        hero_layout.addLayout(identity_layout)
        hero_layout.addStretch(1)
        price = getattr(parent, "snapshot", {}).get(ticker)
        price_text = (
            f"{price.close:,.2f}" if price is not None and price.close is not None else "—"
        )
        change_pct = _change_pct(price) if price is not None else None
        change_text = f"{change_pct:+.2f}%" if change_pct is not None else "今日漲跌 —"
        status = None if change_pct is None else ("positive" if change_pct >= 0 else "negative")
        self.hero_stat = StatCard(bordered=False)
        self.hero_stat.value_label.setAlignment(QtCore.Qt.AlignRight)
        self.hero_stat.change_label.setAlignment(QtCore.Qt.AlignRight)
        self.hero_stat.set_value(price_text, change=change_text, status=status)
        hero_layout.addWidget(self.hero_stat)

        cached = load_position_detail_cache(POSITION_DETAIL_CACHE_PATH, ticker) or {}
        technical = load_local_technical(HISTORY_DB_PATH, ticker, cached.get("price_history"))

        trend_row = QtWidgets.QHBoxLayout()
        trend_row.setSpacing(6)
        _populate_trend_badge_row(trend_row, technical)

        self.status_label = QtWidgets.QLabel("查詢中...")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(QtCore.Qt.RichText)

        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(20, 20, 20, 16)
        self._layout.addWidget(self.hero)
        self._layout.addLayout(trend_row)
        self._layout.addWidget(self.status_label)
        self.local_technical_widget = TechnicalAnalysisWidget()
        self.local_technical_widget.set_data(technical)
        self._layout.addWidget(self.local_technical_widget)
        self.resize(900, 650)

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
        metrics = []
        if valuation is None:
            valuation_date = "估值資料不足"
            metrics.extend((("PER", "—"), ("PBR", "—"), ("殖利率", "—")))
        else:
            per = valuation["per"]
            pbr = valuation["pbr"]
            yield_pct = valuation["dividend_yield"]
            valuation_date = f"估值日期 {valuation['date']}"
            metrics.extend(
                (
                    ("PER", f"{per:.2f}" if per is not None else "—"),
                    ("PBR", f"{pbr:.2f}" if pbr is not None else "—"),
                    ("殖利率", f"{yield_pct:.2f}%" if yield_pct is not None else "—"),
                )
            )
        metric_html = "".join(
            f"<td width='150'><span style='color:{COLOR_MUTED}'>{label}</span><br>"
            f"<span style='font-size:18px;font-weight:600'>{value}</span></td>"
            for label, value in metrics
        )
        if institutional is None:
            institution_html = "三大法人買賣超：資料不足"
        else:
            items = []
            for row in institutional["breakdown"]:
                color = gain_loss_color(row["net"])
                items.append(
                    f"<span style='color:{color}'>{row['label']} {row['net']:+,}</span>"
                )
            institution_html = (
                f"三大法人淨買賣（{institutional['date']}，股）　" + "　".join(items)
            )
        self.status_label.setText(
            f"<table cellspacing='4'><tr>{metric_html}</tr></table>"
            f"<div style='color:{COLOR_MUTED};margin-top:4px'>{valuation_date}</div>"
            f"<div style='margin-top:10px'>{institution_html}</div>"
        )
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
        關閉鈕之間。文字摘要固定顯示在上方，五張圖表改用 QTabWidget（分頁
        選單在上方切換），一次只顯示一張圖，不用像 QScrollArea 那樣把多張圖
        疊起來捲動瀏覽；同時把視窗放大到看得下內容的尺寸（初始只有一行狀態
        文字時不需要這麼大）。"""
        self.full_detail_label = QtWidgets.QLabel("")
        self.full_detail_label.setWordWrap(True)
        self.full_detail_label.setMaximumHeight(170)
        # 完整摘要已包含基本估值與法人資訊，展開後收起上方簡版以免重複並節省高度。
        self.status_label.setVisible(False)
        self.local_technical_widget.setVisible(False)

        self.full_detail_price_chart = pg.PlotWidget()
        self.full_detail_flow_chart = pg.PlotWidget()
        self.full_detail_institutional_detail_chart = pg.PlotWidget()
        self.full_detail_margin_chart = pg.PlotWidget()
        self.full_detail_vpt_chart = pg.PlotWidget()
        self.full_detail_mfi_chart = pg.PlotWidget()
        self.full_detail_sbl_chart = pg.PlotWidget()
        self.full_detail_lending_chart = pg.PlotWidget()
        for chart in (
            self.full_detail_price_chart,
            self.full_detail_flow_chart,
            self.full_detail_institutional_detail_chart,
            self.full_detail_margin_chart,
            self.full_detail_vpt_chart,
            self.full_detail_mfi_chart,
            self.full_detail_sbl_chart,
            self.full_detail_lending_chart,
        ):
            chart.setBackground(COLOR_SURFACE)
            chart.showGrid(x=True, y=True, alpha=0.08)
            chart.setMinimumHeight(240)
            chart.addLegend()
        _setup_price_chart_click(self.full_detail_price_chart)

        self.full_detail_tabs = QtWidgets.QTabWidget()
        self.full_detail_tabs.addTab(self.full_detail_price_chart, "歷史股價")
        self.full_detail_tabs.addTab(self.full_detail_flow_chart, "三大法人")
        self.full_detail_tabs.addTab(self.full_detail_institutional_detail_chart, "法人分別")
        self.full_detail_tabs.addTab(self.full_detail_margin_chart, "融資融券")
        self.full_detail_tabs.addTab(self.full_detail_vpt_chart, "VPT")
        self.full_detail_tabs.addTab(self.full_detail_mfi_chart, "MFI")
        self.full_detail_tabs.addTab(self.full_detail_sbl_chart, "借券賣出餘額")
        self.full_detail_tabs.addTab(self.full_detail_lending_chart, "借券成交")
        self.full_detail_technical_widget = TechnicalAnalysisWidget()
        self.full_detail_tabs.addTab(self.full_detail_technical_widget, "技術分析")

        insert_at = self._layout.indexOf(self.full_detail_button) + 1
        self._layout.insertWidget(insert_at, self.full_detail_label)
        self._layout.insertWidget(insert_at + 1, self.full_detail_tabs)
        self._full_detail_widgets_built = True
        width, height = _screen_fit_size(self, preferred_width=700, preferred_height=620, ratio=0.9)
        self.setMinimumSize(min(640, width), min(300, height))
        self.resize(width, height)
        _center_on_screen(self)

    def _on_show_full_detail(self):
        if not self._full_detail_widgets_built:
            self._build_full_detail_widgets()

        self.full_detail_button.setEnabled(False)
        self.full_detail_button.setText("查詢中...")
        # 股票身分與即時價格已在上方 hero 區呈現，詳細摘要不再重複一次標題。
        header = ""

        # 跟「部位紀錄」頁 _load_position_detail 同一招：先顯示上次永久存下來
        # 的結果（有的話），背景照樣重打一次 FinMind 拿最新資料。
        cached = load_position_detail_cache(POSITION_DETAIL_CACHE_PATH, self.ticker)
        if cached is not None:
            note = f"（上次查詢：{_format_fetched_at(cached['fetched_at'])}，背景更新中...）"
            _render_detail_block(
                self.full_detail_label,
                self.full_detail_price_chart,
                self.full_detail_flow_chart,
                self.full_detail_institutional_detail_chart,
                self.full_detail_margin_chart,
                self.full_detail_vpt_chart,
                self.full_detail_mfi_chart,
                self.full_detail_sbl_chart,
                self.full_detail_lending_chart,
                header,
                cached,
                note,
                technical_widget=self.full_detail_technical_widget,
            )
        else:
            self.full_detail_label.setText(f"{header}\n\nFinMind 查詢中...")
            self.full_detail_technical_widget.set_data(None)

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
            self.full_detail_institutional_detail_chart,
            self.full_detail_margin_chart,
            self.full_detail_vpt_chart,
            self.full_detail_mfi_chart,
            self.full_detail_sbl_chart,
            self.full_detail_lending_chart,
            header,
            data,
            technical_widget=self.full_detail_technical_widget,
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
                self.full_detail_institutional_detail_chart,
                self.full_detail_margin_chart,
                self.full_detail_vpt_chart,
                self.full_detail_mfi_chart,
                self.full_detail_sbl_chart,
                self.full_detail_lending_chart,
                header,
                cached,
                note,
                technical_widget=self.full_detail_technical_widget,
            )
        else:
            self.full_detail_button.setText("顯示完整籌碼面資訊（同部位紀錄）")
            self.full_detail_label.setText(
                f"{header}\n\nFinMind 查詢失敗：{message}\n\n"
                "可能原因：FinMind token 未設定或已失效、已超過免費額度，或該股票暫無此資料。"
            )
        self._notify_finmind_call()


class IndustryTopStocksDialog(QtWidgets.QDialog):
    """點擊「資金流向分析」頁的族群泡泡／熱度清單時彈出的小視窗，顯示該群組
    近 days 個交易日累積成交金額前 N 大成分股（市值資料的代理指標，見
    get_industry_top_stocks_range；N 不足時全部顯示），並附上近日成交量／均量／
    本益比。純本地資料（daily_prices／valuation_history 快取），不打任何
    API，開啟即顯示。days 來自資金流向頁最上方的共用期間，讓累積成交金額與
    近日均量都跟畫面上看到的分析口徑一致；本益比則取既有最新估值快取。"""

    def __init__(self, parent, group_name, top_stocks, days):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
        self.setWindowTitle(
            f"{group_name}　近 {days} 日成交金額前 {len(top_stocks)} 大成分股"
        )
        self.setMinimumWidth(420)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        if top_stocks:
            first = top_stocks[0]
            layout.addWidget(QtWidgets.QLabel(
                f"分析期間：{first.get('start_date', '')} ～ {first.get('end_date', '')}；"
                "1 日採當日漲跌，量比均量不含截止日。"
            ))
        table = QtWidgets.QTableWidget(len(top_stocks), 9)
        table.setHorizontalHeaderLabels(
            [
                "代號",
                "名稱",
                "現價",
                "累積漲跌%",
                "累積成交金額(億)",
                "近日量(張)",
                "均量(張)",
                "本益比",
                "EPS",
            ]
        )
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for row, stock in enumerate(top_stocks):
            change_pct = stock["change_pct"]
            change_str = f"{change_pct:+.2f}%" if change_pct is not None else f"實際有 {stock.get('actual_days', 0)}／需要 {stock.get('required_days', days)} 個交易日（價格不足）"
            color = gain_loss_color(change_pct or 0)
            avg_volume = stock["avg_volume"]
            per = stock["per"]
            eps = stock["close"] / per if per and stock["close"] is not None else None
            values = [
                stock["ticker"],
                stock["name"] or "-",
                f"{stock['close']:.2f}" if stock["close"] is not None else "資料不足",
                change_str,
                f"{stock['trading_value'] / 1e8:,.2f}",
                f"{stock['today_volume'] / 1000:,.0f}" if stock["today_volume"] is not None else "-",
                f"{avg_volume / 1000:,.0f}" if avg_volume is not None else f"實際有 {stock.get('actual_volume_days', 0)}／需要 {stock.get('required_volume_days', days)} 個歷史交易日",
                f"{per:.2f}" if per is not None else "N/A",
                f"{eps:.2f}" if eps is not None else "N/A",
            ]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if col in (0, 2, 3, 4, 5, 6, 7, 8):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                if col == 3 and change_pct is not None:
                    item.setForeground(QtGui.QColor(color))
                table.setItem(row, col, item)
        table.resizeColumnsToContents()
        _, height_cap = _screen_fit_size(self, preferred_height=760, ratio=0.8)
        table.setFixedHeight(min(height_cap, 36 + table.rowCount() * 30))
        layout.addWidget(table)

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)
        _center_on_screen(self)


# 「大額交易人未沖銷部位」明細表欄位——「期貨」頁下方常駐面板（_build_futures_lt_panel）
# 與雙擊彈窗（FuturesLargeTradersDialog）共用同一組欄位與同一份填表邏輯
# （_populate_large_traders_table）。
LARGE_TRADERS_TABLE_COLUMNS = [
    "到期月份", "交易人類別", "前5大買", "前5大賣", "前10大買", "前10大賣",
    "前10大淨", "前10大買佔比", "全市場未沖銷",
]


def _populate_large_traders_table(table, groups):
    """把某商品的大額交易人未沖銷部位（get_large_traders_for_product 的回傳
    groups）填進 table（欄位須為 LARGE_TRADERS_TABLE_COLUMNS）。每個到期月份最多
    兩列（所有交易人／特定法人）。前10大淨＝前10大買－賣，正紅負綠；前10大買佔比
    ＝前10大買 ÷ 全市場未沖銷。回傳實際畫出的列數（0＝查無資料）。"""
    display_rows = []
    for group in groups:
        for label in ("所有交易人", "特定法人"):
            entry = group["by_type"].get(label)
            if entry is not None:
                display_rows.append((group, label, entry))

    table.setRowCount(len(display_rows))
    for row, (group, label, entry) in enumerate(display_rows):
        top10_net = (entry["top10_buy"] or 0) - (entry["top10_sell"] or 0)
        market_oi = entry["market_oi"]
        buy_share = (
            f"{(entry['top10_buy'] or 0) / market_oi * 100:.1f}%" if market_oi else "-"
        )
        values = [
            _format_settlement_month(group["settlement_month"]),
            label,
            f"{entry['top5_buy']:,}" if entry["top5_buy"] is not None else "-",
            f"{entry['top5_sell']:,}" if entry["top5_sell"] is not None else "-",
            f"{entry['top10_buy']:,}" if entry["top10_buy"] is not None else "-",
            f"{entry['top10_sell']:,}" if entry["top10_sell"] is not None else "-",
            f"{top10_net:+,}",
            buy_share,
            f"{market_oi:,}" if market_oi is not None else "-",
        ]
        for col, value in enumerate(values):
            item = QtWidgets.QTableWidgetItem(value)
            if col != 1:
                item.setTextAlignment(QtCore.Qt.AlignCenter)
            if col == 6:  # 前10大淨：正紅負綠
                item.setForeground(QtGui.QColor(gain_loss_color(top10_net)))
            table.setItem(row, col, item)
    return len(display_rows)


def _large_traders_summary_entry(groups, contract_month):
    """從某商品的大額交易人分組挑一筆代表值給「期貨」頁主表格的摘要欄用：優先取
    到期月份等於近月合約（contract_month）的那組，其次取「所有契約合計」，再不然
    第一組；回傳該組的「所有交易人」entry（沒有就 None）。"""
    if not groups:
        return None
    chosen = next((g for g in groups if g["settlement_month"] == contract_month), None)
    if chosen is None:
        chosen = next((g for g in groups if g["is_all_contracts"]), None)
    if chosen is None:
        chosen = groups[0]
    return chosen["by_type"].get("所有交易人")


def _populate_large_traders_trend(chart, series):
    """畫某商品「所有契約合計・所有交易人」前10大買方／賣方未沖銷部位的逐日趨勢
    （兩條線，跟 _populate_margin_chart 一樣直接畫原始「部位數（口）」、不累加）。
    series 是 tradingnote_taifex.get_large_traders_history_series 的回傳（歷史由
    背景回補累積，見 backfill_large_traders_history）；空的就清空圖並提示。
    以「所有契約合計」為序列而非近月，是因為近月合約每月換倉會造成序列斷點，
    所有契約合計才連續。"""
    chart.clear()
    if not series:
        chart.setTitle(
            "大額交易人未沖銷部位趨勢（尚無歷史：背景回補中，或本商品無此統計）",
            color=COLOR_TEXT,
            size="10pt",
        )
        return

    dates = [r["date"] for r in series]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    buy = [r["top10_buy"] or 0 for r in series]
    sell = [r["top10_sell"] or 0 for r in series]
    chart.plot(x, buy, pen=pg.mkPen("#1f77b4", width=2), name="前10大買方")
    chart.plot(x, sell, pen=pg.mkPen("#ff7f0e", width=2), name="前10大賣方")
    chart.setLabel("left", "未沖銷部位（口）", color=COLOR_TEXT)
    chart.setTitle(
        f"前10大交易人未沖銷部位趨勢（所有契約·所有交易人）｜近 {len(dates)} 日",
        color=COLOR_TEXT,
        size="10pt",
    )
    all_y = buy + sell
    _limit_zoom_to_data(chart, x, all_y)
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


class FuturesLargeTradersDialog(QtWidgets.QDialog):
    """雙擊「期貨」頁表格某商品時彈出的較大檢視：顯示該商品的「大額交易人未沖銷
    部位」（TAIFEX OpenInterestOfLargeTradersFutures）。內容跟「期貨」頁下方常駐
    的明細面板相同（共用 _populate_large_traders_table），只是彈窗版面更大、方便
    細看。資料由呼叫端從「期貨」頁重新整理時已抓好的全市場清單裡篩出（純本地、
    開啟即顯示，不另打 API）。"""

    def __init__(self, parent, product, groups):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
        title_name = groups[0]["contract_name"] if groups else ""
        data_date = _futures_date_to_iso(groups[0]["date"]) if groups else ""
        self.setWindowTitle(
            f"{product} {title_name}　大額交易人未沖銷部位"
            + (f"（{data_date}）" if data_date else "")
        )
        self.setMinimumWidth(560)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        hint = QtWidgets.QLabel(
            "單位：口數。「特定法人」為前述大額交易人中屬期交所公告特定法人者。"
            "「前10大淨」＝前10大買方－賣方未沖銷部位；「前10大買佔比」＝前10大"
            "買方未沖銷部位 ÷ 全市場未沖銷部位。資料來自 TAIFEX 官方盤後統計，"
            "每交易日更新一次。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        table = QtWidgets.QTableWidget(0, len(LARGE_TRADERS_TABLE_COLUMNS))
        table.setHorizontalHeaderLabels(LARGE_TRADERS_TABLE_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        row_count = _populate_large_traders_table(table, groups)
        table.resizeColumnsToContents()
        _, height_cap = _screen_fit_size(self, preferred_height=760, ratio=0.8)
        table.setFixedHeight(min(height_cap, 36 + max(row_count, 1) * 30))
        layout.addWidget(table)

        if row_count == 0:
            layout.addWidget(QtWidgets.QLabel("查無此商品的大額交易人資料。"))

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)
        _center_on_screen(self)


class BackfillDialog(QtWidgets.QDialog):
    def __init__(self, parent, target_days=DEFAULT_BACKFILL_TARGET_DAYS, on_complete=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
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
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
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
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
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
        self.setBackground(COLOR_SURFACE)
        self.showGrid(x=True, y=True, alpha=0.08)
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


VALUATION_METRIC_LABELS = {"per": "本益比 PER", "pbr": "股價淨值比 PBR"}


def _flow_quadrant_specs(mode):
    """回傳象限名稱、解讀與底色；鍵值依序是 X／Y 是否高於參考線。"""
    if mode == "valuation":
        return {
            (False, True): ("便宜吸金", "估值較低・資金升溫", COLOR_GAIN_TINT),
            (True, True): ("強勢追價", "估值較高・資金升溫", COLOR_WARNING_TINT),
            (False, False): ("低估觀望", "估值較低・資金降溫", COLOR_NEUTRAL_TINT),
            (True, False): ("高估降溫", "估值較高・資金降溫", COLOR_LOSS_TINT),
        }
    return {
        (False, True): ("放量承壓", "區間下跌・今日放量", COLOR_LOSS_TINT),
        (True, True): ("強勢吸金", "區間上漲・今日放量", COLOR_GAIN_TINT),
        (False, False): ("弱勢觀望", "區間下跌・量能不足", COLOR_NEUTRAL_TINT),
        (True, False): ("量縮走強", "區間上漲・量能待確認", COLOR_WARNING_TINT),
    }


def _add_flow_quadrants(chart, bounds, x_ref, y_ref, mode):
    """以固定八個圖元畫四象限底色與名稱，不隨泡泡數增加繪圖成本。"""
    x_low, x_high, y_low, y_high = bounds
    x_span = max(x_high - x_low, 1e-9)
    y_span = max(y_high - y_low, 1e-9)
    specs = _flow_quadrant_specs(mode)
    areas = {
        # 上方標籤以頂緣為錨點向下展開；下方反之，避免文字被圖框裁掉。
        (False, True): (x_low, x_ref, y_ref, y_high, (0, 0)),
        (True, True): (x_ref, x_high, y_ref, y_high, (1, 0)),
        (False, False): (x_low, x_ref, y_low, y_ref, (0, 1)),
        (True, False): (x_ref, x_high, y_low, y_ref, (1, 1)),
    }
    for quadrant, (left, right, bottom, top, anchor) in areas.items():
        name, detail, color = specs[quadrant]
        region = QtWidgets.QGraphicsRectItem(
            QtCore.QRectF(left, bottom, max(right - left, 0), max(top - bottom, 0))
        )
        region.setBrush(pg.mkBrush(QtGui.QColor(color)))
        region.setPen(pg.mkPen(None))
        region.setZValue(-20)
        region._flow_quadrant_region = True
        chart.addItem(region)

        x = (left + 0.035 * x_span) if anchor[0] == 0 else (right - 0.035 * x_span)
        y = (top - 0.055 * y_span) if anchor[1] == 0 else (bottom + 0.055 * y_span)
        label = pg.TextItem(
            html=(
                f'<div style="color:{COLOR_TEXT}; font-size:11pt; font-weight:600;">{name}</div>'
                f'<div style="color:{COLOR_MUTED}; font-size:8pt;">{detail}</div>'
            ),
            anchor=anchor,
        )
        label.setPos(x, y)
        label.setZValue(-10)
        label._flow_quadrant_label = True
        chart.addItem(label)
    return specs


def _populate_institutional_direction_chart(chart, rows, value_attr, title):
    """畫單一法人的族群淨買賣左右發散圖；右買超、左賣超。"""
    chart.clear()
    plot = chart.getPlotItem()
    plot.hideButtons()
    plot.setMouseEnabled(x=True, y=False)
    ranked = sorted(
        (row for row in rows if getattr(row, value_attr, 0)),
        key=lambda row: abs(getattr(row, value_attr)),
        reverse=True,
    )[:8]
    if not ranked:
        empty = pg.TextItem("尚無法人資料", color=COLOR_MUTED, anchor=(0.5, 0.5))
        empty.setPos(0, 0)
        chart.addItem(empty)
        chart.setTitle(title, color=COLOR_TEXT, size="11pt")
        return

    ranked.reverse()
    values = [getattr(row, value_attr) / 100_000_000 for row in ranked]
    positions = list(range(len(ranked)))
    positive_y = [y for y, value in zip(positions, values) if value >= 0]
    positive_x = [value for value in values if value >= 0]
    negative_y = [y for y, value in zip(positions, values) if value < 0]
    negative_x = [value for value in values if value < 0]
    if positive_x:
        chart.addItem(
            pg.BarGraphItem(
                y=positive_y,
                height=0.64,
                x0=0,
                x1=positive_x,
                brush=pg.mkBrush(46, 155, 101, 205),
                pen=pg.mkPen("#247E53"),
            )
        )
    if negative_x:
        chart.addItem(
            pg.BarGraphItem(
                y=negative_y,
                height=0.64,
                x0=0,
                x1=negative_x,
                brush=pg.mkBrush(217, 88, 82, 205),
                pen=pg.mkPen("#B94743"),
            )
        )
    chart.addLine(x=0, pen=pg.mkPen(COLOR_MUTED, width=1))
    plot.getAxis("left").setTicks(
        [[(index, row.group) for index, row in enumerate(ranked)]]
    )
    plot.setYRange(-0.7, len(ranked) - 0.3, padding=0)
    max_abs = max(abs(value) for value in values) or 1
    plot.setXRange(-max_abs * 1.15, max_abs * 1.15, padding=0)
    total = sum(getattr(row, value_attr) for row in rows)
    direction = "買超" if total > 0 else "賣超" if total < 0 else "持平"
    chart.setTitle(
        f"{title}｜整體{direction} {abs(total) / 100_000_000:,.1f} 億",
        color=COLOR_TEXT,
        size="11pt",
    )
    chart.setLabel("bottom", "← 賣超　估算淨額（億）　買超 →", color=COLOR_MUTED)


def populate_flow_chart(
    chart,
    db_path,
    snapshot,
    avg_days=5,
    on_industry_click=None,
    mode="momentum",
    valuation_metric="per",
    dashboard=None,
    classification_label="官方產業",
    overlapping_groups=False,
):
    """把族群資金流向資料畫進既有的 FlowChartWidget。
    avg_days 決定 X／Y 兩軸的天數（5/10/20 日流向切換），只影響 mode="momentum"；
    mode="valuation" 的短期窗口最多 5 日、長期基準至少 20 日，避免預設 5 日區間
    造成分子分母相同、所有泡泡都落在 Y=1；沒有 dashboard 時才回退到獨立計算。
    on_industry_click 若提供，點擊泡泡時會被呼叫並帶入該群組名稱（例如用來刷新
    下方的成分股面板）。

    `mode="momentum"`（預設）：X＝近 avg_days 日累積漲跌%、Y＝今日量比，即原本的
    「產業資金流向」泡泡圖。`mode="valuation"`：X＝valuation_metric（PER 或 PBR）
    最新一筆數值（成交金額加權平均，只需要今天/最近一次的估值快照，不用等歷史
    百分位）、Y＝資金流入強度（短期窗口 ÷ 所選流向區間），找的是左上角——
    估值比其他產業便宜、流入強度高（錢已經在進）的產業，見 compute_valuation_flow。"""
    chart.clear()
    vb = chart.getPlotItem().getViewBox()

    if mode == "valuation":
        valuation_short_days = min(5, max(1, avg_days))
        valuation_long_days = max(20, max(1, avg_days))
        flow = (
            dashboard.valuation_flow
            if dashboard is not None and dashboard.valuation_flow is not None
            else compute_valuation_flow(
                db_path,
                snapshot,
                short_days=valuation_short_days,
                long_days=valuation_long_days,
                metric=valuation_metric,
            )
        )
        plotted = [
            f for f in flow if f.latest_valuation is not None and f.money_flow_ratio is not None
        ]
        xs_of = lambda f: f.latest_valuation  # noqa: E731
        ys_of = lambda f: f.money_flow_ratio  # noqa: E731
        metric_label = VALUATION_METRIC_LABELS[valuation_metric]
        empty_hint = "尚無估值資料可繪製（等待下一次「重新整理」或啟動時的估值快照）"
        empty_title = f"{classification_label}估值 vs 資金流向"
        x_label = f"最新 {metric_label}（成交金額加權中位數）"
        y_label = f"資金流入強度（近{valuation_short_days}日均額 / 近{valuation_long_days}日均額）"
        title_prefix = (
            f"{classification_label}估值 vs 資金流向｜最新{metric_label} × "
            f"{valuation_short_days}/{valuation_long_days}日資金流入強度"
        )
        # X 沒有像百分位那樣天然的 0～100 參考值，改用「所有產業目前值的中位數」當
        # 參考線——落在線左邊＝比其他產業便宜、右邊＝比其他產業貴，是同業間的相對定位，
        # 不是自身歷史定位（後者需要長天期歷史，見 compute_valuation_flow 的取捨說明）。
        xs_for_ref = [xs_of(f) for f in plotted]
        x_ref_line = statistics.median(xs_for_ref) if xs_for_ref else 0
        y_ref_line = 1
    else:
        flow = (
            dashboard.industry_flow
            if dashboard is not None
            else compute_industry_flow(db_path, snapshot, avg_days=avg_days)
        )
        plotted = [f for f in flow if f.volume_ratio is not None and f.avg_change_pct is not None]
        xs_of = lambda f: f.avg_change_pct  # noqa: E731
        ys_of = lambda f: f.volume_ratio  # noqa: E731
        empty_hint = "尚無足夠歷史資料可繪製（請先執行「回補歷史資料」，\n或等待逐日累積達到最小天數）"
        empty_title = f"{classification_label}資金流向"
        x_label = f"近{avg_days}日成交金額加權平均累積漲跌 %"
        y_label = f"今日成交量 / 近{avg_days}日均量"
        title_prefix = f"{classification_label}資金流向｜{avg_days}日"
        x_ref_line, y_ref_line = 0, 1

    skipped = len(flow) - len(plotted)

    if not plotted:
        text = pg.TextItem(empty_hint, color=COLOR_MUTED, anchor=(0.5, 0.5))
        chart.addItem(text)
        text.setPos(0, 0)
        chart.setTitle(empty_title, color=COLOR_TEXT, size="13pt")
        vb.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
        return

    # 泡泡大小用「資金比重(%)」（該群組成交金額 ÷ 全市場今日總成交金額）而非絕對金額，
    # 讓數字換算成跟當日大盤規模脫鉤的相對占比，不再是每天隨大盤總量起伏的絕對值。
    max_share = max(max(f.capital_share_pct for f in plotted), 0.0001)
    xs = [xs_of(f) for f in plotted]
    ys = [ys_of(f) for f in plotted]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_pad = max((x_max - x_min) * 0.15, 1.0)
    y_pad = max((y_max - y_min) * 0.15, 0.2)
    view_bounds = (x_min - x_pad, x_max + x_pad, y_min - y_pad, y_max + y_pad)
    quadrant_specs = _add_flow_quadrants(
        chart, view_bounds, x_ref_line, y_ref_line, mode
    )

    # 當日方向來自同一批 dashboard 的動能資料。估值模式也沿用這份方向，讓使用者
    # 切換模式後仍看到一致的紅綠語意，不需要再查一次資料庫。
    direction_source = dashboard.industry_flow if dashboard is not None else flow
    direction_by_group = {
        f.industry: (
            getattr(f, "daily_change_pct", None),
            getattr(f, "turnover_ratio", None),
            getattr(f, "directional_flow_value", None),
        )
        for f in direction_source
    }
    prominent = {
        f.industry
        for f in sorted(plotted, key=lambda f: f.capital_share_pct, reverse=True)[:5]
    }
    spots = []
    for f in plotted:
        x, y = xs_of(f), ys_of(f)
        quadrant_name, quadrant_detail, _quadrant_color = quadrant_specs[
            (x >= x_ref_line, y >= y_ref_line)
        ]
        size = max(14.0, (f.capital_share_pct / max_share) ** 0.5 * 55.0)
        daily_change, turnover_ratio, directional_value = direction_by_group.get(
            f.industry, (None, None, None)
        )
        if daily_change is None or abs(daily_change) < 0.05:
            color = QtGui.QColor(COLOR_MUTED)
            direction_text = "中性"
        elif daily_change > 0:
            color = QtGui.QColor(COLOR_GAIN)
            direction_text = "偏流入"
        else:
            color = QtGui.QColor(COLOR_LOSS)
            direction_text = "偏流出"
        tip = (
            f"{f.industry}｜當日{direction_text}\n"
            f"方向推估：{_compact_money(directional_value)}　"
            f"當日漲跌：{daily_change:+.2f}%\n" if daily_change is not None else
            f"{f.industry}｜當日方向資料不足\n"
        )
        tip += (
            f"成交活躍度：{turnover_ratio:.2f} 倍\n"
            if turnover_ratio is not None else "成交活躍度：—\n"
        )
        if dashboard is not None:
            dates = dashboard.period.actual_dates
            tip += f"分析期間：{dates[0] if dates else '—'} ～ {dashboard.period.end_date}\n"
        tip += (
            f"所在象限：{quadrant_name}（{quadrant_detail}）\n"
            f"成交佔比：{f.capital_share_pct:.1f}%\n"
            f"{x_label}：{x:.2f}\n{y_label}：{y:.2f}\n點擊查看成分股"
        )
        spots.append(
            {
                "pos": (x, y),
                "size": size,
                "data": {"industry": f.industry, "tip": tip},
                "brush": pg.mkBrush(color.red(), color.green(), color.blue(), 185),
                "pen": pg.mkPen(color.darker(125), width=1),
            }
        )
        if f.industry not in prominent:
            continue
        display_name = f.industry if len(f.industry) <= 18 else f"{f.industry[:17]}…"
        label = pg.TextItem(
            f"{display_name}\n{f.capital_share_pct:.1f}%",
            color=COLOR_TEXT,
            anchor=(0.5, 1.3),
        )
        label.setPos(x, y)
        chart.addItem(label)

    # 所有泡泡共用單一 ScatterPlotItem，避免每次更新建立數十個 GraphicsObject。
    # 點擊仍可由 point.data() 找回群組，因此效能改善不犧牲互動。
    scatter = pg.ScatterPlotItem(
        spots=spots,
        hoverable=True,
        tip=lambda _x, _y, data: data["tip"],
        hoverPen=pg.mkPen(COLOR_ACCENT, width=2),
    )
    if on_industry_click is not None:
        scatter.sigClicked.connect(
            lambda _plot, points, _ev: points
            and on_industry_click(points[0].data()["industry"])
        )
    chart.addItem(scatter)

    chart.addLine(x=x_ref_line, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.addLine(y=y_ref_line, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("bottom", x_label, color=COLOR_TEXT)
    chart.setLabel("left", y_label, color=COLOR_TEXT)
    share_label = "成交涵蓋率%" if overlapping_groups else "資金比重%"
    title = f"{title_prefix}（泡泡大小＝{share_label}，點擊可查看成分股）"
    if skipped:
        title += f"　（另有 {skipped} 個群組因歷史資料不足未顯示）"
    chart.setTitle(title, color=COLOR_TEXT, size="13pt")

    # 限制縮小（滾輪／觸控板捏合）的下限，最多縮到剛好看見全部泡泡為止，避免
    # 縮出一大片空白；邊界抓資料範圍的 15% 當緩衝，讓泡泡本身（半徑）與旁邊的
    # 產業名稱標籤不會被邊緣裁到。上限（放大）不受影響，仍可無限拉近。
    vb.setLimits(
        xMin=view_bounds[0],
        xMax=view_bounds[1],
        yMin=view_bounds[2],
        yMax=view_bounds[3],
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
    # chart.clear() 會把先前點擊留下的標記線／文字一併清掉，這裡順便重置追蹤
    # 用的屬性，避免 _on_price_chart_click 之後 removeItem 到已經不存在的物件。
    chart._click_marker_items = []
    chart._price_dates = None
    chart._price_closes = None
    if not price_history:
        return

    dates = [row["date"] for row in price_history]
    closes = [row["close"] for row in price_history]
    chart._price_dates = dates
    chart._price_closes = closes
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


def _setup_price_chart_click(chart):
    """幫「歷史股價」圖加上滑鼠點擊查看該日股價的功能：點圖上任一位置，找出
    最接近的交易日，畫一條垂直虛線＋文字標出「日期｜收盤價」。只在 PlotWidget
    建立時呼叫一次——scene 的訊號連線是永久的，不會因為 _populate_price_chart
    之後的 chart.clear() 而消失；_populate_price_chart 每次重繪時把最新的
    dates／closes 存到 chart 物件上（見該函式），這裡的 handler 讀取當下存的
    那份，不用另外傳參數，重新整理／切換股票後點擊仍然對得上目前顯示的資料。"""
    vb = chart.getPlotItem().getViewBox()

    def on_click(event):
        dates = getattr(chart, "_price_dates", None)
        closes = getattr(chart, "_price_closes", None)
        if not dates:
            return
        if not chart.getPlotItem().sceneBoundingRect().contains(event.scenePos()):
            return
        point = vb.mapSceneToView(event.scenePos())
        idx = round(point.x())
        idx = max(0, min(len(dates) - 1, idx))

        for item in getattr(chart, "_click_marker_items", []):
            chart.removeItem(item)

        marker_line = pg.InfiniteLine(
            pos=idx, angle=90, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1)
        )
        label = pg.TextItem(
            f"{dates[idx]}｜{closes[idx]:.2f}", color=COLOR_TEXT, anchor=(0.5, 1)
        )
        label.setPos(idx, closes[idx])
        chart.addItem(marker_line)
        chart.addItem(label)
        chart._click_marker_items = [marker_line, label]

    chart.scene().sigMouseClicked.connect(on_click)


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
    # 現在這張圖可能是 QTabWidget 裡目前沒被切到的分頁（隱藏、還沒有真正版面
    # 尺寸），這時 enableAutoRange() 算出來的範圍不可靠（同 _populate_price_chart
    # 的理由），改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _populate_institutional_detail_chart(chart, detail_history):
    """法人分別累計買賣超：跟 _populate_flow_chart（合併三大類）完全同一種畫法
    ——逐日淨買賣超累加成累計曲線、加一條零軸參考線——但畫的是五個細項各自
    一條線（外資／外資自營商／投信／自營商(自行)／自營商(避險)，見
    tradingnote_finmind.INSTITUTIONAL_DETAIL_BUCKETS），讓使用者看得出「哪一
    類法人在持續進出」而不只是合併後的三大類。detail_history 是
    fetch_institutional_investors_detailed_history() 的回傳值（也就是
    fetch_position_detail() 存進 institutional_detail_history 的那份）。"""
    chart.clear()
    if detail_history is None or not detail_history["dates"]:
        return

    dates = detail_history["dates"]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    detail_colors = {
        "外資": "#1f77b4",
        "外資自營商": "#17becf",
        "投信": "#2ca02c",
        "自營商(自行)": "#d62728",
        "自營商(避險)": "#ff7f0e",
    }
    all_y = [0]  # 包含 0，讓零軸參考線不會被縮出視野邊界外
    for label, series in detail_history["series"].items():
        cumulative = []
        running_total = 0
        for net in series:
            running_total += net
            cumulative.append(running_total)
        all_y.extend(cumulative)
        chart.plot(
            x,
            cumulative,
            pen=pg.mkPen(detail_colors.get(label, COLOR_TEXT), width=2),
            name=label,
        )

    chart.addLine(y=0, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("left", "累計淨買賣超（股）", color=COLOR_TEXT)
    chart.setTitle(
        f"法人分別累計買賣超｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt"
    )
    _limit_zoom_to_data(chart, x, all_y)
    # 同 _populate_flow_chart：這張圖可能是 QTabWidget 裡目前沒被切到的隱藏分頁，
    # enableAutoRange() 算出來的範圍不可靠，改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


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
    # 同 _populate_flow_chart：這張圖也可能是 QTabWidget 裡目前沒被切到的
    # 分頁，enableAutoRange() 不可靠，改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _mark_latest_value(chart, x, dates, series, color, value_fmt="{:.1f}"):
    """在圖上用水平虛線＋左側文字標出最後一筆資料的數值與日期，方便一眼看到
    目前值，不用把滑鼠移到線的最右端去對。"""
    latest_y = series[-1]
    latest_label = f"{value_fmt.format(latest_y)}｜{dates[-1]}"
    chart.addLine(y=latest_y, pen=pg.mkPen(color, style=QtCore.Qt.DashLine, width=1))
    text = pg.TextItem(latest_label, color=color, anchor=(0, 0.5))
    text.setPos(min(x), latest_y)
    chart.addItem(text)


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
    _mark_latest_value(chart, x, dates, series, "#17becf", "{:,.0f}")


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
    _mark_latest_value(chart, x, dates, series, "#bcbd22", "{:.1f}")


def _populate_short_sale_balance_chart(chart, sbl_balance):
    """借券賣出餘額（股）趨勢圖：跟 _populate_margin_chart 一樣是「餘額」，直接
    畫原始值。這是證券商辦理有價證券借貸的餘額，單位是股，跟融資融券頁籤的
    「張」不是同一個量級，所以是獨立分頁，不是加進融資融券那張圖。"""
    chart.clear()
    if sbl_balance is None or not sbl_balance.get("dates"):
        return

    dates = sbl_balance["dates"]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    series = sbl_balance["series"]["借券賣出餘額"]
    chart.plot(x, series, pen=pg.mkPen(COLOR_ACCENT, width=2), name="借券賣出餘額")
    chart.setLabel("left", "餘額（股）", color=COLOR_TEXT)
    chart.setTitle(f"借券賣出餘額｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")


def _populate_lending_volume_chart(chart, lending):
    """借券成交量（張）趨勢圖：借券市場的每日成交量，跟借券賣出餘額是不同性質
    的數字（一個是累積餘額，一個是當日流量），單位也不同（股 vs 張），所以
    分開兩張圖，不合併成一張雙軸圖。"""
    chart.clear()
    if lending is None or not lending.get("dates"):
        return

    dates = lending["dates"]
    x = list(range(len(dates)))
    step = max(1, len(dates) // 8)
    chart.getPlotItem().getAxis("bottom").setTicks(
        [[(i, dates[i]) for i in range(0, len(dates), step)]]
    )

    series = lending["series"]["借券成交量"]
    chart.plot(x, series, pen=pg.mkPen(COLOR_SPECIAL, width=2), name="借券成交量")
    chart.setLabel("left", "成交量（張）", color=COLOR_TEXT)
    chart.setTitle(f"借券成交量｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")


def _plot_technical_series(chart, dates, series, color, name):
    points = [
        (index, value)
        for index, value in enumerate(series or ())
        if value is not None
    ]
    if not points:
        return []
    x, y = zip(*points)
    chart.plot(list(range(len(series))), [float("nan") if value is None else value for value in series], connect="finite", pen=pg.mkPen(color, width=2), name=name)
    return list(y)


def _populate_technical_chart(chart, technical_data, mode="kd"):
    """繪製本地計算的 KD／MACD／均線圖。"""
    chart.clear()
    if not technical_data or not technical_data.get("dates"):
        empty = pg.TextItem("尚無足夠歷史價格資料可計算技術指標", color=COLOR_MUTED)
        chart.addItem(empty)
        empty.setPos(0, 0)
        chart.setTitle("技術分析", color=COLOR_TEXT, size="11pt")
        return

    dates = technical_data["dates"]
    spec = technical_data.get("charts", {}).get(mode)
    if not spec:
        chart.setTitle("此快取尚無指標，請重新載入資料")
        return
    values = []
    colors = ("#409cff", "#ffab40", "#43c59e", "#dc709f", "#b399ff", "#b8c85a", "#cccccc")
    for index, (name, series) in enumerate(spec["series"].items()):
        values += _plot_technical_series(chart, dates, series, colors[index % len(colors)], name)
    for level in spec["levels"]:
        chart.addLine(y=level, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine))
    chart.setTitle(spec["title"], color=COLOR_TEXT, size="11pt")
    chart.setLabel("left", spec["unit"])
    step = max(1, len(dates)//6)
    chart.getAxis("bottom").setTicks([[(i, dates[i]) for i in range(0,len(dates),step)]])
    chart.setXRange(0, max(1,len(dates)-1), padding=.02)
    if values:
        lo, hi = min(values), max(values)
        pad = max(abs(lo)*.02, 1) if lo == hi else (hi-lo)*.1
        chart.setYRange(lo-pad, hi+pad, padding=0)
    else:
        chart.setYRange(0, 1)
        empty = pg.TextItem("資料不足：缺少必要欄位或尚未滿計算期數", color=COLOR_MUTED)
        chart.addItem(empty)
        empty.setPos(0,.5)


class TechnicalAnalysisWidget(QtWidgets.QWidget):
    """個股明細共用的技術分析視圖。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("指標："))
        self.mode_combo = QtWidgets.QComboBox()
        catalog = calculate_indicators([dict(date="sample", close=1)])["charts"]
        for key, spec in catalog.items():
            self.mode_combo.addItem(spec["title"], key)
        self.mode_combo.currentIndexChanged.connect(self._refresh_chart)
        controls.addWidget(self.mode_combo)
        self.range_combo = QtWidgets.QComboBox()
        for title, count in (("全部歷史", 0), ("近60筆", 60), ("近120筆", 120), ("近240筆", 240)):
            self.range_combo.addItem(title, count)
        self.range_combo.currentIndexChanged.connect(self._refresh_chart)
        controls.addWidget(self.range_combo)
        self.latest_label = QtWidgets.QLabel("")
        self.latest_label.setProperty("muted", True)
        self.latest_label.setWordWrap(True)
        layout.addWidget(self.latest_label)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.chart = pg.PlotWidget()
        self.chart.setBackground(COLOR_SURFACE)
        self.chart.showGrid(x=True, y=True, alpha=0.08)
        self.chart.setMinimumHeight(240)
        self.chart.addLegend()
        layout.addWidget(self.chart, 1)
        self._technical_data = None

    def set_data(self, technical_data):
        self._technical_data = technical_data
        self._refresh_chart()

    def _refresh_chart(self):
        mode = self.mode_combo.currentData()
        data = self._technical_data
        count = self.range_combo.currentData()
        if data and count:
            data = {**data, "dates": data["dates"][-count:], "charts": {
                key: {**spec, "series": {name: values[-count:] for name, values in spec["series"].items()}}
                for key, spec in data.get("charts", {}).items()
            }}
        _populate_technical_chart(self.chart, data, mode)
        self.latest_label.setText(self._latest_text(mode))

    def _latest_text(self, mode):
        data = self._technical_data
        if not data:
            return "資料不足"
        spec = data.get("charts", {}).get(mode, {})
        latest = "　".join(f"{name} {self._last(series)}" for name, series in spec.get("series", {}).items())
        dates = data.get("dates", [])
        return f"{dates[0]} ～ {dates[-1]}｜{len(dates)} 筆日線｜{latest}" if dates else "資料不足"

    @staticmethod
    def _last(values):
        value = values[-1] if values else None
        return f"{value:.2f}" if value is not None else "N/A"



def _render_detail_block(
    label,
    price_chart,
    flow_chart,
    institutional_detail_chart,
    margin_chart,
    vpt_chart,
    mfi_chart,
    sbl_chart,
    lending_chart,
    header,
    data,
    note=None,
    technical_widget=None,
):
    """畫「個股籌碼面詳細資訊」文字摘要＋八張趨勢圖與技術分析（歷史股價／三大法人／
    法人分別／融資融券／VPT／MFI／借券賣出餘額／借券成交量）。data 是
    fetch_position_detail() 的回傳值（不管是剛查到的，還是 position_detail_cache.json
    讀出來的上次結果，shape 都相同，見 tradingnote_finmind.POSITION_DETAIL_FIELDS）；
    「部位紀錄」頁跟「個股」頁的 StockDetailDialog 共用這份畫面邏輯，畫在各自
    傳入的 label／圖表 widget 上。note 非 None 時插在 header 下面一行，用來標示
    「這是上次的快取，背景更新中」或「背景更新失敗，顯示上次結果」。"""
    valuation = data["valuation"]
    history = data["institutional_history"]
    margin_history = data["margin_history"]
    foreign_shareholding = data["foreign_shareholding"]
    lending = data["lending"]
    suspension = data["suspension"]
    sbl_balance = data.get("sbl_short_balance")

    lines = [header] if header else []
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

    margin_cost = data.get("margin_cost_estimate")
    if margin_cost is None:
        lines.append("融資成本（估算）：查無資料")
    else:
        lines.append(
            f"融資成本（估算，{margin_cost['date']}）：約 {margin_cost['cost']:.2f} 元　"
            "※非券商真實成本，僅由歷史融資量反推近似值"
        )

    if sbl_balance is None:
        lines.append("借券賣出餘額：查無資料")
    else:
        lines.append(
            f"借券賣出餘額（{sbl_balance['date']}，單位：股）："
            f"{sbl_balance['balance']:,}（{sbl_balance['change']:+,}）"
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

    # QLabel 的簡單 Rich Text 可增加段落層級，又不需要額外建立大量 widget。
    # 這個摘要在「部位紀錄」與個股彈窗共用，因此保留原有內容，只改善掃讀性。
    html_lines = []
    for index, line in enumerate(lines):
        if not line:
            continue
        safe = html.escape(line).replace("　", "&nbsp;&nbsp;")
        if index == 0 and header:
            html_lines.append(f"<div style='font-weight:600'>{safe}</div>")
        elif note and line == note:
            html_lines.append(f"<div style='color:{COLOR_MUTED};margin:3px 0 7px'>{safe}</div>")
        elif line.startswith("　"):
            html_lines.append(f"<div style='margin-left:14px'>{safe}</div>")
        elif line.startswith("⚠"):
            html_lines.append(f"<div style='color:{COLOR_LOSS};margin-top:5px'>{safe}</div>")
        else:
            html_lines.append(f"<div style='margin-top:4px'>{safe}</div>")
    label.setTextFormat(QtCore.Qt.RichText)
    label.setText("".join(html_lines))
    _populate_price_chart(price_chart, data.get("price_history"))
    _populate_flow_chart(flow_chart, history)
    _populate_institutional_detail_chart(
        institutional_detail_chart, data.get("institutional_detail_history")
    )
    _populate_margin_chart(margin_chart, margin_history)
    _populate_vpt_chart(vpt_chart, data.get("vpt_mfi_history"))
    _populate_mfi_chart(mfi_chart, data.get("vpt_mfi_history"))
    _populate_short_sale_balance_chart(sbl_chart, sbl_balance)
    _populate_lending_volume_chart(lending_chart, lending)
    if technical_widget is not None:
        technical_widget.set_data(calculate_indicators([dict(date=r["date"], open=r.get("open"), max=r.get("high"), min=r.get("low"), close=r.get("close"), Trading_Volume=r.get("volume"), Trading_money=r.get("trading_value")) for r in data.get("price_history", [])]) or data.get("technical_indicators"))


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
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
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
        self.setMinimumSize(680, 480)
        width, height = _screen_fit_size(self, ratio=0.88)
        self.resize(width, height)
        _center_on_screen(self)

        self.settings = load_settings(SETTINGS_PATH)
        self.positions = load_positions(POSITIONS_PATH)
        initialize_journal(HISTORY_DB_PATH, self.positions)
        # 概念目錄在啟動時讀入一次；重建 concepts.json 後需重開程式，
        # 才會讓持股概念標籤與資金流向分類同時更新。
        self._concepts = load_concepts()
        self._ticker_concept_map = build_ticker_concept_map(self._concepts)
        self._classification_catalog = build_classification_catalog(self._concepts)
        self.snapshot = snapshot
        self.last_error = last_error
        self.flow_service = FlowAnalysisService(
            HISTORY_DB_PATH,
            classification_catalog=self._classification_catalog,
            institutional_cache_path=INSTITUTIONAL_CACHE_PATH,
        )
        self.staleness_warning = (
            "；".join(snapshot_staleness_warnings(self.snapshot)) if self.snapshot else ""
        )

        self.continuity_note = ""
        self.large_traders_note = ""
        self._total_pnl = None
        self._sync_timer = None
        self._large_traders_sync_timer = None

        # 「有新資料可更新」提示：_known_data_date 是目前畫面上顯示的資料所屬交易日，
        # _dismissed_data_date 是使用者按過「✕」關閉、暫時不想再看到提示的那個交易日
        # （避免同一天的新資料每次背景檢查都重新跳出來吵）。
        self._known_data_date = _snapshot_date(self.snapshot)
        self._dismissed_data_date = None
        self._pending_data_date = None
        self._new_data_check_timer = None

        self.flow_tab = QtWidgets.QWidget()
        self.positions_tab = QtWidgets.QWidget()
        self.journal_tab = QtWidgets.QWidget()
        self.stocks_tab = QtWidgets.QWidget()
        self.futures_tab = QtWidgets.QWidget()
        self.settings_tab = QtWidgets.QWidget()
        # 主功能改用左側導覽列，頁面本體放進右側堆疊；各頁內的技術圖表分頁仍維持
        # QTabWidget，讓主導覽與頁內切換有清楚的視覺層級。
        self._page_specs = (
            (self.flow_tab, "資金流向", "市場族群、排行與法人方向"),
            (self.positions_tab, "部位紀錄", "持股損益與個股明細"),
            (self.journal_tab, "交易週誌", "每週持股變化與交易筆記"),
            (self.stocks_tab, "個股查詢", "全市場股票搜尋與基本資料"),
            (self.futures_tab, "期貨行情", "期貨盤後與大額交易人部位"),
            (self.settings_tab, "設定", "資料同步、回補與本機偏好"),
        )

        central = QtWidgets.QWidget()
        shell_layout = QtWidgets.QHBoxLayout(central)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self._shell_layout = shell_layout

        self.sidebar = QtWidgets.QFrame()
        self.sidebar.setObjectName("sidebar")
        sidebar_layout = QtWidgets.QVBoxLayout(self.sidebar)
        self._sidebar_layout = sidebar_layout
        sidebar_layout.setContentsMargins(10, 14, 10, 10)
        sidebar_layout.setSpacing(10)

        sidebar_header = QtWidgets.QFrame()
        sidebar_header.setObjectName("sidebarHeader")
        sidebar_header_layout = QtWidgets.QVBoxLayout(sidebar_header)
        sidebar_header_layout.setContentsMargins(9, 2, 9, 6)
        sidebar_header_layout.setSpacing(1)
        brand = QtWidgets.QLabel("TradingNote")
        brand.setObjectName("brandTitle")
        sidebar_header_layout.addWidget(brand)
        self.sidebar_subtitle = QtWidgets.QLabel("台股投資工作台")
        self.sidebar_subtitle.setObjectName("sidebarSubtitle")
        sidebar_header_layout.addWidget(self.sidebar_subtitle)
        sidebar_layout.addWidget(sidebar_header)

        self.main_navigation = QtWidgets.QListWidget()
        self.main_navigation.setObjectName("mainNavigation")
        self.main_navigation.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.main_navigation.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.main_navigation.setSpacing(1)
        sidebar_layout.addWidget(self.main_navigation, 1)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        self._central_layout = content_layout
        content_layout.setContentsMargins(18, 14, 18, 8)
        content_layout.setSpacing(10)

        heading = QtWidgets.QHBoxLayout()
        heading.setSpacing(10)
        title_block = QtWidgets.QVBoxLayout()
        title_block.setSpacing(1)
        self.page_title = QtWidgets.QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QtWidgets.QLabel()
        self.page_subtitle.setProperty("muted", True)
        title_block.addWidget(self.page_title)
        title_block.addWidget(self.page_subtitle)
        heading.addLayout(title_block)
        heading.addStretch()
        content_layout.addLayout(heading)
        self._build_new_data_banner(content_layout)

        self.page_stack = QtWidgets.QStackedWidget()
        self.page_stack.setObjectName("mainPageStack")
        for page, label, description in self._page_specs:
            self.page_stack.addWidget(page)
            item = QtWidgets.QListWidgetItem(label)
            item.setToolTip(description)
            item.setSizeHint(QtCore.QSize(0, 44))
            self.main_navigation.addItem(item)
        content_layout.addWidget(self.page_stack, 1)
        shell_layout.addWidget(self.sidebar)
        shell_layout.addWidget(content, 1)
        self.setCentralWidget(central)

        self.main_navigation.currentRowChanged.connect(self._set_main_page)
        self.main_navigation.setCurrentRow(0)

        self._build_flow_tab()
        self._build_positions_tab()
        self._build_journal_tab()
        self._build_stocks_tab()
        self._build_futures_tab()
        self._build_settings_tab()
        self._apply_responsive_density()

        self.status_bar = QtWidgets.QStatusBar()
        self.status_bar.setSizeGripEnabled(False)
        self.status_bar_label = QtWidgets.QLabel()
        self.status_bar_label.setWordWrap(True)
        self.status_bar_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.status_bar_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred
        )
        self.status_bar.addWidget(self.status_bar_label, 1)
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
            # 期貨大額交易人未沖銷部位歷史：同一個「每次啟動自動檢測」開關控制，綁定
            # 同一顆「回補天數」設定，在背景把缺的日期補進 large_traders_history，供
            # 「期貨」頁趨勢圖用（見 _sync_large_traders_history）。
            self._sync_large_traders_history()
            # 估值歷史（valuation_history）不在這裡回補：泡泡圖「估值」模式改成只看
            # 每檔股票「最新一筆」PER/PBR（見 compute_valuation_flow），不需要長天期
            # 歷史。「最新一筆」資料靠每次「重新整理」／啟動都會呼叫的
            # _record_valuation_snapshot_best_effort 存一筆快照即可，足夠這個模式使用。

        self._flow_revision_timer = QtCore.QTimer(self)
        self._flow_revision_timer.timeout.connect(self._refresh_flow_on_revision)
        self._flow_revision_timer.start(1000)

        self._new_data_check_timer = QtCore.QTimer(self)
        self._new_data_check_timer.timeout.connect(self._check_for_new_data)
        self._new_data_check_timer.start(NEW_DATA_CHECK_INTERVAL_MS)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_density()

    def _set_main_page(self, index):
        """同步左側導覽、右側頁面與頁首說明。"""
        if not 0 <= index < len(self._page_specs):
            return
        self.page_stack.setCurrentIndex(index)
        _page, title, description = self._page_specs[index]
        self.page_title.setText(title)
        self.page_subtitle.setText(description)

    def _apply_responsive_density(self):
        """依可用視窗切換版面密度；只在跨過門檻時更新，拖曳視窗不會反覆重排。"""
        if not hasattr(self, "_central_layout"):
            return
        compact = self.width() < 1080 or self.height() < 700
        if getattr(self, "_compact_layout", None) == compact:
            return
        self._compact_layout = compact

        margin_x, margin_top, spacing = (10, 8, 8) if compact else (18, 14, 12)
        self._central_layout.setContentsMargins(margin_x, margin_top, margin_x, 6)
        self._central_layout.setSpacing(spacing)
        if hasattr(self, "sidebar"):
            self.sidebar.setFixedWidth(164 if compact else 218)
            self._sidebar_layout.setContentsMargins(7 if compact else 10, 8 if compact else 14, 7 if compact else 10, 8)
            self.sidebar_subtitle.setVisible(not compact)
            item_height = 40 if compact else 44
            for row in range(self.main_navigation.count()):
                self.main_navigation.item(row).setSizeHint(QtCore.QSize(0, item_height))
        elif hasattr(self, "tabs"):
            # 測試用的精簡 journal harness 仍以 QTabWidget 提供 icon size API。
            self.tabs.setIconSize(QtCore.QSize(24 if compact else 22, 24 if compact else 22))

        if hasattr(self, "journal_layout"):
            journal_margin = 6 if compact else 10
            self.journal_layout.setContentsMargins(
                journal_margin, journal_margin, journal_margin, journal_margin
            )
            self.journal_layout.setSpacing(7 if compact else 10)
            self.journal_previous_button.setText("上週" if compact else "上一週")
            self.journal_today_button.setText("本週" if compact else "回到本週")
            self.journal_next_button.setText("下週" if compact else "下一週")
            self.journal_jump_label.setVisible(not compact)
            self.journal_date_edit.setDisplayFormat("MM-dd" if compact else "yyyy-MM-dd")
            card_height = 132 if compact else 164
            card_min_width = 64 if compact else 92
            for card in self.journal_cards:
                card.setFixedHeight(card_height)
                card.setMinimumWidth(card_min_width)
            if self.journal_days:
                self._refresh_journal_week()

        if hasattr(self, "stock_search_hint"):
            self.stock_search_hint.setVisible(not compact)

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
        refresh_button = QtWidgets.QPushButton("立即更新", clicked=self._apply_new_data_now)
        _set_standard_icon(refresh_button, QtWidgets.QStyle.SP_BrowserReload, "更新最新資料")
        banner_layout.addWidget(refresh_button)
        close_button = QtWidgets.QPushButton(clicked=self._dismiss_new_data_banner)
        _set_standard_icon(close_button, QtWidgets.QStyle.SP_DialogCloseButton, "關閉提示")
        close_button.setAccessibleName("關閉提示")
        banner_layout.addWidget(close_button)
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
        # 外層保留一層 QScrollArea 作為小視窗／高 DPI 的最後防線；主要內容改用
        # 「分類按鍵＋單一內容頁」呈現，避免泡泡圖、產業清單、個股清單與爆量
        # 清單同時垂直堆疊，讓資金流向頁不必長距離捲動。
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

        self.flow_period = FlowPeriod(
            "0001-01-01", 5
        )

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("流向區間："))
        self.flow_date_button = QtWidgets.QPushButton()
        _set_standard_icon(
            self.flow_date_button,
            QtWidgets.QStyle.SP_FileDialogDetailedView,
            "選擇流向區間",
        )
        self.flow_date_button.clicked.connect(self._open_flow_date_picker)
        toolbar.addWidget(self.flow_date_button)

        toolbar.addSpacing(16)
        toolbar.addWidget(QtWidgets.QLabel("分類方式："))
        self.flow_classification_combo = QtWidgets.QComboBox()
        for classification_mode, label in CLASSIFICATION_LABELS.items():
            if classification_mode == CLASSIFICATION_VALUE_CHAIN_LEAF:
                available = bool(self._classification_catalog.value_chain_scopes)
            else:
                available = bool(
                    self._classification_catalog.groups(classification_mode)
                )
            if classification_mode != CLASSIFICATION_INDUSTRY and not available:
                continue
            self.flow_classification_combo.addItem(label, classification_mode)
        self.flow_classification_combo.currentIndexChanged.connect(
            self._on_flow_classification_changed
        )
        toolbar.addWidget(self.flow_classification_combo)

        self.flow_classification_scope_label = QtWidgets.QLabel("主鏈：")
        self.flow_classification_scope_combo = QtWidgets.QComboBox()
        for scope in self._classification_catalog.value_chain_scopes:
            self.flow_classification_scope_combo.addItem(scope, scope)
        self.flow_classification_scope_combo.currentIndexChanged.connect(
            lambda _: self.refresh_flow_tab()
        )
        self.flow_classification_scope_label.setVisible(False)
        self.flow_classification_scope_combo.setVisible(False)
        toolbar.addWidget(self.flow_classification_scope_label)
        toolbar.addWidget(self.flow_classification_scope_combo)

        toolbar.addSpacing(16)
        toolbar.addWidget(QtWidgets.QLabel("泡泡圖模式："))
        self.flow_bubble_mode_combo = QtWidgets.QComboBox()
        self.flow_bubble_mode_combo.addItem("動能象限（漲跌% × 量比）", "momentum")
        self.flow_bubble_mode_combo.addItem("估值象限（PER/PBR × 資金熱度）", "valuation")
        self.flow_bubble_mode_combo.currentIndexChanged.connect(self._on_flow_bubble_mode_changed)
        toolbar.addWidget(self.flow_bubble_mode_combo)

        self.flow_valuation_metric_combo = QtWidgets.QComboBox()
        self.flow_valuation_metric_combo.addItem("本益比 PER", "per")
        self.flow_valuation_metric_combo.addItem("股價淨值比 PBR", "pbr")
        self.flow_valuation_metric_combo.currentIndexChanged.connect(lambda _: self.refresh_flow_tab())
        self.flow_valuation_metric_combo.setVisible(False)
        toolbar.addWidget(self.flow_valuation_metric_combo)

        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        self._update_flow_date_button()

        section_bar = QtWidgets.QHBoxLayout()
        section_bar.setSpacing(6)
        section_bar.addWidget(QtWidgets.QLabel("顯示分類："))
        self.flow_section_group = QtWidgets.QButtonGroup(self)
        self.flow_section_group.setExclusive(True)
        self.flow_section_buttons = []

        self.flow_section_stack = QtWidgets.QStackedWidget()
        self.flow_section_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )

        sections = (
            ("族群泡泡圖", self._build_flow_chart_section),
            ("法人方向", self._build_institutional_direction_section),
            ("族群熱度排行", self._build_flow_list_section),
            ("資金流入前 50", self._build_stock_capital_flow_section),
            ("個股爆量", self._build_volume_outliers_section),
        )
        for index, (label, builder) in enumerate(sections):
            button = QtWidgets.QPushButton(label)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setProperty("flowSectionButton", True)
            button.clicked.connect(
                lambda _checked, page_index=index: self.flow_section_stack.setCurrentIndex(
                    page_index
                )
            )
            self.flow_section_group.addButton(button, index)
            self.flow_section_buttons.append(button)
            section_bar.addWidget(button)

            page = QtWidgets.QWidget()
            page_layout = QtWidgets.QVBoxLayout(page)
            page_layout.setContentsMargins(0, 8, 0, 0)
            builder(page_layout)
            self.flow_section_stack.addWidget(page)

        section_bar.addStretch(1)
        layout.addLayout(section_bar)
        layout.addWidget(self.flow_section_stack, 1)
        self.flow_section_buttons[0].setChecked(True)

    def _build_institutional_direction_section(self, layout):
        card = SectionCard(
            title="三大法人族群調整方向",
            description=(
                "每張圖獨立顯示該法人的前八大族群：右側為買超、左側為賣超。"
                "方向採交易所實際淨買賣股數，淨額以當日收盤價估算。"
            ),
        )
        self.institutional_direction_date_label = QtWidgets.QLabel("資料尚未載入")
        self.institutional_direction_date_label.setProperty("muted", True)
        card.header_row.addWidget(self.institutional_direction_date_label)

        chart_row = QtWidgets.QHBoxLayout()
        chart_row.setSpacing(8)
        self.institutional_direction_charts = []
        for _title in ("外資", "投信", "自營商"):
            chart = pg.PlotWidget()
            chart.setBackground(COLOR_CARD_BG)
            chart.showGrid(x=True, y=False, alpha=0.08)
            chart.setMinimumSize(180, 260)
            chart_row.addWidget(chart, 1)
            self.institutional_direction_charts.append(chart)
        card.body_layout.addLayout(chart_row, 1)
        layout.addWidget(card, 1)

    def _refresh_institutional_direction(self, dashboard):
        rows = dashboard.institutional_flow
        data_date = max((row.date for row in rows if row.date), default="—")
        covered = max((row.covered_stocks for row in rows), default=0)
        self.institutional_direction_date_label.setText(
            f"資料日 {data_date}・單一族群最多涵蓋 {covered} 檔"
        )
        for chart, (value_attr, title) in zip(
            self.institutional_direction_charts,
            (
                ("foreign_value", "外資"),
                ("trust_value", "投信"),
                ("dealer_value", "自營商"),
            ),
        ):
            _populate_institutional_direction_chart(chart, rows, value_attr, title)

    def _build_flow_chart_section(self, layout):
        self.flow_chart_header = QtWidgets.QLabel("官方產業資金流向圖")
        self.flow_chart_header.setProperty("header", True)
        layout.addWidget(self.flow_chart_header)
        summary_row = QtWidgets.QHBoxLayout()
        summary_row.setSpacing(8)
        self.flow_summary_cards = []
        for title in ("今日偏流入", "今日偏流出", "成交最活躍"):
            card = StatCard(title=title)
            summary_row.addWidget(card, 1)
            self.flow_summary_cards.append(card)
        layout.addLayout(summary_row)
        layout.addSpacing(8)

        self.flow_insight_card = InsightCard("資料載入中...", tone="neutral")
        layout.addWidget(self.flow_insight_card)
        layout.addSpacing(4)

        legend_row = QtWidgets.QHBoxLayout()
        legend_row.setSpacing(6)
        legend_row.addWidget(SignalBadge("偏流入", tone="positive"))
        legend_row.addWidget(SignalBadge("偏流出", tone="negative"))
        legend_row.addWidget(SignalBadge("中性", tone="neutral"))
        legend_row.addStretch(1)
        layout.addLayout(legend_row)
        layout.addSpacing(4)

        self.flow_chart_hint = QtWidgets.QLabel(
            "顏色＝當日量價方向（見上方圖例）；標示成交佔比前五大族群，滑鼠移至泡泡查看數值。"
        )
        self.flow_chart_hint.setProperty("muted", True)
        self.flow_chart_hint.setWordWrap(True)
        layout.addWidget(self.flow_chart_hint)
        layout.addSpacing(8)
        self.flow_chart = FlowChartWidget()
        self.flow_chart.setMinimumHeight(280)
        layout.addWidget(self.flow_chart, 1)

    def _update_flow_date_button(self):
        period = self.flow_period.resolve(HISTORY_DB_PATH, self.snapshot)
        start = period.actual_dates[0] if period.actual_dates else period.start_date
        self.flow_date_button.setText(
            f"{start} ～ {period.end_date} · 近 {period.trading_days} 日"
        )

        self.flow_date_button.setToolTip(
            f"實際有 {len(period.actual_dates)}／需要 {period.trading_days} 個交易日；點擊選日期"
        )

    def _on_flow_bubble_mode_changed(self, _index):
        is_valuation = self.flow_bubble_mode_combo.currentData() == "valuation"
        self.flow_valuation_metric_combo.setVisible(is_valuation)
        self.refresh_flow_tab()

    def _flow_classification_mode(self):
        return self.flow_classification_combo.currentData() or CLASSIFICATION_INDUSTRY

    def _flow_classification_scope(self):
        if self._flow_classification_mode() != CLASSIFICATION_VALUE_CHAIN_LEAF:
            return None
        return self.flow_classification_scope_combo.currentData()

    def _on_flow_classification_changed(self, _index):
        show_scope = self._flow_classification_mode() == CLASSIFICATION_VALUE_CHAIN_LEAF
        self.flow_classification_scope_label.setVisible(show_scope)
        self.flow_classification_scope_combo.setVisible(show_scope)
        self.refresh_flow_tab()

    def _open_flow_date_picker(self):
        end_date = max((p.date for p in self.snapshot.values() if p.date), default=date.today().isoformat())
        valid_dates = sorted(set(d for d in get_available_dates(HISTORY_DB_PATH) if d <= end_date)
                             | {p.date for p in self.snapshot.values() if p.date and p.date <= end_date})
        dialog = TradingDateDialog(self, valid_dates, "選擇流向區間起始日期")
        if dialog.exec() == QtWidgets.QDialog.Accepted and dialog.selected_date:
            days = max(1, sum(d >= dialog.selected_date for d in valid_dates))
            self.flow_period = FlowPeriod(dialog.selected_date, days)
            self._update_flow_date_button()
            self.refresh_flow_tab()

    def _refresh_flow_on_revision(self):
        if get_data_revision(HISTORY_DB_PATH) != getattr(self, "_flow_data_revision", None):
            self.refresh_flow_tab()

    def refresh_flow_tab(self):
        self._flow_data_revision = get_data_revision(HISTORY_DB_PATH)
        period = self.flow_period
        classification_mode = self._flow_classification_mode()
        classification_scope = self._flow_classification_scope()
        classification_label = CLASSIFICATION_LABELS[classification_mode]
        overlapping_groups = classification_mode != CLASSIFICATION_INDUSTRY
        dashboard = self.flow_service.analyze(
            self.snapshot,
            period,
            bubble_mode=self.flow_bubble_mode_combo.currentData(),
            valuation_metric=self.flow_valuation_metric_combo.currentData(),
            classification_mode=classification_mode,
            classification_scope=classification_scope,
        )
        period = dashboard.period
        self._display_flow_period = period
        self._update_flow_date_button()
        data_date = period.end_date or "資料日期未明"
        self.flow_chart_header.setText(
            f"{classification_label}資金流向圖　·　{data_date}"
        )
        if overlapping_groups:
            self.flow_chart_hint.setText(
                "先看淡色象限判斷區間狀態，再看泡泡大小與當日紅綠。概念可重疊；"
                "泡泡大小為成交涵蓋率，各群組合計可能超過 100%。"
            )
        else:
            self.flow_chart_hint.setText(
                "先看淡色象限判斷區間狀態，再看泡泡大小（成交佔比）與當日紅綠；"
                "滑鼠移至泡泡可看完整數值與象限解讀。"
            )
        if not period.sufficient:
            self.flow_chart_hint.setText(self.flow_chart_hint.text() +
                f" 實際有 {len(period.actual_dates)}／需要 {period.trading_days} 個交易日。")
        stale_count = sum(p.date != period.end_date for p in self.snapshot.values())
        if stale_count:
            self.flow_chart_hint.setText(self.flow_chart_hint.text() +
                f" {stale_count} 檔來源日期與截止日不同，未混用其價格計算。")
        populate_flow_chart(
            self.flow_chart,
            HISTORY_DB_PATH,
            self.snapshot,
            avg_days=period.trading_days,
            on_industry_click=self._on_industry_bubble_clicked,
            mode=self.flow_bubble_mode_combo.currentData(),
            valuation_metric=self.flow_valuation_metric_combo.currentData(),
            dashboard=dashboard,
            classification_label=classification_label,
            overlapping_groups=overlapping_groups,
        )
        self._refresh_flow_summary(dashboard)
        self._refresh_institutional_direction(dashboard)
        self.refresh_flow_list(dashboard)
        self.refresh_stock_capital_flow_list(dashboard)
        self.refresh_volume_outliers_list(dashboard)

    def _refresh_flow_summary(self, dashboard):
        """用既有 dashboard 更新三張摘要卡，不觸發額外 DB 或網路工作。"""
        rows = [
            row
            for row in dashboard.industry_flow
            if row.daily_change_pct is not None
        ]
        inflow = [row for row in rows if row.daily_change_pct >= 0.05]
        outflow = [row for row in rows if row.daily_change_pct <= -0.05]
        active = max(
            (row for row in rows if row.turnover_ratio is not None),
            key=lambda row: row.turnover_ratio,
            default=None,
        )
        self.flow_summary_cards[0].set_value(f"{len(inflow)} 個族群", status="positive")
        self.flow_summary_cards[1].set_value(f"{len(outflow)} 個族群", status="negative")
        if active is None:
            self.flow_summary_cards[2].set_value("—")
        else:
            self.flow_summary_cards[2].set_value(
                f"{active.industry}　{active.turnover_ratio:.2f}×"
            )
        headline, detail, tone = _flow_momentum_insight(rows)
        self.flow_insight_card.set_content(headline, detail=detail, tone=tone)

    def _show_industry_top_stocks(self, industry):
        period = self.flow_period
        top_stocks = self.flow_service.get_group_top_stocks(
            self.snapshot,
            period,
            industry,
            classification_mode=self._flow_classification_mode(),
            classification_scope=self._flow_classification_scope(),
            top_n=30,
        )
        IndustryTopStocksDialog(self, industry, top_stocks, days=period.trading_days).exec()

    def _on_industry_bubble_clicked(self, industry):
        # 泡泡圖本身就是共用流向期間的視覺化，點擊彈出的成分股
        # 清單也該用同一個區間的累積成交金額，跟泡泡代表的資料口徑一致。
        self._show_industry_top_stocks(industry)

    def _on_flow_list_item_clicked(self, industry):
        # 所有資金流向小分類共用最上方的流向區間，避免泡泡圖、排行與成分股
        # 彈窗使用不同的日期口徑。
        self._show_industry_top_stocks(industry)

    # ---------- 資金動向清單（與泡泡圖共用最上方的流向區間） ----------

    def _build_flow_list_section(self, layout):
        header = QtWidgets.QLabel("資金動向清單")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        self.flow_list_hint = QtWidgets.QLabel(
            "資料區間與上方「流向區間」一致。分數為同批產業的"
            "相對百分位（0～100）：人氣＝今日資金比重、動能＝區間漲跌、量能＝量比；"
            "綜合熱度為三者平均，清單預設依綜合熱度排序。"
        )
        self.flow_list_hint.setProperty("muted", True)
        self.flow_list_hint.setWordWrap(True)
        layout.addWidget(self.flow_list_hint)
        layout.addSpacing(8)

        self.flow_list = QtWidgets.QTreeWidget()
        self.flow_list.setColumnCount(11)
        self.flow_list.setHeaderLabels(
            [
                "熱度排名",
                "分類",
                "檔數",
                "今日成交金額(億)",
                "今日資金比重%",
                "加權漲跌%",
                "量比",
                "人氣分數",
                "動能分數",
                "量能分數",
                "綜合熱度",
            ]
        )
        self.flow_list.setRootIsDecorated(False)
        self.flow_list.setAlternatingRowColors(True)
        # 由 refresh_flow_list 先排好綜合熱度；避免 Windows Qt 在建立格式化
        # 數值項目時再次觸發原生排序，這在部分環境會造成程序中止。
        self.flow_list.setSortingEnabled(False)
        self.flow_list.setMinimumHeight(240)
        self.flow_list.itemClicked.connect(
            lambda item, _col: self._on_flow_list_item_clicked(
                item.data(0, QtCore.Qt.UserRole)
            )
        )
        layout.addWidget(self.flow_list)

    def refresh_flow_list(self, dashboard=None):
        if dashboard is None:
            dashboard = self.flow_service.analyze(self.snapshot, self.flow_period)
        flow = dashboard.industry_flow
        overlapping_groups = dashboard.classification_mode != CLASSIFICATION_INDUSTRY
        classification_label = CLASSIFICATION_LABELS[dashboard.classification_mode]
        share_name = "成交涵蓋率" if overlapping_groups else "今日資金比重"
        self.flow_list.headerItem().setText(1, classification_label)
        self.flow_list.headerItem().setText(4, f"{share_name}%")
        overlap_note = (
            "概念可重疊，各群組涵蓋率合計可能超過 100%。"
            if overlapping_groups
            else ""
        )
        self.flow_list_hint.setText(
            f"資料區間與上方「流向區間」一致。{overlap_note}"
            f"分數為同批{classification_label}的相對百分位（0～100）："
            f"人氣＝{share_name}、動能＝區間漲跌、量能＝量比；"
            "綜合熱度為三者平均，清單預設依綜合熱度排序。"
        )
        # 進入畫面時直接把最能同時代表人氣／動能／量能的族群排在前面。
        flow = sorted(
            flow,
            key=lambda result: (
                result.composite_score is not None,
                result.composite_score if result.composite_score is not None else -1,
            ),
            reverse=True,
        )
        self.flow_list.clear()
        ranked_count = 0
        for f in flow:
            if f.avg_change_pct is None:
                change_text = f"實際有 {f.actual_days}／需要 {f.required_days} 個交易日（價格不足）"
            else:
                change_text = f"{f.avg_change_pct:+.2f}%"
            volume_text = f"{f.volume_ratio:.2f}" if f.volume_ratio is not None else f"實際有 {f.actual_volume_days}／需要 {f.required_days} 個歷史交易日（量比不足）"
            score_texts = [
                f"{score:.0f}" if score is not None else "資料不足"
                for score in (
                    f.popularity_score,
                    f.momentum_score,
                    f.volume_score,
                    f.composite_score,
                )
            ]
            if f.composite_score is not None:
                ranked_count += 1
                rank_text = str(ranked_count)
            else:
                rank_text = "-"

            item = QtWidgets.QTreeWidgetItem(
                [
                    rank_text,
                    f.industry,
                    str(f.stock_count),
                    f"{f.total_trading_value / 1e8:,.1f}",
                    f"{f.capital_share_pct:.2f}%",
                    change_text,
                    volume_text,
                    *score_texts,
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, f.industry)
            for col in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            if f.avg_change_pct is not None:
                color = gain_loss_color(f.avg_change_pct)
                item.setForeground(5, QtGui.QColor(color))
            else:
                item.setForeground(5, QtGui.QColor(COLOR_MUTED))
                item.setForeground(6, QtGui.QColor(COLOR_MUTED))
            if f.composite_score is not None:
                item.setForeground(10, QtGui.QColor(COLOR_ACCENT))
            else:
                for col in (7, 8, 9, 10):
                    item.setForeground(col, QtGui.QColor(COLOR_MUTED))
            self.flow_list.addTopLevelItem(item)
        # 由 refresh_flow_list 先排好綜合熱度；避免 Windows Qt 在建立格式化
        # 數值項目時再次觸發原生排序，這在部分環境會造成程序中止。
        for col in range(self.flow_list.columnCount()):
            self.flow_list.resizeColumnToContents(col)

    # ---------- 個股資金流入前50名（以區間累積成交金額估算） ----------

    def _build_stock_capital_flow_section(self, layout):
        header = QtWidgets.QLabel("個股資金流入前 50 名")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        hint = QtWidgets.QLabel(
            "依上方資金動向清單的資料區間排序；「資金流入」以區間累積成交金額估算，"
            "不代表法人淨買超。雙擊股票可查看完整個股資訊。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)

        self.stock_capital_flow_list = QtWidgets.QTreeWidget()
        self.stock_capital_flow_list.setColumnCount(9)
        self.stock_capital_flow_list.setHeaderLabels(
            [
                "排名",
                "代號",
                "名稱",
                "產業",
                "區間成交金額(億)",
                "今日成交金額(億)",
                "區間漲跌%",
                "量比",
                "全市場比重%",
            ]
        )
        self.stock_capital_flow_list.setRootIsDecorated(False)
        self.stock_capital_flow_list.setAlternatingRowColors(True)
        self.stock_capital_flow_list.setSortingEnabled(False)
        self.stock_capital_flow_list.setMinimumHeight(240)
        self.stock_capital_flow_list.itemDoubleClicked.connect(
            self._on_stock_capital_flow_double_clicked
        )
        layout.addWidget(self.stock_capital_flow_list)

    def refresh_stock_capital_flow_list(self, dashboard=None):
        if dashboard is None:
            dashboard = self.flow_service.analyze(self.snapshot, self.flow_period)
        stocks = dashboard.stock_capital_flow

        self.stock_capital_flow_list.setSortingEnabled(False)
        self.stock_capital_flow_list.clear()
        for rank, stock in enumerate(stocks, start=1):
            change_text = (
                f"{stock.change_pct:+.2f}%" if stock.change_pct is not None else f"實際有 {stock.actual_days}／需要 {stock.required_days} 個交易日（價格不足）"
            )
            volume_ratio_text = (
                f"{stock.volume_ratio:.2f}" if stock.volume_ratio is not None else "-"
            )
            item = QtWidgets.QTreeWidgetItem(
                [
                    str(rank),
                    stock.ticker,
                    stock.name or "-",
                    stock.industry,
                    f"{stock.trading_value / 1e8:,.2f}",
                    f"{stock.today_trading_value / 1e8:,.2f}",
                    change_text,
                    volume_ratio_text,
                    f"{stock.capital_share_pct:.2f}%",
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, stock.ticker)
            item.setData(0, QtCore.Qt.UserRole + 1, stock.market)
            for col in (0, 1, 3, 4, 5, 6, 7, 8):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            if stock.change_pct is not None:
                color = gain_loss_color(stock.change_pct)
                item.setForeground(6, QtGui.QColor(color))
            self.stock_capital_flow_list.addTopLevelItem(item)

        for col in range(self.stock_capital_flow_list.columnCount()):
            self.stock_capital_flow_list.resizeColumnToContents(col)

    def _on_stock_capital_flow_double_clicked(self, item, _column):
        ticker = item.data(0, QtCore.Qt.UserRole)
        if not ticker:
            return
        name = item.text(2)
        market = item.data(0, QtCore.Qt.UserRole + 1)
        token = self.settings.get("finmind_token", "")
        StockDetailDialog(self, ticker, name, token, market=market).exec()

    # ---------- 個股量比異常清單（今日量 ÷ 近N日均量，逐檔股票各自比較自己的
    # 歷史均量，抓「個股」層級的爆量，不像資金動向清單是整個產業加總後的量比，
    # 單一檔股票爆量會被同產業其他股票稀釋掉） ----------

    def _build_volume_outliers_section(self, layout):
        header = QtWidgets.QLabel("個股量比異常清單")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        controls = QtWidgets.QHBoxLayout()
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
            "均量天數與上方「流向區間」一致；今日成交量 ÷ 近N日均量 ≥ 1.5 倍的個股，"
            "依比值分成 1.5～2倍／2～3倍／3倍以上；"
            "雙擊股票查詢本益比／殖利率／三大法人買賣超（同「個股」分頁，走 FinMind）。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)

        self.outlier_tier_filter = None
        self.volume_outliers_list = QtWidgets.QTreeWidget()
        self.volume_outliers_list.setColumnCount(8)
        self.volume_outliers_list.setHeaderLabels(
            ["級距", "代號", "名稱", "族群", "現價", "漲跌%", "量比", "今日量(張)"]
        )
        self.volume_outliers_list.setRootIsDecorated(False)
        self.volume_outliers_list.setAlternatingRowColors(True)
        self.volume_outliers_list.setSortingEnabled(True)
        self.volume_outliers_list.setMinimumHeight(240)
        self.volume_outliers_list.itemDoubleClicked.connect(
            self._on_volume_outlier_double_clicked
        )
        layout.addWidget(self.volume_outliers_list)

    def _on_outlier_tier_filter_changed(self, _index):
        self.outlier_tier_filter = self.outlier_tier_combo.currentData()
        self.refresh_volume_outliers_list()

    def refresh_volume_outliers_list(self, dashboard=None):
        if dashboard is None:
            dashboard = self.flow_service.analyze(self.snapshot, self.flow_period)
        outliers = dashboard.volume_outliers
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
                    o.industry,
                    f"{o.close:.2f}" if o.close is not None else "-",
                    change_text,
                    f"{o.volume_ratio:.2f}",
                    f"{o.today_volume / 1000:,.0f}",
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, o.ticker)
            item.setData(0, QtCore.Qt.UserRole + 1, o.market)
            for col in (1, 2, 3, 4, 5, 6, 7):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            item.setForeground(0, QtGui.QColor(tier_colors[o.tier]))
            if o.change_pct is not None:
                color = gain_loss_color(o.change_pct)
                item.setForeground(5, QtGui.QColor(color))
            self.volume_outliers_list.addTopLevelItem(item)
        self.volume_outliers_list.setSortingEnabled(True)
        self.volume_outliers_list.sortByColumn(6, QtCore.Qt.DescendingOrder)
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

    def _sync_large_traders_history(self):
        """啟動時在背景把最近「回補天數」天的期貨大額交易人未沖銷部位歷史補進
        large_traders_history（TAIFEX 網站歷史 CSV，見
        tradingnote_taifex.backfill_large_traders_history），供「期貨」頁趨勢圖用。
        跟 _sync_history_continuity 同一套設計：綁定同一顆 backfill_target_days、
        受同一個「每次啟動自動檢測」開關控制，資料已是最新時幾乎瞬間完成、狀態列
        不留痕跡；真的補到資料才在完成後刷新目前選取商品的趨勢圖。冪等、可安全
        中斷重跑（見 backfill_large_traders_history）。"""
        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)

        def work():
            return backfill_large_traders_history(HISTORY_DB_PATH, target_days)

        def on_done(result):
            had_note = bool(self.large_traders_note)
            self.large_traders_note = ""
            self._update_status_bar()
            # 真的補到新資料（或本來就在等）才刷新趨勢圖，避免無謂重畫。
            if result.get("rows") or had_note:
                self._on_futures_row_selected()

        def on_error(_message):
            self.large_traders_note = ""
            self._update_status_bar()

        self.large_traders_note = "期貨大額交易人歷史回補中..."
        self._update_status_bar()
        self._large_traders_sync_timer = run_task_in_thread(
            self, work, on_done, on_error
        )

    # ---------- 部位紀錄頁 ----------

    def _build_positions_tab(self):
        layout = QtWidgets.QVBoxLayout(self.positions_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        add_button = accent_button("新增部位", self.open_add_dialog)
        _set_standard_icon(add_button, QtWidgets.QStyle.SP_FileDialogNewFolder, "新增部位")
        toolbar.addWidget(add_button)
        edit_button = QtWidgets.QPushButton("編輯部位", clicked=self.open_edit_dialog)
        _set_standard_icon(edit_button, QtWidgets.QStyle.SP_FileDialogDetailedView, "編輯部位")
        toolbar.addWidget(edit_button)
        delete_button = QtWidgets.QPushButton("刪除部位", clicked=self.delete_selected)
        _set_standard_icon(delete_button, QtWidgets.QStyle.SP_TrashIcon, "刪除選取部位")
        toolbar.addWidget(delete_button)
        price_button = QtWidgets.QPushButton("查價", clicked=self.open_price_dialog)
        _set_standard_icon(price_button, QtWidgets.QStyle.SP_FileDialogInfoView, "查詢股價")
        toolbar.addWidget(price_button)
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
        不同，這裡不彈窗，直接嵌在部位紀錄頁裡。文字摘要固定顯示在上方，六張
        圖表用 QTabWidget 分頁選單切換（跟 StockDetailDialog._build_full_detail_widgets
        同一招），一次只顯示一張，不用像以前那樣把圖疊起來捲動瀏覽。"""
        container = QtWidgets.QWidget()
        outer_layout = QtWidgets.QVBoxLayout(container)
        outer_layout.setContentsMargins(0, 10, 0, 0)

        header = QtWidgets.QLabel("個股詳細資訊")
        header.setToolTip("選取上方部位，查看歷史股價、法人、融資融券及技術指標。")
        header.setProperty("header", True)
        outer_layout.addWidget(header)
        outer_layout.addSpacing(4)

        self.position_detail_label = QtWidgets.QLabel("尚未選取部位。")
        self.position_detail_label.setWordWrap(True)
        outer_layout.addWidget(self.position_detail_label)

        self.position_price_chart = pg.PlotWidget()
        self.position_flow_chart = pg.PlotWidget()
        self.position_institutional_detail_chart = pg.PlotWidget()
        self.position_margin_chart = pg.PlotWidget()
        self.position_vpt_chart = pg.PlotWidget()
        self.position_mfi_chart = pg.PlotWidget()
        self.position_sbl_chart = pg.PlotWidget()
        self.position_lending_chart = pg.PlotWidget()
        for chart in (
            self.position_price_chart,
            self.position_flow_chart,
            self.position_institutional_detail_chart,
            self.position_margin_chart,
            self.position_vpt_chart,
            self.position_mfi_chart,
            self.position_sbl_chart,
            self.position_lending_chart,
        ):
            chart.setBackground(COLOR_SURFACE)
            chart.showGrid(x=True, y=True, alpha=0.08)
            chart.setMinimumHeight(240)
            chart.addLegend()
        _setup_price_chart_click(self.position_price_chart)

        position_detail_tabs = QtWidgets.QTabWidget()
        position_detail_tabs.addTab(self.position_price_chart, "歷史股價")
        position_detail_tabs.addTab(self.position_flow_chart, "三大法人")
        position_detail_tabs.addTab(self.position_institutional_detail_chart, "法人分別")
        position_detail_tabs.addTab(self.position_margin_chart, "融資融券")
        position_detail_tabs.addTab(self.position_vpt_chart, "VPT")
        position_detail_tabs.addTab(self.position_mfi_chart, "MFI")
        position_detail_tabs.addTab(self.position_sbl_chart, "借券賣出餘額")
        position_detail_tabs.addTab(self.position_lending_chart, "借券成交")
        self.position_technical_widget = TechnicalAnalysisWidget()
        position_detail_tabs.addTab(self.position_technical_widget, "技術分析")
        outer_layout.addWidget(position_detail_tabs)

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
                color = gain_loss_color(pnl.unrealized_pnl)
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
        save_portfolio_snapshot(HISTORY_DB_PATH, date.today().isoformat(), self.positions)
        if hasattr(self, "journal_cards"):
            self._save_journal_if_dirty()
            self._refresh_journal_week()
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
            self.position_institutional_detail_chart.clear()
            self.position_margin_chart.clear()
            self.position_vpt_chart.clear()
            self.position_mfi_chart.clear()
            self.position_sbl_chart.clear()
            self.position_lending_chart.clear()
            self.position_technical_widget.set_data(None)
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
            self.position_institutional_detail_chart.clear()
            self.position_margin_chart.clear()
            self.position_vpt_chart.clear()
            self.position_mfi_chart.clear()
            self.position_sbl_chart.clear()
            self.position_lending_chart.clear()
            self.position_technical_widget.set_data(None)

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
        """畫「部位詳細資訊」文字摘要＋八張趨勢圖與技術分析。實際畫面邏輯是模組層級的
        _render_detail_block（跟「個股」頁 StockDetailDialog 的「顯示完整籌碼
        面資訊」按鈕共用同一份），這裡只是把部位紀錄頁自己的 label／圖表
        widget 傳進去。"""
        _render_detail_block(
            self.position_detail_label,
            self.position_price_chart,
            self.position_flow_chart,
            self.position_institutional_detail_chart,
            self.position_margin_chart,
            self.position_vpt_chart,
            self.position_mfi_chart,
            self.position_sbl_chart,
            self.position_lending_chart,
            header,
            data,
            note,
            technical_widget=self.position_technical_widget,
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
        self.position_institutional_detail_chart.clear()
        self.position_margin_chart.clear()
        self.position_vpt_chart.clear()
        self.position_mfi_chart.clear()
        self.position_sbl_chart.clear()
        self.position_lending_chart.clear()
        self.position_technical_widget.set_data(None)
        self.update_finmind_count_label()

    def _update_status_bar(self):
        cache_note = ""
        if CACHE_PATH.exists():
            import json as _json

            with CACHE_PATH.open("r", encoding="utf-8") as f:
                fetched_at = _json.load(f).get("fetched_at", "")
            cache_note = f"價格更新：{_format_fetched_at(fetched_at)}"
        total_note = (
            f"總損益：{self._total_pnl:+,.0f}" if self._total_pnl is not None else "總損益：N/A"
        )
        error_note = f"　⚠ {self.last_error}" if self.last_error else ""
        staleness_note = f"　⚠ {self.staleness_warning}" if self.staleness_warning else ""
        continuity_note = f"　{self.continuity_note}" if self.continuity_note else ""
        large_traders_note = (
            f"　{self.large_traders_note}" if self.large_traders_note else ""
        )
        status_text = (
            f"{cache_note}　{total_note}{error_note}{staleness_note}"
            f"{continuity_note}{large_traders_note}"
        )
        self.status_bar_label.setText(status_text)
        self.status_bar_label.setToolTip(status_text)

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

    # ---------- 交易週誌 ----------

    def _build_journal_tab(self):
        layout = QtWidgets.QVBoxLayout(self.journal_tab)
        self.journal_layout = layout
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        today = date.today()
        self.journal_week_start = today - timedelta(days=today.weekday())
        self.journal_selected_date = today.isoformat()
        self.journal_days = {}
        self.journal_dirty = False
        self._journal_loading = False

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setSpacing(6)
        self.journal_previous_button = QtWidgets.QPushButton(
            "上一週", clicked=self._journal_previous_week
        )
        _set_standard_icon(
            self.journal_previous_button, QtWidgets.QStyle.SP_ArrowLeft, "查看上一週"
        )
        toolbar.addWidget(self.journal_previous_button)
        self.journal_today_button = QtWidgets.QPushButton("回到本週", clicked=self._journal_current_week)
        _set_standard_icon(
            self.journal_today_button, QtWidgets.QStyle.SP_DirHomeIcon, "回到本週"
        )
        toolbar.addWidget(self.journal_today_button)
        self.journal_next_button = QtWidgets.QPushButton("下一週", clicked=self._journal_next_week)
        _set_standard_icon(
            self.journal_next_button, QtWidgets.QStyle.SP_ArrowRight, "查看下一週"
        )
        toolbar.addWidget(self.journal_next_button)
        toolbar.addSpacing(12)
        self.journal_week_label = QtWidgets.QLabel()
        self.journal_week_label.setProperty("header", True)
        toolbar.addWidget(self.journal_week_label)
        toolbar.addStretch(1)
        self.journal_jump_label = QtWidgets.QLabel("跳到日期")
        toolbar.addWidget(self.journal_jump_label)
        self.journal_date_edit = QtWidgets.QDateEdit()
        self.journal_date_edit.setCalendarPopup(True)
        self.journal_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.journal_date_edit.setMaximumDate(QtCore.QDate.currentDate())
        self.journal_date_edit.setDate(QtCore.QDate.currentDate())
        self.journal_date_edit.dateChanged.connect(self._journal_jump_to_date)
        toolbar.addWidget(self.journal_date_edit)
        layout.addLayout(toolbar)

        hint = QtWidgets.QLabel(
            "每日金額變化＝股數 ×（當日收盤－前一交易日收盤）；週一通常與前週五比較。"
            "這是整日價格變化，不代表盤中實際成交損益。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        cards_widget = QtWidgets.QWidget()
        cards_layout = QtWidgets.QGridLayout(cards_widget)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(6)
        self.journal_card_group = QtWidgets.QButtonGroup(self)
        self.journal_card_group.setExclusive(True)
        self.journal_cards = []
        for index in range(7):
            card = QtWidgets.QPushButton()
            card.setCheckable(True)
            card.setMinimumHeight(164)
            card.setMinimumWidth(92)
            card.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            card.clicked.connect(
                lambda _checked, day_index=index: self._select_journal_day_index(day_index)
            )
            self.journal_card_group.addButton(card, index)
            self.journal_cards.append(card)
            cards_layout.addWidget(card, 0, index)
            cards_layout.setColumnStretch(index, 1)
        layout.addWidget(cards_widget)

        detail_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        holdings_panel = QtWidgets.QWidget()
        holdings_layout = QtWidgets.QVBoxLayout(holdings_panel)
        holdings_layout.setContentsMargins(0, 0, 6, 0)
        self.journal_detail_title = QtWidgets.QLabel()
        self.journal_detail_title.setProperty("header", True)
        holdings_layout.addWidget(self.journal_detail_title)
        self.journal_source_label = QtWidgets.QLabel()
        self.journal_source_label.setProperty("muted", True)
        self.journal_source_label.setWordWrap(True)
        holdings_layout.addWidget(self.journal_source_label)
        self.journal_holdings_table = QtWidgets.QTableWidget(0, 6)
        self.journal_holdings_table.setHorizontalHeaderLabels(
            ["代號", "名稱", "股數", "收盤", "漲跌%", "金額變化"]
        )
        self.journal_holdings_table.verticalHeader().setVisible(False)
        self.journal_holdings_table.setAlternatingRowColors(True)
        self.journal_holdings_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        journal_header = self.journal_holdings_table.horizontalHeader()
        journal_header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        journal_header.setMinimumSectionSize(54)
        holdings_layout.addWidget(self.journal_holdings_table, 1)

        editor_panel = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(6, 0, 0, 0)
        editor_header = QtWidgets.QLabel("交易日誌")
        editor_header.setProperty("header", True)
        editor_layout.addWidget(editor_header)
        editor_hint = QtWidgets.QLabel("自由記錄交易心情、犯錯的地方與當日體悟。")
        editor_hint.setProperty("muted", True)
        editor_layout.addWidget(editor_hint)
        self.journal_note_edit = QtWidgets.QPlainTextEdit()
        self.journal_note_edit.setPlaceholderText(
            "今天做得如何？當下的心情是什麼？\n有哪些錯誤、值得保留的判斷或新的體悟？"
        )
        self.journal_note_edit.textChanged.connect(self._on_journal_note_changed)
        editor_layout.addWidget(self.journal_note_edit, 1)
        save_row = QtWidgets.QHBoxLayout()
        self.journal_save_status = QtWidgets.QLabel("")
        self.journal_save_status.setProperty("muted", True)
        save_row.addWidget(self.journal_save_status)
        save_row.addStretch(1)
        self.journal_save_button = accent_button("儲存日誌", self._save_journal_clicked)
        _set_standard_icon(
            self.journal_save_button, QtWidgets.QStyle.SP_DialogSaveButton, "儲存交易日誌 (Ctrl+S)"
        )
        save_row.addWidget(self.journal_save_button)
        editor_layout.addLayout(save_row)

        detail_splitter.addWidget(holdings_panel)
        detail_splitter.addWidget(editor_panel)
        detail_splitter.setStretchFactor(0, 3)
        detail_splitter.setStretchFactor(1, 2)
        layout.addWidget(detail_splitter, 1)

        save_shortcut = QtGui.QShortcut(QtGui.QKeySequence.Save, self.journal_tab)
        save_shortcut.activated.connect(self._save_journal_clicked)
        self._journal_save_shortcut = save_shortcut
        self._refresh_journal_week()

    @staticmethod
    def _journal_money(value):
        return "N/A" if value is None else f"{value:+,.0f}"

    def _refresh_journal_week(self):
        week = load_week(
            HISTORY_DB_PATH,
            self.journal_week_start,
            self.positions,
        )
        self.journal_days = {day.date: day for day in week}
        end = self.journal_week_start + timedelta(days=6)
        if getattr(self, "_compact_layout", False):
            week_text = f"{self.journal_week_start:%m/%d}－{end:%m/%d}"
        else:
            week_text = f"{self.journal_week_start:%Y/%m/%d}－{end:%Y/%m/%d}"
        self.journal_week_label.setText(week_text)
        current_week = date.today() - timedelta(days=date.today().weekday())
        self.journal_next_button.setEnabled(self.journal_week_start < current_week)
        weekday_names = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")
        for index, (card, day) in enumerate(zip(self.journal_cards, week)):
            is_future = day.date > date.today().isoformat()
            card.setEnabled(not is_future)
            card.setProperty("journalDate", day.date)
            moves = sorted(
                (move for move in day.holdings if move.change_amount is not None),
                key=lambda move: abs(move.change_amount),
                reverse=True,
            )[:3]
            if is_future:
                summary = "未來日期"
            elif not day.market_open:
                summary = "休市／無行情"
            elif not day.holdings:
                summary = "無持股"
            else:
                prefix = "約 " if day.holding_source == "estimated" else ""
                summary = f"合計 {prefix}{self._journal_money(day.total_change_amount)}"
            lines = [weekday_names[index], day.date[5:], summary]
            lines.extend(
                f"{move.ticker} {move.change_pct:+.2f}% {move.change_amount:+,.0f}"
                for move in moves
            )
            first_note_line = next(
                (line.strip() for line in day.note.splitlines() if line.strip()), ""
            )
            if first_note_line:
                lines.append("✎ " + first_note_line[:22] + ("…" if len(first_note_line) > 22 else ""))
            card.setText("\n".join(lines))
            color = (
                COLOR_GAIN if day.total_change_amount is not None and day.total_change_amount > 0
                else COLOR_LOSS if day.total_change_amount is not None and day.total_change_amount < 0
                else COLOR_MUTED
            )
            checked = day.date == self.journal_selected_date
            card.setChecked(checked)
            card.setStyleSheet(
                "QPushButton { text-align:left; padding:9px; "
                f"font-size:{10 if getattr(self, '_compact_layout', False) else 11}px; "
                f"color:{color}; border:1px solid {COLOR_BORDER}; background:{COLOR_SURFACE}; }}"
                f"QPushButton:checked {{ border:2px solid {COLOR_ACCENT}; background:{COLOR_SELECTED_BG}; }}"
                f"QPushButton:disabled {{ background:{COLOR_DISABLED_BG}; color:{COLOR_DISABLED_TEXT}; }}"
            )
        if self.journal_selected_date not in self.journal_days:
            self.journal_selected_date = self.journal_week_start.isoformat()
        self._render_journal_day()

    def _render_journal_day(self):
        day = self.journal_days.get(self.journal_selected_date)
        if day is None:
            return
        selected_date = date.fromisoformat(day.date)
        self.journal_detail_title.setText(f"{day.date}　持股漲跌明細")
        if day.holding_source == "estimated":
            source = "估算持股：依目前持股與進場日回推，過去加減碼及已賣出股票可能未包含。"
        elif day.holding_source == "future":
            source = "未來日期不提供持股與日誌編輯。"
        elif day.snapshot_date == day.date:
            source = f"已記錄持股快照：{day.snapshot_date}"
        else:
            source = f"持股沿用最近快照：{day.snapshot_date or '尚無快照'}"
        if not day.market_open and day.holding_source != "future":
            source += "　｜　休市或尚無當日行情。"
        self.journal_source_label.setText(source)

        self.journal_holdings_table.setRowCount(len(day.holdings))
        for row, move in enumerate(day.holdings):
            values = (
                move.ticker,
                move.name or "-",
                f"{move.shares:,}",
                "N/A" if move.close is None else f"{move.close:.2f}",
                "N/A" if move.change_pct is None else f"{move.change_pct:+.2f}%",
                self._journal_money(move.change_amount),
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column in (0, 2, 3, 4, 5):
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if column in (4, 5) and move.change_amount is not None:
                    item.setForeground(
                        QtGui.QColor(gain_loss_color(move.change_amount))
                    )
                self.journal_holdings_table.setItem(row, column, item)

        self._journal_loading = True
        self.journal_note_edit.setPlainText(day.note)
        self._journal_loading = False
        editable = selected_date <= date.today()
        self.journal_note_edit.setEnabled(editable)
        self.journal_save_button.setEnabled(editable)
        self.journal_dirty = False
        self.journal_save_status.setText("已儲存" if day.note else "尚無日誌")

    def _select_journal_day_index(self, index):
        self._save_journal_if_dirty()
        self.journal_selected_date = (
            self.journal_week_start + timedelta(days=index)
        ).isoformat()
        self._refresh_journal_week()

    def _on_journal_note_changed(self):
        if self._journal_loading:
            return
        self.journal_dirty = True
        self.journal_save_status.setText("尚未儲存")

    def _save_journal_if_dirty(self):
        if not getattr(self, "journal_dirty", False):
            return
        if self.journal_selected_date > date.today().isoformat():
            return
        save_journal_entry(
            HISTORY_DB_PATH,
            self.journal_selected_date,
            self.journal_note_edit.toPlainText(),
        )
        self.journal_dirty = False
        self.journal_save_status.setText("已儲存")

    def _save_journal_clicked(self):
        if self.journal_selected_date > date.today().isoformat():
            return
        if self.journal_selected_date == date.today().isoformat():
            save_portfolio_snapshot(HISTORY_DB_PATH, self.journal_selected_date, self.positions)
        save_journal_entry(
            HISTORY_DB_PATH,
            self.journal_selected_date,
            self.journal_note_edit.toPlainText(),
        )
        self.journal_dirty = False
        self._refresh_journal_week()
        self.journal_save_status.setText("已儲存")

    def _set_journal_week(self, target):
        self._save_journal_if_dirty()
        current_week = date.today() - timedelta(days=date.today().weekday())
        target = min(target, current_week)
        self.journal_week_start = target
        self.journal_selected_date = (
            date.today().isoformat() if target == current_week else target.isoformat()
        )
        blocker = QtCore.QSignalBlocker(self.journal_date_edit)
        self.journal_date_edit.setDate(
            QtCore.QDate.fromString(self.journal_selected_date, "yyyy-MM-dd")
        )
        del blocker
        self._refresh_journal_week()

    def _journal_previous_week(self):
        self._set_journal_week(self.journal_week_start - timedelta(days=7))

    def _journal_next_week(self):
        self._set_journal_week(self.journal_week_start + timedelta(days=7))

    def _journal_current_week(self):
        today = date.today()
        self._set_journal_week(today - timedelta(days=today.weekday()))

    def _journal_jump_to_date(self, qdate):
        target = date(qdate.year(), qdate.month(), qdate.day())
        self._save_journal_if_dirty()
        self.journal_week_start = target - timedelta(days=target.weekday())
        self.journal_selected_date = target.isoformat()
        self._refresh_journal_week()

    # ---------- 個股頁 ----------

    def _build_stocks_tab(self):
        layout = QtWidgets.QVBoxLayout(self.stocks_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("搜尋"))
        self.stock_search_edit = QtWidgets.QLineEdit()
        self.stock_search_edit.setPlaceholderText("搜尋股票代號或名稱")
        self.stock_search_edit.setMinimumWidth(160)
        self.stock_search_edit.setMaximumWidth(320)
        self.stock_search_edit.addAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView),
            QtWidgets.QLineEdit.LeadingPosition,
        )
        self.stock_search_edit.textChanged.connect(self._filter_stocks_tree)
        toolbar.addWidget(self.stock_search_edit)

        hint = QtWidgets.QLabel(
            "雙擊股票查詢本益比／法人買賣（上市透過 FinMind API，上櫃本益比／殖利率"
            "改查 TPEX 官方端點；法人買賣不分市場皆為 FinMind）"
        )
        hint.setProperty("muted", True)
        hint.setToolTip(hint.text())
        self.stock_search_hint = hint
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
        self.stocks_tree.currentItemChanged.connect(self._on_stock_selected)

        self.stock_preview = QtWidgets.QFrame()
        self.stock_preview.setProperty("stockHero", True)
        preview_layout = QtWidgets.QVBoxLayout(self.stock_preview)
        preview_layout.setContentsMargins(18, 18, 18, 18)
        preview_caption = QtWidgets.QLabel("個股摘要")
        preview_caption.setProperty("muted", True)
        self.stock_preview_title = QtWidgets.QLabel("選取股票查看摘要")
        self.stock_preview_title.setProperty("header", True)
        self.stock_preview_price = StatCard(bordered=False)
        self.stock_preview_meta = QtWidgets.QLabel("雙擊股票可開啟完整估值與法人資訊。")
        self.stock_preview_meta.setProperty("muted", True)
        self.stock_preview_meta.setWordWrap(True)
        self.stock_preview_button = accent_button("查看完整分析", self._open_selected_stock_detail)
        _set_standard_icon(
            self.stock_preview_button,
            QtWidgets.QStyle.SP_FileDialogInfoView,
            "開啟完整個股分析",
        )
        self.stock_preview_button.setEnabled(False)
        self.stock_preview_trend_row = QtWidgets.QHBoxLayout()
        self.stock_preview_trend_row.setSpacing(6)
        preview_layout.addWidget(preview_caption)
        preview_layout.addWidget(self.stock_preview_title)
        preview_layout.addSpacing(8)
        preview_layout.addWidget(self.stock_preview_price)
        preview_layout.addLayout(self.stock_preview_trend_row)
        preview_layout.addWidget(self.stock_preview_meta)
        preview_layout.addStretch(1)
        preview_layout.addWidget(self.stock_preview_button)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self.stocks_tree)
        splitter.addWidget(self.stock_preview)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([850, 260])
        layout.addWidget(splitter, 1)

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
                child.setData(0, QtCore.Qt.UserRole + 2, industry)
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
        """更新「個股」頁的 FinMind 用量提醒；免費額度是
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

        label = getattr(self, "finmind_count_label", None)
        if label is not None:
            label.setText(text)
            label.setStyleSheet(f"color: {color};")

    def _on_stock_double_clicked(self, item, _column):
        ticker = item.data(0, QtCore.Qt.UserRole)
        if not ticker:
            return  # 點到的是產業分組列，不是個股
        name = item.text(0)
        market = item.data(0, QtCore.Qt.UserRole + 1)
        token = self.settings.get("finmind_token", "")
        StockDetailDialog(self, ticker, name, token, market=market).exec()

    def _on_stock_selected(self, item, _previous=None):
        ticker = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        if not ticker:
            self.stock_preview_title.setText("選取股票查看摘要")
            self.stock_preview_price.set_value("—")
            self.stock_preview_meta.setText("雙擊股票可開啟完整估值與法人資訊。")
            self.stock_preview_button.setEnabled(False)
            _clear_layout(self.stock_preview_trend_row)
            return
        price = self.snapshot.get(ticker)
        name = item.text(0)
        market = item.data(0, QtCore.Qt.UserRole + 1) or "—"
        industry = item.data(0, QtCore.Qt.UserRole + 2) or "未分類"
        self.stock_preview_title.setText(f"{name}　{ticker}")
        _populate_trend_badge_row(
            self.stock_preview_trend_row, load_local_technical(HISTORY_DB_PATH, ticker)
        )
        if price is not None and price.close is not None:
            change_pct = _change_pct(price)
            change_text = f"{change_pct:+.2f}%" if change_pct is not None else "—"
            status = None if change_pct is None else ("positive" if change_pct >= 0 else "negative")
            self.stock_preview_price.set_value(
                f"{price.close:,.2f}　{change_text}", status=status
            )
            volume_text = f"{(price.volume or 0) / 1000:,.0f} 張"
            self.stock_preview_meta.setText(
                f"{market} · {industry}\n成交量 {volume_text}\n資料日期 {price.date or '—'}"
            )
        else:
            self.stock_preview_price.set_value("暫無行情")
            self.stock_preview_meta.setText(f"{market} · {industry}")
        self.stock_preview_button.setEnabled(True)

    def _open_selected_stock_detail(self):
        item = self.stocks_tree.currentItem()
        if item is not None:
            self._on_stock_double_clicked(item, 0)

    # ---------- 期貨頁（TAIFEX 官方盤後行情） ----------
    # 原本串接 Fugle 的 data-futopt 即時行情，但查證 developer.fugle.tw 定價文件
    # 後發現期貨/選擇權完全不在免費方案內（連歷史/盤後資料都沒有，只有 intraday
    # 且需要付費 Developer 方案）。改用 TAIFEX 官方公開、免金鑰的「期貨每日交易
    # 行情」，只反映盤後（EOD）資訊，不是即時報價——跟 TWSE/TPEX 股票走 EOD 的既有
    # 原則一致，見 tradingnote_taifex.py。一天只更新一次，不需要背景輪詢計時器，
    # 隨「重新整理」（見 _apply_refresh_result）跟股票資料一起刷新即可。

    FUTURES_COLUMNS = [
        "商品", "標的", "契約月份", "時段", "最後成交", "漲跌", "漲跌%",
        "結算價", "成交量", "未沖銷契約數",
        "大額前10買", "大額前10賣", "大額前10淨", "大額買佔比",
    ]
    FUTURES_SESSION_ORDER = ["一般", "盤後"]

    def _build_futures_tab(self):
        layout = QtWidgets.QVBoxLayout(self.futures_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("搜尋"))
        self.futures_search_edit = QtWidgets.QLineEdit()
        self.futures_search_edit.setPlaceholderText("輸入代號／名稱，如 TX／台積電／2330／布蘭特")
        self.futures_search_edit.setMinimumWidth(170)
        self.futures_search_edit.setMaximumWidth(320)
        self.futures_search_edit.addAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView),
            QtWidgets.QLineEdit.LeadingPosition,
        )
        self.futures_search_edit.textChanged.connect(self._filter_futures_table)
        toolbar.addWidget(self.futures_search_edit)

        hint = QtWidgets.QLabel("盤後行情 · 點選商品查看大額交易人部位，雙擊查看完整明細。")
        hint.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        hint.setToolTip("可搜尋契約代碼、股票代號或名稱。大額前10欄位為近月所有交易人部位；一般為日盤，盤後為夜盤。資料每日更新，可至設定重新整理。")
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
        self.futures_table.cellDoubleClicked.connect(self._on_futures_row_double_clicked)
        self.futures_table.itemSelectionChanged.connect(self._on_futures_row_selected)
        for col, width in enumerate(
            [70, 130, 90, 60, 90, 80, 70, 90, 90, 110, 95, 95, 95, 90]
        ):
            self.futures_table.setColumnWidth(col, width)

        # 表格（上）＋大額交易人未沖銷部位明細面板（下），用 QSplitter 分隔可調高度，
        # 跟「部位紀錄」頁「表格＋詳細區塊」的排版一致。
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.addWidget(self.futures_table)
        splitter.addWidget(self._build_futures_lt_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        self._futures_snapshot = {}
        self._futures_all_products = list(DEFAULT_FUTURES_PRODUCTS)
        self._futures_large_traders_rows = []
        self._futures_lt_by_code = {}
        self._futures_ssf_map = {}
        self._futures_name_map = {}
        self._rebuild_futures_table()

    def _build_futures_lt_panel(self):
        """「期貨」頁表格下方的常駐明細面板：點選某商品即顯示該商品完整的大額
        交易人未沖銷部位（各到期月份 × 所有交易人／特定法人），跟雙擊彈窗
        （FuturesLargeTradersDialog）共用同一份填表邏輯 _populate_large_traders_table。"""
        container = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(container)
        panel_layout.setContentsMargins(0, 6, 0, 0)

        self.futures_lt_detail_label = QtWidgets.QLabel(
            "點選上方商品，這裡顯示其大額交易人未沖銷部位（各到期月份 × "
            "所有交易人／特定法人）。"
        )
        self.futures_lt_detail_label.setWordWrap(True)
        self.futures_lt_detail_label.setProperty("muted", True)
        panel_layout.addWidget(self.futures_lt_detail_label)

        self.futures_lt_detail_table = QtWidgets.QTableWidget(
            0, len(LARGE_TRADERS_TABLE_COLUMNS)
        )
        self.futures_lt_detail_table.setHorizontalHeaderLabels(LARGE_TRADERS_TABLE_COLUMNS)
        self.futures_lt_detail_table.verticalHeader().setVisible(False)
        self.futures_lt_detail_table.setAlternatingRowColors(True)
        self.futures_lt_detail_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.futures_lt_detail_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)

        # 趨勢圖：選取商品的前10大買/賣方未沖銷部位逐日走勢（歷史由背景回補累積，
        # 見 _sync_large_traders_history）。放在明細表右邊，用 QSplitter 可調寬度。
        self.futures_lt_trend_chart = pg.PlotWidget()
        self.futures_lt_trend_chart.setBackground(COLOR_SURFACE)
        self.futures_lt_trend_chart.showGrid(x=True, y=True, alpha=0.08)
        self.futures_lt_trend_chart.addLegend()
        self.futures_lt_trend_chart.setMinimumHeight(200)

        inner = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        inner.addWidget(self.futures_lt_detail_table)
        inner.addWidget(self.futures_lt_trend_chart)
        inner.setStretchFactor(0, 3)
        inner.setStretchFactor(1, 2)
        panel_layout.addWidget(inner)
        return container

    def _futures_display_name(self, product):
        """商品在「期貨」頁顯示用的名稱：股票期貨用「股票簡稱 代號」（台積電 2330），
        其餘（指數／商品期貨）用大額端點的中文契約名（布蘭特原油期貨），都沒有則空字串。"""
        ssf = self._futures_ssf_map.get(product)
        if ssf:
            return f"{ssf['stock_name'] or ''} {ssf['stock_code'] or ''}".strip()
        return self._futures_name_map.get(product) or ""

    def _futures_groups_for_product(self, product):
        """回傳某日盤商品在大額端點對應的分組（自動換算去尾 F 的代碼）；查無回空清單。"""
        lt_code = large_traders_code_for_product(product, self._futures_ssf_map)
        return self._futures_lt_by_code.get(lt_code, [])

    def _on_futures_row_selected(self):
        selected = self.futures_table.selectionModel().selectedRows()
        if not selected:
            return
        row = selected[0].row()
        if row < 0 or row >= len(self._futures_rows):
            return
        product, _session = self._futures_rows[row]
        groups = self._futures_groups_for_product(product)
        name = self._futures_display_name(product)
        header = f"{product} {name}".strip()
        if groups:
            self.futures_lt_detail_label.setText(f"{header}　大額交易人未沖銷部位（單位：口數）")
        else:
            self.futures_lt_detail_label.setText(
                f"{header}：TAIFEX 未單獨提供此商品的大額交易人未沖銷部位。"
            )
        _populate_large_traders_table(self.futures_lt_detail_table, groups)

        # 趨勢圖用歷史表（大額端點代碼、所有契約合計、所有交易人）。
        lt_code = large_traders_code_for_product(product, self._futures_ssf_map)
        trend = get_large_traders_history_series(
            HISTORY_DB_PATH, lt_code, LARGE_TRADERS_HISTORY_ALL_CONTRACTS_MONTH, "0"
        )
        _populate_large_traders_trend(self.futures_lt_trend_chart, trend)

    def _on_futures_row_double_clicked(self, row, _col):
        """雙擊某商品列 → 彈出該商品的大額交易人未沖銷部位（用「期貨」頁重新
        整理時已抓好、快取在 self._futures_large_traders_rows 的全市場清單就地
        篩出，不另打 API）。尚未抓到大額資料時提示稍後重試。"""
        if row < 0 or row >= len(self._futures_rows):
            return
        product, _session = self._futures_rows[row]
        # 股票期貨在大額端點的代碼是去掉結尾 F（CDF→CD），指數／商品期貨沿用原碼；
        # 換算＋查分組都在 _futures_groups_for_product 裡處理（用重新整理時已建好的
        # self._futures_lt_by_code，不另打 API）。
        groups = self._futures_groups_for_product(product)
        if not groups:
            # 區分兩種空：整份大額資料還沒載入（重新整理可解決）vs. 已載入但
            # TAIFEX 這支端點本來就沒有這個商品（只涵蓋部分主要契約，例如小型
            # 臺指 MTX 併入臺股期貨 TX 統計、不單獨列出，重新整理也不會有）。
            if not self._futures_large_traders_rows:
                message = (
                    "大額交易人未沖銷部位資料尚未載入，請在「設定」頁按"
                    "「重新整理所有資料」後再試。"
                )
            else:
                message = (
                    f"TAIFEX 未單獨提供 {product} 的大額交易人未沖銷部位統計"
                    "（此端點僅涵蓋部分主要契約；小型臺指 MTX 等併入對應大型契約"
                    "如臺股期貨 TX 一併統計）。"
                )
            QtWidgets.QMessageBox.information(self, "大額交易人未沖銷部位", message)
            return
        FuturesLargeTradersDialog(self, product, groups).exec()

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

        underlying_text = self._futures_display_name(product) or "-"

        # 大額交易人摘要欄（近月「所有交易人」前10大未沖銷部位）。
        summary = _large_traders_summary_entry(
            self._futures_groups_for_product(product), data.get("contract_month")
        )
        if summary:
            t10_buy, t10_sell, market_oi = (
                summary["top10_buy"], summary["top10_sell"], summary["market_oi"],
            )
            lt_net = (t10_buy or 0) - (t10_sell or 0)
            lt_buy_text = f"{t10_buy:,}" if t10_buy is not None else "-"
            lt_sell_text = f"{t10_sell:,}" if t10_sell is not None else "-"
            lt_net_text = f"{lt_net:+,}"
            lt_share_text = f"{(t10_buy or 0) / market_oi * 100:.1f}%" if market_oi else "-"
        else:
            lt_net = None
            lt_buy_text = lt_sell_text = lt_net_text = lt_share_text = "-"

        values = [
            product, underlying_text, contract_month, session,
            last_text, change_text, change_pct_text,
            settlement_text, volume_text, oi_text,
            lt_buy_text, lt_sell_text, lt_net_text, lt_share_text,
        ]
        for col, text in enumerate(values):
            item = QtWidgets.QTableWidgetItem(text)
            if col in (4, 5) and color:
                item.setForeground(QtGui.QColor(color))
            if col == 12 and lt_net is not None:  # 大額前10淨：正紅負綠
                item.setForeground(QtGui.QColor(gain_loss_color(lt_net)))
            self.futures_table.setItem(index, col, item)

    def refresh_futures_tab(self, force_refresh=False):
        def fetch():
            rows = get_cached_daily_futures_report(FUTURES_CACHE_PATH, force_refresh=force_refresh)
            all_products = list_all_products(rows)
            # 大額交易人未沖銷部位是另一支 TAIFEX 端點，抓失敗（且無舊快取）時
            # 不該讓整個期貨盤後表格跟著壞掉——退回空清單，雙擊時再提示稍後重試。
            try:
                large_traders_rows = get_cached_large_traders_futures_report(
                    FUTURES_LARGE_TRADERS_CACHE_PATH, force_refresh=force_refresh
                )
            except PriceFetchError:
                large_traders_rows = []
            # 股票期貨標的清單也是輔助資訊，抓失敗不該讓主行情表跟著壞——退回空 map，
            # 個股期貨那幾列的「標的」欄暫時顯示 "-"、仍可用契約代碼搜尋。
            try:
                ssf_map = build_ssf_map(
                    get_cached_ssf_list(FUTURES_SSF_CACHE_PATH, force_refresh=force_refresh)
                )
            except PriceFetchError:
                ssf_map = {}
            return (
                all_products,
                get_futures_snapshot(all_products, rows=rows),
                large_traders_rows,
                ssf_map,
            )

        def on_done(result):
            all_products, snapshot, large_traders_rows, ssf_map = result
            self._futures_all_products = all_products
            self._futures_snapshot = snapshot
            self._futures_large_traders_rows = large_traders_rows
            self._futures_lt_by_code = group_large_traders_all(large_traders_rows)
            self._futures_ssf_map = ssf_map
            # 非股票期貨的中文名（供顯示／搜尋）：用大額端點的 ContractName，key 換算
            # 成日盤商品代碼。股票期貨的名稱另外走 ssf_map（_futures_display_name 處理）。
            lt_name_map = build_large_traders_name_map(large_traders_rows)
            self._futures_name_map = {
                product: lt_name_map[code]
                for product in all_products
                for code in [large_traders_code_for_product(product, ssf_map)]
                if code in lt_name_map
            }
            data_date = _futures_snapshot_date(snapshot)
            self.futures_status_label.setText(f"資料日期：{data_date}" if data_date else "")
            self._rebuild_futures_table()
            self._refresh_data_freshness_label()

        def on_error(message):
            self.futures_status_label.setText(f"期貨盤後資訊取得失敗：{message}")

        self._futures_task_timer = run_task_in_thread(self, fetch, on_done, on_error)

    def _filter_futures_table(self, text):
        """依關鍵字（不分大小寫、子字串比對）篩選期貨表格：**關鍵字為空時顯示
        全部商品**，輸入關鍵字後只留符合的列。比對字串含契約代碼（TX／CDF）、股票
        期貨標的代號（2330）與名稱（台積電，見 self._futures_ssf_map），以及大額
        端點提供的中文契約名（布蘭特原油期貨，見 self._futures_name_map）。"""
        keyword = text.strip().lower()
        for index, (product, _session) in enumerate(self._futures_rows):
            if not keyword:
                self.futures_table.setRowHidden(index, False)
                continue
            haystack = product.lower()
            ssf = self._futures_ssf_map.get(product)
            if ssf:
                haystack += f" {ssf['stock_code'] or ''} {(ssf['stock_name'] or '').lower()}"
            name = self._futures_name_map.get(product)
            if name:
                haystack += f" {name.lower()}"
            self.futures_table.setRowHidden(index, keyword not in haystack)

    # ---------- 設定頁 ----------

    def _build_settings_tab(self):
        outer = QtWidgets.QVBoxLayout(self.settings_tab)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QtWidgets.QVBoxLayout(content)
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
        refresh_all_button = accent_button("重新整理所有資料", self.force_refresh)
        _set_standard_icon(
            refresh_all_button, QtWidgets.QStyle.SP_BrowserReload, "重新整理所有資料"
        )
        layout.addWidget(refresh_all_button, alignment=QtCore.Qt.AlignLeft)
        layout.addSpacing(4)

        self.data_freshness_label = QtWidgets.QLabel("")
        self.data_freshness_label.setProperty("muted", True)
        self.data_freshness_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.data_freshness_label)
        self._refresh_data_freshness_label()
        layout.addSpacing(20)

        self.auto_check_box = QtWidgets.QCheckBox(
            "每次啟動自動檢測（TWSE 股價、期貨大額交易人未沖銷部位歷史有缺口時自動補齊）"
        )
        self.auto_check_box.setChecked(self.settings.get("auto_check_continuity", True))
        self.auto_check_box.toggled.connect(self._on_auto_check_toggle)
        layout.addWidget(self.auto_check_box)

        hint = QtWidgets.QLabel("關閉後，仍可用下方按鈕手動回補。")
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        layout.addSpacing(10)

        backfill_days_row = QtWidgets.QHBoxLayout()
        backfill_days_row.addWidget(
            QtWidgets.QLabel("回補天數（上市 TWSE／上櫃 TPEX 股價、期貨大額交易人歷史共用）")
        )
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

        layout.addSpacing(10)
        valuation_backfill_hint = QtWidgets.QLabel(
            "「資金流向分析」頁泡泡圖的「估值」模式（PER/PBR）只看每檔股票最新一筆"
            "本益比／淨值比，靠每次「重新整理」／啟動時自動存的估值快照即可，"
            "不需要另外回補歷史資料，也沒有對應的設定選項。"
        )
        valuation_backfill_hint.setProperty("muted", True)
        valuation_backfill_hint.setWordWrap(True)
        layout.addWidget(valuation_backfill_hint)

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

    def closeEvent(self, event):
        """離開前保存使用者正在編輯的週誌內容。"""
        try:
            if hasattr(self, "journal_note_edit"):
                self._save_journal_if_dirty()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self,
                "週誌尚未儲存",
                f"交易週誌儲存失敗：{exc}\n\n請先處理問題後再關閉程式。",
            )
            event.ignore()
            return
        event.accept()

def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    pg.setConfigOptions(antialias=True, background=COLOR_SURFACE, foreground=COLOR_TEXT)

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
