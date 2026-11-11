import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

import tradingnote_esun as esun
import tradingnote_gui as gui
from test_tradingnote_esun import FakeSDK, push
from test_tradingnote_positions_gui import PositionsHarness
from tradingnote_core import PriceInfo
from tradingnote_tradejournal import open_trade, record_execution
from ui import app_paths


class QuotesHarness(PositionsHarness):
    def __init__(self):
        super().__init__()
        self.quotes_tab = QtWidgets.QWidget()
        self._build_quotes_tab()
        self._column_keys  # noqa: B018 (built by _build_positions_tab)


class QuotesGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_redraw_shows_pushed_quote_and_updates_position_price(self):
        window = QuotesHarness()
        session = esun.QuoteSession(FakeSDK(), on_update=lambda s: None)
        window._quotes_session = session
        session.add("2330", "stock")
        push(session, "stock", "2330", 700.0)
        window._redraw_quotes()
        self.assertEqual(window.quotes_table.rowCount(), 1)
        self.assertEqual(window.quotes_table.item(0, 0).text(), "2330")
        self.assertEqual(window.quotes_table.item(0, 3).text(), "700.0")

        window.snapshot = {
            "2330": PriceInfo("2330", "台積電", 1, 1, 1, 600.0, 0, 1, "2026-11-11", "TWSE")
        }
        price = window._position_price("2330")
        self.assertEqual(price.close, 700.0)
        self.assertEqual(price.name, "台積電")
        session.stop()
        window._quotes_session = None
        self.assertEqual(window._position_price("2330").close, 600.0)

    def test_connect_without_sdk_reports_message(self):
        window = QuotesHarness()
        with patch.object(esun, "load_sdk_class", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                esun.connect_session({"esun_api_key": "k"})
        self.assertIn("SDK", str(ctx.exception))
        window._shutdown_quotes()


if __name__ == "__main__":
    unittest.main()
