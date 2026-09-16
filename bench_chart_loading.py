"""效能基準：量測「個股完整籌碼」／「部位紀錄」詳細資訊區塊目前建立與繪圖的
耗時，供圖表架構統整第一~三階段前後比對用（見
C:\\Users\\Evan\\.claude\\plans\\read-tradingnote-handoff-md-virtual-hopcroft.md）。

只量測 StockChart／_populate_*_chart／ComparisonWidget／DetailChartPanel 這幾個
已知會被 StockDetailDialog._build_full_detail_widgets 與 TradingNoteWindow.
_build_position_detail_section 呼叫的既有函式／類別本身，不透過
StockDetailDialog／TradingNoteWindow 整個類別（後者建構需要 settings／
snapshot／本地 DB 等一整套應用程式狀態，不適合放進輕量基準腳本）。
`detail_chart_panel_open_only_active_tab` 量的是第三階段之後兩個呼叫點實際會
付出的成本（DetailChartPanel 只建立目前作用中那個分頁）；
`populate_all_8_plus_technical_plus_comparison`／`full_detail_block_equivalent`
量的是「如果沒做延遲建立」會是什麼成本，留著當對照組／第一階段基準比較用。

全部用合成資料（不打任何 API／不讀真實 data/history.db），確保任何人在任何
環境下重跑都拿到同樣量級的數字，可重複比對。

用法：
    python bench_chart_loading.py [label]

輸出：印一份人類可讀報表到 stdout，並把原始數字存成
bench_results_<label>.json（預設 label 是 "latest"）；重新量測後可以跟先前
存的 baseline 檔案手動比對。
"""
import json
import os
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtGui, QtWidgets

from ui.theme import STYLESHEET
from ui.stock_charts import StockChart, ComparisonWidget, chart_page
import tradingnote_gui as gui
from tradingnote_technical import calculate_indicators

REPEATS = 20
DAYS = 150


def _make_dates(n=DAYS):
    # 純數字序列即可，_populate_*_chart 只把 date 字串當標籤用，不解析格式。
    return [f"2026-{(i // 28) % 12 + 1:02d}-{i % 28 + 1:02d}" for i in range(n)]


def _make_synthetic_data():
    """比照 tradingnote_finmind.POSITION_DETAIL_FIELDS 的實際回傳形狀手刻合成
    資料，欄位名稱與巢狀結構跟 fetch_position_detail() 一致，這樣量到的
    _populate_*_chart／calculate_indicators 耗時才反映真實資料量下的行為。"""
    dates = _make_dates()
    closes = [100 + (i % 17) - 8 + i * 0.05 for i in range(DAYS)]
    price_history = [
        dict(date=d, open=c - 1, high=c + 1.5, low=c - 1.5, close=c, volume=2_000_000 + i * 1000,
             trading_value=c * (2_000_000 + i * 1000))
        for i, (d, c) in enumerate(zip(dates, closes))
    ]

    def flow_series(seed):
        return [((i * seed) % 41 - 20) * 1000 for i in range(DAYS)]

    institutional_history = {
        "dates": dates,
        "series": {"外資": flow_series(7), "投信": flow_series(11), "自營商": flow_series(13)},
    }
    institutional_detail_history = {
        "dates": dates,
        "series": {
            "外資": flow_series(7), "外資自營商": flow_series(5), "投信": flow_series(11),
            "自營商(自行)": flow_series(13), "自營商(避險)": flow_series(17),
        },
    }
    margin_series = {
        "融資餘額": [5000 + (i % 30) * 10 for i in range(DAYS)],
        "融券餘額": [800 + (i % 20) * 5 for i in range(DAYS)],
    }
    margin_history = {
        "dates": dates,
        "series": margin_series,
        "latest": {
            "date": dates[-1],
            "margin_balance": margin_series["融資餘額"][-1],
            "margin_change": 10,
            "short_balance": margin_series["融券餘額"][-1],
            "short_change": -5,
        },
    }
    vpt_mfi_history = {
        "dates": dates,
        "vpt": [sum(flow_series(3)[: i + 1]) for i in range(DAYS)],
        "mfi": [50 + (i % 40) - 20 for i in range(DAYS)],
    }
    sbl_short_balance = {
        "dates": dates,
        "series": {"借券賣出餘額": [300_000 + (i % 25) * 1000 for i in range(DAYS)]},
        "date": dates[-1],
        "balance": 300_000,
        "change": 1000,
    }
    lending = {
        "dates": dates,
        "series": {"借券成交量": [50 + (i % 15) for i in range(DAYS)]},
        "date": dates[-1],
        "volume": 62,
        "avg_fee_rate": 1.2,
    }
    foreign_shareholding = {"date": dates[-1], "ratio": 35.2, "change": 0.3}
    valuation = {"date": dates[-1], "per": 18.4, "pbr": 2.1, "dividend_yield": 3.5}
    margin_cost_estimate = {"date": dates[-1], "cost": closes[-1] * 0.95}

    return {
        "valuation": valuation,
        "price_history": price_history,
        "institutional_history": institutional_history,
        "institutional_detail_history": institutional_detail_history,
        "margin_history": margin_history,
        "foreign_shareholding": foreign_shareholding,
        "lending": lending,
        "suspension": [],
        "vpt_mfi_history": vpt_mfi_history,
        "technical_indicators": None,
        "sbl_short_balance": sbl_short_balance,
        "margin_cost_estimate": margin_cost_estimate,
    }


def _timeit(fn, repeats=REPEATS):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {
        "mean_ms": statistics.mean(samples),
        "median_ms": statistics.median(samples),
        "stdev_ms": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def bench_widget_construction(app, n=10):
    def build():
        charts = []
        for _ in range(n):
            chart = StockChart()
            chart.setBackground("#1b1f24")
            chart.showGrid(x=True, y=True, alpha=0.08)
            chart.setMinimumHeight(240)
            chart.addLegend()
            charts.append(chart)
        for chart in charts:
            chart.close()
            chart.deleteLater()
        app.processEvents()
    return _timeit(build)


def bench_populate_all(data):
    technical_data = calculate_indicators([
        dict(date=r["date"], open=r["open"], max=r["high"], min=r["low"], close=r["close"],
             Trading_Volume=r["volume"], Trading_money=r["trading_value"])
        for r in data["price_history"]
    ])

    def build_and_populate():
        price = StockChart()
        flow = StockChart()
        inst_detail = StockChart()
        margin = StockChart()
        vpt = StockChart()
        mfi = StockChart()
        sbl = StockChart()
        lending = StockChart()
        gui._populate_price_chart(price, data["price_history"])
        gui._populate_flow_chart(flow, data["institutional_history"])
        gui._populate_institutional_detail_chart(inst_detail, data["institutional_detail_history"])
        gui._populate_margin_chart(margin, data["margin_history"])
        gui._populate_vpt_chart(vpt, data["vpt_mfi_history"])
        gui._populate_mfi_chart(mfi, data["vpt_mfi_history"])
        gui._populate_short_sale_balance_chart(sbl, data["sbl_short_balance"])
        gui._populate_lending_volume_chart(lending, data["lending"])
        technical_chart = StockChart()
        gui._populate_technical_chart(technical_chart, technical_data, "kd")
        comparison = ComparisonWidget()
        comparison.set_sources([
            ("股價", price), ("三大法人累計", flow), ("法人分別累計", inst_detail),
            ("融資融券", margin), ("VPT", vpt), ("MFI", mfi),
            ("借券餘額", sbl), ("借券成交", lending),
            *gui._technical_comparison_sources(technical_data),
        ])
        for widget in (price, flow, inst_detail, margin, vpt, mfi, sbl, lending, technical_chart, comparison):
            widget.close()
            widget.deleteLater()

    return _timeit(build_and_populate)


def bench_full_detail_block_equivalent(data):
    """階段三之前的做法：8 張明細圖 + 技術分析 + 雙資料比較一次全部建立＋
    populate，模擬 StockDetailDialog._build_full_detail_widgets +
    _on_show_full_detail 或 TradingNoteWindow._build_position_detail_section +
    _render_position_detail 單次「開啟並顯示資料」的總成本。階段三之後兩個
    呼叫點都改用 DetailChartPanel，不再是這個成本，這裡保留只是當作
    「如果沒做延遲建立」的對照組。"""
    return bench_populate_all(data)


def bench_detail_chart_panel(data):
    """階段三之後的實際成本：DetailChartPanel() 建構（只建立目前作用中那個
    分頁）＋ set_data(data)（目前分頁立刻用新資料 populate，其餘 9 個分頁
    維持延遲，等使用者真的切過去才建立＋populate）。這是 StockDetailDialog
    按下「顯示完整籌碼面資訊」／部位紀錄頁開啟＋選取第一筆部位，目前實際要
    付出的圖表成本。"""
    def build_and_set():
        panel = gui.DetailChartPanel()
        panel.set_data(data)
        panel.tabs.close()
        panel.tabs.deleteLater()
    return _timeit(build_and_set)


def bench_detail_chart_panel_visit_all_tabs(data):
    """DetailChartPanel 建構＋set_data()＋使用者依序切過全部 10 個分頁（等同
    使用者真的點過每一張圖）的總成本，跟 bench_full_detail_block_equivalent
    互相對照：兩者理論上應該收斂到差不多的量級（因為到最後全部分頁都建立
    過了），差別只在於「使用者實際會不會切到每一分頁」。"""
    def build_and_visit_all():
        panel = gui.DetailChartPanel()
        panel.set_data(data)
        for i in range(panel.tabs.count()):
            panel.tabs.setCurrentIndex(i)
        panel.tabs.close()
        panel.tabs.deleteLater()
    return _timeit(build_and_visit_all)


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "latest"
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setFont(QtGui.QFont("Microsoft JhengHei", 10))
    app.setStyleSheet(STYLESHEET)

    data = _make_synthetic_data()

    results = {
        "days": DAYS,
        "repeats": REPEATS,
        "widget_construction_10_charts": bench_widget_construction(app, n=10),
        "populate_all_8_plus_technical_plus_comparison": bench_populate_all(data),
        "full_detail_block_equivalent": bench_full_detail_block_equivalent(data),
        "detail_chart_panel_open_only_active_tab": bench_detail_chart_panel(data),
        "detail_chart_panel_visit_all_tabs": bench_detail_chart_panel_visit_all_tabs(data),
    }

    print(f"=== bench_chart_loading（label={label}，{DAYS} 個交易日合成資料，"
          f"每項 {REPEATS} 次取平均） ===\n")
    for name, stats in results.items():
        if not isinstance(stats, dict):
            continue
        print(f"{name}:")
        print(f"  mean={stats['mean_ms']:.2f}ms  median={stats['median_ms']:.2f}ms  "
              f"stdev={stats['stdev_ms']:.2f}ms  min={stats['min_ms']:.2f}ms  max={stats['max_ms']:.2f}ms")
    print(f"\n（圖表架構統整第三階段後，PositionRecordPage._build_position_detail_section"
          " 與 StockDetailDialog._build_full_detail_widgets 都改用 DetailChartPanel，"
          "實際成本是「detail_chart_panel_open_only_active_tab」這行，不是"
          "「full_detail_block_equivalent」——後者只當作「如果沒做延遲建立」的對照組。）")

    out_path = f"bench_results_{label}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n原始數字已存到 {out_path}")


if __name__ == "__main__":
    main()
