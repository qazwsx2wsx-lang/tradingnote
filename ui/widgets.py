"""GUI 共用的小型 widget helper（按鈕、版面清空、螢幕適配尺寸、對話框基底 DialogBase）。"""

from PySide6 import QtCore, QtGui, QtWidgets


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


def _color_swatch_icon(color, size=10):
    """畫一個實心色塊小圖示，給 QTreeWidgetItem.setIcon() 用，讓清單列跟
    泡泡圖用同一個 _category_color() 產生視覺上可以直接比對的顏色。"""
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setBrush(QtGui.QBrush(color))
    painter.setPen(QtCore.Qt.NoPen)
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return QtGui.QIcon(pixmap)


class DialogBase(QtWidgets.QDialog):
    """所有一般對話框的共同基底：標題列加上「放大」按鈕（原本每個對話框各自
    在 __init__ 第一行設定同一個 window flag）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)
