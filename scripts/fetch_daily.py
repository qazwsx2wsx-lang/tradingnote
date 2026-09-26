#!/usr/bin/env python3
"""每日盤後排程（交易日 16:30 後執行）：抓今天的資料，順便補上最近漏掉的交易日。

非交易日或資料尚未公布時什麼都不寫、正常結束（exit code 0）。
"""

import sys

import _bootstrap  # noqa: F401
import tradingnote_institutional_history as ih
from tradingnote_paths import APP_PATHS


def main():
    today = ih.taipei_today().isoformat()
    written = ih.backfill(APP_PATHS.history_db, target_days=ih.DEFAULT_TARGET_DAYS)
    if today in written:
        print(f"{today} 已更新")
    elif written:
        print(f"補上 {', '.join(written)}；{today} 非交易日或尚未公布")
    else:
        print(f"{today}：沒有新資料（非交易日、尚未公布，或已是最新）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
