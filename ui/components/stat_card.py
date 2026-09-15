"""StatCard：「標題 + 大數字 + 次要資訊」摘要卡，取代各頁手刻的同款 QFrame。

視覺對齊既有全域 QSS 的 `summaryCard`/`metricValue`（見 ui/theme.py），所以換上
這個元件不會改變任何畫面外觀，只是把重複的 layout 程式碼收斂成一個 class。
"""

from PySide6 import QtWidgets

from ui.format import gain_loss_color
from ui.theme import COLOR_MUTED


class StatCard(QtWidgets.QFrame):
    """title 上方小字、value(+unit) 大字、change 次要小字（可選，帶漲跌色）。

    範例：StatCard(title="成交量", value="42,381", unit="張", change="+32.4%", status="positive")

    bordered=False：不畫自己的卡片背景/邊框（不套 `summaryCard`、不留卡片內距），
    給「已經在別的 stockHero/summaryCard 容器裡面」的情境用，避免卡片疊卡片。
    title 給 None／空字串時不顯示標題那行（不是每個嵌入情境都需要小標題）。
    """

    def __init__(
        self, title=None, value="—", unit=None, change=None, status=None,
        bordered=True, parent=None,
    ):
        super().__init__(parent)
        if bordered:
            self.setProperty("summaryCard", True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(*((12, 8, 12, 8) if bordered else (0, 0, 0, 0)))
        layout.setSpacing(2)

        self.title_label = QtWidgets.QLabel(title or "")
        self.title_label.setProperty("muted", True)
        self.title_label.setVisible(bool(title))
        layout.addWidget(self.title_label)

        self.value_label = QtWidgets.QLabel()
        self.value_label.setProperty("metricValue", True)
        layout.addWidget(self.value_label)

        self.change_label = QtWidgets.QLabel()
        self.change_label.setProperty("muted", True)
        self.change_label.setVisible(False)
        layout.addWidget(self.change_label)

        self.set_value(value, unit=unit, change=change, status=status)

    def set_value(self, value, unit=None, change=None, status=None):
        """更新數值（背景重新整理完成後呼叫這個，不用重建整張卡）。

        status 只影響顏色，不影響是否顯示 change：有 change 文字時色彩套在
        change_label（大數字維持中性色，例如「成交量 42,381 張」＋綠色「↑32.4%」）；
        沒有 change 文字時色彩改套在 value_label 本身（例如「今日偏流入 12 個族群」
        整行直接上色）。
        """
        text = str(value) if value is not None else "—"
        if unit:
            text = f"{text} {unit}"
        self.value_label.setText(text)

        if status == "positive":
            color = gain_loss_color(1)
        elif status == "negative":
            color = gain_loss_color(-1)
        elif status == "neutral":
            color = COLOR_MUTED
        else:
            color = None

        if change:
            self.change_label.setText(str(change))
            self.change_label.setVisible(True)
            self.change_label.setStyleSheet(f"color: {color};" if color else "")
            self.value_label.setStyleSheet("")
        else:
            self.change_label.setVisible(False)
            self.value_label.setStyleSheet(f"color: {color};" if color else "")
