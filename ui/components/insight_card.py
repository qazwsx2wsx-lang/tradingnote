"""InsightCard：把數據轉成一句話讓人快速看懂，而不是丟一串數字要使用者自己解讀。

例如原始資料「量比 1.83、當日漲跌 +2.3%」→ 顯示成：
    資金動能增強
    今日成交量為近期均量的 1.83 倍，且價格同步走強（+2.30%）。

這裡只負責「顯示」——headline／detail／tone 由呼叫端的 rule-based 邏輯決定
（規格明確要求：先用 rule-based，不要接 LLM API）。
"""

from PySide6 import QtWidgets

from ui.theme import TONE_COLORS


class InsightCard(QtWidgets.QFrame):
    """headline（一句結論，依 tone 上色）＋ detail（muted，補充依據）。"""

    def __init__(self, headline, detail=None, tone="neutral", parent=None):
        super().__init__(parent)
        self.setProperty("summaryCard", True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)

        self.headline_label = QtWidgets.QLabel()
        self.headline_label.setProperty("header", True)
        self.headline_label.setWordWrap(True)
        layout.addWidget(self.headline_label)

        self.detail_label = QtWidgets.QLabel()
        self.detail_label.setProperty("muted", True)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.set_content(headline, detail=detail, tone=tone)

    def set_content(self, headline, detail=None, tone="neutral"):
        text_color, _bg = TONE_COLORS.get(tone, TONE_COLORS["neutral"])
        self.headline_label.setText(headline)
        self.headline_label.setStyleSheet(f"color: {text_color};")
        self.detail_label.setText(detail or "")
        self.detail_label.setVisible(bool(detail))
