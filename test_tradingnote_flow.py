import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

import tradingnote_history as history
from tradingnote_core import PriceInfo
from tradingnote_flow import FlowAnalysisService, FlowPeriod
from tradingnote_institutional import aggregate_institutional_by_group, InstitutionalTickerFlow


class FlowConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'history.db'
        self.snapshot = {'1111': PriceInfo('1111', '測試', None, None, None, 121, 11, 300,
                                          '2026-09-15', 'TWSE', 10000)}
        history.upsert_daily_prices(self.db, [
            ('2026-09-10', '1111', 'TWSE', '測試', 90, None, 100, 10000),
            ('2026-09-11', '1111', 'TWSE', '測試', 100, None, 100, 10000),
            ('2026-09-14', '1111', 'TWSE', '測試', 110, None, 200, 10000),
        ])
        for module in ('tradingnote_history', 'tradingnote_flow'):
            mock = patch(module + '.get_industry_map', return_value={'1111': '測試族群'})
            mock.start()
            self.addCleanup(mock.stop)
        self.service = FlowAnalysisService(self.db)

    def period(self, n):
        return FlowPeriod('2026-09-10', n)

    def test_two_three_and_one_day_match(self):
        for n, expected in ((1, 10), (2, 10), (3, 21)):
            with self.subTest(days=n):
                dashboard = self.service.analyze(self.snapshot, self.period(n))
                detail = self.service.get_group_top_stocks(self.snapshot, self.period(n), '測試族群')[0]
                self.assertAlmostEqual(dashboard.industry_flow[0].avg_change_pct, expected)
                self.assertAlmostEqual(dashboard.stock_capital_flow[0].change_pct, expected)
                self.assertAlmostEqual(detail['change_pct'], expected)
                if n == 2:
                    self.assertAlmostEqual(dashboard.industry_flow[0].volume_ratio, 2)
                    self.assertAlmostEqual(detail['avg_volume'], 150)

    def test_missing_data_is_not_flat(self):
        period = self.period(5)
        dashboard = self.service.analyze(self.snapshot, period)
        detail = self.service.get_group_top_stocks(self.snapshot, period, '測試族群')[0]
        self.assertIsNone(dashboard.industry_flow[0].avg_change_pct)
        self.assertIsNone(detail['change_pct'])
        self.assertEqual((detail['actual_days'], detail['required_days']), (4, 5))

    def test_holiday_and_mixed_market_dates(self):
        period = FlowPeriod('2026-09-10', 3, end_date='2026-09-13').resolve(self.db, {})
        self.assertEqual(period.end_date, '2026-09-11')
        self.assertEqual(period.actual_dates, ('2026-09-10', '2026-09-11'))
        snapshot = dict(self.snapshot)
        snapshot['2222'] = replace(snapshot['1111'], ticker='2222', date='2026-09-11', market='TPEX')
        detail = history.get_group_top_stocks_range(self.db, snapshot, ['2222'], 2, period=self.period(2))[0]
        self.assertIsNone(detail['change_pct'])
        self.assertEqual(detail['data_date'], '2026-09-11')
        self.assertEqual(detail['end_date'], '2026-09-15')

    def test_history_revision_and_snapshot_change_invalidate(self):
        period = self.period(3)
        old = self.service.analyze(self.snapshot, period)
        self.assertAlmostEqual(old.industry_flow[0].avg_change_pct, 21)
        conn = history._connect(self.db)
        conn.execute("UPDATE daily_prices SET close=50 WHERE date='2026-09-11'")
        conn.commit()
        conn.close()
        new = self.service.analyze(self.snapshot, period)
        self.assertAlmostEqual(new.industry_flow[0].avg_change_pct, 142)
        self.assertEqual(new, FlowAnalysisService(self.db).analyze(self.snapshot, period))
        changed = {'1111': replace(self.snapshot['1111'], change=21)}
        self.assertNotEqual(self.service.analyze(changed, self.period(1)).industry_flow[0].avg_change_pct,
                            self.service.analyze(self.snapshot, self.period(1)).industry_flow[0].avg_change_pct)

    def test_one_day_missing_change(self):
        snap = {'1111': replace(self.snapshot['1111'], change=None)}
        self.assertIsNone(self.service.analyze(snap, self.period(1)).industry_flow[0].avg_change_pct)

    def test_historical_cutoff_does_not_use_future_snapshot(self):
        period = FlowPeriod('2026-09-10', 2, '2026-09-14')
        dashboard = self.service.analyze(self.snapshot, period)
        detail = self.service.get_group_top_stocks(self.snapshot, period, '測試族群')[0]
        self.assertAlmostEqual(dashboard.industry_flow[0].avg_change_pct, 10)
        self.assertAlmostEqual(detail['change_pct'], 10)
        self.assertEqual(detail['close'], 110)
        self.assertEqual(detail['data_date'], '2026-09-14')

    def test_missing_volume_and_calendar_gap(self):
        conn = history._connect(self.db)
        conn.execute("UPDATE daily_prices SET volume=NULL WHERE date='2026-09-14'")
        conn.commit()
        conn.close()
        dashboard = self.service.analyze(self.snapshot, self.period(2))
        self.assertAlmostEqual(dashboard.industry_flow[0].avg_change_pct, 10)
        self.assertIsNone(dashboard.industry_flow[0].volume_ratio)
        self.assertEqual(dashboard.volume_outliers, ())
        history.upsert_daily_prices(self.db, [('2026-09-14','2222','TPEX','other',1,None,1,1)])
        conn = history._connect(self.db)
        conn.execute("DELETE FROM daily_prices WHERE ticker='1111' AND date='2026-09-14'")
        conn.commit()
        conn.close()
        self.assertIsNone(self.service.analyze(self.snapshot, self.period(2)).industry_flow[0].avg_change_pct)

    def test_valuation_observations_preserve_past_versions(self):
        conn = history._connect(self.db)
        conn.executemany("INSERT INTO valuation_observations VALUES (?, ?, ?, ?, ?, ?, ?)", [
            ('2026-09-11', '1111', 'TWSE', 10, 1, 1, '2026-09-11T12:00:00'),
            ('2026-09-11', '1111', 'TWSE', 20, 1, 1, '2026-09-15T12:00:00'),
        ])
        conn.commit()
        conn.close()
        period = FlowPeriod('2026-09-10', 2, '2026-09-14')
        detail = self.service.get_group_top_stocks(self.snapshot, period, '測試族群')[0]
        self.assertEqual(detail['per'], 10)
        self.service.analyze(self.snapshot, period, bubble_mode='valuation')

    def test_institutional_only_same_date(self):
        row = InstitutionalTickerFlow('1111', '測試', '2026-09-11', 'TWSE', 100, 20, -10)
        self.assertEqual(aggregate_institutional_by_group([row], {'測試':['1111']}, self.snapshot), [])
        row = replace(row, date='2026-09-15')
        result = aggregate_institutional_by_group([row], {'測試':['1111']}, self.snapshot)
        self.assertEqual(result[0].foreign_value, 12100)

    def test_valuation_source_date_and_retrieval(self):
        history.record_valuation_snapshot(self.db, {'1111': {'date':'2026-09-11', 'per':12}}, 'TWSE', '2026-09-15')
        conn = history._connect(self.db)
        row = conn.execute('SELECT date, retrieved_at FROM valuation_history').fetchone()
        self.assertEqual(row[0], '2026-09-11')
        self.assertTrue(row[1])
        conn.execute("UPDATE valuation_history SET retrieved_at='2026-09-16T12:00:00'")
        conn.commit()
        conn.close()
        detail = history.get_group_top_stocks_range(self.db, self.snapshot, ['1111'], 2, period=FlowPeriod('2026-09-10', 2, '2026-09-14'))[0]
        self.assertIsNone(detail['per'])


if __name__ == '__main__':
    unittest.main()
