"""一次性搬遷：讀取舊版 data/positions.json，搬進 trades 資料表後把檔案改名為
.bak，避免下次啟動再次觸發（見 tradingnote_tradejournal.migrate_legacy_position）。
這個模組的存在是暫時的——等所有使用者都搬遷完，positions.json 不會再出現，
之後可以整個刪除這個模組（含呼叫它的那幾行 GUI／CLI 啟動流程）。"""

import json
from pathlib import Path

from tradingnote_core import Position


def load_legacy_positions(path):
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return [Position(**item) for item in raw]


def archive_legacy_positions_file(path):
    p = Path(path)
    if not p.exists():
        return
    p.rename(p.with_name(p.name + ".bak"))
