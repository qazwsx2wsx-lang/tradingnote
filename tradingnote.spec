# PyInstaller spec for TradingNote（macOS .app）。
#
#     /opt/homebrew/bin/python3.12 -m PyInstaller tradingnote.spec --noconfirm --clean
#
# 一定要用專案指定的 Python 3.12（見 啟動TradingNote.command／ARCHITECTURE.md）：
# 系統內建較新的 Python 連的 OpenSSL 版本會讓 TWSE／TPEx 的憑證驗證失敗。
# PyInstaller 打包時是把「建置當下那個直譯器」的 libpython／C extension 原封不動
# 包進去，所以用哪個 python 執行這個 spec，打包出來的 app 就繼承哪個 OpenSSL；
# 用非 3.12 的直譯器打包會把同一個 TLS 問題重現在打包後的 app 裡。
#
# concepts.json／sector_overrides.json／assets/ 這些非 Python 資源用 --add-data
# 保持跟原始碼一樣的相對路徑結構；執行期由 tradingnote_paths.py 透過
# sys._MEIPASS 找回來（PyInstaller 在 onedir／onefile 都會設定這個變數）。

import sys
from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "concepts.json"), "."),
    (str(ROOT / "sector_overrides.json"), "."),
    (str(ROOT / "assets"), "assets"),
]

a = Analysis(
    ["tradingnote_gui.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TradingNote",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "AppIcon.icns"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TradingNote",
)

app = BUNDLE(
    coll,
    name="TradingNote.app",
    icon=str(ROOT / "assets" / "AppIcon.icns"),
    bundle_identifier="com.tradingnote.app",
    info_plist={
        "CFBundleName": "TradingNote",
        "CFBundleDisplayName": "TradingNote",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
