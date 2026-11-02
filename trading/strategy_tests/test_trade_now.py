"""Trade Now on a pending trade: the nearest entry goes to market, a second entry stays a limit order."""
import json
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from telegram_monitor.models import MANUAL_TELEGRAM_ID
from trading import views
from trading.models import Trade
from trading.services import monitor_service as ms
from trading.strategy_tests.support import Harness, make_signal, make_source, orders, working


TPS = ("2100", "2200", "2300")  # far enough above the market price used in these tests


def trade_now(h, trade):
    return async_to_sync(h.monitor.trade_now)(trade.id)


class TradeNowEngineTests(TestCase):
    def test_a_single_entry_goes_to_market_with_the_margin_recomputed_at_the_market_price(self):
        h, source = Harness(price="2030"), make_source()
        trade = h.open(make_signal(source, entries=("2000",), targets=TPS, margin="30", leverage=10))  # limit buy 2000, market 2030
        self.assertEqual(trade.status, "PENDING_ENTRY")
        planned = orders(trade, "ENTRY")[0].quantity  # 0.15 = 30 * 10 / 2000

        self.assertEqual(trade_now(h, trade), (ms.TRADED, ""))

        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        entry = orders(trade, "ENTRY")
        self.assertEqual([(o.kind, o.status) for o in entry], [("LIMIT", "CANCELED"), ("MARKET", "FILLED")])
        self.assertEqual(entry[1].avg_fill_price, D("2030"))
        self.assertEqual(entry[1].quantity, D("0.147"))  # 30 * 10 / 2030, floored to the step: the margin stays 30
        self.assertLess(entry[1].quantity, planned)
        self.assertEqual(trade.filled_quantity, D("0.147"))
        self.assertEqual(len(working(trade, "SL")), 1)  # the stop-loss and the TPs cover the new size
        self.assertEqual(working(trade, "SL")[0].quantity, D("0.147"))

    def test_two_entries_the_nearest_goes_to_market_and_the_farther_limit_stays(self):
        h, source = Harness(price="2030"), make_source(entry_mode="BOTH")
        trade = h.open(make_signal(source, entries=("2000", "1990"), targets=TPS, margin="30", leverage=10))
        self.assertEqual(len(working(trade, "ENTRY")) + len(working(trade, "ENTRY2")), 2)
        far_before = working(trade, "ENTRY2")[0]

        self.assertEqual(trade_now(h, trade), (ms.TRADED, ""))

        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        market = orders(trade, "ENTRY", status="FILLED")[0]
        self.assertEqual((market.kind, market.quantity), ("MARKET", D("0.073")))  # half the margin (15) at 2030
        far = working(trade, "ENTRY2")
        self.assertEqual([(o.id, o.price, o.kind) for o in far], [(far_before.id, D("1990"), "LIMIT")])  # untouched
        self.assertEqual(trade.filled_quantity, D("0.073"))

        h.run(1995, 1990, 1992, polls=2)  # the second entry fills later and the position grows as usual
        trade.refresh_from_db()
        self.assertGreater(trade.filled_quantity, D("0.073"))
        self.assertEqual(trade.status, "OPEN")

    def test_a_short_with_two_entries_works_the_same_way(self):
        h, source = Harness(price="1990"), make_source(entry_mode="BOTH")
        trade = h.open(make_signal(source, side="SELL", entries=("2000", "2010"), targets=("1980", "1960", "1940"),
                                   stoploss="2030", margin="30", leverage=10))
        self.assertEqual(trade.status, "PENDING_ENTRY")
        self.assertEqual(trade_now(h, trade), (ms.TRADED, ""))
        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(orders(trade, "ENTRY", status="FILLED")[0].kind, "MARKET")
        self.assertEqual([o.price for o in working(trade, "ENTRY2")], [D("2010")])

    def test_price_that_already_reached_tp1_cancels_the_trade_with_the_reason(self):
        h, source = Harness(price="2010"), make_source()
        trade = h.open(make_signal(source, entries=("2000",), targets=TPS))
        h.market.push(2105)  # price jumped past TP1 (2100) without touching the entry
        self.assertEqual(trade_now(h, trade), (ms.CANCELLED, "PRICE_HIT_TP1"))
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CANCELLED", "PRICE_HIT_TP1"))
        self.assertFalse(orders(trade, "ENTRY", status="FILLED"))
        self.assertEqual(trade_now(h, trade), (ms.ALREADY_OVER, ""))

    def test_price_that_already_reached_the_stop_loss_cancels_the_trade_with_the_reason(self):
        h, source = Harness(price="2010"), make_source()
        trade = h.open(make_signal(source, entries=("2000",), targets=TPS))
        h.market.push(1975)
        self.assertEqual(trade_now(h, trade), (ms.CANCELLED, "PRICE_HIT_SL"))
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CANCELLED", "PRICE_HIT_SL"))

    def test_an_open_trade_is_not_pending(self):
        h, source = Harness(), make_source()
        trade = h.open(make_signal(source))
        h.run(2005, 2000, 1995)
        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade_now(h, trade), (ms.NOT_PENDING, ""))
        self.assertEqual(async_to_sync(h.monitor.trade_now)(999999), (ms.ALREADY_OVER, ""))

    def test_a_partly_filled_entry_keeps_its_fill_and_only_the_rest_is_bought(self):
        h, source = Harness(price="2030"), make_source()
        trade = h.open(make_signal(source, entries=("2000",), targets=TPS))
        entry = orders(trade, "ENTRY")[0]
        # the limit order has filled 0.05 of 0.15 (scripted by hand: the paper gateway fills whole orders)
        sim = h.paper._orders[entry.exchange_id]
        sim.status, sim.executed_qty, sim.avg_price = "PARTIALLY_FILLED", D("0.05"), D("2000")
        h.paper._positions[trade.id] = D("0.05")
        Trade.objects.filter(pk=trade.pk).update()

        self.assertEqual(trade_now(h, trade), (ms.TRADED, ""))

        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.filled_quantity, D("0.147"))  # 0.05 kept + 0.097 bought = the margin share at 2030
        rows = [(o.kind, o.status, o.executed_quantity) for o in orders(trade, "ENTRY")]
        self.assertEqual(rows, [("LIMIT", "CANCELED", D("0.05")), ("MARKET", "FILLED", D("0.097"))])

    def test_a_size_below_the_exchange_minimum_changes_nothing(self):
        h, source = Harness(price="2030"), make_source()
        trade = h.open(make_signal(source, entries=("2000",), targets=TPS, margin="30", leverage=10))
        h.market._filters = h.market._filters.__class__(D("0.01"), D("0.001"), D("0.5"), D("1000"), D("5"))  # min qty 0.5
        self.assertEqual(trade_now(h, trade), (ms.TOO_SMALL, ""))
        trade.refresh_from_db()
        self.assertEqual(trade.status, "PENDING_ENTRY")
        self.assertEqual(len(working(trade, "ENTRY")), 1)  # the limit entry was not touched

    def test_when_the_market_order_fails_the_limit_entry_is_put_back(self):
        h, source = Harness(price="2030"), make_source()
        trade = h.open(make_signal(source, entries=("2000",), targets=TPS))
        real_place = h.paper.place_order

        async def failing(symbol, spec, trade_ref):
            if spec.kind.value == "MARKET":
                raise RuntimeError("exchange down")
            return await real_place(symbol, spec, trade_ref)

        with mock.patch.object(h.paper, "place_order", failing):
            self.assertEqual(trade_now(h, trade), (ms.FAILED, ""))
        trade.refresh_from_db()
        self.assertEqual(trade.status, "PENDING_ENTRY")
        self.assertEqual([o.price for o in working(trade, "ENTRY")], [D("2000")])


class TradeNowViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))
        self.h = Harness()
        self.trade = self.h.open(make_signal(make_source()))  # pending

    def post(self, name, *args):
        return self.client.post(reverse(name, args=args), json.dumps({}), content_type="application/json")

    def engine(self, result=(ms.TRADED, "")):
        return mock.patch.object(views, "engine", SimpleNamespace(trade_now=mock.AsyncMock(return_value=result)))

    def test_forward_and_real_trade_now_need_no_confirmation(self):
        with self.engine() as stub:
            self.assertEqual(self.post("forward-api-trade-now", self.trade.id).status_code, 200)
            Trade.objects.filter(pk=self.trade.pk).update(mode="LIVE")
            self.assertEqual(self.post("real-api-trade-now", self.trade.id).status_code, 200)  # no "confirmed" sent
        self.assertEqual(stub.trade_now.await_count, 2)

    def test_a_button_can_only_act_on_a_trade_of_its_own_page(self):
        with self.engine() as stub:
            self.assertEqual(self.post("real-api-trade-now", self.trade.id).status_code, 404)  # a Forward trade
            self.assertEqual(self.post("manual-api-trade-now", self.trade.id).status_code, 404)
            self.assertEqual(self.post("forward-api-trade-now", 999999).status_code, 404)
            source = self.trade.signal.source
            source.telegram_id = MANUAL_TELEGRAM_ID
            source.save()
            self.assertEqual(self.post("forward-api-trade-now", self.trade.id).status_code, 404)
            stub.trade_now.assert_not_called()
            self.assertEqual(self.post("manual-api-trade-now", self.trade.id).status_code, 200)

    def test_a_price_cancel_names_the_reason(self):
        with self.engine((ms.CANCELLED, "PRICE_HIT_TP1")):
            data = self.post("forward-api-trade-now", self.trade.id).json()
        self.assertFalse(data["ok"])
        self.assertIn("already reached TP1", data["message"])
        self.assertIn("cancelled", data["message"])

    def test_each_outcome_has_a_message_and_a_status(self):
        cases = ((ms.NOT_PENDING, 409), (ms.ALREADY_OVER, 409), (ms.PLACING, 409), (ms.NO_GATEWAY, 503), (ms.TOO_SMALL, 409), (ms.FAILED, 502))
        for outcome, status in cases:
            with self.engine((outcome, "")):
                response = self.post("forward-api-trade-now", self.trade.id)
            self.assertEqual(response.status_code, status, outcome)
            self.assertTrue(response.json()["message"])

    def test_login_and_post_are_required(self):
        url = reverse("forward-api-trade-now", args=[self.trade.id])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        with self.engine() as stub:
            self.assertEqual(self.post("forward-api-trade-now", self.trade.id).status_code, 302)
            stub.trade_now.assert_not_called()

    def test_the_pending_pages_carry_the_trade_now_url_and_other_tabs_do_not(self):
        for name in ("forward-trades-pending", "real-trades-pending", "manual-trades-pending"):
            self.assertIn("data-trade-now-url", self.client.get(reverse(name)).content.decode(), name)
        for name in ("forward-trades-positions", "forward-trades-cancelled"):
            self.assertNotIn("data-trade-now-url", self.client.get(reverse(name)).content.decode(), name)
