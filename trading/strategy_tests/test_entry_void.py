"""A signal whose TP1 / SL was already reached before any entry is not placed: the trade is
recorded as Cancelled (the Cancelled tab shows it, with its levels), not hidden as Rejected."""
from decimal import Decimal as D

from django.test import TestCase

from trading.services import report_service
from trading.services import trade_repository as repo
from trading.strategy_tests.support import Harness, make_signal, make_source


class EntryVoidTests(TestCase):
    def test_tp1_reached_before_the_entry_places_nothing_and_is_cancelled(self):
        trade = Harness("2025").open(make_signal(make_source()))
        self.assertEqual((trade.status, trade.reason_code), ("CANCELLED", "PRICE_HIT_TP1"))
        self.assertEqual(trade.orders.count(), 0)
        self.assertIsNotNone(trade.closed_at)
        self.assertEqual(
            (trade.entry_price, trade.stoploss, trade.leverage, trade.quantity), (D("2000"), D("1980"), 10, D("0.150"))
        )

    def test_sl_reached_before_the_entry_places_nothing_and_is_cancelled(self):
        trade = Harness("1975").open(make_signal(make_source()))
        self.assertEqual((trade.status, trade.reason_code), ("CANCELLED", "PRICE_HIT_SL"))
        self.assertEqual(trade.orders.count(), 0)

    def test_it_shows_in_the_cancelled_tab_with_its_levels_and_reason(self):
        trade = Harness("2025").open(make_signal(make_source()))
        row = next(r for r in repo.trade_rows("PAPER", repo.ST_CANCELLED) if r["id"] == trade.id)
        rendered = report_service.cancelled_rows([row])[0]
        self.assertEqual(rendered["result"], "PRICE_HIT_TP1")
        self.assertEqual(rendered["entry_price"], 2000)
        self.assertEqual(rendered["stoploss"], 1980)
        self.assertEqual(rendered["leverage"], 10)
        self.assertIsNotNone(rendered["closed_at"])

    def test_a_signal_whose_levels_were_not_reached_still_places_its_orders(self):
        trade = Harness("2010").open(make_signal(make_source()))
        self.assertEqual(trade.status, "PENDING_ENTRY")
        self.assertGreater(trade.orders.count(), 0)
