"""讓 tools/ 底下的腳本可以直接 import 專案根目錄的核心模組。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
