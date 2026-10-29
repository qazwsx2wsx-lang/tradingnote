"""Application paths shared by the CLI and GUI entry points."""

from dataclasses import dataclass
import os
import sys
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    """Filesystem locations used by one TradingNote installation."""

    root: Path
    data_dir: Path
    positions: Path
    price_cache: Path
    futures_cache: Path
    futures_large_traders_cache: Path
    futures_ssf_cache: Path
    institutional_cache: Path
    position_detail_cache: Path
    history_db: Path
    settings: Path


def _default_frozen_data_dir() -> Path:
    """PyInstaller 打包後的 .app／.exe：資料不能寫進 bundle 裡（唯讀、簽章後也不該
    改動），改用各平台慣例的使用者資料目錄。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TradingNote"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "TradingNote"
    return Path.home() / ".tradingnote"


def get_app_paths(root=None):
    """Return application paths, optionally overriding the data directory."""

    is_frozen = bool(getattr(sys, "frozen", False))
    if root is not None:
        root_path = Path(root)
    elif is_frozen:
        # PyInstaller 在 onefile／onedir 兩種模式下都會設定 sys._MEIPASS 指向
        # 打包進去的資源所在目錄（onefile 是解壓到暫存資料夾，onedir 則是建置
        # 時就展開好的目錄），concepts.json／sector_overrides.json／assets/
        # 都是從這裡找，跟原始碼目錄結構一致。
        root_path = Path(sys._MEIPASS)
    else:
        root_path = Path(__file__).resolve().parent

    configured_data_dir = os.environ.get("TRADINGNOTE_DATA_DIR")
    if configured_data_dir:
        data_dir = Path(configured_data_dir)
    elif is_frozen:
        data_dir = _default_frozen_data_dir()
    else:
        data_dir = root_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return AppPaths(
        root=root_path,
        data_dir=data_dir,
        positions=data_dir / "positions.json",
        price_cache=data_dir / "price_cache.json",
        futures_cache=data_dir / "futures_cache.json",
        futures_large_traders_cache=data_dir / "futures_large_traders_cache.json",
        futures_ssf_cache=data_dir / "futures_ssf_cache.json",
        institutional_cache=data_dir / "institutional_cache.json",
        position_detail_cache=data_dir / "position_detail_cache.json",
        history_db=data_dir / "history.db",
        settings=data_dir / "settings.json",
    )


APP_PATHS = get_app_paths()
