#!/usr/bin/env python3
"""補 daily_prices 的最高／最低價（每日選股的 KD 鈍化條件要用；可中斷續跑）。

    python scripts/backfill_high_low.py
    python scripts/backfill_high_low.py --days 120 --delay 1.0

一天上市、上櫃各打一次官方 API（免費）。已有高低價的日子不會重抓。
"""

import argparse

import _bootstrap  # noqa: F401
import tradingnote_history as history
import tradingnote_screener as screener
from tradingnote_paths import APP_PATHS


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=screener.KD_BACKFILL_DAYS, help="最近幾個交易日")
    parser.add_argument("--delay", type=float, default=history.DEFAULT_BACKFILL_DELAY_SECONDS)
    parser.add_argument("--db", default=str(APP_PATHS.history_db), help="資料庫路徑（預設＝App 的資料庫）")
    args = parser.parse_args()
    pending = history.missing_high_low(args.db, args.days)
    print(f"最近 {args.days} 個交易日中，要補高低價的（日期, 市場）共 {len(pending)} 筆", flush=True)

    def progress(done, total):
        print(f"{done}/{total}", flush=True)

    print(history.backfill_high_low(args.db, args.days, args.delay, on_progress=progress), flush=True)


if __name__ == "__main__":
    main()
