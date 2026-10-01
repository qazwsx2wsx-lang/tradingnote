#!/usr/bin/env python3
"""由本機產業資料庫與 concepts.json 重建 CONCEPT.MD。

這支腳本只做文件產生，不改動資料庫或 concepts.json。先讓 TradingNote 更新
data/history.db 的 industry_map，再執行本檔即可取得最新清冊。
"""

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import _bootstrap  # noqa: F401
from tradingnote_history import INDUSTRY_CODE_NAMES


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "history.db"
CONCEPTS_PATH = ROOT / "concepts.json"
OUTPUT_PATH = ROOT / "docs" / "CONCEPT.MD"
MARKET_LABELS = {"TWSE": "上市", "TPEX": "上櫃"}
INDUSTRY_ORDER = {
    name: index for index, name in enumerate(INDUSTRY_CODE_NAMES.values())
}


def _load_directory():
    if not DB_PATH.exists():
        raise SystemExit(f"找不到產業資料庫：{DB_PATH}")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ticker, name, industry, market, updated_at "
            "FROM industry_map ORDER BY ticker"
        ).fetchall()
    if not rows:
        raise SystemExit("industry_map 是空的；請先啟動 TradingNote 更新產業清單。")
    return [
        {
            "ticker": ticker,
            "name": name,
            "industry": INDUSTRY_CODE_NAMES.get(industry, industry or "未分類"),
            "market": market,
            "updated_at": updated_at,
        }
        for ticker, name, industry, market, updated_at in rows
    ]


def _load_concepts():
    if not CONCEPTS_PATH.exists():
        return []
    with CONCEPTS_PATH.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    concepts = payload.get("concepts")
    return concepts if isinstance(concepts, list) else []


def _chunks(items, size=10):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _stock_lines(stocks):
    return [
        "  - " + "、".join(f"`{stock['ticker']}` {stock['name']}" for stock in chunk)
        for chunk in _chunks(stocks)
    ]


def render_document(directory, concepts):
    names = {stock["ticker"]: stock["name"] for stock in directory}
    universe = set(names)
    by_type = defaultdict(list)
    ticker_concepts = defaultdict(list)
    assignments = []
    for concept in concepts:
        classification_type = concept.get("classification_type") or "manual_theme"
        by_type[classification_type].append(concept)
        for ticker in concept.get("stock_ids") or []:
            if ticker in universe:
                ticker_concepts[ticker].append(concept["name"])
                assignments.append(ticker)

    tagged = set(assignments)
    coverage = len(tagged) / len(universe) * 100 if universe else 0
    multi_tagged = sum(len(set(tags)) > 1 for tags in ticker_concepts.values())
    value_chain_tagged = {
        ticker
        for concept in by_type["official_value_chain"]
        for ticker in (concept.get("stock_ids") or [])
        if ticker in universe
    }
    snapshot = max(stock["updated_at"] for stock in directory).split("T", 1)[0]
    markets = {
        market: sum(stock["market"] == market for stock in directory)
        for market in MARKET_LABELS
    }

    lines = [
        "# 台股產業與概念分類清冊",
        "",
        "> 本文件是可追溯的全市場分類底稿，不是投資建議。每檔個股至少有一個官方產業標籤；",
        "> 可從櫃買中心產業價值鏈取得更細的多重概念，人工題材則保留作補充。",
        "",
        "## 現況摘要",
        "",
        f"- 資料快照：`{snapshot}`（個股母體）／`{max((c.get('source_date') or '') for c in concepts)}`（概念分類）",
        f"- 個股全集：**{len(directory):,} 檔**；上市 {markets['TWSE']:,} 檔、上櫃 {markets['TPEX']:,} 檔",
        f"- 實際概念資料：**{len(concepts):,} 類、{len(assignments):,} 筆個股—概念關聯**",
        f"- 個股覆蓋率：**{len(tagged):,}/{len(universe):,}（{coverage:.2f}%）**；不是只列出股票，而是每檔都已寫入 `concepts.json`",
        f"- 細分類覆蓋：櫃買中心產業價值鏈涵蓋 **{len(value_chain_tagged):,} 檔**；其餘 {len(universe - value_chain_tagged):,} 檔仍有官方產業標籤保底",
        f"- 多標籤個股：**{multi_tagged:,} 檔**；分類組成為人工題材 {len(by_type['manual_theme']):,} 類、官方產業 {len(by_type['official_industry']):,} 類、官方價值鏈 {len(by_type['official_value_chain']):,} 類",
        "- 範圍：上市、上櫃公司與官方清單中的臺灣存託憑證；不含 ETF、ETN、權證、債券。興櫃目前不在 TradingNote 行情全集。",
        "",
        "## 分類方法",
        "",
        "### 第一層：官方產業（單一分類、全數必填）",
        "",
        "每檔證券以公開資訊觀測站／交易所公司基本資料的產業代碼為準。這層保證全市場都有分類，但粒度較粗，例如 IC 設計、晶圓代工、封測都會落在「半導體業」。",
        "",
        "- 上市來源：[TWSE 上市公司基本資料](https://openapi.twse.com.tw/v1/opendata/t187ap03_L)",
        "- 上櫃來源：[TPEx 上櫃公司基本資料](https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O)",
        "- 產業代碼：[TPEx 收市後交易資訊格式說明](https://www.tpex.org.tw/storage/regular_system/%E6%96%B0%E7%89%88%E6%94%B6%E5%B8%82%E5%BE%8C%E4%BA%A4%E6%98%93%E8%B3%87%E8%A8%8A%E6%A0%BC%E5%BC%8F%E8%AA%AA%E6%98%8E%28V1.33%E7%89%88%29.pdf?t=20251127)，其中 `37`＝運動休閒、`38`＝居家生活",
        "",
        "### 第二層：官方產業價值鏈（多重細分類）",
        "",
        "從[櫃買中心產業價值鏈資訊平台](https://ic.tpex.org.tw/)擷取公司所在節點，例如「半導體／IC設計／網路通訊IC」、「綠色能源／電動車」或「數位科技／人工智慧」。同一公司可同時出現在多個節點；每筆資料都保留來源網址、取得日期與 A 級信心。",
        "",
        "### 第三層：人工題材（補充分類）",
        "",
        "保留原本的 AI 伺服器、航運、電信等 11 組人工標籤。後續新增人工題材時應記錄關聯理由與日期；新聞或社群敘事不直接當成官方分類。主題指數可作高信心種子，但通常另有市值與流動性條件，不能視為全部概念股。",
        "",
        "## 資金流向泡泡圖的分類方式",
        "",
        "TradingNote 可在「資金流向分析」分別選擇官方產業、價值鏈主題、價值鏈細項與人工題材。價值鏈細項需再選一個主鏈，避免同時畫出數百個泡泡。動能／估值是獨立的圖表模式，切換分類不會改變 X、Y 軸的計算定義。",
        "",
        "官方產業是互斥單一分類，泡泡顯示「資金比重」；其他三種為可重疊概念，同一檔股票可同時計入多個群組。因此概念模式顯示的是「成交涵蓋率」：每個群組皆以全市場成交金額為分母，但群組間可重複計入個股，所以各群組合計可能超過 100%。點擊泡泡或熱度排行可查看當前分類群組的成分股。",
        "",
        "## 更新流程",
        "",
        "1. 更新 TradingNote 的 `industry_map`，取得最新上市／上櫃母體與官方產業。",
        "2. 執行 `refresh_concepts.py --write`，保留人工題材、重建 35 組產業標籤，並抓取櫃買中心產業價值鏈細分類。",
        "3. 驗證未知代號、重複概念名稱、空概念與全集覆蓋；未達 100% 時拒絕寫入。",
        "4. 執行 `generate_concept_md.py` 重建本文件與逐股概念索引。",
        "5. 可用 FinMind [`TaiwanStockInfo`](https://finmind.github.io/tutor/TaiwanMarket/Technical/#taiwanstockinfo) 交叉檢查名稱與市場別；轉板股票必須取每個 `stock_id` 日期最新的一列。",
        "",
        "## 人工題材",
        "",
        "| 概念 | 說明 | 已收錄個股 |",
        "|---|---|---|",
    ]

    for concept in by_type["manual_theme"]:
        stock_text = "、".join(
            f"`{ticker}` {names.get(ticker, '（全集查無）')}"
            for ticker in (concept.get("stock_ids") or [])
        ) or "—"
        description = str(concept.get("description") or "—").replace("|", "\\|")
        lines.append(f"| {concept.get('name') or '未命名'} | {description} | {stock_text} |")

    lines.extend(["", "## 官方產業基礎分類", ""])
    for concept in by_type["official_industry"]:
        stock_ids = concept.get("stock_ids") or []
        lines.extend([f"### {concept['name']}（{len(stock_ids)}）", ""])
        stocks = [{"ticker": ticker, "name": names[ticker]} for ticker in stock_ids]
        lines.extend(_stock_lines(stocks))
        lines.append("")

    value_chain_groups = defaultdict(list)
    for concept in by_type["official_value_chain"]:
        parts = concept["name"].split("｜")
        group = parts[1] if len(parts) > 1 else "其他"
        value_chain_groups[group].append(concept)

    lines.extend(["## 官方產業價值鏈細分類", ""])
    for group, group_concepts in value_chain_groups.items():
        source = group_concepts[0].get("source")
        heading = f"[{group}]({source})" if source else group
        lines.extend([f"### {heading}（{len(group_concepts)} 個細分類）", ""])
        for concept in group_concepts:
            parts = concept["name"].split("｜")
            label = "｜".join(parts[2:]) if len(parts) > 2 else concept["name"]
            stock_ids = concept.get("stock_ids") or []
            lines.append(f"- **{label}（{len(stock_ids)}）**")
            for chunk in _chunks(stock_ids):
                lines.append(
                    "  - "
                    + "、".join(f"`{ticker}` {names[ticker]}" for ticker in chunk)
                )
        lines.append("")

    def concept_display(name):
        if name.startswith("產業｜"):
            return "[產業] " + name.removeprefix("產業｜")
        if name.startswith("價值鏈｜"):
            return "[價值鏈] " + name.removeprefix("價值鏈｜").replace("｜", "／")
        return "[人工題材] " + name

    grouped_stocks = defaultdict(list)
    for stock in directory:
        grouped_stocks[stock["industry"]].append(stock)

    lines.extend(
        [
            "## 全部個股概念索引",
            "",
            "以下 1,985 檔各自列出實際寫入 `concepts.json` 的全部標籤，可直接核對某檔股票不是只有出現在名冊，而是真的已有概念關聯。",
            "",
        ]
    )
    for industry in sorted(
        grouped_stocks, key=lambda name: (INDUSTRY_ORDER.get(name, 10_000), name)
    ):
        stocks = sorted(grouped_stocks[industry], key=lambda row: row["ticker"])
        lines.extend([f"### {industry}（{len(stocks)}）", ""])
        for stock in stocks:
            tags = list(dict.fromkeys(ticker_concepts[stock["ticker"]]))
            market = MARKET_LABELS.get(stock["market"], stock["market"])
            lines.append(
                f"- `{stock['ticker']}` {stock['name']}（{market}；{len(tags)} 個概念）"
            )
            for chunk in _chunks([concept_display(tag) for tag in tags], size=4):
                lines.append("  - " + "；".join(chunk))
        lines.append("")

    lines.extend(
        [
            "## 維護指令",
            "",
            "先在 TradingNote 內更新產業資料，再於專案根目錄執行：",
            "",
            "```powershell",
            ".\\.venv\\Scripts\\python.exe .\\tools\\refresh_concepts.py --write",
            ".\\.venv\\Scripts\\python.exe .\\tools\\generate_concept_md.py",
            "```",
            "",
            "更新器只有在所有個股都至少有一個分類，且沒有未知代號或重複概念名稱時才會寫入。",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    document = render_document(_load_directory(), _load_concepts())
    OUTPUT_PATH.write_text(document, encoding="utf-8")
    print(f"已產生 {OUTPUT_PATH}（{document.count(chr(10)) + 1:,} 行）")


if __name__ == "__main__":
    main()
