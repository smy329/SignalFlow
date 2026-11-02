"""A closed live trade is priced from Binance's own fills, so History matches Binance's Position History."""
import dataclasses
from decimal import Decimal as D
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase

from trading.domain.exchange_result import exchange_result
from trading.models import Trade
from trading.services.execution_service import ExecutionService
from trading.services.monitor_service import MonitorService
from trading.strategy_tests.support import Harness, make_signal, make_source, settings_for_tests

# POLUSDT short from the Binance screenshot: 325 sold @0.09222 (maker), bought back @0.09073 (taker)
POL = [
    dict(order_id=1, side="SELL", price=D("0.09222"), qty=D("325"), realized_pnl=D("0"), commission=D("0.005994"), commission_asset="USDT"),
    dict(order_id=7, side="BUY", price=D("0.09073"), qty=D("325"), realized_pnl=D("0.48425"), commission=D("0.014743"), commission_asset="USDT"),
]


class ExchangeResultTests(TestCase):
    def test_the_net_result_is_realized_pnl_minus_commission(self):
        r = exchange_result("SELL", POL, D("0.0005"))
        self.assertEqual((r.gross, r.fees, r.exit_price), (D("0.48425"), D("0.020737"), D("0.09073")))
        self.assertEqual(round(r.net, 4), D("0.4635"))  # Binance shows 0.46
        self.assertTrue(r.fully_closed)
        self.assertEqual(r.closing_order_ids, {7})

    def test_a_losing_long_and_partial_closes_are_averaged(self):
        fills = [
            dict(order_id=1, side="BUY", price=D("100"), qty=D("2"), realized_pnl=D("0"), commission=D("0.1"), commission_asset="USDT"),
            dict(order_id=2, side="SELL", price=D("102"), qty=D("1"), realized_pnl=D("2"), commission=D("0.05"), commission_asset="USDT"),
            dict(order_id=3, side="SELL", price=D("98"), qty=D("1"), realized_pnl=D("-2"), commission=D("0.05"), commission_asset="USDT"),
        ]
        r = exchange_result("BUY", fills, D("0.0005"))
        self.assertEqual((r.exit_price, r.net, r.closing_order_ids), (D("100"), D("-0.2"), {2, 3}))

    def test_a_commission_paid_in_bnb_is_estimated_from_the_notional(self):
        fills = [{**POL[0], "commission_asset": "BNB", "commission": D("0.00001")}, POL[1]]
        r = exchange_result("SELL", fills, D("0.0005"))
        self.assertEqual(r.fees, D("0.09222") * D("325") * D("0.0005") + D("0.014743"))

    def test_without_an_entry_and_a_closing_fill_there_is_no_result(self):
        self.assertIsNone(exchange_result("SELL", POL[:1], D("0.0005")))
        self.assertIsNone(exchange_result("SELL", POL[1:], D("0.0005")))
        self.assertFalse(exchange_result("SELL", [POL[0], {**POL[1], "qty": D("100")}], D("0.0005")).fully_closed)


class LiveHarness(Harness):
    """The paper gateway stands in for Binance, registered as the LIVE gateway."""

    def __init__(self):
        super().__init__()
        self.cfg = dataclasses.replace(settings_for_tests(), trading_enabled=True)  # live trading on, as in Settings
        self.executor = ExecutionService({"LIVE": self.paper}, self.cfg)
        self.monitor = MonitorService({"LIVE": self.paper}, self.cfg)
        self.executor.attach_monitor(self.monitor)

    def open(self, signal):
        async_to_sync(self.executor.handle_signal)(signal.id)
        return Trade.objects.get(signal=signal, mode="LIVE")

    def run(self, *prices, polls=1):
        self.market.push(*prices)
        for _ in range(polls):
            async_to_sync(self.paper.sync_market)(["ETHUSDT"])  # the monitor only advances the simulation of PAPER trades
            async_to_sync(self.monitor.poll_once)()


class LiveCloseTests(TestCase):
    def setUp(self):
        self.h = LiveHarness()
        self.sleep = mock.patch("trading.services.monitor_service.asyncio.sleep", new=mock.AsyncMock())
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def open_trade(self):
        from telegram_monitor.models import TelegramSource

        trade = self.h.open(make_signal(make_source(mode=TelegramSource.Mode.REAL)))
        self.h.run(2005, 2000, 1995)
        trade.refresh_from_db()
        self.assertEqual((trade.mode, trade.status), ("LIVE", "OPEN"))
        return trade

    def fills(self, trade, order_id, side="SELL", price="1995", pnl="-5", entry_qty=None):
        qty = entry_qty or trade.filled_quantity
        return [
            dict(order_id=1, side="BUY", price=D("2000"), qty=qty, realized_pnl=D("0"), commission=D("0.4"), commission_asset="USDT"),
            dict(order_id=order_id, side=side, price=D(price), qty=qty, realized_pnl=D(pnl), commission=D("0.3"), commission_asset="USDT"),
        ]

    def test_a_live_trade_closed_by_us_stores_binances_numbers(self):
        trade = self.open_trade()
        with mock.patch.object(self.h.paper, "account_fills", mock.AsyncMock(return_value=self.fills(trade, 99)), create=True):
            outcome = async_to_sync(self.h.monitor.close_now)(trade.id)
        trade.refresh_from_db()
        self.assertEqual(outcome, "CLOSED")
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "CLOSED_MANUALLY"))
        self.assertEqual((trade.realized_pnl, trade.fees_paid, trade.exit_price), (D("-5.7"), D("0.7"), D("1995")))

    def test_a_trade_found_closed_is_named_after_the_order_that_closed_it(self):
        trade = self.open_trade()
        sl = trade.orders.filter(role="SL").first()
        tp = trade.orders.filter(role="TP1").first()
        for order, expected in ((sl, "SL_HIT"), (tp, "TP_COMPLETED")):
            with self.subTest(expected):
                Trade.objects.filter(pk=trade.pk).update(status="OPEN", reason_code="", realized_pnl=None, exit_price=None)
                fills = self.fills(trade, 555)
                state = mock.AsyncMock(side_effect=lambda symbol, exchange_id, is_algo, ref: type("S", (), {"order_id": 555 if exchange_id == order.exchange_id else 1000 + exchange_id})())
                with mock.patch.object(self.h.paper, "account_fills", mock.AsyncMock(return_value=fills), create=True), \
                        mock.patch.object(self.h.paper, "get_order", state):
                    snap = async_to_sync(self.h.monitor._snap)(trade.id) if hasattr(self.h.monitor, "_snap") else None
                    from trading.services import trade_repository as repo
                    snap = repo.snapshot_trade(trade.id)
                    async_to_sync(self.h.monitor._wrap_up)(self.h.paper, snap, "CLOSED", "CLOSED_EXTERNALLY")
                trade.refresh_from_db()
                self.assertEqual(trade.reason_code, expected)
                self.assertEqual(trade.exit_price, D("1995"))

    def test_a_close_nobody_of_ours_made_stays_closed_externally_with_the_real_price(self):
        trade = self.open_trade()
        from trading.services import trade_repository as repo

        with mock.patch.object(self.h.paper, "account_fills", mock.AsyncMock(return_value=self.fills(trade, 4242, price="2010", pnl="10")), create=True):
            async_to_sync(self.h.monitor._wrap_up)(self.h.paper, repo.snapshot_trade(trade.id), "CLOSED", "CLOSED_EXTERNALLY")
        trade.refresh_from_db()
        self.assertEqual((trade.reason_code, trade.exit_price, trade.realized_pnl), ("CLOSED_EXTERNALLY", D("2010"), D("9.3")))

    def test_when_binance_cannot_be_read_the_estimate_is_used(self):
        trade = self.open_trade()
        with mock.patch.object(self.h.paper, "account_fills", mock.AsyncMock(side_effect=RuntimeError("down")), create=True):
            outcome = async_to_sync(self.h.monitor.close_now)(trade.id)
        trade.refresh_from_db()
        self.assertEqual((outcome, trade.status), ("CLOSED", "CLOSED"))
        self.assertIsNotNone(trade.realized_pnl)

    def test_the_history_page_shows_the_recorded_closing_price(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        trade = self.open_trade()
        with mock.patch.object(self.h.paper, "account_fills", mock.AsyncMock(return_value=self.fills(trade, 99)), create=True):
            async_to_sync(self.h.monitor.close_now)(trade.id)
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))
        rows = self.client.get(reverse("real-api-history")).json()["rows"]
        self.assertEqual([(r["exit_price"], r["pnl"]) for r in rows], [(1995.0, -5.7)])
