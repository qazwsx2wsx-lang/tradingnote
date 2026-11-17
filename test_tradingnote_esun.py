import json
import os
import unittest
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tradingnote_esun as esun


class FakeClient:
    def __init__(self):
        self.handlers, self.subscribed, self.connected = {}, [], False

    def on(self, event, handler):
        self.handlers[event] = handler

    def connect(self):
        self.connected = True

    def subscribe(self, payload):
        self.subscribed.append(payload["symbol"])

    def unsubscribe(self, payload):
        self.subscribed.remove(payload["symbol"])

    def disconnect(self):
        self.connected = False


class FakeSDK:
    def __init__(self, config=None):
        self.config = config
        self.logged_in = False
        self.futopt_symbols = {"TXFK6"}
        self.stock_symbols = {"2330"}
        self.ws = type("WS", (), {"futopt": FakeClient(), "stock": FakeClient()})()
        outer = self

        class Intraday:
            def quote(self, symbol, session=None):
                return {"symbol": symbol, "lastPrice": 1.0} if symbol in outer.futopt_symbols else {"statusCode": 404}

            def tickers(self, type):
                return {"data": [
                    {"symbol": "QFFL6", "name": "小型台積電期貨 12月", "endDate": "2026-12-16"},
                    {"symbol": "QFFK6", "name": "小型台積電期貨 11月", "endDate": "2026-11-18"},
                ]}

        class StockIntraday:
            def quote(self, symbol):
                return {"symbol": symbol} if symbol in outer.stock_symbols else {"statusCode": 404}

        self.rest_client = type("R", (), {
            "futopt": type("F", (), {"intraday": Intraday()})(),
            "stock": type("S", (), {"intraday": StockIntraday()})(),
        })()
        self.websocket_client = self.ws

    def login(self):
        self.logged_in = True


def push(session, kind, symbol, price):
    client = session.sdk.websocket_client.futopt if kind == "futopt" else session.sdk.websocket_client.stock
    client.handlers["message"](json.dumps(
        {"event": "aggregates", "data": {"symbol": symbol, "lastPrice": price, "change": 1, "changePercent": 0.5}}
    ))


class EsunPureTests(unittest.TestCase):
    def test_configured_requires_key_secret_cert(self):
        s = {"esun_api_key": "k", "esun_api_secret": "s", "esun_cert_path": "/c.p12"}
        self.assertTrue(esun.esun_configured(s))
        self.assertFalse(esun.esun_configured({**s, "esun_api_secret": " "}))

    def test_build_config_sections(self):
        config = esun.build_config({"esun_api_key": "k", "esun_api_secret": "s", "esun_cert_path": "/c.p12"})
        self.assertEqual(config["Api"]["Key"], "k")
        self.assertEqual(config["Core"]["Environment"], "SIMULATION")
        self.assertEqual(config["Core"]["Entry"], esun.DEFAULT_ENTRY)

    def test_session_windows(self):
        self.assertEqual(esun.day_session_status(datetime(2026, 11, 11, 10, 0)), "交易中")
        self.assertEqual(esun.day_session_status(datetime(2026, 11, 11, 14, 0)), "已收盤")
        self.assertFalse(esun.should_show_night_session(datetime(2026, 11, 11, 10, 0)))
        self.assertTrue(esun.should_show_night_session(datetime(2026, 11, 11, 16, 0)))
        self.assertTrue(esun.should_show_night_session(datetime(2026, 11, 11, 8, 30)))

    def test_quote_fields(self):
        self.assertFalse(esun.quote_fields(None)["ok"])
        self.assertIn("失敗", esun.quote_fields({"statusCode": 500, "message": "x"})["message"])
        fields = esun.quote_fields({"symbol": "A", "lastPrice": 5, "total": {"tradeVolume": 9}})
        self.assertEqual((fields["last"], fields["volume"], fields["name"]), (5, 9, "A"))

    def test_connect_requires_sdk_and_config(self):
        with self.assertRaises(RuntimeError):
            esun.connect_session({}, sdk_class=FakeSDK)
        s = {"esun_api_key": "k", "esun_api_secret": "s", "esun_cert_path": "/c.p12"}
        session = esun.connect_session(s, sdk_class=FakeSDK)
        self.assertTrue(session.sdk.logged_in)
        session.stop()


class QuoteSessionTests(unittest.TestCase):
    def setUp(self):
        self.updates = []
        self.session = esun.QuoteSession(FakeSDK(), on_update=self.updates.append)

    def tearDown(self):
        self.session.stop()

    def test_classify_and_add(self):
        self.assertEqual(self.session.add("TXFK6"), ("futopt", None))
        self.assertEqual(self.session.add("2330"), ("stock", None))
        kind, error = self.session.add("9999")
        self.assertIsNone(kind)
        self.assertIn("9999", error)
        self.assertEqual(set(self.session.snapshot()), {"TXFK6", "2330"})

    def test_push_updates_last_price(self):
        self.session.add("2330", "stock")
        self.assertIsNone(self.session.last_price("2330"))
        push(self.session, "stock", "2330", 612.5)
        self.assertEqual(self.session.last_price("2330"), 612.5)
        self.assertIn("2330", self.updates)

    def test_message_for_unwatched_symbol_ignored(self):
        self.session.add("2330", "stock")
        push(self.session, "stock", "2317", 100)
        self.assertIsNone(self.session.last_price("2317"))

    def test_bad_message_ignored(self):
        self.session.add("2330", "stock")
        self.session._on_message("not json")
        self.session._on_message(json.dumps({"event": "heartbeat"}))

    def test_remove_unsubscribes(self):
        self.session.add("2330", "stock")
        self.assertTrue(self.session.remove("2330"))
        self.assertFalse(self.session.remove("2330"))
        self.assertEqual(self.session.sdk.websocket_client.stock.subscribed, [])

    def test_search_sorted_by_end_date(self):
        results = self.session.search_keyword("台積電")
        # fake 對 FUTURE 與 OPTION 回傳相同清單，所以每筆出現兩次；重點是依到期日排序
        self.assertEqual([r[0] for r in results], ["QFFK6", "QFFK6", "QFFL6", "QFFL6"])


if __name__ == "__main__":
    unittest.main()
