"""集中管理的視覺設計 token：顏色、字型、全域 QSS。

2026-09-15 從淺色主題改成 dark-mode-first（user 規格明確要求）。原本的淺色 hex
值不在這裡保留一份「備用亮色主題」——目前沒有執行期切換主題的需求，留著只會變成
沒人維護的死碼；真的要找回舊配色，`git log`／`CHANGELOG.md` 2026-09-15 條目都查
得到。

顏色/樣式規則：任何畫面需要的顏色都應該從這裡取得（`COLOR_*` 或 `STYLESHEET` 裡
已定義的 Qt 動態屬性，例如 `summaryCard`/`metricValue`/`muted`/`header`），不要
在 `tradingnote_gui.py` 或 `ui/` 底下其他檔案另外 hardcode hex 顏色。
"""

import sys

from tradingnote_paths import APP_PATHS

# ---------- 核心色彩（對外 import 用，語意名稱不變，只是換成深色數值） ----------
COLOR_BG = "#0F1115"          # 全域背景／側欄
COLOR_SURFACE = "#181B21"     # 主內容面板（page stack）
COLOR_TEXT = "#F1F3F5"        # 主要文字
COLOR_MUTED = "#9DA5B4"       # 次要／說明文字
COLOR_ACCENT = "#3B82F6"      # 主要操作藍
COLOR_ACCENT_ACTIVE = "#5B9DF9"  # accent hover/active：深色底下 hover 要更亮，跟淺色主題方向相反
COLOR_ACCENT_TEXT = "#FFFFFF"
COLOR_BORDER = "#292E38"
COLOR_ROW_ALT = "#1D2129"     # 表格斑馬紋，比 COLOR_SURFACE 略淺
COLOR_HOVER = "#20242C"
COLOR_CARD_BG = "#1E222B"     # StatCard／SectionCard／stockHero 卡片背景，比 COLOR_SURFACE 略亮，做出「浮起」層次
# 漲跌（損益）刻意保留紅綠上色——功能性色彩，用來一眼辨識盈虧方向，不算裝飾用色。
# 2026-09-15（續7）改成台灣市場慣例「正紅負綠」（原本是「正綠負紅」的西式慣例，
# 見 CHANGELOG.md 續4／續7）：COLOR_GAIN／COLOR_LOSS 這兩個名字語意不變（正值／
# 負值），只是把兩個 hex 值對調——所有透過 `gain_loss_color()`／這兩個常數名稱
# 取色的地方（個股漲跌、三大法人買賣超、大額交易人淨部位、資金流向徽章等）都
# 自動一起變成紅漲綠跌，不需要另外改呼叫端程式碼。
COLOR_GAIN = "#EF4444"
COLOR_LOSS = "#22C55E"

# 語意色彩（2026-09-15 新增）：給 SignalBadge／InsightCard 等元件用，避免所有「非
# 漲跌」的狀態訊號也順手套用紅綠，讓畫面看起來只剩紅綠兩色。
COLOR_INFO = COLOR_ACCENT
COLOR_WARNING = "#F59E0B"
COLOR_SPECIAL = "#A78BFA"
COLOR_NEUTRAL = COLOR_MUTED

# 淡色調（用來畫大範圍底色，例如泡泡圖四象限、任何需要「柔和標示某個區域/狀態」
# 但不能蓋過文字可讀性的場合），alpha 統一偏低（0x26 ≈ 15%）。8 碼 "#AARRGGBB"
# 是 QColor 認得的格式。
COLOR_GAIN_TINT = "#26EF4444"
COLOR_LOSS_TINT = "#2622C55E"
COLOR_WARNING_TINT = "#26F59E0B"
COLOR_NEUTRAL_TINT = "#1E9DA5B4"
COLOR_INFO_TINT = "#263B82F6"
COLOR_SPECIAL_TINT = "#26A78BFA"

# tone → (文字色, 淡色底) 的共用對照表，給 SignalBadge／InsightCard 等「用顏色
# 表達狀態」的元件共用，不要各自重複定義一份。positive/negative 是漲跌方向專用，
# info/warning/special/neutral 給其他非漲跌的狀態訊號用。給不認得的 tone 時呼叫端
# 應該 fallback 成 "neutral"。
TONE_COLORS = {
    "positive": (COLOR_GAIN, COLOR_GAIN_TINT),
    "negative": (COLOR_LOSS, COLOR_LOSS_TINT),
    "info": (COLOR_INFO, COLOR_INFO_TINT),
    "warning": (COLOR_WARNING, COLOR_WARNING_TINT),
    "special": (COLOR_SPECIAL, COLOR_SPECIAL_TINT),
    "neutral": (COLOR_NEUTRAL, COLOR_NEUTRAL_TINT),
}

# 「選取／已勾選」狀態的底色與文字色——側欄選中項目、可勾選按鈕組（flowSectionButton）、
# 交易週誌的已選日期卡片，語意相同，統一用同一組 token。
COLOR_SELECTED_BG = "#1E2A3F"
COLOR_SELECTED_TEXT = COLOR_ACCENT_ACTIVE
COLOR_SELECTED_BORDER = "#2C4A73"

# disabled 狀態（QPushButton:disabled 以外，少數對話框直接 setStyleSheet 時也共用）。
COLOR_DISABLED_BG = "#14161B"
COLOR_DISABLED_TEXT = "#525A66"

# ---------- 延伸 token：只有下面的 STYLESHEET 自己用，其他檔案不需要 import ----------
_SIDEBAR_ITEM_TEXT = COLOR_MUTED
_BUTTON_HOVER_BORDER = "#3A4150"
_BUTTON_PRESSED_BG = "#14161B"
_ACCENT_DISABLED_BG = "#28344A"
_SELECTION_BG = "#1E2A3F"
_TABLE_GRIDLINE = "#242832"
_HEADER_BG = "#14161B"
_TOOLTIP_BG = "#2A2F3A"
_PROGRESS_BG = "#14161B"

# 依作業系統選擇內建的繁體中文字型：macOS 用 PingFang TC，Windows 用微軟正黑體
# （Microsoft JhengHei），其他平台留給 Qt 自行 fallback。字型名稱不存在時 Qt 會
# 回退到預設字型，不會報錯，但指定正確的系統字型中文才不會變成方框／宋體。
if sys.platform == "darwin":
    FONT_FAMILY = "PingFang TC"
elif sys.platform.startswith("win"):
    FONT_FAMILY = "Microsoft JhengHei"
else:
    FONT_FAMILY = "Noto Sans CJK TC"

STYLESHEET = f"""
QWidget {{ color: {COLOR_TEXT}; font-family: "{FONT_FAMILY}"; font-size: 12px; }}
QMainWindow, QDialog {{ background: {COLOR_BG}; }}
QLabel {{ background: transparent; }}
QLabel[muted="true"] {{ color: {COLOR_MUTED}; font-size: 11px; }}
QLabel[header="true"] {{ font-size: 14px; font-weight: 600; }}
QLabel#brandTitle {{ font-size: 20px; font-weight: 700; color: {COLOR_TEXT}; }}
QLabel#pageTitle {{ font-size: 19px; font-weight: 700; color: {COLOR_TEXT}; }}
QFrame#sidebar {{ background: {COLOR_BG}; border-right: 1px solid {COLOR_BORDER}; }}
QFrame#sidebarHeader {{ background: transparent; border: none; }}
QLabel#sidebarSubtitle {{ color: {COLOR_MUTED}; font-size: 10px; }}
QListWidget#mainNavigation {{ background: transparent; border: none; outline: none; padding: 2px 7px; }}
QListWidget#mainNavigation::item {{ color: {_SIDEBAR_ITEM_TEXT}; border-radius: 7px; padding: 9px 10px; margin: 2px 0; }}
QListWidget#mainNavigation::item:hover {{ background: {COLOR_HOVER}; color: {COLOR_TEXT}; }}
QListWidget#mainNavigation::item:selected {{ background: {COLOR_SELECTED_BG}; color: {COLOR_SELECTED_TEXT}; font-weight: 700; }}
QStackedWidget#mainPageStack {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 10px; }}
QFrame[summaryCard="true"] {{ background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER}; border-radius: 8px; }}
QLabel[metricValue="true"] {{ font-size: 16px; font-weight: 700; }}
QFrame[stockHero="true"] {{ background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER}; border-radius: 10px; }}
QTabWidget::pane {{ border: 1px solid {COLOR_BORDER}; background: {COLOR_SURFACE}; border-radius: 8px; }}
QTabBar::tab {{ background: transparent; color: {COLOR_MUTED}; padding: 9px 14px; margin-right: 3px; border-bottom: 3px solid transparent; }}
QTabBar::tab:selected {{ color: {COLOR_ACCENT_ACTIVE}; border-bottom: 3px solid {COLOR_ACCENT_ACTIVE}; font-weight: 700; background: {COLOR_SURFACE}; }}
QTabBar::tab:hover:!selected {{ background: {COLOR_HOVER}; color: {COLOR_TEXT}; }}
QPushButton {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 6px 10px; min-height: 18px; qproperty-iconSize: 22px 22px; }}
QPushButton:hover {{ background: {COLOR_HOVER}; border-color: {_BUTTON_HOVER_BORDER}; }}
QPushButton:pressed {{ background: {_BUTTON_PRESSED_BG}; }}
QPushButton:focus {{ border: 1px solid {COLOR_ACCENT}; }}
QPushButton:disabled {{ background: {COLOR_DISABLED_BG}; color: {COLOR_DISABLED_TEXT}; border-color: {COLOR_BORDER}; }}
QPushButton[accent="true"] {{ background: {COLOR_ACCENT}; color: white; border: 1px solid {COLOR_ACCENT}; font-weight: 600; }}
QPushButton[accent="true"]:hover {{ background: {COLOR_ACCENT_ACTIVE}; }}
QPushButton[accent="true"]:disabled {{ background: {_ACCENT_DISABLED_BG}; border-color: {_ACCENT_DISABLED_BG}; color: {COLOR_DISABLED_TEXT}; }}
QPushButton[flowSectionButton="true"] {{ padding: 7px 14px; border-radius: 6px; }}
QPushButton[flowSectionButton="true"]:checked {{ background: {COLOR_SELECTED_BG}; color: {COLOR_SELECTED_TEXT}; border-color: {COLOR_SELECTED_BORDER}; font-weight: 600; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 5px; padding: 5px 7px; min-height: 18px; selection-background-color: {COLOR_ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateEdit:focus {{ border-color: {COLOR_ACCENT}; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ width: 22px; border: none; }}
QComboBox::down-arrow {{ image: url("{(APP_PATHS.root / 'assets' / 'chevron-down-dark.svg').as_posix()}"); width: 14px; height: 14px; }}
QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: none; }}
QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; border: none; }}
QSpinBox::up-arrow {{ image: url("{(APP_PATHS.root / 'assets' / 'chevron-up-dark.svg').as_posix()}"); width: 12px; height: 12px; }}
QSpinBox::down-arrow {{ image: url("{(APP_PATHS.root / 'assets' / 'chevron-down-dark.svg').as_posix()}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {COLOR_SURFACE}; selection-background-color: {_SELECTION_BG}; selection-color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER}; }}
QTextEdit, QPlainTextEdit {{ background: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 7px; padding: 8px; }}
QTableWidget, QTreeWidget {{ background: {COLOR_SURFACE}; alternate-background-color: {COLOR_ROW_ALT}; gridline-color: {_TABLE_GRIDLINE}; border: 1px solid {COLOR_BORDER}; border-radius: 7px; selection-background-color: {_SELECTION_BG}; selection-color: {COLOR_TEXT}; outline: none; }}
QTableWidget::item, QTreeWidget::item {{ padding: 4px 6px; }}
QTableWidget::item:hover, QTreeWidget::item:hover {{ background: {COLOR_HOVER}; }}
QHeaderView {{ background: {_HEADER_BG}; }}
QHeaderView::section {{ background: {_HEADER_BG}; color: {COLOR_MUTED}; padding: 7px 6px; border: none; border-bottom: 1px solid {COLOR_BORDER}; font-weight: 600; }}
QTableCornerButton::section {{ background: {_HEADER_BG}; border: none; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: {COLOR_SURFACE}; }}
QCheckBox {{ spacing: 8px; }}
QGroupBox {{ border: 1px solid {COLOR_BORDER}; border-radius: 8px; margin-top: 16px; padding: 14px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; }}
QSplitter::handle {{ background: {COLOR_BORDER}; }}
QStatusBar {{ background: {COLOR_BG}; color: {COLOR_MUTED}; border-top: 1px solid {COLOR_BORDER}; font-size: 11px; padding: 3px 7px; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{ border: 1px solid {COLOR_BORDER}; border-radius: 5px; background: {_PROGRESS_BG}; text-align: center; min-height: 18px; color: {COLOR_TEXT}; }}
QProgressBar::chunk {{ background: {COLOR_ACCENT}; border-radius: 4px; }}
QToolTip {{ background: {_TOOLTIP_BG}; color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER}; padding: 6px; }}
"""
