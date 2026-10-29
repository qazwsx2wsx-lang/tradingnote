import sqlite3
import tempfile
import unittest
from pathlib import Path

from tradingnote_core import Position
from tradingnote_tradejournal import (
    _connect,
    attach_tag,
    cancel_empty_position,
    cancel_trade,
    close_trade,
    create_strategy,
    create_tag,
    detach_tag,
    get_strategy,
    get_trade,
    list_open_positions,
    list_positions,
    list_strategies,
    list_tags,
    list_trades,
    migrate_legacy_position,
    open_position,
    open_trade,
    record_execution,
    trade_as_position,
    update_position_plan,
    upsert_trade_analytics,
)

ALL_TABLES = (
    "strategies",
    "tags",
    "trades",
    "trade_executions",
    "trade_tags",
    "trade_analytics",
)


class TradeJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _table_columns(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        finally:
            conn.close()

    def test_schema_creation_is_idempotent(self):
        conn1 = _connect(self.db_path)
        conn1.close()
        columns_first = {table: self._table_columns(table) for table in ALL_TABLES}

        conn2 = _connect(self.db_path)
        conn2.close()
        columns_second = {table: self._table_columns(table) for table in ALL_TABLES}

        self.assertEqual(columns_first, columns_second)
        for table in ALL_TABLES:
            self.assertTrue(columns_first[table], f"{table} should have columns")

    def test_strategy_crud(self):
        strategy_id = create_strategy(self.db_path, "突破順勢", description="趨勢突破策略")
        strategy = get_strategy(self.db_path, strategy_id)
        self.assertEqual(strategy.name, "突破順勢")
        self.assertEqual(strategy.description, "趨勢突破策略")

        strategies = list_strategies(self.db_path)
        self.assertEqual(len(strategies), 1)

        with self.assertRaises(sqlite3.IntegrityError):
            create_strategy(self.db_path, "突破順勢")

    def test_tag_crud_and_category_filter(self):
        create_tag(self.db_path, "FOMO", category="EMOTION")
        create_tag(self.db_path, "未依計畫平倉", category="EXECUTION")
        create_tag(self.db_path, "盤整格局", category="MARKET_CONDITION")

        self.assertEqual(len(list_tags(self.db_path)), 3)
        emotion_tags = list_tags(self.db_path, category="EMOTION")
        self.assertEqual([t.name for t in emotion_tags], ["FOMO"])

        with self.assertRaises(sqlite3.IntegrityError):
            create_tag(self.db_path, "invalid-category-tag", category="NOT_A_CATEGORY")

    def test_long_trade_lifecycle_with_partial_fills(self):
        trade_id = open_trade(
            self.db_path,
            symbol="2330",
            side="LONG",
            initial_stop_loss=9.0,
            opened_at="2026-10-01T09:00:00",
        )
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.total_qty, 0)

        record_execution(
            self.db_path, trade_id, action="BUY", price=10.0, qty=100,
            executed_at="2026-10-01T09:01:00",
        )
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.total_qty, 100)
        self.assertEqual(trade.avg_entry_price, 10.0)
        self.assertEqual(trade.status, "OPEN")

        record_execution(
            self.db_path, trade_id, action="BUY", price=12.0, qty=100,
            executed_at="2026-10-01T09:02:00",
        )
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.total_qty, 200)
        self.assertEqual(trade.avg_entry_price, 11.0)

        record_execution(
            self.db_path, trade_id, action="SELL", price=15.0, qty=50,
            executed_at="2026-10-02T09:00:00",
        )
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.total_qty, 150)
        self.assertAlmostEqual(trade.realized_pnl, (15.0 - 11.0) * 50)
        self.assertEqual(trade.status, "OPEN")
        self.assertIsNone(trade.closed_at)

        record_execution(
            self.db_path, trade_id, action="SELL", price=14.0, qty=150,
            commission=5.0, executed_at="2026-10-03T09:00:00",
        )
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.total_qty, 0)
        self.assertEqual(trade.status, "CLOSED")
        self.assertIsNotNone(trade.closed_at)

        expected_realized_pnl = (15.0 - 11.0) * 50 + (14.0 - 11.0) * 150
        self.assertAlmostEqual(trade.realized_pnl, expected_realized_pnl)
        self.assertAlmostEqual(trade.total_commission, 5.0)
        self.assertAlmostEqual(trade.net_pnl, expected_realized_pnl - 5.0)
        self.assertEqual(len(trade.executions), 4)

    def test_short_trade_lifecycle(self):
        trade_id = open_trade(
            self.db_path,
            symbol="2317",
            side="SHORT",
            initial_stop_loss=120.0,
            opened_at="2026-10-01T09:00:00",
        )

        record_execution(
            self.db_path, trade_id, action="SELL", price=100.0, qty=100,
            executed_at="2026-10-01T09:01:00",
        )
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.total_qty, 100)
        self.assertEqual(trade.avg_entry_price, 100.0)

        record_execution(
            self.db_path, trade_id, action="BUY", price=90.0, qty=100,
            executed_at="2026-10-02T09:00:00",
        )
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.total_qty, 0)
        self.assertEqual(trade.status, "CLOSED")
        self.assertAlmostEqual(trade.realized_pnl, (100.0 - 90.0) * 100)

    def test_cancel_trade_with_no_executions(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
        )
        cancel_trade(self.db_path, trade_id)
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.status, "CANCELLED")

    def test_close_trade_sets_execution_rating(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
        )
        close_trade(self.db_path, trade_id, execution_rating=4)
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.execution_rating, 4)

    def test_tag_attach_and_detach(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
        )
        tag_id = create_tag(self.db_path, "FOMO", category="EMOTION")

        attach_tag(self.db_path, trade_id, tag_id)
        attach_tag(self.db_path, trade_id, tag_id)  # idempotent, no error
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual([t.name for t in trade.tags], ["FOMO"])

        detach_tag(self.db_path, trade_id, tag_id)
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.tags, ())

    def test_upsert_trade_analytics_updates_single_row(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
        )
        upsert_trade_analytics(self.db_path, trade_id, mae_r=-0.5, mfe_r=1.0)
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.analytics.mae_r, -0.5)
        self.assertEqual(trade.analytics.mfe_r, 1.0)

        upsert_trade_analytics(self.db_path, trade_id, mae_r=-0.8, mfe_r=3.2, realized_r=2.1)
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.analytics.mae_r, -0.8)
        self.assertEqual(trade.analytics.mfe_r, 3.2)
        self.assertEqual(trade.analytics.realized_r, 2.1)

        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM trade_analytics WHERE trade_id=?", (trade_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_get_trade_returns_none_for_missing_trade(self):
        self.assertIsNone(get_trade(self.db_path, 999))

    def test_list_trades_filters(self):
        t1 = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
            opened_at="2026-10-01T09:00:00",
        )
        t2 = open_trade(
            self.db_path, symbol="2317", side="SHORT", initial_stop_loss=120.0,
            opened_at="2026-10-05T09:00:00",
        )
        cancel_trade(self.db_path, t2)

        self.assertEqual(
            {t.trade_id for t in list_trades(self.db_path, symbol="2330")}, {t1}
        )
        self.assertEqual(
            {t.trade_id for t in list_trades(self.db_path, status="CANCELLED")}, {t2}
        )
        self.assertEqual(
            {t.trade_id for t in list_trades(self.db_path, date_from="2026-10-03")},
            {t2},
        )
        self.assertEqual(
            {t.trade_id for t in list_trades(self.db_path, date_to="2026-10-03")},
            {t1},
        )
        self.assertEqual(len(list_trades(self.db_path)), 2)

    def test_cascade_delete_removes_children(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
        )
        record_execution(self.db_path, trade_id, action="BUY", price=10.0, qty=100)
        tag_id = create_tag(self.db_path, "FOMO", category="EMOTION")
        attach_tag(self.db_path, trade_id, tag_id)
        upsert_trade_analytics(self.db_path, trade_id, realized_r=1.0)

        conn = _connect(self.db_path)
        try:
            conn.execute("DELETE FROM trades WHERE trade_id=?", (trade_id,))
            conn.commit()

            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM trade_executions WHERE trade_id=?",
                    (trade_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM trade_tags WHERE trade_id=?", (trade_id,)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM trade_analytics WHERE trade_id=?",
                    (trade_id,),
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_trade_as_position_maps_fields_and_whole_share_count(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
            opened_at="2026-10-01T09:00:00",
        )
        record_execution(self.db_path, trade_id, action="BUY", price=10.0, qty=100)
        trade = get_trade(self.db_path, trade_id)

        position = trade_as_position(trade)
        self.assertEqual(position.ticker, "2330")
        self.assertEqual(position.shares, 100)
        self.assertIsInstance(position.shares, int)
        self.assertEqual(position.entry_price, 10.0)
        self.assertEqual(position.entry_date, "2026-10-01")
        self.assertEqual(position.id, str(trade_id))

    def test_list_positions_filters_by_status(self):
        open_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        cancelled_id = open_trade(self.db_path, symbol="2317", side="LONG", initial_stop_loss=100.0)
        cancel_trade(self.db_path, cancelled_id)

        open_positions = list_open_positions(self.db_path)
        self.assertEqual([p.id for p in open_positions], [str(open_id)])

        cancelled_positions = list_positions(self.db_path, statuses=("CANCELLED",))
        self.assertEqual([p.id for p in cancelled_positions], [str(cancelled_id)])

        all_positions = list_positions(self.db_path, statuses=None)
        self.assertEqual(len(all_positions), 2)

    def test_open_position_seeds_execution_and_validates_ticker(self):
        position = open_position(
            self.db_path, ticker="2330", shares=1000, entry_price=10.0,
            entry_date="2026-10-01", initial_stop_loss=9.0,
        )
        self.assertEqual(position.shares, 1000)
        self.assertEqual(position.entry_price, 10.0)

        with self.assertRaises(ValueError):
            open_position(
                self.db_path, ticker="!!", shares=100, entry_price=10.0,
                entry_date="2026-10-01", initial_stop_loss=9.0,
            )

    def test_update_position_plan_sentinel_semantics(self):
        trade_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)

        update_position_plan(self.db_path, trade_id, initial_take_profit=15.0)
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.initial_take_profit, 15.0)
        self.assertEqual(trade.initial_stop_loss, 9.0)  # untouched field stays as-is

        update_position_plan(self.db_path, trade_id)  # no args: pure no-op
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.initial_take_profit, 15.0)

        with self.assertRaises(ValueError):
            update_position_plan(self.db_path, trade_id, initial_stop_loss=None)

    def test_cancel_empty_position_refuses_once_executed(self):
        empty_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        self.assertTrue(cancel_empty_position(self.db_path, empty_id))
        self.assertEqual(get_trade(self.db_path, empty_id).status, "CANCELLED")

        executed_id = open_trade(self.db_path, symbol="2317", side="LONG", initial_stop_loss=100.0)
        record_execution(self.db_path, executed_id, action="BUY", price=100.0, qty=100)
        self.assertFalse(cancel_empty_position(self.db_path, executed_id))
        self.assertEqual(get_trade(self.db_path, executed_id).status, "OPEN")

    def test_migrate_legacy_position_matches_original_values(self):
        legacy = Position(
            ticker="2301", shares=5000, entry_price=271.0, entry_date="2026-10-17",
            note="", name="光寶科", market="TWSE", id="a872a8c6", updated_at="2026-10-17",
        )
        trade_id = migrate_legacy_position(self.db_path, legacy, initial_stop_loss=250.0)
        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.symbol, "2301")
        self.assertEqual(trade.total_qty, 5000)
        self.assertEqual(trade.avg_entry_price, 271.0)
        self.assertEqual(trade.initial_stop_loss, 250.0)
        self.assertEqual(trade.opened_at, "2026-10-17")
        self.assertEqual(len(trade.executions), 1)


if __name__ == "__main__":
    unittest.main()
