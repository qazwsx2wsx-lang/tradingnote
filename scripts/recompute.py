#!/usr/bin/env python3
"""只重算類股對照與指標（改完 sector_overrides.json 後執行），不打任何 API。"""

import _bootstrap  # noqa: F401
import tradingnote_institutional_history as ih
from tradingnote_paths import APP_PATHS

if __name__ == "__main__":
    ih.refresh_sector_map(APP_PATHS.history_db)
    print(f"重算 {ih.compute_metrics(APP_PATHS.history_db)} 筆類股指標")
