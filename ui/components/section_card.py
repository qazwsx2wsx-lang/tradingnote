"""SectionCard：一頁裡「一個主要區塊」的統一容器（標題＋內容），取代各分頁自己
土法煉鋼的「header QLabel＋內容直接塞進頁面」寫法，讓主要區塊在畫面上有清楚的
卡片邊界，而不是整頁文字/圖表流在一起分不出區塊。

視覺沿用 `StatCard` 同一套 `summaryCard` QSS（`ui/theme.py`）——同一種「卡片」語言，
不要另外發明新的邊框/背景樣式。
"""

from PySide6 import QtWidgets


class SectionCard(QtWidgets.QFrame):
    """title（必要）＋ description（可選，muted 小字）＋ body（呼叫端自由塞內容）。

    - `card.header_row`：標題所在的 QHBoxLayout，已經 `addStretch(1)`，呼叫端可以
      在右側加額外 widget（例如資料日期、狀態徽章）：`card.header_row.addWidget(x)`。
    - `card.body_layout`：內容區的 QVBoxLayout，呼叫端用 `addWidget`/`addLayout` 加東西，
      不用另外包一層 QVBoxLayout。
    """

    def __init__(self, title, description=None, parent=None):
        super().__init__(parent)
        self.setProperty("summaryCard", True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(6)

        self.header_row = QtWidgets.QHBoxLayout()
        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setProperty("header", True)
        self.header_row.addWidget(self.title_label)
        self.header_row.addStretch(1)
        outer.addLayout(self.header_row)

        self.description_label = None
        if description:
            self.description_label = QtWidgets.QLabel(description)
            self.description_label.setProperty("muted", True)
            self.description_label.setWordWrap(True)
            outer.addWidget(self.description_label)

        self.body_layout = QtWidgets.QVBoxLayout()
        self.body_layout.setContentsMargins(0, 4, 0, 0)
        outer.addLayout(self.body_layout, 1)
