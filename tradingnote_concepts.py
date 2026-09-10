"""tradingnote - 概念股分類（讀取專案自己的 concepts.json）。

concepts.json 現在包含三層資料：人工題材、TWSE／TPEx 官方產業，以及 TPEx 產業
價值鏈細分類。官方資料由 refresh_concepts.py 重建，人工題材會在更新時保留；跟
同層 trade-journal 專案的 themes.json 完全獨立、不共用。GUI 只在啟動時讀一次，
所以更新 concepts.json 後要重開程式才會生效。
"""

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

CONCEPTS_PATH = Path(__file__).parent / "concepts.json"

CLASSIFICATION_INDUSTRY = "industry"
CLASSIFICATION_VALUE_CHAIN_THEME = "value_chain_theme"
CLASSIFICATION_VALUE_CHAIN_LEAF = "value_chain_leaf"
CLASSIFICATION_MANUAL_THEME = "manual_theme"

CLASSIFICATION_LABELS = OrderedDict(
    [
        (CLASSIFICATION_INDUSTRY, "官方產業"),
        (CLASSIFICATION_VALUE_CHAIN_THEME, "價值鏈主題"),
        (CLASSIFICATION_VALUE_CHAIN_LEAF, "價值鏈細項"),
        (CLASSIFICATION_MANUAL_THEME, "人工題材"),
    ]
)


@dataclass(frozen=True)
class ConceptCatalog:
    """泡泡圖可用的分類目錄。

    groups_by_mode 的值是 OrderedDict[label, frozenset[ticker]]；價值鏈細項另外依
    scope（半導體、人工智慧等主鏈）分層，避免一次把數百個節點全畫到圖上。
    """

    groups_by_mode: dict
    value_chain_leaf_groups: OrderedDict
    revision: str

    def groups(self, mode, scope=None):
        if mode == CLASSIFICATION_VALUE_CHAIN_LEAF:
            if scope is None and self.value_chain_leaf_groups:
                scope = next(iter(self.value_chain_leaf_groups))
            return self.value_chain_leaf_groups.get(scope, OrderedDict())
        return self.groups_by_mode.get(mode, OrderedDict())

    @property
    def value_chain_scopes(self):
        return tuple(self.value_chain_leaf_groups)


def build_classification_catalog(concepts):
    """把 concepts.json 轉成泡泡圖使用的四種分類。

    「價值鏈主題」會把同一主鏈下所有細項成分股做聯集；「價值鏈細項」則保留
    主鏈內的完整路徑。一檔股票在同一群組內以 set 去重，但可同時屬於不同群組。
    """
    industry_groups = OrderedDict()
    manual_groups = OrderedDict()
    value_chain_themes = OrderedDict()
    value_chain_leaves = OrderedDict()

    for concept in concepts:
        name = str(concept.get("name") or "").strip()
        if not name:
            continue
        members = frozenset(str(ticker) for ticker in (concept.get("stock_ids") or []))
        if not members:
            continue
        classification_type = concept.get("classification_type") or "manual_theme"
        if classification_type == "official_industry":
            label = name.removeprefix("產業｜")
            industry_groups[label] = members
        elif classification_type == "official_value_chain":
            parts = name.split("｜")
            if len(parts) < 3:
                continue
            scope = parts[1]
            leaf_label = "｜".join(parts[2:])
            value_chain_themes.setdefault(scope, set()).update(members)
            value_chain_leaves.setdefault(scope, OrderedDict())[leaf_label] = members
        else:
            manual_groups[name] = members

    frozen_themes = OrderedDict(
        (scope, frozenset(members)) for scope, members in value_chain_themes.items()
    )
    frozen_leaves = OrderedDict(
        (scope, OrderedDict(groups)) for scope, groups in value_chain_leaves.items()
    )
    groups_by_mode = {
        CLASSIFICATION_INDUSTRY: industry_groups,
        CLASSIFICATION_VALUE_CHAIN_THEME: frozen_themes,
        CLASSIFICATION_MANUAL_THEME: manual_groups,
    }
    revision_payload = [
        (mode, scope, label, sorted(members))
        for mode, mode_groups in groups_by_mode.items()
        for scope, groups in (("", mode_groups),)
        for label, members in groups.items()
    ]
    revision_payload.extend(
        (CLASSIFICATION_VALUE_CHAIN_LEAF, scope, label, sorted(members))
        for scope, groups in frozen_leaves.items()
        for label, members in groups.items()
    )
    revision = hashlib.sha1(
        json.dumps(revision_payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return ConceptCatalog(groups_by_mode, frozen_leaves, revision)


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
