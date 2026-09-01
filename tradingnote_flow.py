"""資金流向模組的期間模型、分析服務與結果集合。

這個模組刻意不依賴 PySide6。GUI 只需要提供 snapshot 與 FlowPeriod，
由 FlowAnalysisService 產生同一批資料給各個子分類顯示，避免每個 widget
各自決定區間、各自查詢資料庫。
"""

from collections import OrderedDict
from dataclasses import dataclass, replace

from tradingnote_history import (
    compute_industry_flow,
    compute_stock_capital_flow,
    compute_valuation_flow,
    compute_volume_ratio_outliers,
    get_industry_top_stocks_range,
)


@dataclass(frozen=True)
class FlowPeriod:
    """資金流向頁共用的分析期間。"""

    start_date: str
    trading_days: int

    def __post_init__(self):
        if not self.start_date:
            raise ValueError("資金流向期間需要起始日期")
        if int(self.trading_days) < 1:
            raise ValueError("資金流向期間至少需要 1 個交易日")
        object.__setattr__(self, "trading_days", int(self.trading_days))

    @property
    def recent_days(self):
        """估值模式使用的短期窗口，受同一流向區間限制。"""
        return min(5, self.trading_days)


@dataclass(frozen=True)
class FlowDashboardData:
    """同一次分析產生、供資金流向頁各子分類共用的資料。"""

    period: FlowPeriod
    industry_flow: tuple
    stock_capital_flow: tuple
    volume_outliers: tuple
    valuation_flow: tuple | None = None


class FlowAnalysisService:
    """統一資金流向查詢口徑，並快取最近幾次分析結果。"""

    def __init__(self, db_path, max_cache_entries=4):
        self.db_path = db_path
        self.max_cache_entries = max(1, int(max_cache_entries))
        self._cache = OrderedDict()
        self._valuation_cache = OrderedDict()

    @staticmethod
    def _snapshot_key(snapshot):
        # 不直接把 snapshot 放進 cache key（PriceInfo 不保證可 hash），只取會影響
        # 流向結果的欄位；同一天重新整理後價格變化也能正確失效。
        values = tuple(
            sorted(
                (
                    ticker,
                    price.date,
                    price.close,
                    price.volume,
                    price.trading_value,
                )
                for ticker, price in snapshot.items()
            )
        )
        return hash(values)

    def analyze(self, snapshot, period, bubble_mode="momentum", valuation_metric="per"):
        """產生資金流向頁完整資料；四個子分類共用同一個 period。"""
        if not isinstance(period, FlowPeriod):
            raise TypeError("period 必須是 FlowPeriod")
        if bubble_mode not in ("momentum", "valuation"):
            raise ValueError("bubble_mode 必須是 momentum 或 valuation")
        base_key = (self._snapshot_key(snapshot), period)
        cached = self._cache.get(base_key)
        if cached is not None:
            self._cache.move_to_end(base_key)
        else:
            days = period.trading_days
            cached = FlowDashboardData(
                period=period,
                industry_flow=tuple(
                    compute_industry_flow(self.db_path, snapshot, avg_days=days)
                ),
                stock_capital_flow=tuple(
                    compute_stock_capital_flow(
                        self.db_path,
                        snapshot,
                        avg_days=days,
                        top_n=50,
                        volume_avg_days=days,
                    )
                ),
                volume_outliers=tuple(
                    compute_volume_ratio_outliers(
                        self.db_path, snapshot, avg_days=days, min_ratio=1.5
                    )
                ),
            )
            self._cache[base_key] = cached
            self._cache.move_to_end(base_key)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)

        if bubble_mode == "momentum":
            return cached

        valuation_key = (base_key, valuation_metric)
        valuation_flow = self._valuation_cache.get(valuation_key)
        if valuation_flow is None:
            valuation_flow = tuple(
                compute_valuation_flow(
                    self.db_path,
                    snapshot,
                    short_days=period.recent_days,
                    long_days=period.trading_days,
                    metric=valuation_metric,
                )
            )
            self._valuation_cache[valuation_key] = valuation_flow
            self._valuation_cache.move_to_end(valuation_key)
            while len(self._valuation_cache) > self.max_cache_entries:
                self._valuation_cache.popitem(last=False)
        else:
            self._valuation_cache.move_to_end(valuation_key)

        return replace(cached, valuation_flow=valuation_flow)

    def get_industry_top_stocks(self, snapshot, period, industry, top_n=30):
        """取得產業明細，量／均量基準也跟隨共用期間。"""
        return get_industry_top_stocks_range(
            self.db_path,
            snapshot,
            industry,
            period.trading_days,
            top_n=top_n,
            volume_avg_days=period.trading_days,
        )
