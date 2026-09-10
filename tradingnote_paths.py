"""Application paths shared by the CLI and GUI entry points."""

from dataclasses import dataclass
import os
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


def get_app_paths(root=None):
    """Return application paths, optionally overriding the data directory."""

    root_path = Path(root) if root is not None else Path(__file__).resolve().parent
    configured_data_dir = os.environ.get("TRADINGNOTE_DATA_DIR")
    data_dir = Path(configured_data_dir) if configured_data_dir else root_path / "data"
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
