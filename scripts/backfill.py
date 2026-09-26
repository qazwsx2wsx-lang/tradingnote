#!/usr/bin/env python3
"""回補最近 N 個交易日（預設 60）的三大法人＋收盤價，並重算類股指標。

    python scripts/backfill.py              # 60 個交易日
    python scripts/backfill.py --days 120   # 自訂天數

已存在的日期會跳過，可隨時中斷重跑。每個請求間隔 3 秒，60 天約需 20 分鐘。
"""

import argparse

import _bootstrap  # noqa: F401
import tradingnote_institutional_history as ih
from tradingnote_paths import APP_PATHS


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=ih.DEFAULT_TARGET_DAYS)
    parser.add_argument("--delay", type=float, default=ih.DEFAULT_DELAY_SECONDS)
    args = parser.parse_args()
    written = ih.backfill(APP_PATHS.history_db, target_days=args.days, delay_seconds=args.delay)
    print(f"完成：新寫入 {len(written)} 個交易日，資料庫共 {len(ih.completed_dates(APP_PATHS.history_db))} 天")


if __name__ == "__main__":
    main()
