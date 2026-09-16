"""資金流向模組的期間模型、分析服務與結果集合。

這個模組刻意不依賴 PySide6。GUI 只需要提供 snapshot 與 FlowPeriod，
由 FlowAnalysisService 產生同一批資料給各個子分類顯示，避免每個 widget
各自決定區間、各自查詢資料庫。
"""

from collections import OrderedDict
from dataclasses import dataclass, replace

from tradingnote_history import (
    compute_group_flow,
    get_available_dates,
    get_data_revision,
    analysis_snapshot,
    compute_group_valuation_flow,
    compute_stock_capital_flow,
    compute_volume_ratio_outliers,
    get_group_top_stocks_range,
    get_industry_map,
    get_industry_top_stocks_range,
)
from tradingnote_concepts import (
    CLASSIFICATION_INDUSTRY,
    CLASSIFICATION_VALUE_CHAIN_LEAF,
)
from tradingnote_institutional import (
    aggregate_institutional_by_group,
    load_cached_institutional_snapshot,
)


@dataclass(frozen=True)
class FlowPeriod:
    """資金流向頁共用的分析期間。"""

    start_date: str
    trading_days: int
    end_date: str | None = None
    actual_dates: tuple = ()
    prior_dates: tuple = ()

    @property
    def sufficient(self):
        return len(self.actual_dates) == self.trading_days

    def resolve(self, db_path, snapshot):
        end = self.end_date or max((p.date for p in snapshot.values() if p.date), default=self.start_date)
        dates = set(get_available_dates(db_path)) | {p.date for p in snapshot.values() if p.date}
        available = sorted(d for d in dates if d <= end)
        end = available[-1] if available else end
        dates = [d for d in available if self.start_date <= d][-self.trading_days:]
        return replace(self, end_date=end, actual_dates=tuple(dates),
                       prior_dates=tuple(d for d in available if d < end))

    def __post_init__(self):
        if not self.start_date:
            raise ValueError("資金流向期間需要起始日期")
        if int(self.trading_days) < 1:
            raise ValueError("資金流向期間至少需要 1 個交易日")
        object.__setattr__(self, "trading_days", int(self.trading_days))

    @property
    def recent_days(self):
        """估值模式使用的短期窗口。"""
        return min(5, self.trading_days)

    @property
    def valuation_baseline_days(self):
        """估值資金熱度至少以 20 日為基準，避免短長窗口相同。"""
        return max(20, self.trading_days)


@dataclass(frozen=True)
class FlowDashboardData:
    """同一次分析產生、供資金流向頁各子分類共用的資料。"""

    period: FlowPeriod
    industry_flow: tuple
    stock_capital_flow: tuple
    volume_outliers: tuple
    valuation_flow: tuple | None = None
    classification_mode: str = CLASSIFICATION_INDUSTRY
    classification_scope: str | None = None
    institutional_flow: tuple = ()


class FlowAnalysisService:
    """統一資金流向查詢口徑，並快取最近幾次分析結果。"""

    def __init__(
        self,
        db_path,
        classification_catalog=None,
        institutional_cache_path=None,
        max_cache_entries=4,
    ):
        self.db_path = db_path
        self.classification_catalog = classification_catalog
        self.max_cache_entries = max(1, int(max_cache_entries))
        self._cache = OrderedDict()
        self._group_cache = OrderedDict()
        self._valuation_cache = OrderedDict()
        self.institutional_cache_path = institutional_cache_path
        self._institutional_group_cache = OrderedDict()

        if classification_catalog is not None:
            industry_groups = classification_catalog.groups(CLASSIFICATION_INDUSTRY)
            self._catalog_revision = classification_catalog.revision
        else:
            industry_groups = None
            self._catalog_revision = "industry-map"
        if not industry_groups:
            industry_map = get_industry_map(db_path)
            industry_groups = OrderedDict()
            for ticker, industry in industry_map.items():
                industry_groups.setdefault(industry, set()).add(ticker)
        self._industry_groups = industry_groups
        self._market_universe = frozenset(
            ticker for members in industry_groups.values() for ticker in members
        )

    def _remember(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.max_cache_entries:
            cache.popitem(last=False)

    def _groups(self, classification_mode, classification_scope=None):
        if self.classification_catalog is None:
            if classification_mode != CLASSIFICATION_INDUSTRY:
                raise ValueError("沒有概念分類目錄，只能使用官方產業")
            industry_map = get_industry_map(self.db_path)
            groups = OrderedDict()
            for ticker, industry in industry_map.items():
                groups.setdefault(industry, set()).add(ticker)
            return groups, None

        if classification_mode == CLASSIFICATION_INDUSTRY:
            return self._industry_groups, None
        if (
            classification_mode == CLASSIFICATION_VALUE_CHAIN_LEAF
            and classification_scope is None
        ):
            scopes = self.classification_catalog.value_chain_scopes
            classification_scope = scopes[0] if scopes else None
        groups = self.classification_catalog.groups(
            classification_mode, classification_scope
        )
        if not groups:
            raise ValueError("所選分類沒有可用群組")
        return groups, classification_scope

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
                    price.change,
                    price.volume,
                    price.trading_value,
                    price.name,
                    price.market,
                )
                for ticker, price in snapshot.items()
            )
        )
        return values

    def analyze(
        self,
        snapshot,
        period,
        bubble_mode="momentum",
        valuation_metric="per",
        classification_mode=CLASSIFICATION_INDUSTRY,
        classification_scope=None,
    ):
        """產生資金流向頁完整資料；四個子分類共用同一個 period。"""
        if not isinstance(period, FlowPeriod):
            raise TypeError("period 必須是 FlowPeriod")
        if bubble_mode not in ("momentum", "valuation", "institutional_sync"):
            raise ValueError("bubble_mode 必須是 momentum、valuation 或 institutional_sync")
        groups, classification_scope = self._groups(
            classification_mode, classification_scope
        )
        period = period.resolve(self.db_path, snapshot)
        snapshot = analysis_snapshot(self.db_path, snapshot, period)
        revision = get_data_revision(self.db_path)
        market_key = (revision, self._snapshot_key(snapshot), period)
        market_cached = self._cache.get(market_key)
        if market_cached is not None:
            self._cache.move_to_end(market_key)
        else:
            days = period.trading_days
            stocks = compute_stock_capital_flow(
                self.db_path, snapshot, avg_days=days, top_n=len(snapshot),
                volume_avg_days=days, period=period,
            )
            market_cached = (
                tuple(stocks[:50]),
                tuple(compute_volume_ratio_outliers(
                    self.db_path, snapshot, avg_days=days, min_ratio=1.5,
                    period=period, stock_rows=stocks,
                )),
            )
            self._remember(self._cache, market_key, market_cached)

        group_key = (
            market_key,
            self._catalog_revision,
            classification_mode,
            classification_scope,
        )
        group_flow = self._group_cache.get(group_key)
        if group_flow is None:
            group_flow = tuple(
                compute_group_flow(
                    self.db_path,
                    snapshot,
                    groups,
                    avg_days=period.trading_days,
                    period=period,
                    market_universe=self._market_universe,
                )
            )
            self._remember(self._group_cache, group_key, group_flow)
        else:
            self._group_cache.move_to_end(group_key)

        institutional_flow = ()
        if self.institutional_cache_path is not None:
            institutional_rows = load_cached_institutional_snapshot(
                self.institutional_cache_path
            )
            institutional_revision = hash(tuple(institutional_rows))
            institutional_key = (group_key, institutional_revision)
            institutional_flow = self._institutional_group_cache.get(
                institutional_key
            )
            if institutional_flow is None:
                institutional_flow = tuple(
                    aggregate_institutional_by_group(
                        institutional_rows, groups, snapshot
                    )
                )
                self._remember(
                    self._institutional_group_cache,
                    institutional_key,
                    institutional_flow,
                )
            else:
                self._institutional_group_cache.move_to_end(institutional_key)

        cached = FlowDashboardData(
            period=period,
            industry_flow=group_flow,
            stock_capital_flow=market_cached[0],
            volume_outliers=market_cached[1],
            classification_mode=classification_mode,
            classification_scope=classification_scope,
            institutional_flow=institutional_flow,
        )

        if bubble_mode != "valuation":
            # momentum／institutional_sync 都不需要 valuation_flow：
            # institutional_flow 已經在上面無條件算好、放進 cached。
            return cached

        valuation_key = (group_key, valuation_metric)
        valuation_flow = self._valuation_cache.get(valuation_key)
        if valuation_flow is None:
            valuation_flow = tuple(
                compute_group_valuation_flow(
                    self.db_path,
                    snapshot,
                    groups,
                    period=period,
                    short_days=period.recent_days,
                    long_days=period.valuation_baseline_days,
                    metric=valuation_metric,
                    market_universe=self._market_universe,
                )
            )
            self._remember(self._valuation_cache, valuation_key, valuation_flow)
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
            period=period,
        )

    def get_group_top_stocks(
        self,
        snapshot,
        period,
        group_name,
        classification_mode=CLASSIFICATION_INDUSTRY,
        classification_scope=None,
        top_n=30,
    ):
        """取得目前分類下某個泡泡／排行項目的成分股。"""
        groups, _scope = self._groups(classification_mode, classification_scope)
        members = groups.get(group_name)
        if not members:
            return []
        return get_group_top_stocks_range(
            self.db_path,
            snapshot,
            members,
            period.trading_days,
            top_n=top_n,
            volume_avg_days=period.trading_days,
            period=period,
        )
