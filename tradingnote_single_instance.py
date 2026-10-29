"""單一執行個體保護：避免同一份資料目錄被兩個 GUI process 同時開啟並互相覆寫存檔。

鎖的識別字串是用 resolved data_dir 路徑算出來的 SHA-256 雜湊，不是寫死的常數：
- 測試或另一份安裝透過 TRADINGNOTE_DATA_DIR 指到不同資料夾時不會被擋。
- 同一台機器上不同使用者各自的 data_dir 也不會互相干擾。
只處理 QLocalServer/QLocalSocket 這層 IPC 邏輯，不碰 QtWidgets——要不要彈對話框、
用什麼文字、以什麼結束碼退出，都留給呼叫端（tradingnote_gui.py::main()）決定。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6 import QtNetwork

# 連線逾時：本機 IPC，另一個 instance 若存在，accept 幾乎是瞬間完成；
# 200ms 足夠寬裕，又不會讓啟動卡太久。
_CONNECT_TIMEOUT_MS = 200


def _lock_key(data_dir: Path) -> str:
    """把 resolved data_dir 路徑壓成固定長度的識別字串。

    QLocalServer 的名稱底層對應 Windows named pipe／Unix domain socket，
    兩邊都有長度限制（Unix 的 sun_path 通常只有 ~100 字元），直接把整條路徑
    當名稱有風險；取 SHA-256 的前 16 個十六進位字元（64 bits，碰撞機率可忽略）
    接在固定前綴後面，長度固定且短，兩個平台都安全。
    """
    resolved = str(Path(data_dir).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"TradingNote-{digest}"


def ensure_single_instance(data_dir: Path) -> QtNetwork.QLocalServer | None:
    """判斷並佔用「同一個 data_dir」的單一執行個體鎖。

    回傳值：
    - 不是 None：這個 process 是存活的那一個，回傳的 QLocalServer 必須被呼叫端
      保留住參照到程式結束（否則會被 GC 回收，等同於沒上鎖）。
    - None：已經有另一個存活的 instance 占著同一個 data_dir。
    """
    key = _lock_key(data_dir)

    # Connect-then-listen：先試著連過去，連得上代表已經有活的 instance 在聽。
    probe = QtNetwork.QLocalSocket()
    probe.connectToServer(key)
    already_running = probe.waitForConnected(_CONNECT_TIMEOUT_MS)
    probe.close()
    if already_running:
        return None

    # 連不上有兩種可能：(a) 真的沒人開，或 (b) 上一個 instance 異常中止
    # （例如被強制關閉程序），留下沒人在聽的殘留 socket 檔案/名稱。
    # removeServer 在兩種情況下都安全：沒有殘留時是 no-op；有殘留時會清掉它，
    # 讓接下來的 listen() 可以重新用這個名字。
    QtNetwork.QLocalServer.removeServer(key)

    server = QtNetwork.QLocalServer()
    server.listen(key)
    return server
