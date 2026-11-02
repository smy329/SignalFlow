"""The Close button: close an open trade at market now; it then shows in History as "Closed manually"."""
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
from trading.services import monitor_service as ms
from trading.services import position_service
from trading.services import trade_repository as repo
from trading.models import Trade
from trading.strategy_tests.support import Harness, make_signal, make_source, orders, working


def open_long(h, source, *prices):
    """A long that filled at 2000 and then drifted (default 1995)."""
    trade = h.open(make_signal(source))
    h.run(*(prices or (2005, 2000, 1995)))
    trade.refresh_from_db()
    assert trade.status == "OPEN", trade.status
    return trade


def close(h, trade):
    return async_to_sync(h.monitor.close_now)(trade.id)


class CloseNowTests(TestCase):
    def test_an_open_trade_is_closed_at_market_and_goes_to_history(self):
        h, source = Harness(), make_source()
        trade = open_long(h, source)

        self.assertEqual(close(h, trade), ms.CLOSED)

        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "CLOSED_MANUALLY"))
        self.assertIsNotNone(trade.closed_at)
        self.assertLess(trade.realized_pnl, 0)  # long from 2000 closed near 1995: the loss is recorded
        for role in ("SL", "TP1", "TP2", "TP3"):
            self.assertEqual(working(trade, role), [], role)
        self.assertEqual(async_to_sync(h.paper.position_amount)("ETHUSDT", trade.id), D("0"))

        history = repo.trade_rows(repo.MODE_PAPER, repo.ST_CLOSED)
        self.assertEqual([(r["id"], r["reason"]) for r in history], [(trade.id, "CLOSED_MANUALLY")])
        self.assertEqual(repo.trade_rows(repo.MODE_PAPER, repo.ST_OPEN), [])

    def test_only_what_is_left_after_a_filled_take_profit_is_closed(self):
        h, source = Harness(), make_source()
        trade = open_long(h, source)
        h.run(2025, 2022)  # TP1 (2020) fills
        self.assertTrue(orders(trade, "TP1", status="FILLED"))
        left = async_to_sync(h.paper.position_amount)("ETHUSDT", trade.id)
        self.assertGreater(left, 0)

        self.assertEqual(close(h, trade), ms.CLOSED)

        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "CLOSED_MANUALLY"))
        self.assertGreater(trade.realized_pnl, 0)  # TP1 gain on part, small move on the rest
        self.assertEqual(async_to_sync(h.paper.position_amount)("ETHUSDT", trade.id), D("0"))

    def test_the_monitor_never_reopens_or_overwrites_a_closed_trade(self):
        h, source = Harness(), make_source()
        trade = open_long(h, source)
        close(h, trade)
        h.run(2100, 2150, 1900, polls=3)
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "CLOSED_MANUALLY"))

    def test_a_pending_trade_has_its_entry_orders_cancelled_and_goes_to_cancelled(self):
        h, source = Harness(), make_source()
        trade = h.open(make_signal(source))  # limit buy at 2000, never reached
        self.assertEqual(trade.status, "PENDING_ENTRY")

        self.assertEqual(close(h, trade), ms.CANCELLED)

        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CANCELLED", "CLOSED_MANUALLY"))
        self.assertFalse(working(trade, "ENTRY"))
        h.run(2100, 1900, polls=3)  # nothing is left to fill or to re-place
        trade.refresh_from_db()
        self.assertEqual(trade.status, "CANCELLED")
        self.assertEqual(close(h, trade), ms.ALREADY_OVER)  # a double click closes once

    def test_a_trade_that_is_already_closed_is_reported(self):
        h, source = Harness(), make_source()
        trade = open_long(h, source)
        close(h, trade)
        self.assertEqual(close(h, trade), ms.ALREADY_OVER)  # a double click closes once
        self.assertEqual(async_to_sync(h.monitor.close_now)(999999), ms.ALREADY_OVER)

    def test_when_the_exchange_refuses_the_trade_stays_open_and_the_stop_loss_comes_back(self):
        h, source = Harness(), make_source()
        trade = open_long(h, source)

        with mock.patch.object(h.paper, "market_close", side_effect=RuntimeError("exchange down")):
            self.assertEqual(close(h, trade), ms.FAILED)

        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("OPEN", ""))
        self.assertGreater(async_to_sync(h.paper.position_amount)("ETHUSDT", trade.id), 0)
        h.run(1996, polls=2)  # the normal monitor takes over again and re-places the cancelled stop-loss
        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(len(working(trade, "SL")), 1)
        self.assertEqual(close(h, trade), ms.CLOSED)  # and a retry works


class CloseViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))
        self.h = Harness()
        self.trade = open_long(self.h, make_source())

    def post(self, name, *args, confirmed=None, **kw):
        body = {} if confirmed is None else {"confirmed": confirmed}
        return self.client.post(reverse(name, args=args), json.dumps(body), content_type="application/json", **kw)

    def engine(self, outcome=ms.CLOSED):
        return mock.patch.object(views, "engine", SimpleNamespace(close_trade=mock.AsyncMock(return_value=outcome)))

    def make_live(self):
        Trade.objects.filter(pk=self.trade.pk).update(mode="LIVE")

    def make_manual(self):
        source = self.trade.signal.source
        source.telegram_id = MANUAL_TELEGRAM_ID
        source.save()

    def test_forward_closes_with_one_click(self):
        with self.engine() as stub:
            response = self.post("forward-api-close", self.trade.id)
        self.assertEqual((response.status_code, response.json()["ok"]), (200, True))
        stub.close_trade.assert_called_once_with(self.trade.id)

    def test_real_needs_the_confirmation_on_the_server_too(self):
        self.make_live()
        with self.engine() as stub:
            for confirmed in (None, False, "yes"):
                self.assertEqual(self.post("real-api-close", self.trade.id, confirmed=confirmed).status_code, 400)
            stub.close_trade.assert_not_called()
            self.assertEqual(self.post("real-api-close", self.trade.id, confirmed=True).status_code, 200)

    def test_a_button_can_only_close_a_trade_of_its_own_page(self):
        with self.engine() as stub:
            self.assertEqual(self.post("real-api-close", self.trade.id, confirmed=True).status_code, 404)  # a Forward trade
            self.assertEqual(self.post("manual-api-close", self.trade.id).status_code, 404)  # not a manual trade
            self.assertEqual(self.post("forward-api-close", 999999).status_code, 404)
            self.make_manual()
            self.assertEqual(self.post("forward-api-close", self.trade.id).status_code, 404)  # a manual trade
            stub.close_trade.assert_not_called()
            self.assertEqual(self.post("manual-api-close", self.trade.id).status_code, 200)

    def test_manual_real_trade_also_needs_the_confirmation(self):
        self.make_manual()
        self.make_live()
        with self.engine() as stub:
            self.assertEqual(self.post("manual-api-close", self.trade.id).status_code, 400)
            stub.close_trade.assert_not_called()
            self.assertEqual(self.post("manual-api-close", self.trade.id, confirmed=True).status_code, 200)

    def test_cancelling_a_pending_trade_answers_with_its_own_message_and_real_still_asks_first(self):
        with self.engine(ms.CANCELLED):
            ok = self.post("forward-api-close", self.trade.id).json()
        self.assertEqual(ok["ok"], True)
        self.assertIn("pending entry cancelled", ok["message"])
        self.make_live()
        with self.engine(ms.CANCELLED) as stub:
            self.assertEqual(self.post("real-api-close", self.trade.id).status_code, 400)
            stub.close_trade.assert_not_called()

    def test_each_outcome_has_a_message_and_a_status(self):
        for outcome, status in ((ms.ALREADY_OVER, 409), (ms.PLACING, 409), (ms.NO_GATEWAY, 503), (ms.FAILED, 502)):
            with self.engine(outcome):
                response = self.post("forward-api-close", self.trade.id)
            self.assertEqual(response.status_code, status, outcome)
            self.assertFalse(response.json()["ok"])
            self.assertTrue(response.json()["message"])

    def test_engine_problems_are_reported_not_raised(self):
        for error, status in ((views.EngineNotReady("x"), 503), (TimeoutError(), 504), (RuntimeError("boom"), 502)):
            stub = SimpleNamespace(close_trade=mock.AsyncMock(side_effect=error))
            with mock.patch.object(views, "engine", stub):
                response = self.post("forward-api-close", self.trade.id)
            self.assertEqual(response.status_code, status)
            self.assertFalse(response.json()["ok"])

    def test_login_and_post_are_required(self):
        url = reverse("forward-api-close", args=[self.trade.id])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.logout()
        with self.engine() as stub:
            self.assertEqual(self.post("forward-api-close", self.trade.id).status_code, 302)
            stub.close_trade.assert_not_called()

    def test_a_closed_trade_is_in_the_history_api(self):
        close(self.h, self.trade)
        rows = self.client.get(reverse("forward-api-history")).json()["rows"]
        self.assertEqual([(r["id"], r["result"]) for r in rows], [(self.trade.id, "CLOSED_MANUALLY")])
        self.assertEqual(self.client.get(reverse("forward-api-positions")).json()["rows"], [])

    def test_position_rows_carry_the_trade_id_and_the_pages_have_the_button_wiring(self):
        with mock.patch.object(views, "engine", SimpleNamespace(is_running=True, gateways={"PAPER": object()}, settings=None, market_gateway=None)):
            rows = self.client.get(reverse("forward-api-positions")).json()["rows"]
        self.assertEqual([r["trade_id"] for r in rows], [self.trade.id])

        for name in ("forward-trades-positions", "real-trades-positions", "manual-trades-positions"):
            page = self.client.get(reverse(name)).content.decode()
            self.assertIn("data-close-url", page, name)
            self.assertIn("csrfmiddlewaretoken", page, name)
        for name in ("forward-trades-pending", "real-trades-pending", "manual-trades-pending"):  # the same button on Pending
            page = self.client.get(reverse(name)).content.decode()
            self.assertIn("data-close-url", page, name)
            self.assertIn("csrfmiddlewaretoken", page, name)
        self.assertNotIn("data-close-url", self.client.get(reverse("forward-trades-cancelled")).content.decode())


class LivePositionRowTests(TestCase):
    def test_only_positions_with_a_trade_record_can_be_closed(self):
        h = Harness()
        trade = open_long(h, make_source())
        Trade.objects.filter(pk=trade.pk).update(mode="LIVE")

        def snap(symbol):
            return {
                "symbol": symbol, "amount": D("1"), "mark_price": D("2000"), "notional": D("2000"), "unrealized_pnl": D("0"),
                "leverage": 10, "initial_margin": D("200"), "maint_margin": D("10"), "entry_price": D("2000"), "liq_price": D("1800"),
            }

        gateway = SimpleNamespace(position_snapshots=mock.AsyncMock(return_value=[snap("ETHUSDT"), snap("BTCUSDT")]))
        with mock.patch.object(position_service.runtime, "places", return_value=4):  # no DB read inside the event loop
            rows = {r["symbol"]: r for r in async_to_sync(position_service.build_positions)(gateway)}
        self.assertEqual(rows["ETHUSDT"]["trade_id"], trade.id)
        self.assertIsNone(rows["BTCUSDT"]["trade_id"])  # opened by hand on Binance
