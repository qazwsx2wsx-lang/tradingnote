#!/usr/bin/env python3
"""tradingnote - 圖形介面版本（PySide6 + pyqtgraph）"""

import gc
from datetime import date

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
    snapshot_staleness_warnings,
    update_position,
)
from tradingnote_history import (
    VOLUME_RATIO_TIERS,
    _change_pct,
    get_available_dates,
    get_industry_directory,
    get_industry_map,
    get_data_revision,
)
from tradingnote_finmind import (
    FINMIND_HOURLY_LIMIT,
    fetch_position_detail,
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
from tradingnote_flow import FlowAnalysisService, FlowPeriod
from tradingnote_http import PriceFetchError
from tradingnote_cache import TTLCache
from tradingnote_tasks import run_background_task, wait_for_background_tasks
from tradingnote_technical import load_local_technical
from tradingnote_journal import initialize_journal, save_portfolio_snapshot

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
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_MUTED,
    COLOR_ACCENT,
    COLOR_CARD_BG,
    COLOR_LOSS,
    STYLESHEET,
)
from ui.format import gain_loss_color  # noqa: E402
from ui.components.stat_card import StatCard  # noqa: E402
from ui.components.signal_badge import SignalBadge  # noqa: E402
from ui.components.section_card import SectionCard  # noqa: E402
from ui.components.insight_card import InsightCard  # noqa: E402
from ui.pages.institutional_flow_page import InstitutionalFlowPage  # noqa: E402
from ui import app_paths
from ui.badges import _flow_momentum_insight, _stock_trend_badges, _populate_badge_row
from ui.format import _snapshot_date, _format_fetched_at
from ui.widgets import (
    accent_button,
    _set_standard_icon,
    _clear_layout,
    _screen_fit_size,
    _center_on_screen,
    _color_swatch_icon,
)
from ui.workers import run_task_in_thread, run_startup_preload_in_thread
from ui.charts.detail import _detail_summary_scroll, _render_detail_summary, DetailChartPanel
from ui.charts.flow_chart import (
    FlowChartWidget,
    _populate_institutional_direction_chart,
    _category_color,
    populate_flow_chart,
)
from ui.dialogs.positions import PositionFormDialog, PriceLookupDialog
from ui.dialogs.progress import StartupProgressDialog, RefreshDialog
from ui.dialogs.stock_detail import StockDetailDialog, IndustryTopStocksDialog
from ui.dialogs.trading_date import TradingDateDialog
from ui.tabs.settings_tab import SettingsTabMixin
from ui.tabs.futures_tab import FuturesTabMixin
from ui.tabs.journal_tab import JournalTabMixin


class TradingNoteWindow(JournalTabMixin, FuturesTabMixin, SettingsTabMixin, QtWidgets.QMainWindow):
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
        self.flow_bubble_mode_combo.addItem("主力同步買超（三大法人方向一致）", "institutional_sync")
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
        # 圖例徽章文字依泡泡圖模式而變（見 _on_flow_bubble_mode_changed）：
        # 動能/估值模式代表當日量價方向，主力同步買超模式代表三大法人一致性。
        self.flow_direction_badge_positive = SignalBadge("偏流入", tone="positive")
        self.flow_direction_badge_negative = SignalBadge("偏流出", tone="negative")
        self.flow_direction_badge_neutral = SignalBadge("中性", tone="neutral")
        legend_row.addWidget(self.flow_direction_badge_positive)
        legend_row.addWidget(self.flow_direction_badge_negative)
        legend_row.addWidget(self.flow_direction_badge_neutral)
        legend_row.addStretch(1)
        layout.addLayout(legend_row)
        layout.addSpacing(4)

        self.flow_chart_hint = QtWidgets.QLabel(
            "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝方向（見上方圖例）；"
            "標示成交佔比前五大族群，滑鼠移至泡泡查看數值。"
        )
        self.flow_chart_hint.setProperty("muted", True)
        self.flow_chart_hint.setWordWrap(True)
        layout.addWidget(self.flow_chart_hint)
        layout.addSpacing(8)
        self.flow_chart = FlowChartWidget()
        self.flow_chart.setMinimumHeight(420)
        layout.addWidget(self.flow_chart, 1)

    def _update_flow_date_button(self):
        period = self.flow_period.resolve(app_paths.HISTORY_DB_PATH, self.snapshot)
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
        if self.flow_bubble_mode_combo.currentData() == "institutional_sync":
            self.flow_direction_badge_positive.set_text_and_tone("主力同步買超", "positive")
            self.flow_direction_badge_negative.set_text_and_tone("主力同步賣超", "negative")
            self.flow_direction_badge_neutral.set_text_and_tone("方向不一致", "neutral")
        else:
            self.flow_direction_badge_positive.set_text_and_tone("偏流入", "positive")
            self.flow_direction_badge_negative.set_text_and_tone("偏流出", "negative")
            self.flow_direction_badge_neutral.set_text_and_tone("中性", "neutral")
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
        valid_dates = sorted(set(d for d in get_available_dates(app_paths.HISTORY_DB_PATH) if d <= end_date)
                             | {p.date for p in self.snapshot.values() if p.date and p.date <= end_date})
        dialog = TradingDateDialog(self, valid_dates, "選擇流向區間起始日期")
        if dialog.exec() == QtWidgets.QDialog.Accepted and dialog.selected_date:
            days = max(1, sum(d >= dialog.selected_date for d in valid_dates))
            self.flow_period = FlowPeriod(dialog.selected_date, days)
            self._update_flow_date_button()
            self.refresh_flow_tab()

    def _refresh_flow_on_revision(self):
        if get_data_revision(app_paths.HISTORY_DB_PATH) != getattr(self, "_flow_data_revision", None):
            self.refresh_flow_tab()

    def refresh_flow_tab(self):
        """啟動（或排入佇列）一次背景資金流向分析；實際套用結果見
        _apply_flow_dashboard。呼叫這個方法本身不會卡住主執行緒。"""
        self._flow_refresh_pending_request = (
            self.snapshot,
            self.flow_period,
            self.flow_bubble_mode_combo.currentData(),
            self.flow_valuation_metric_combo.currentData(),
            self._flow_classification_mode(),
            self._flow_classification_scope(),
        )
        if self._flow_refresh_in_flight:
            return
        self._start_flow_refresh()

    def _start_flow_refresh(self):
        (
            snapshot,
            period,
            bubble_mode,
            valuation_metric,
            classification_mode,
            classification_scope,
        ) = self._flow_refresh_pending_request
        self._flow_refresh_pending_request = None
        self._flow_refresh_in_flight = True
        self._flow_data_revision = get_data_revision(app_paths.HISTORY_DB_PATH)

        def work(_cancel_event, _emit_progress):
            return self.flow_service.analyze(
                snapshot,
                period,
                bubble_mode=bubble_mode,
                valuation_metric=valuation_metric,
                classification_mode=classification_mode,
                classification_scope=classification_scope,
            )

        def on_done(dashboard):
            self._apply_flow_dashboard(dashboard, classification_mode, bubble_mode, valuation_metric)
            self._flow_refresh_in_flight = False
            if self._flow_refresh_pending_request is not None:
                self._start_flow_refresh()

        def on_error(message):
            self._flow_refresh_in_flight = False
            QtWidgets.QMessageBox.warning(self, "錯誤", f"資金流向分析失敗：{message}")
            if self._flow_refresh_pending_request is not None:
                self._start_flow_refresh()

        # 見 _load_stock_trend_badges 同樣的註解：一定要留住回傳值，否則
        # BackgroundTask 可能在背景執行緒做完之前就被回收，callback 不會被呼叫。
        self._flow_refresh_task = run_background_task(self, work, on_done, on_error)

    def _apply_flow_dashboard(self, dashboard, classification_mode, bubble_mode, valuation_metric):
        classification_label = CLASSIFICATION_LABELS[classification_mode]
        overlapping_groups = classification_mode != CLASSIFICATION_INDUSTRY
        period = dashboard.period
        self._display_flow_period = period
        self._update_flow_date_button()
        data_date = period.end_date or "資料日期未明"
        self.flow_chart_header.setText(
            f"{classification_label}資金流向圖　·　{data_date}"
        )
        if bubble_mode == "institutional_sync":
            self.flow_chart_hint.setText(
                "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝三大法人同步方向"
                "（見上方圖例）。僅反映最新一筆三大法人資料，不受「流向區間」影響；"
                "滑鼠移至泡泡可看外資／投信／自營商個別金額。"
            )
        elif overlapping_groups:
            self.flow_chart_hint.setText(
                "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝當日量價方向"
                "（見上方圖例）；泡泡大小為成交涵蓋率。概念可重疊，各群組合計可能"
                "超過 100%。先看淡色象限判斷區間狀態，滑鼠移至泡泡可看完整數值。"
            )
        else:
            self.flow_chart_hint.setText(
                "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝當日量價方向"
                "（見上方圖例）；泡泡大小為成交佔比。先看淡色象限判斷區間狀態，"
                "滑鼠移至泡泡可看完整數值與象限解讀。"
            )
        if bubble_mode != "institutional_sync" and not period.sufficient:
            self.flow_chart_hint.setText(self.flow_chart_hint.text() +
                f" 實際有 {len(period.actual_dates)}／需要 {period.trading_days} 個交易日。")
        stale_count = sum(p.date != period.end_date for p in self.snapshot.values())
        if stale_count:
            self.flow_chart_hint.setText(self.flow_chart_hint.text() +
                f" {stale_count} 檔來源日期與截止日不同，未混用其價格計算。")
        populate_flow_chart(
            self.flow_chart,
            app_paths.HISTORY_DB_PATH,
            self.snapshot,
            avg_days=period.trading_days,
            on_industry_click=self._on_industry_bubble_clicked,
            mode=bubble_mode,
            valuation_metric=valuation_metric,
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
            # 分類欄加色塊 icon，跟泡泡圖共用 _category_color()，讓清單列跟
            # 泡泡圖的顏色可以直接對照，不新增欄位（避免牽動既有欄位索引）。
            item.setIcon(1, _color_swatch_icon(_category_color(f.industry)))
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
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([180, 540])
        layout.addWidget(splitter)

    def _build_position_detail_section(self):
        """選取上方某筆部位時顯示的詳細資訊區塊：族群／概念股（本地資料，
        即時顯示，見 self._ticker_concept_map／get_industry_map）＋ FinMind
        本益比／殖利率／三大法人買賣超／融資融券餘額／外資持股／借券／停資停券
        （背景查詢，見 _load_position_detail）。跟「個股」頁的 StockDetailDialog
        不同，這裡不彈窗，直接嵌在部位紀錄頁裡；圖表區塊用 DetailChartPanel
        （10 個分頁，延遲建立——見該類別 docstring）。這個區塊本身在部位紀錄
        頁一打開就會建構（不像 StockDetailDialog 延遲到按鈕點擊），但
        DetailChartPanel 只有目前作用中的那個分頁會在建構當下真正建立圖表
        widget，不用等使用者選取任何一筆部位。"""
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
        outer_layout.addWidget(_detail_summary_scroll(self.position_detail_label))

        self.position_panel = DetailChartPanel()
        outer_layout.addWidget(self.position_panel.tabs, 1)

        return container

    def refresh_table(self):
        total_pnl = 0.0
        has_pnl = False
        self.table.setRowCount(len(self.positions))
        try:
            industry_map = get_industry_map(app_paths.HISTORY_DB_PATH)
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

        save_positions(app_paths.POSITIONS_PATH, self.positions)
        save_portfolio_snapshot(app_paths.HISTORY_DB_PATH, date.today().isoformat(), self.positions)
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
            self.position_panel.clear()
            return
        pos = find_position(self.positions, position_id)
        if pos is None:
            return
        self._load_position_detail(pos)

    def _load_position_detail(self, pos):
        try:
            industry = get_industry_map(app_paths.HISTORY_DB_PATH).get(pos.ticker) or "未分類"
        except PriceFetchError:
            industry = "未分類"
        concepts = self._ticker_concept_map.get(pos.ticker) or []
        concept_text = "、".join(concepts) if concepts else "（尚無分類，可編輯 concepts.json 新增）"
        header = f"{pos.ticker} {pos.name or ''}　族群：{industry}　概念股：{concept_text}"

        # 先看 position_detail_cache.json 有沒有這檔股票上次查到、永久存下來的
        # 結果：有就先顯示（不用等這次背景查詢），沒有才顯示「查詢中」空白狀態。
        # 不管有沒有快取，下面都照樣背景重打一次 FinMind 拿最新資料、查到就覆寫
        # 快取檔——快取讓「隨時可以取用」，不是拿來取代查新資料。
        cached = load_position_detail_cache(app_paths.POSITION_DETAIL_CACHE_PATH, pos.ticker)
        if cached is not None:
            note = f"（上次查詢：{_format_fetched_at(cached['fetched_at'])}，背景更新中...）"
            self._render_position_detail(header, cached, note)
        else:
            self.position_detail_label.setText(f"{header}\n\nFinMind 查詢中...")
            self.position_panel.clear()

        token = self.settings.get("finmind_token", "")
        market = pos.market

        def fetch():
            data = fetch_position_detail(pos.ticker, token, market=market)
            save_position_detail_cache(app_paths.POSITION_DETAIL_CACHE_PATH, pos.ticker, data)
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
        """畫「部位詳細資訊」文字摘要（`_render_detail_summary`，跟「個股」頁
        StockDetailDialog 的「顯示完整籌碼面資訊」按鈕共用同一份）＋更新
        `self.position_panel` 的資料——分頁圖表延遲建立，見 DetailChartPanel。"""
        _render_detail_summary(self.position_detail_label, header, data, note)
        self.position_panel.set_data(data)

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
        self.position_panel.clear()
        self.update_finmind_count_label()

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
        self.stock_search_edit.setPlaceholderText("搜尋股票代號或名稱")
        self.stock_search_edit.setMinimumWidth(160)
        self.stock_search_edit.setMaximumWidth(320)
        self.stock_search_edit.addAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView),
            QtWidgets.QLineEdit.LeadingPosition,
        )
        self._stock_filter_timer = QtCore.QTimer(self)
        self._stock_filter_timer.setSingleShot(True)
        self._stock_filter_timer.timeout.connect(
            lambda: self._filter_stocks_tree(self.stock_search_edit.text())
        )
        self.stock_search_edit.textChanged.connect(self._on_stock_search_text_changed)
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
            directory = get_industry_directory(app_paths.HISTORY_DB_PATH)
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

        self.stocks_tree.setUpdatesEnabled(False)
        try:
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
        finally:
            self.stocks_tree.setUpdatesEnabled(True)

        self._filter_stocks_tree(self.stock_search_edit.text())

    def _on_stock_search_text_changed(self, _text):
        """打字時 debounce（150ms）才真的過濾，避免每個按鍵都對約1700個項目
        逐一 setHidden 造成明顯卡頓；停止輸入後才觸發 _filter_stocks_tree。"""
        self._stock_filter_timer.start(150)

    def _filter_stocks_tree(self, text):
        """依代號／名稱關鍵字（不分大小寫、子字串比對）過濾樹狀列表：符合的個股
        顯示，其餘隱藏；產業分組列則依底下是否還有符合的個股決定顯示／隱藏，
        有輸入關鍵字時自動展開有符合結果的分組。"""
        keyword = text.strip().lower()
        self.stocks_tree.setUpdatesEnabled(False)
        try:
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
        finally:
            self.stocks_tree.setUpdatesEnabled(True)

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
            self._stock_trend_timer.stop()
            self._stock_trend_expected_ticker = None
            return
        price = self.snapshot.get(ticker)
        name = item.text(0)
        market = item.data(0, QtCore.Qt.UserRole + 1) or "—"
        industry = item.data(0, QtCore.Qt.UserRole + 2) or "未分類"
        self.stock_preview_title.setText(f"{name}　{ticker}")
        # load_local_technical 內含 SQLite 查詢＋全歷史技術指標運算，改成
        # debounce（120ms，方向鍵快速瀏覽時不必每格都觸發）＋背景執行緒
        # （見 _load_stock_trend_badges），不在選取事件裡同步卡住主執行緒。
        self._stock_trend_expected_ticker = ticker
        self._stock_trend_timer.start(120)
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

    def _load_stock_trend_badges(self):
        """_on_stock_selected debounce 後實際觸發：背景執行緒查技術指標
        （TTLCache 依 ticker 快取 5 分鐘，同一檔位重選不必重算），完成時若
        使用者已經選到別的股票就丟棄，避免舊結果蓋掉新選取的畫面。"""
        ticker = self._stock_trend_expected_ticker
        if not ticker:
            return

        def work(_cancel_event, _emit_progress):
            return self._stock_technical_cache.get_or_fetch(
                ticker, lambda: load_local_technical(app_paths.HISTORY_DB_PATH, ticker)
            )

        def on_done(technical):
            if ticker != self._stock_trend_expected_ticker:
                return
            _populate_badge_row(
                self.stock_preview_trend_row,
                _stock_trend_badges(technical),
                empty_text="歷史資料不足，暫無法判斷趨勢／動能／量能。",
            )

        def on_error(_message):
            if ticker != self._stock_trend_expected_ticker:
                return
            _clear_layout(self.stock_preview_trend_row)

        # 一定要留住回傳值：BackgroundTask 沒有其他地方持有參照的話，
        # Python 會在背景執行緒做完之前就把它回收，callback 永遠不會被呼叫
        # （其餘既有的 run_background_task／run_task_in_thread 呼叫點都遵守
        # 這個慣例，見 tradingnote_gui.py 內其他 self._xxx_timer = ... 賦值）。
        self._stock_trend_task = run_background_task(self, work, on_done, on_error)

    def _open_selected_stock_detail(self):
        item = self.stocks_tree.currentItem()
        if item is not None:
            self._on_stock_double_clicked(item, 0)

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
