#!/usr/bin/env python3
"""tradingnote - 圖形介面版本（PySide6 + pyqtgraph）"""

import gc

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from tradingnote_cache import TTLCache
from tradingnote_concepts import build_classification_catalog, build_ticker_concept_map, load_concepts
from tradingnote_core import (
    get_market_snapshot,
    load_positions,
    load_settings,
    snapshot_staleness_warnings,
)
from tradingnote_flow import FlowAnalysisService
from tradingnote_journal import initialize_journal
from tradingnote_tasks import wait_for_background_tasks

from ui import app_paths
from ui.dialogs.progress import RefreshDialog, StartupProgressDialog
from ui.format import _format_fetched_at, _snapshot_date
from ui.pages.institutional_flow_page import InstitutionalFlowPage
from ui.tabs.flow_tab import FlowTabMixin
from ui.tabs.futures_tab import FuturesTabMixin
from ui.tabs.journal_tab import JournalTabMixin
from ui.tabs.positions_tab import PositionsTabMixin
from ui.tabs.settings_tab import SettingsTabMixin
from ui.tabs.stocks_tab import StocksTabMixin
from ui.theme import COLOR_SURFACE, COLOR_TEXT, STYLESHEET
from ui.widgets import _center_on_screen, _screen_fit_size, _set_standard_icon
from ui.workers import run_startup_preload_in_thread, run_task_in_thread

# 背景檢查「今天的資料是否已發布」的輪詢間隔。get_market_snapshot 本身有 30 分鐘
# 快取（tradingnote_core.CACHE_TTL_SECONDS），所以就算這裡設得比 30 分鐘短，實際
# 打 TWSE／TPEX 的頻率仍受快取保護，不會因為輪詢變密就增加對外部 API 的負擔。
NEW_DATA_CHECK_INTERVAL_MS = 5 * 60 * 1000


class TradingNoteWindow(
    FlowTabMixin,
    StocksTabMixin,
    PositionsTabMixin,
    JournalTabMixin,
    FuturesTabMixin,
    SettingsTabMixin,
    QtWidgets.QMainWindow,
):
    """主視窗外殼：左側導覽、頁面堆疊、狀態列、「有新資料」提示與全域重新整理。

    各分頁的建立／刷新／事件處理放在 ui/tabs/*_tab.py 的 mixin 裡，
    透過多重繼承併入；mixin 方法直接共用這個視窗的 self 狀態。"""

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

        self.settings = load_settings(app_paths.SETTINGS_PATH)
        self.positions = load_positions(app_paths.POSITIONS_PATH)
        initialize_journal(app_paths.HISTORY_DB_PATH, self.positions)
        # 概念目錄在啟動時讀入一次；重建 concepts.json 後需重開程式，
        # 才會讓持股概念標籤與資金流向分類同時更新。
        self._concepts = load_concepts()
        self._ticker_concept_map = build_ticker_concept_map(self._concepts)
        self._classification_catalog = build_classification_catalog(self._concepts)
        self.snapshot = snapshot
        self.last_error = last_error
        self.flow_service = FlowAnalysisService(
            app_paths.HISTORY_DB_PATH,
            classification_catalog=self._classification_catalog,
            institutional_cache_path=app_paths.INSTITUTIONAL_CACHE_PATH,
        )
        self.staleness_warning = (
            "；".join(snapshot_staleness_warnings(self.snapshot)) if self.snapshot else ""
        )

        # 「個股」頁選取項目即時查技術指標用的行程內快取＋debounce 狀態：
        # load_local_technical 內含 SQLite 查詢＋全歷史指標運算，直接同步跑在
        # 選取事件裡會卡住 UI（尤其方向鍵快速瀏覽時每格都觸發一次），改成背景
        # 執行緒＋短 TTL 快取，同一檔位在 TTL 內重選不必重算。
        # 資金流向分析（flow_service.analyze，全市場約1700檔聚合）改成背景執行緒
        # 跑，避免直接卡在主執行緒；同一時間只讓一份分析在跑，若跑的期間又有新的
        # 重新整理請求（例如連續切換日期／分類），只記下最新一份請求，等目前這份
        # 跑完再接著跑，不讓多個背景執行緒同時呼叫 analyze()（内部快取字典不是
        # thread-safe，同時呼叫可能互相干擾）。
        self._flow_refresh_in_flight = False
        self._flow_refresh_pending_request = None

        self._stock_technical_cache = TTLCache(ttl_seconds=300)
        self._stock_trend_timer = QtCore.QTimer(self)
        self._stock_trend_timer.setSingleShot(True)
        self._stock_trend_timer.timeout.connect(self._load_stock_trend_badges)
        self._stock_trend_expected_ticker = None

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
        self.institutional_flow_tab = InstitutionalFlowPage(app_paths.HISTORY_DB_PATH)
        self.positions_tab = QtWidgets.QWidget()
        self.journal_tab = QtWidgets.QWidget()
        self.stocks_tab = QtWidgets.QWidget()
        self.futures_tab = QtWidgets.QWidget()
        self.settings_tab = QtWidgets.QWidget()
        # 主功能改用左側導覽列，頁面本體放進右側堆疊；各頁內的技術圖表分頁仍維持
        # QTabWidget，讓主導覽與頁內切換有清楚的視覺層級。
        self._page_specs = (
            (self.flow_tab, "資金流向", "市場族群、排行與法人方向"),
            (self.institutional_flow_tab, "法人資金流去哪？",
             "每天盤後整理外資、投信、自營商在各類股的買賣超，一眼看出資金正在加碼或撤出哪些類股"),
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
            return get_market_snapshot(app_paths.CACHE_PATH, force_refresh=False)

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

    def _update_status_bar(self):
        cache_note = ""
        if app_paths.CACHE_PATH.exists():
            import json as _json

            with app_paths.CACHE_PATH.open("r", encoding="utf-8") as f:
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
    # 2026-09-17：用 PYTHONFAULTHANDLER=1 重現反覆出現的原生層級當機（Fatal
    # Python error: Aborted）發現，兩次抓到的堆疊都停在「Garbage-collecting」
    # 這一步，且都同時有一個背景 daemon thread（run_background_task()）正在
    # 跑 sqlite3／解析報價這類 C extension 呼叫——不只發生在關閉視窗那一刻
    # （直譯器 finalize 會強制跑一次 GC），一次是在正常操作中（雙擊個股跳出
    # StockDetailDialog 那當下）就撞上了，代表只要背景執行緒還在跑，CPython
    # 週期性自動觸發的 GC 隨時都可能跟它互撞。這個 app 幾乎不會用到需要 GC
    # 才能回收的循環參照（一般 refcounting 就夠了），關掉自動 GC 换取穩定性
    # 是合理的取捨；gc.freeze() 把目前（含所有模組層級物件）先凍結進
    # permanent generation，避免之後萬一手動呼叫 gc.collect() 要重新掃過這些
    # 早就穩定不變的物件。
    gc.disable()
    gc.freeze()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    pg.setConfigOptions(antialias=True, background=COLOR_SURFACE, foreground=COLOR_TEXT)
    # 關閉視窗時，run_background_task() 開的 daemon thread 不會被 CPython 等待
    # 就直接進入直譯器關閉流程；上面的 gc.disable() 擋不掉直譯器 finalize
    # 自己強制觸發的最後一次 GC，所以這裡另外用 aboutToQuit（事件迴圈真正
    # 停止前觸發）等在跑的背景執行緒收尾（有限時間內），逾時仍未結束就直接
    # os._exit()，跳過會跟它們互撞的 finalize／GC（見
    # tradingnote_tasks.wait_for_background_tasks）。
    app.aboutToQuit.connect(wait_for_background_tasks)

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
