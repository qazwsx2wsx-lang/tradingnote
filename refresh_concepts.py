#!/usr/bin/env python3
"""從官方資料重建 concepts.json 的全市場概念分類。

資料分成三層：
1. 原本人工維護的題材標籤（保留）；
2. TWSE／TPEx 公司基本資料提供的官方產業（每檔必有一個）；
3. TPEx 產業價值鏈資訊平台的細分類（同一檔可有多個）。

預設只顯示統計，不寫檔；加 --write 才會覆寫 concepts.json。
"""

import argparse
import html
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from tradingnote_api_config import TPEX_CHAIN_ROOT_URL as CHAIN_ROOT_URL
from tradingnote_history import INDUSTRY_CODE_NAMES


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "history.db"
CONCEPTS_PATH = ROOT / "concepts.json"
USER_AGENT = "Mozilla/5.0 (compatible; TradingNote concept catalog updater)"

CHAIN_LINK_RE = re.compile(r"introduce\.php\?ic=([A-Za-z0-9]+)")
PAGE_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
PARENT_RE = re.compile(
    r"<div\s+id=[\"']companyList_([A-Za-z0-9]+)[\"']\s+"
    r"title=[\"']([^\"']+)[\"']",
    re.I,
)
NODE_RE = re.compile(
    r"<div\s+id=[\"']sc_link_([A-Za-z0-9]+)[\"'][^>]*>(.*?)</div>",
    re.I | re.S,
)
TABLE_RE = re.compile(r"<table\b([^>]*)>(.*?)</table>", re.I | re.S)
TABLE_ID_RE = re.compile(
    r"\bid=[\"']sc_company_([A-Za-z0-9]+)[\"']", re.I
)
STOCK_RE = re.compile(r"company_basic\.php\?stk_code=([A-Za-z0-9]+)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
COUNT_SUFFIX_RE = re.compile(r"\s*\(\s*\d+\s*家\s*\)\s*$")


def _clean_text(value):
    value = TAG_RE.sub(" ", value)
    value = html.unescape(value).replace("\ufeff", " ").replace("\xa0", " ")
    return " ".join(value.split()).strip()


def _fetch_text(url, attempts=3):
    error = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
    raise RuntimeError(f"讀取失敗：{url}：{error}") from error


def _load_directory():
    if not DB_PATH.exists():
        raise SystemExit(f"找不到產業資料庫：{DB_PATH}")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ticker, name, industry, market FROM industry_map ORDER BY ticker"
        ).fetchall()
    if not rows:
        raise SystemExit("industry_map 是空的；請先在 TradingNote 更新產業資料。")
    return {
        ticker: {
            "name": name,
            "industry": INDUSTRY_CODE_NAMES.get(industry, industry or "未分類"),
            "market": market,
        }
        for ticker, name, industry, market in rows
    }


def _load_manual_concepts():
    if not CONCEPTS_PATH.exists():
        return []
    with CONCEPTS_PATH.open(encoding="utf-8") as stream:
        concepts = json.load(stream).get("concepts") or []
    result = []
    for concept in concepts:
        classification_type = concept.get("classification_type")
        if classification_type in ("official_industry", "official_value_chain"):
            continue
        copy = dict(concept)
        copy["classification_type"] = "manual_theme"
        result.append(copy)
    return result


def _page_name(source):
    match = PAGE_TITLE_RE.search(source)
    title = _clean_text(match.group(1)) if match else ""
    if ">" in title:
        title = title.rsplit(">", 1)[-1].strip()
    return re.sub(r"產業鏈簡介\s*$", "", title).strip() or "未命名產業鏈"


def _nearest_parent(parent_matches, position):
    current = None
    for match in parent_matches:
        if match.start() > position:
            break
        current = match
    if current is None:
        return "未細分"
    return _clean_text(current.group(2)) or "未細分"


def _parse_chain_page(url, source, universe):
    page_name = _page_name(source)
    parents = list(PARENT_RE.finditer(source))
    node_names = {
        match.group(1): COUNT_SUFFIX_RE.sub("", _clean_text(match.group(2)))
        .lstrip("►▶ ")
        .strip()
        for match in NODE_RE.finditer(source)
    }
    concepts = []
    unmapped_tables = []
    for table in TABLE_RE.finditer(source):
        stock_ids = sorted(set(STOCK_RE.findall(table.group(2))) & universe)
        if not stock_ids:
            continue
        parent_name = _nearest_parent(parents, table.start())
        table_id_match = TABLE_ID_RE.search(table.group(1))
        node_id = table_id_match.group(1) if table_id_match else None
        node_name = node_names.get(node_id) if node_id else parent_name
        if node_id and not node_name:
            node_name = parent_name
            unmapped_tables.append(node_id)
        path = [page_name]
        if parent_name and parent_name != page_name:
            path.append(parent_name)
        if node_name and node_name != path[-1]:
            path.append(node_name)
        concepts.append(
            {
                "name": "價值鏈｜" + "｜".join(path),
                "description": "櫃買中心產業價值鏈：" + "／".join(path),
                "classification_type": "official_value_chain",
                "source": url,
                "source_date": date.today().isoformat(),
                "confidence": "A",
                "stock_ids": stock_ids,
            }
        )
    return concepts, unmapped_tables


def _fetch_value_chain_concepts(universe, delay_seconds):
    home = _fetch_text(CHAIN_ROOT_URL)
    chain_codes = list(dict.fromkeys(CHAIN_LINK_RE.findall(home)))
    if not chain_codes:
        raise RuntimeError("首頁找不到任何產業鏈連結，網站結構可能已變更。")

    combined = defaultdict(lambda: {"stock_ids": set()})
    diagnostics = {"chain_pages": len(chain_codes), "unmapped_tables": []}
    for index, chain_code in enumerate(chain_codes, start=1):
        url = urljoin(CHAIN_ROOT_URL, f"introduce.php?ic={chain_code}")
        source = _fetch_text(url)
        concepts, unmapped = _parse_chain_page(url, source, universe)
        diagnostics["unmapped_tables"].extend(
            f"{chain_code}:{node_id}" for node_id in unmapped
        )
        for concept in concepts:
            entry = combined[concept["name"]]
            entry.update({key: value for key, value in concept.items() if key != "stock_ids"})
            entry["stock_ids"].update(concept["stock_ids"])
        print(
            f"[{index:02d}/{len(chain_codes):02d}] {chain_code} "
            f"{_page_name(source)}：{len(concepts)} 個有效節點"
        )
        if delay_seconds and index < len(chain_codes):
            time.sleep(delay_seconds)

    result = []
    for entry in combined.values():
        entry["stock_ids"] = sorted(entry["stock_ids"])
        result.append(entry)
    return result, diagnostics


def _build_industry_concepts(directory):
    grouped = defaultdict(list)
    for ticker, info in directory.items():
        grouped[info["industry"]].append(ticker)
    industry_position = {
        name: index for index, name in enumerate(INDUSTRY_CODE_NAMES.values())
    }
    concepts = []
    for industry in sorted(grouped, key=lambda name: (industry_position.get(name, 999), name)):
        concepts.append(
            {
                "name": f"產業｜{industry}",
                "description": f"TWSE／TPEx 公司基本資料的官方產業分類：{industry}",
                "classification_type": "official_industry",
                "source": "TWSE t187ap03_L／TPEx mopsfin_t187ap03_O",
                "source_date": date.today().isoformat(),
                "confidence": "A",
                "stock_ids": sorted(grouped[industry]),
            }
        )
    return concepts


def _validate(concepts, universe):
    names = [concept.get("name") for concept in concepts]
    assignments = [
        ticker for concept in concepts for ticker in (concept.get("stock_ids") or [])
    ]
    covered = set(assignments) & universe
    unknown = sorted(set(assignments) - universe)
    duplicate_names = sorted(name for name, count in Counter(names).items() if count > 1)
    if unknown:
        raise RuntimeError(f"概念資料含全集不存在的代號：{unknown[:20]}")
    if duplicate_names:
        raise RuntimeError(f"概念名稱重複：{duplicate_names[:20]}")
    if covered != universe:
        raise RuntimeError(f"仍有 {len(universe - covered)} 檔沒有任何分類。")
    return {
        "concepts": len(concepts),
        "assignments": len(assignments),
        "covered_stocks": len(covered),
        "coverage_pct": round(len(covered) / len(universe) * 100, 2),
        "multi_tagged_stocks": sum(
            count > 1 for count in Counter(assignments).values()
        ),
    }


def build_payload(delay_seconds=0.1):
    directory = _load_directory()
    universe = set(directory)
    manual = _load_manual_concepts()
    industries = _build_industry_concepts(directory)
    value_chains, diagnostics = _fetch_value_chain_concepts(
        universe, delay_seconds
    )
    concepts = manual + industries + value_chains
    stats = _validate(concepts, universe)
    stats.update(
        {
            "manual_concepts": len(manual),
            "industry_concepts": len(industries),
            "value_chain_concepts": len(value_chains),
            "value_chain_covered_stocks": len(
                {
                    ticker
                    for concept in value_chains
                    for ticker in concept["stock_ids"]
                }
            ),
            **diagnostics,
        }
    )
    payload = {
        "_readme": (
            "全市場概念分類。manual_theme 為人工題材；official_industry 來自 "
            "TWSE／TPEx 公司基本資料並保證每檔至少一類；official_value_chain "
            "來自 TPEx 產業價值鏈資訊平台，可一檔多標籤。請用 "
            "refresh_concepts.py --write 更新，不要手動覆蓋官方分類。"
        ),
        "generated_at": date.today().isoformat(),
        "coverage": stats,
        "concepts": concepts,
    }
    return payload, stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write", action="store_true", help="驗證成功後覆寫 concepts.json"
    )
    parser.add_argument("--delay", type=float, default=0.1, help="頁面間隔秒數")
    args = parser.parse_args()

    payload, stats = build_payload(max(0, args.delay))
    print(json.dumps(stats, ensure_ascii=True, indent=2))
    if args.write:
        CONCEPTS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已更新 {CONCEPTS_PATH}")
    else:
        print("dry-run：未寫入；確認結果後加 --write。")


if __name__ == "__main__":
    main()
