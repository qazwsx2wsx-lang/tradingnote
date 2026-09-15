"""SignalBadge：統一的分析狀態徽章（偏多/偏空/中性/風險/資金流入/資金流出等）。

目的是不要讓各分頁各自發明一套「用顏色代表狀態」的做法（例如某頁直接
`setStyleSheet(f"color: {COLOR_GAIN}")`、某頁用純文字寫「綠色＝...」說明顏色代表
什麼）。tone 只決定顏色，文字內容（"偏多"、"風險"、"資金流入"...）由呼叫端決定，
這裡不做關鍵字猜測——避免這個元件反過來變成另一種隱性 magic。
"""

from PySide6 import QtWidgets

from ui.theme import TONE_COLORS


class SignalBadge(QtWidgets.QLabel):
    """小圓角徽章，例如 `SignalBadge("偏多", tone="positive")`。

    tone：positive/negative 給漲跌方向專用（紅綠）；info/warning/special/neutral
    給其他非漲跌的狀態訊號用（藍/橘/紫/灰），避免整個畫面只剩紅綠兩色——見規格
    「不要把所有正面訊號都用紅色」。給不認得的 tone 時 fallback 成 neutral，不丟例外
    （徽章顯示錯誤色不該讓整個頁面壞掉）。
    """

    def __init__(self, text, tone="neutral", parent=None):
        super().__init__(text, parent)
        self.set_tone(tone)

    def set_tone(self, tone):
        self._tone = tone
        text_color, bg_color = TONE_COLORS.get(tone, TONE_COLORS["neutral"])
        self.setStyleSheet(
            f"color: {text_color}; background: {bg_color}; "
            "border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 600;"
        )

    def set_text_and_tone(self, text, tone):
        self.setText(text)
        self.set_tone(tone)
