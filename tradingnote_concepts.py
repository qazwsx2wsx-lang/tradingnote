"""tradingnote - 概念股分類（人工維護的靜態清單，不是任何 API 資料）。

FinMind 沒有概念股資料集，「所屬概念股」目前沒有官方／免費 API 來源，改用本檔案旁
的 concepts.json 手動維護清單。跟同層 trade-journal 專案的 themes.json 是兩份完全
獨立的資料，不共用、不同步——tradingnote 本來就跟 trade-journal 無關（見
HANDOFF.md）。之後想增修概念股名單，直接編輯 concepts.json 即可，不需要改程式碼；
GUI 只在啟動時讀一次（跟 settings.json 一樣，改完 concepts.json 要重開程式才生效）。
"""

import json
from pathlib import Path

CONCEPTS_PATH = Path(__file__).parent / "concepts.json"


def load_concepts(path=CONCEPTS_PATH):
    """讀 concepts.json，回傳 [{"name":, "description":, "stock_ids": [...]}, ...]；
    檔案不存在或格式異常時回傳空清單，不拋例外（概念股是錦上添花的資訊，不該讓
    整個「部位紀錄」頁因為這份手動維護的檔案壞掉而打不開）。"""
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    concepts = data.get("concepts")
    return concepts if isinstance(concepts, list) else []


def build_ticker_concept_map(concepts):
    """回傳 {ticker: [concept_name, ...]}，一檔股票可能同時屬於多個概念，
    順序依 concepts 清單原本的順序。"""
    mapping = {}
    for concept in concepts:
        name = concept.get("name")
        if not name:
            continue
        for ticker in concept.get("stock_ids") or []:
            mapping.setdefault(ticker, []).append(name)
    return mapping
