"""GUI 使用的資料檔路徑（集中一處，測試可直接覆寫這個模組的屬性）。

其他 ui 模組一律以 `app_paths.HISTORY_DB_PATH` 這種屬性存取方式在呼叫當下讀取，
不要 `from ui.app_paths import HISTORY_DB_PATH`——那樣測試覆寫就不會生效。"""

from tradingnote_paths import APP_PATHS
DATA_DIR = APP_PATHS.data_dir
POSITIONS_PATH = APP_PATHS.positions
CACHE_PATH = APP_PATHS.price_cache
FUTURES_CACHE_PATH = APP_PATHS.futures_cache
FUTURES_LARGE_TRADERS_CACHE_PATH = APP_PATHS.futures_large_traders_cache
FUTURES_SSF_CACHE_PATH = APP_PATHS.futures_ssf_cache
INSTITUTIONAL_CACHE_PATH = APP_PATHS.institutional_cache
POSITION_DETAIL_CACHE_PATH = APP_PATHS.position_detail_cache
HISTORY_DB_PATH = APP_PATHS.history_db
SETTINGS_PATH = APP_PATHS.settings
