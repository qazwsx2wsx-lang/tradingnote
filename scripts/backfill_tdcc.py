#!/usr/bin/env python3
"""補集保戶股權分散表歷史（與 App 內「補集保大戶歷史…」同一套邏輯，可中斷續跑）。

    python scripts/backfill_tdcc.py --market TPEX --weeks 3
    python scripts/backfill_tdcc.py --market all --weeks 8 --delay 1.0

--market：TWSE／TPEX／all（四碼個股，依產業對照表）。已查過的（代號, 週）不會重查。
"""

import argparse

import _bootstrap  # noqa: F401
import tradingnote_chips as chips
import tradingnote_history as history
from tradingnote_paths import APP_PATHS


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", choices=("TWSE", "TPEX", "all"), default="all")
    parser.add_argument("--weeks", type=int, default=3)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()
    directory = history.get_industry_directory(APP_PATHS.history_db, max_age_days=10 ** 6)
    tickers = sorted(t for t, info in directory.items()
                     if len(t) == 4 and t.isdigit() and args.market in ("all", info.get("market")))
    print(f"{args.market}：{len(tickers)} 檔，最近 {args.weeks} 週", flush=True)
    last = [0]

    def progress(done, total, label):
        if done - last[0] >= 50 or done == total:
            last[0] = done
            print(f"{done}/{total}　{label}", flush=True)

    result = chips.backfill_tdcc_history(
        APP_PATHS.history_db, tickers, progress=progress, delay_seconds=args.delay, weeks=args.weeks)
    print(result, flush=True)


if __name__ == "__main__":
    main()
