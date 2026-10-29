#!/usr/bin/env python3
"""產生 TradingNote 的 app icon（簡單預設版，之後有正式 logo 可以直接換掉這個檔案）。

畫一個深色圓角方塊背景＋藍色漸層長條圖＋白色趨勢線，配色跟 ui/theme.py 一致。
在 macOS 上執行，會先輸出各尺寸 PNG 到一個 .iconset 資料夾，再用系統內建的
iconutil 轉成 assets/AppIcon.icns。

    /opt/homebrew/bin/python3.12 scripts/make_app_icon.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# 跟 ui/theme.py 的 COLOR_SURFACE／COLOR_BG／COLOR_ACCENT／COLOR_ACCENT_ACTIVE／
# COLOR_TEXT 對齊，讓 icon 跟 app 本身的視覺語言一致。
COLOR_BG_TOP = "#181B21"
COLOR_BG_BOTTOM = "#0F1115"
COLOR_ACCENT = "#3B82F6"
COLOR_ACCENT_LIGHT = "#5B9DF9"
COLOR_LINE = "#F1F3F5"

# macOS 官方 icon 格線的圓角比例約是邊長的 22.37%。
CORNER_RATIO = 0.2237

# (iconset 檔名後綴, 實際像素邊長)
ICONSET_ENTRIES = [
    ("16x16", 16), ("16x16@2x", 32),
    ("32x32", 32), ("32x32@2x", 64),
    ("128x128", 128), ("128x128@2x", 256),
    ("256x256", 256), ("256x256@2x", 512),
    ("512x512", 512), ("512x512@2x", 1024),
]


def draw_icon(size: int) -> QtGui.QImage:
    image = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32_Premultiplied)
    image.fill(QtCore.Qt.transparent)

    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

    radius = size * CORNER_RATIO
    bg_path = QtGui.QPainterPath()
    bg_path.addRoundedRect(QtCore.QRectF(0, 0, size, size), radius, radius)

    bg = QtGui.QLinearGradient(0, 0, 0, size)
    bg.setColorAt(0.0, QtGui.QColor(COLOR_BG_TOP))
    bg.setColorAt(1.0, QtGui.QColor(COLOR_BG_BOTTOM))
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(bg)
    painter.drawPath(bg_path)

    # 三根漸增的長條（象徵選股／資金流向的上升趨勢），置中偏下。
    margin = size * 0.22
    gap = size * 0.07
    bar_width = (size - margin * 2 - gap * 2) / 3
    base_y = size * 0.72
    heights = [size * 0.18, size * 0.30, size * 0.42]

    bar_top_color = QtGui.QColor(COLOR_ACCENT_LIGHT)
    bar_bottom_color = QtGui.QColor(COLOR_ACCENT)

    tops = []
    for i, h in enumerate(heights):
        x = margin + i * (bar_width + gap)
        y = base_y - h
        rect = QtCore.QRectF(x, y, bar_width, h)
        grad = QtGui.QLinearGradient(0, y, 0, base_y)
        grad.setColorAt(0.0, bar_top_color)
        grad.setColorAt(1.0, bar_bottom_color)
        painter.setBrush(grad)
        bar_radius = bar_width * 0.18
        bar_path = QtGui.QPainterPath()
        bar_path.addRoundedRect(rect, bar_radius, bar_radius)
        painter.drawPath(bar_path)
        tops.append(QtCore.QPointF(x + bar_width / 2, y))

    # 白色趨勢折線，連接三根長條的頂點，再往右上延伸一截小箭頭。
    pen = QtGui.QPen(QtGui.QColor(COLOR_LINE))
    pen.setWidthF(max(size * 0.035, 1.0))
    pen.setCapStyle(QtCore.Qt.RoundCap)
    pen.setJoinStyle(QtCore.Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)

    line_path = QtGui.QPainterPath()
    line_path.moveTo(tops[0])
    for pt in tops[1:]:
        line_path.lineTo(pt)
    arrow_end = QtCore.QPointF(tops[-1].x() + bar_width * 0.55, tops[-1].y() - size * 0.10)
    line_path.lineTo(arrow_end)
    painter.drawPath(line_path)

    # 箭頭三角形。
    arrow_size = size * 0.05
    direction = QtCore.QPointF(arrow_end.x() - tops[-1].x(), arrow_end.y() - tops[-1].y())
    length = (direction.x() ** 2 + direction.y() ** 2) ** 0.5 or 1.0
    ux, uy = direction.x() / length, direction.y() / length
    perp_x, perp_y = -uy, ux
    p1 = arrow_end
    p2 = QtCore.QPointF(
        arrow_end.x() - ux * arrow_size - perp_x * arrow_size * 0.6,
        arrow_end.y() - uy * arrow_size - perp_y * arrow_size * 0.6,
    )
    p3 = QtCore.QPointF(
        arrow_end.x() - ux * arrow_size + perp_x * arrow_size * 0.6,
        arrow_end.y() - uy * arrow_size + perp_y * arrow_size * 0.6,
    )
    arrow_poly = QtGui.QPolygonF([p1, p2, p3])
    painter.setPen(QtCore.Qt.NoPen)
    painter.setBrush(QtGui.QColor(COLOR_LINE))
    painter.drawPolygon(arrow_poly)

    painter.end()
    return image


def main():
    if sys.platform != "darwin":
        print("這個腳本目前只處理 macOS 的 .icns 輸出（用系統內建 iconutil）。")
        sys.exit(1)
    if shutil.which("iconutil") is None:
        print("找不到 iconutil（應該是系統內建工具，確認是不是在 macOS 上執行）。")
        sys.exit(1)

    QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])

    iconset_dir = ASSETS / "AppIcon.iconset"
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir(parents=True)

    for suffix, px in ICONSET_ENTRIES:
        image = draw_icon(px)
        out_path = iconset_dir / f"icon_{suffix}.png"
        image.save(str(out_path), "PNG")
        print(f"寫出 {out_path.name} ({px}x{px})")

    icns_path = ASSETS / "AppIcon.icns"
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
        check=True,
    )
    shutil.rmtree(iconset_dir)
    print(f"完成：{icns_path}")


if __name__ == "__main__":
    main()
