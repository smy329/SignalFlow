"""Wiring tests: signal hook, dispatcher, views, report data, engine restore/recovery."""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from runtime_settings import service as runtime
from telegram_monitor import signals as signal_events
from telegram_monitor.services import signal_service
from telegram_monitor.tests.test_signal_service import make_message
from trading import dispatcher
from trading.config import TradingSettings
from trading.exchange import base
from trading.exchange.paper_gateway import PaperGateway
from telegram_monitor.models import TelegramSource
from trading.models import Trade, TradeOrder
from trading.services import report_service
from trading.services import trade_repository as repo
from trading.services.engine import TradingEngine
from trading.services.execution_service import ExecutionService
from trading.services.monitor_service import MonitorService
from trading.tests.fakes import FakeGateway, FakeMarket
from trading.tests.helpers import make_signal, make_source

D = Decimal


def cfg(**kw):
    base_ = dict(trading_enabled=True, forward_test_enabled=True, poll_seconds=5, fee_rate=D("0.0005"),
                 api_key="k", api_secret="s", testnet=False)
    return TradingSettings(**{**base_, **kw})


class SignalHookTests(TestCase):
    def setUp(self):
        self.source = make_source(mode="FORWARD")
        self.received = []
        self.receiver = lambda sender, signal_id, **kw: self.received.append(signal_id)
        signal_events.signal_committed.connect(self.receiver, weak=False, dispatch_uid="test-receiver")
        self.addCleanup(signal_events.signal_committed.disconnect, dispatch_uid="test-receiver")

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_signal_committed_fires_once_after_commit(self, _broadcast):
        message = make_message(self.source)
        with self.captureOnCommitCallbacks(execute=True):
            signal = signal_service.process_message(message)
        self.assertEqual(self.received, [signal.id])

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_nothing_fires_before_the_commit(self, _broadcast):
        signal_service.process_message(make_message(self.source))
        self.assertEqual(self.received, [])

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_failing_receiver_never_breaks_the_listener_or_the_broadcast(self, broadcast):
        def boom(sender, signal_id, **kw):
            raise RuntimeError("receiver exploded")

        signal_events.signal_committed.connect(boom, weak=False, dispatch_uid="boom")
        self.addCleanup(signal_events.signal_committed.disconnect, dispatch_uid="boom")
        with self.captureOnCommitCallbacks(execute=True):
            signal = signal_service.process_message(make_message(self.source))
        self.assertIsNotNone(signal)
        broadcast.assert_called_once()
        self.assertEqual(self.received, [signal.id])

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal", side_effect=RuntimeError("ws down"))
    def test_failing_broadcast_does_not_stop_the_trading_hook(self, _broadcast):
        with self.captureOnCommitCallbacks(execute=True):
            signal = signal_service.process_message(make_message(self.source))
        self.assertEqual(self.received, [signal.id])


class DispatcherTests(TestCase):
    def test_signal_is_queued_only_while_the_engine_runs(self):
        with patch("trading.services.engine.engine") as engine:
            engine.is_running = False
            dispatcher.on_signal_committed(sender=None, signal_id=5)
            engine.submit_signal.assert_not_called()
            engine.is_running = True
            dispatcher.on_signal_committed(sender=None, signal_id=5)
            engine.submit_signal.assert_called_once_with(5)

    def test_trading_app_is_connected_to_the_signal(self):
        receivers = [r[1]() if callable(r[1]) else r[1] for r in signal_events.signal_committed.receivers]
        self.assertIn(dispatcher.on_signal_committed, receivers)


class EngineUnitTests(TestCase):
    def test_submit_before_start_is_ignored_and_run_raises_when_not_ready(self):
        from trading.services.engine import EngineNotReady

        engine = TradingEngine()
        engine.submit_signal(1)  # no loop yet: must not raise

        async def coro():
            return 1

        with patch.object(engine._ready, "wait", return_value=False):
            with self.assertRaises(EngineNotReady):
                engine.run(coro(), timeout=1)

    def _reconcile(self, **kw):
        from unittest.mock import AsyncMock

        engine = TradingEngine()
        engine.settings = config = cfg(**kw)
        calls = []

        async def fake_connect(key, secret, testnet):
            gateway = MagicMock(name=f"gw-{key or 'public'}-{testnet}")
            gateway.verify_account = AsyncMock()
            gateway.testnet = testnet
            calls.append((key, testnet))
            return gateway

        engine._connect = fake_connect
        async_to_sync(engine._reconcile)(config)
        return engine, calls

    def test_prices_come_from_the_real_market_even_when_trading_on_the_testnet(self):
        engine, calls = self._reconcile(testnet=True)
        self.assertEqual(calls, [("", False), ("k", True)])  # public mainnet market client, trading client on testnet
        self.assertIsNot(engine.market_gateway, engine.live_gateway)
        self.assertIs(engine.live_gateway, engine._live_by_env[True])

    def test_trading_on_the_real_account_still_uses_a_separate_public_market_client(self):
        engine, calls = self._reconcile(testnet=False)
        self.assertEqual(calls, [("", False), ("k", False)])
        self.assertIs(engine.live_gateway, engine._live_by_env[False])
        self.assertIs(engine.gateways["LIVE@MAINNET"], engine.live_gateway)

    def test_without_api_keys_prices_still_come_from_the_real_market(self):
        engine, calls = self._reconcile(api_key="", api_secret="", testnet=True)
        self.assertEqual(calls, [("", False)])
        self.assertEqual(engine._live_by_env, {})
        self.assertFalse(engine.has_live)

    def test_restore_paper_state_rebuilds_orders_positions_and_cursor(self):
        market = FakeMarket(price="2010")
        paper = PaperGateway(market)
        executor = ExecutionService({"PAPER": paper}, cfg(trading_enabled=False))
        signal = make_signal(make_source(mode="FORWARD"))
        async_to_sync(executor.handle_signal)(signal.id)
        trade = Trade.objects.get(signal=signal, mode="PAPER")
        entry = trade.orders.get(role="ENTRY")
        entry.status, entry.executed_quantity, entry.avg_fill_price = "FILLED", D("0.015"), D("2000")
        entry.save()
        Trade.objects.filter(pk=trade.pk).update(status="OPEN", paper_cursor=42)

        engine = TradingEngine()
        engine._market = market
        fresh = PaperGateway(market)
        async_to_sync(engine._restore_paper_state)(fresh)

        self.assertEqual(async_to_sync(fresh.position_amount)("ETHUSDT", trade.id), D("0.015"))
        self.assertEqual(fresh.cursor_for("ETHUSDT"), 42)
        sl_id = trade.orders.get(role="SL").exchange_id
        state = async_to_sync(fresh.get_order)("ETHUSDT", sl_id, False, trade.id)
        self.assertEqual(state.status, base.NEW)


class RecoveryTests(TestCase):
    def test_interrupted_live_trade_is_cleaned_up_and_failed(self):
        gw = FakeGateway()
        service = ExecutionService({"LIVE": gw}, cfg())
        signal = make_signal()
        trade = Trade.objects.create(signal=signal, mode="LIVE", symbol="ETHUSDT", side="BUY")  # still PLACING
        placed = async_to_sync(gw.place_order)("ETHUSDT", _spec("ENTRY", "LIMIT", "BUY", "0.015", "2000", False), trade.id)
        TradeOrder.objects.create(trade=trade, role="ENTRY", kind="LIMIT", side="BUY", quantity="0.015", price="2000",
                                  exchange_id=placed.exchange_id, is_algo=False)
        gw.fill(placed.exchange_id, 2000, entry=True)  # it even filled before the crash

        async_to_sync(service.recover_interrupted_trades)()

        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), (Trade.Status.FAILED, "INTERRUPTED"))
        self.assertEqual(gw.position, D("0"))  # flattened

    def test_interrupted_paper_trade_is_just_failed(self):
        service = ExecutionService({"PAPER": FakeGateway()}, cfg())
        trade = Trade.objects.create(signal=make_signal(), mode="PAPER", symbol="ETHUSDT", side="BUY")
        async_to_sync(service.recover_interrupted_trades)()
        trade.refresh_from_db()
        self.assertEqual(trade.status, Trade.Status.FAILED)


def _spec(role, kind, side, qty, price, reduce_only):
    from trading.domain.types import OrderKind, OrderRole, OrderSpec

    return OrderSpec(OrderRole(role), OrderKind(kind), side, D(qty), D(price), reduce_only)


def closed_paper_trade(signal, pnl, reason="TP_COMPLETED", exit_price="2020", leverage=10, mode="PAPER"):
    trade = Trade.objects.create(
        signal=signal, mode=mode, symbol=signal.symbol, side="BUY", status="CLOSED", leverage=leverage,
        quantity="0.015", entry_price="2000", stoploss="1980", targets=["2020", "2040"], filled_quantity="0.015",
        avg_entry_price="2000", realized_pnl=pnl, fees_paid="0.03", reason_code=reason,
        opened_at=timezone.now(), closed_at=timezone.now(),
    )
    TradeOrder.objects.create(trade=trade, role="ENTRY", kind="LIMIT", side="BUY", quantity="0.015", price="2000",
                              status="FILLED", executed_quantity="0.015", avg_fill_price="2000")
    TradeOrder.objects.create(trade=trade, role="TP1", kind="TAKE_PROFIT_MARKET", side="SELL", quantity="0.015",
                              price=exit_price, status="FILLED", executed_quantity="0.015", avg_fill_price=exit_price,
                              reduce_only=True)
    return trade


def open_paper_trade(signal, mode="PAPER"):
    trade = Trade.objects.create(signal=signal, mode=mode, symbol="ETHUSDT", side="BUY", status="OPEN", leverage=10,
                                 stoploss="1980", targets=["2020", "2040"], opened_at=timezone.now())
    TradeOrder.objects.create(trade=trade, role="ENTRY", kind="LIMIT", side="BUY", quantity="0.015", price="2000",
                              status="FILLED", executed_quantity="0.015", avg_fill_price="2000")
    return trade


class SourceStatsTests(TestCase):
    def test_stats_count_closed_trades_and_pnl_signs_per_source(self):
        a, b = make_source(name="Alpha"), make_source(name="Beta")
        closed_paper_trade(make_signal(a), "1.5")
        closed_paper_trade(make_signal(a), "-0.5", reason="SL_HIT")
        closed_paper_trade(make_signal(a), "0", reason="SL_AT_ENTRY_HIT")
        closed_paper_trade(make_signal(b), "2")
        open_paper_trade(make_signal(b))
        per_source, totals = repo.source_stats("PAPER")
        alpha = per_source[a.id]
        self.assertEqual((alpha["trades"], alpha["wins"], alpha["losses"], alpha["breakeven"]), (3, 1, 1, 1))
        self.assertAlmostEqual(alpha["win_rate"], 33.33, places=1)
        self.assertEqual((alpha["total_pnl"], alpha["avg_pnl"]), (D("1.0"), D("1.0") / 3))
        self.assertEqual((per_source[b.id]["trades"], per_source[b.id]["open"]), (1, 1))
        self.assertEqual((totals["trades"], totals["open"], totals["total_pnl"]), (4, 1, D("3.0")))

    def test_real_and_forward_stats_never_mix(self):
        real, forward = make_source(mode="REAL"), make_source(mode="FORWARD")
        closed_paper_trade(make_signal(real), "5", mode="LIVE")
        closed_paper_trade(make_signal(forward), "2")
        self.assertEqual(repo.source_stats("LIVE")[1]["total_pnl"], D("5"))
        self.assertEqual(repo.source_stats("PAPER")[1]["total_pnl"], D("2"))

    def test_empty_stats(self):
        _, totals = repo.source_stats("PAPER")
        self.assertEqual((totals["trades"], totals["win_rate"], totals["avg_pnl"], totals["total_pnl"]), (0, None, None, D("0")))

    def test_rejected_cancelled_and_pnl_less_trades_are_not_counted(self):
        Trade.objects.create(signal=make_signal(), mode="PAPER", symbol="ETHUSDT", side="BUY", status="REJECTED")
        Trade.objects.create(signal=make_signal(), mode="PAPER", symbol="ETHUSDT", side="BUY", status="CANCELLED")
        self.assertEqual(repo.source_stats("PAPER")[1]["trades"], 0)
        self.assertEqual(repo.trade_rows("PAPER", repo.ST_CLOSED), [])


def cancelled_trade(signal, mode="PAPER", reason="PRICE_HIT_TP1"):
    return Trade.objects.create(signal=signal, mode=mode, symbol="ETHUSDT", side="BUY", status="CANCELLED", leverage=10,
                                quantity="0.015", entry_price="1990", stoploss="1970", targets=["2010"],
                                reason_code=reason, placed_at=timezone.now(), closed_at=timezone.now())


class CancelledTests(TestCase):
    def test_stats_count_cancelled_entries_per_source_without_touching_pnl_stats(self):
        a, b = make_source(name="Alpha"), make_source(name="Beta")
        cancelled_trade(make_signal(a))
        cancelled_trade(make_signal(a), reason="PRICE_HIT_SL")
        cancelled_trade(make_signal(b))
        closed_paper_trade(make_signal(a), "1")
        per_source, totals = repo.source_stats("PAPER")
        self.assertEqual((per_source[a.id]["cancelled"], per_source[a.id]["trades"]), (2, 1))
        self.assertEqual((per_source[b.id]["cancelled"], per_source[b.id]["trades"]), (1, 0))
        self.assertEqual((totals["cancelled"], totals["trades"], totals["total_pnl"]), (3, 1, D("1")))

    def test_cancelled_counts_never_mix_between_real_and_forward(self):
        cancelled_trade(make_signal(make_source(mode="REAL")), mode="LIVE")
        self.assertEqual(repo.source_stats("LIVE")[1]["cancelled"], 1)
        self.assertEqual(repo.source_stats("PAPER")[1]["cancelled"], 0)

    def test_cancelled_row_has_reason_and_planned_entry(self):
        cancelled_trade(make_signal(), reason="PRICE_HIT_SL")
        (row,) = report_service.cancelled_rows(repo.trade_rows("PAPER", repo.ST_CANCELLED))
        self.assertEqual((row["side"], row["entry_price"], row["margin"], row["result"]), ("LONG", 1990.0, 2.985, "PRICE_HIT_SL"))
        self.assertIsNotNone(row["closed_at"])

    def test_cancelled_entries_are_not_in_history_or_pending(self):
        cancelled_trade(make_signal())
        self.assertEqual(repo.trade_rows("PAPER", repo.ST_CLOSED), [])
        self.assertEqual(repo.trade_rows("PAPER", repo.ST_PENDING), [])


class PendingAndMessageCountTests(TestCase):
    def test_pending_counts_per_source_and_total_ignore_other_statuses_and_modes(self):
        a, b = make_source(name="Alpha"), make_source(name="Beta")
        pending = dict(mode="PAPER", symbol="ETHUSDT", side="BUY", status="PENDING_ENTRY", placed_at=timezone.now())
        Trade.objects.create(signal=make_signal(a), **pending)
        Trade.objects.create(signal=make_signal(a), **pending)
        Trade.objects.create(signal=make_signal(b), **pending)
        Trade.objects.create(signal=make_signal(b), **{**pending, "status": "OPEN"})
        Trade.objects.create(signal=make_signal(b), **{**pending, "mode": "LIVE"})
        per_source, totals = repo.source_stats("PAPER")
        self.assertEqual((per_source[a.id]["pending"], per_source[b.id]["pending"], totals["pending"]), (2, 1, 3))
        self.assertEqual(repo.source_stats("LIVE")[1]["pending"], 1)

    def test_sources_without_trades_have_zero_pending(self):
        self.assertEqual(repo.empty_stats()["pending"], 0)

    def test_message_counts_are_per_source_and_per_mode(self):
        from telegram_monitor.services import message_service

        real, fwd = make_source(mode="REAL"), make_source(mode="FORWARD")
        for source, n in ((real, 3), (fwd, 2)):
            for _ in range(n):
                make_signal(source)  # each helper call also stores one message
        self.assertEqual(message_service.message_counts_by_source("REAL"), {real.id: 3})
        self.assertEqual(message_service.message_counts_by_source("FORWARD"), {fwd.id: 2})


class ReportRowsTests(TestCase):
    def test_history_row_shows_pnl_and_percent_of_margin(self):
        closed_paper_trade(make_signal(), "0.24")
        (row,) = report_service.history_rows(repo.trade_rows("PAPER", repo.ST_CLOSED))
        self.assertEqual((row["side"], row["result"]), ("LONG", "TP_COMPLETED"))
        self.assertEqual((row["entry_price"], row["exit_price"], row["pnl"]), (2000.0, 2020.0, 0.24))
        self.assertEqual(row["margin"], 3.0)  # 0.015 * 2000 / 10
        self.assertEqual(row["pnl_pct"], 8.0)
        self.assertIsNotNone(row["opened_at"])
        self.assertIsNotNone(row["closed_at"])

    def test_open_row_shows_live_pnl_from_the_market_price(self):
        open_paper_trade(make_signal())
        rows = repo.trade_rows("PAPER", repo.ST_OPEN)
        self.assertEqual(report_service.symbols(rows), {"ETHUSDT"})
        (row,) = report_service.position_rows(rows, {"ETHUSDT": D("2010")}, D("0"))
        self.assertEqual((row["mark_price"], row["pnl"], row["pnl_pct"]), (2010.0, 0.15, 5.0))
        (no_price,) = report_service.position_rows(rows, {}, D("0"))
        self.assertIsNone(no_price["pnl"])  # unknown, not zero

    def test_pending_row_has_current_price_and_planned_margin(self):
        trade = Trade.objects.create(signal=make_signal(), mode="LIVE", symbol="ETHUSDT", side="SELL", status="PENDING_ENTRY",
                                     leverage=10, quantity="0.015", entry_price="2000", stoploss="2020", targets=["1980"],
                                     placed_at=timezone.now())
        (row,) = report_service.pending_rows(repo.trade_rows("LIVE", repo.ST_PENDING), {"ETHUSDT": D("1995")})
        self.assertEqual((row["side"], row["current_price"], row["entry_price"], row["margin"]), ("SHORT", 1995.0, 2000.0, 3.0))
        self.assertEqual(trade.status, "PENDING_ENTRY")
        self.assertNotIn("pnl", row)


class ViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("yasir", password="pw")

    def login(self):
        self.client.login(username="yasir", password="pw")

    def test_pages_and_apis_require_login(self):
        for scope in ("real", "forward"):
            for name in ("sources", "trades", "history", "api-positions", "api-pending", "api-cancelled", "api-history"):
                response = self.client.get(reverse(f"{scope}-{name}" if not name.startswith("api") else f"{scope}-{name}"))
                self.assertEqual(response.status_code, 302, name)
                self.assertIn("/login/", response["Location"])

    def test_pages_render(self):
        self.login()
        for scope in ("real", "forward"):
            for name in ("sources", "trades-positions", "trades-pending", "trades-cancelled", "history"):
                self.assertEqual(self.client.get(reverse(f"{scope}-{name}")).status_code, 200)

    def test_sidebar_has_the_expected_entries_on_every_page(self):
        self.login()
        page = self.client.get(reverse("dashboard")).content.decode()
        for url in ("dashboard", "real-sources", "real-trades", "real-history", "forward-sources", "forward-trades",
                    "forward-history", "logout"):
            self.assertIn(reverse(url), page)

    def test_sources_page_only_lists_its_own_mode(self):
        self.login()
        make_source(name="RealOne", mode="REAL")
        make_source(name="FwdOne", mode="FORWARD")
        real = self.client.get(reverse("real-sources")).content.decode()
        forward = self.client.get(reverse("forward-sources")).content.decode()
        self.assertIn("RealOne", real)
        self.assertNotIn("FwdOne", real)
        self.assertIn("FwdOne", forward)
        self.assertNotIn("RealOne", forward)

    def test_dashboard_lists_both_modes_without_an_auto_trade_switch(self):
        self.login()
        make_source(name="RealOne", mode="REAL")
        make_source(name="FwdOne", mode="FORWARD")
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("RealOne", page)
        self.assertIn("FwdOne", page)
        self.assertNotIn("Auto-trade", page)

    @patch("trading.views_sources.listener_manager")
    def test_the_same_chat_can_be_a_real_and_a_forward_source(self, listener):
        self.login()
        listener.resolve_entity.return_value = {"name": "Chan", "username": None}
        self.client.post(reverse("real-source-create"), {"telegram_id": "-100555"})
        self.client.post(reverse("forward-source-create"), {"telegram_id": "-100555"})
        self.assertEqual(sorted(TelegramSource.objects.filter(telegram_id=-100555).values_list("mode", flat=True)),
                         ["FORWARD", "REAL"])

    def test_toggle_requires_post_and_flips_only_that_source(self):
        self.login()
        source, other = make_source(mode="REAL"), make_source(mode="FORWARD")
        self.assertEqual(self.client.get(reverse("source-toggle", args=[source.pk])).status_code, 405)
        response = self.client.post(reverse("source-toggle", args=[source.pk]))
        self.assertRedirects(response, reverse("real-sources"))
        source.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((source.is_active, other.is_active), (False, True))

    def test_toggle_returns_to_the_dashboard_when_asked(self):
        self.login()
        source = make_source()
        response = self.client.post(reverse("source-toggle", args=[source.pk]), {"next": reverse("dashboard")})
        self.assertRedirects(response, reverse("dashboard"))
        response = self.client.post(reverse("source-toggle", args=[source.pk]), {"next": "https://evil.example/"})
        self.assertRedirects(response, reverse("forward-sources"))

    def test_delete_removes_the_source_with_its_signals_and_trades(self):
        self.login()
        source = make_source(mode="FORWARD")
        closed_paper_trade(make_signal(source), "1")
        self.client.post(reverse("source-delete", args=[source.pk]))
        self.assertFalse(TelegramSource.objects.filter(pk=source.pk).exists())
        self.assertFalse(Trade.objects.exists())

    def test_delete_is_refused_while_a_real_source_has_live_trades_in_progress(self):
        self.login()
        source = make_source(mode="REAL")
        open_paper_trade(make_signal(source), mode="LIVE")
        self.client.post(reverse("source-delete", args=[source.pk]))
        self.assertTrue(TelegramSource.objects.filter(pk=source.pk).exists())
        self.assertTrue(Trade.objects.exists())

    def test_delete_requires_post(self):
        self.login()
        source = make_source()
        self.assertEqual(self.client.get(reverse("source-delete", args=[source.pk])).status_code, 405)

    def test_positions_api_reports_disabled_without_an_engine(self):
        self.login()
        for scope in ("real", "forward"):
            with patch("trading.views.engine") as engine:
                engine.is_running = False
                data = self.client.get(reverse(f"{scope}-api-positions")).json()
            self.assertFalse(data["enabled"])
            self.assertEqual(data["rows"], [])

    def test_real_positions_api_returns_the_live_account_rows(self):
        self.login()
        snapshot = dict(symbol="ETHUSDT", amount=D("1"), mark_price=D("2000"), notional=D("2000"), unrealized_pnl=D("5"),
                        leverage=10, initial_margin=D("200"), maint_margin=D("10"), entry_price=D("1990"), liq_price=D("1800"))
        with patch("trading.views.engine") as engine:
            engine.is_running = True
            engine.has_live = True
            engine.run.return_value = [snapshot]  # the engine loop only fetches the account's snapshots
            data = self.client.get(reverse("real-api-positions")).json()
        self.assertTrue(data["enabled"])
        self.assertEqual([(r["symbol"], r["side"], r["trade_id"]) for r in data["rows"]], [("ETHUSDT", "LONG", None)])

    def test_real_positions_api_maps_failures_to_error_statuses(self):
        from trading.services.engine import EngineNotReady

        self.login()
        with patch("trading.views.engine") as engine, patch("trading.views.position_service.build_positions", new=MagicMock()):
            engine.is_running = True
            engine.has_live = True
            engine.run.side_effect = EngineNotReady("starting")
            self.assertEqual(self.client.get(reverse("real-api-positions")).status_code, 503)
            engine.run.side_effect = RuntimeError("binance down")
            self.assertEqual(self.client.get(reverse("real-api-positions")).status_code, 502)

    def test_forward_positions_api_uses_real_market_prices_for_open_trades(self):
        self.login()
        open_paper_trade(make_signal())
        with patch("trading.views.engine") as engine:
            engine.is_running = True
            engine.settings = cfg(fee_rate=D("0"))
            engine.gateways = {"PAPER": MagicMock()}
            engine.market_gateway.last_prices.return_value = MagicMock()
            engine.run.return_value = {"ETHUSDT": D("2010")}
            data = self.client.get(reverse("forward-api-positions")).json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["rows"][0]["pnl"], 0.15)
        engine.market_gateway.last_prices.assert_called_once_with({"ETHUSDT"})

    def test_forward_page_never_shows_real_trades_and_vice_versa(self):
        self.login()
        closed_paper_trade(make_signal(make_source(name="R", mode="REAL")), "5", mode="LIVE")
        closed_paper_trade(make_signal(make_source(name="F", mode="FORWARD")), "2")
        with patch("trading.views.engine") as engine:
            engine.is_running = False
            forward = self.client.get(reverse("forward-api-history")).json()["rows"]
            real = self.client.get(reverse("real-api-history")).json()["rows"]
        self.assertEqual([r["channel"] for r in forward], ["F"])
        self.assertEqual([r["channel"] for r in real], ["R"])
        json.dumps(forward)  # JSON-serializable

    def test_pending_api_lists_pending_entries_with_the_market_price(self):
        self.login()
        Trade.objects.create(signal=make_signal(), mode="PAPER", symbol="ETHUSDT", side="BUY", status="PENDING_ENTRY",
                             leverage=10, quantity="0.015", entry_price="1990", stoploss="1970", targets=["2010"],
                             placed_at=timezone.now())
        with patch("trading.views.engine") as engine:
            engine.is_running = True
            engine.gateways = {"PAPER": MagicMock()}
            engine.run.return_value = {"ETHUSDT": D("2000")}
            data = self.client.get(reverse("forward-api-pending")).json()
        self.assertEqual((data["rows"][0]["current_price"], data["rows"][0]["entry_price"]), (2000.0, 1990.0))

    def test_cancelled_api_and_sources_page_are_per_mode(self):
        self.login()
        cancelled_trade(make_signal(make_source(name="F", mode="FORWARD")))
        cancelled_trade(make_signal(make_source(name="R", mode="REAL")), mode="LIVE")
        with patch("trading.views.engine") as engine:
            engine.is_running = False
            forward = self.client.get(reverse("forward-api-cancelled")).json()["rows"]
            real = self.client.get(reverse("real-api-cancelled")).json()["rows"]
        self.assertEqual(([r["channel"] for r in forward], [r["channel"] for r in real]), (["F"], ["R"]))
        page = self.client.get(reverse("forward-sources"))
        self.assertContains(page, "Cancelled")
        self.assertEqual(page.context["totals"]["cancelled"], 1)

    def test_each_trades_tab_is_its_own_page_bound_to_its_own_api(self):
        self.login()
        self.assertRedirects(self.client.get(reverse("real-trades")), reverse("real-trades-positions"))
        for scope in ("real", "forward"):
            for tab in ("positions", "pending", "cancelled"):
                page = self.client.get(reverse(f"{scope}-trades-{tab}"))
                self.assertContains(page, f'data-url="{reverse(f"{scope}-api-{tab}")}"')
                self.assertContains(page, f'data-tab="{tab}"')
        self.assertEqual(self.client.get("/real/trades/bogus/").status_code, 404)

    def test_sources_page_shows_pending_and_message_counts_in_cards_and_table(self):
        self.login()
        source = make_source(name="Alpha", mode="FORWARD")
        make_signal(source)
        make_signal(source)
        Trade.objects.create(signal=make_signal(source), mode="PAPER", symbol="ETHUSDT", side="BUY",
                             status="PENDING_ENTRY", placed_at=timezone.now())
        page = self.client.get(reverse("forward-sources"))
        self.assertEqual((page.context["totals"]["pending"], page.context["totals"]["messages"]), (1, 3))
        (row,) = page.context["rows"]
        self.assertEqual((row["stats"]["pending"], row["stats"]["messages"]), (1, 3))
        for label in ("Pending trades", "Messages received", "Messages"):
            self.assertContains(page, label)

    def test_unknown_scope_is_a_404(self):
        self.login()
        self.assertEqual(self.client.get("/bogus/trades/").status_code, 404)


class SwitchTests(TestCase):
    def setUp(self):
        runtime.save_many({"trading.real_enabled": True, "trading.forward_enabled": True})
        self.user = get_user_model().objects.create_user("yasir", password="pw")
        self.client.login(username="yasir", password="pw")

    def test_everything_is_on_by_default(self):
        from trading.services import switch_service

        self.assertEqual(switch_service.switched_on_modes(), {"LIVE", "PAPER"})

    def test_real_and_forward_switches_are_independent_and_persist(self):
        from trading.services import switch_service
        from trading.scopes import FORWARD, REAL

        self.client.post(reverse("real-switch"), {"enabled": "0"})
        self.assertEqual(switch_service.switched_on_modes(), {"PAPER"})
        self.client.post(reverse("forward-switch"), {"enabled": "0"})
        self.assertEqual(switch_service.switched_on_modes(), set())
        self.client.post(reverse("real-switch"), {"enabled": "1"})
        self.assertEqual(switch_service.switched_on_modes(), {"LIVE"})
        self.assertTrue(switch_service.is_enabled(REAL))
        self.assertFalse(switch_service.is_enabled(FORWARD))

    def test_switch_requires_login_and_post(self):
        self.assertEqual(self.client.get(reverse("real-switch")).status_code, 405)
        self.client.logout()
        response = self.client.post(reverse("real-switch"), {"enabled": "0"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_switch_returns_to_the_page_it_came_from_safely(self):
        response = self.client.post(reverse("forward-switch"), {"enabled": "0", "next": reverse("dashboard")})
        self.assertRedirects(response, reverse("dashboard"))
        response = self.client.post(reverse("forward-switch"), {"enabled": "1", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("forward-sources"))

    def test_settings_master_flag_cannot_be_overridden_from_the_switch(self):
        from trading.services import switch_service
        from trading.scopes import REAL

        runtime.save_many({"trading.real_enabled": False})
        self.client.post(reverse("real-switch"), {"enabled": "1"})
        state = switch_service.state(REAL)
        self.assertEqual((state["allowed"], state["on"]), (False, False))
        self.assertContains(self.client.get(reverse("real-sources")), "Disabled in Settings (Allow live trading)")

    def test_dashboard_and_sources_pages_show_the_right_button(self):
        self.client.post(reverse("real-switch"), {"enabled": "0"})
        dashboard = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(reverse("real-switch"), dashboard)
        self.assertIn(reverse("forward-switch"), dashboard)
        real_page = self.client.get(reverse("real-sources")).content.decode()
        self.assertIn(reverse("real-switch"), real_page)
        self.assertNotIn(reverse("forward-switch"), real_page)
        self.assertIn("Turn ON", real_page)  # real is OFF
        self.assertIn("Turn OFF", self.client.get(reverse("forward-sources")).content.decode())

    def test_sidebar_groups_are_expanded_by_default(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertNotIn("<details", page)
        self.assertNotIn("group-hover:block", page)  # no hover/click needed
        for name in ("sources", "trades", "history", "messages"):
            for scope in ("real", "forward"):
                self.assertIn(f'href="{reverse(f"{scope}-{name}")}"', page)
        self.assertIn(reverse("parser-playground"), page)


class MessagesPageTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user("yasir", password="pw")
        self.client.login(username="yasir", password="pw")

    def _msg(self, source, text, status="FAILED", code="", n=[0]):
        from telegram_monitor.models import TelegramMessage

        n[0] += 1
        return TelegramMessage.objects.create(source=source, telegram_message_id=900000 + n[0], message_text=text,
                                              telegram_date=timezone.now(), parse_status=status, error_code=code)

    def test_lists_only_this_modes_messages_with_full_text_status_and_reason(self):
        real, fwd = make_source(name="RealCh", mode="REAL"), make_source(name="FwdCh", mode="FORWARD")
        long_text = "BTCUSDT LONG " + "x" * 400
        self._msg(real, long_text, "SUCCESS")
        self._msg(real, "gm everyone", "FAILED", "MESSAGE_TOO_SHORT")
        self._msg(real, "x" * 90, "FAILED", "NO_DIRECTION")
        self._msg(fwd, "forward only text", "FAILED", "MESSAGE_TOO_SHORT")
        page = self.client.get(reverse("real-messages"))
        html = page.content.decode()
        self.assertEqual(len(page.context["rows"]), 3)
        self.assertIn(long_text, html)  # full text, not truncated
        self.assertNotIn("forward only text", html)
        for label in ("Valid Signal", "Message too short", "No direction found", "Not a signal", "RealCh"):
            self.assertIn(label, html)
        self.assertIn("forward only text", self.client.get(reverse("forward-messages")).content.decode())

    def test_message_text_is_escaped(self):
        self._msg(make_source(mode="FORWARD"), "<script>alert(1)</script>", "FAILED", "MESSAGE_TOO_SHORT")
        self.assertNotIn("<script>alert(1)</script>", self.client.get(reverse("forward-messages")).content.decode())

    def test_paginates_newest_first(self):
        source = make_source(mode="FORWARD")
        for i in range(55):
            self._msg(source, f"m{i}", "FAILED", "MESSAGE_TOO_SHORT")
        first = self.client.get(reverse("forward-messages"))
        self.assertEqual(len(first.context["rows"]), 50)
        self.assertEqual(first.context["rows"][0]["message"].message_text, "m54")
        self.assertEqual(len(self.client.get(reverse("forward-messages") + "?page=2").context["rows"]), 5)

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("real-messages")).status_code, 302)

    def test_sidebar_has_messages_for_both_modes(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(reverse("real-messages"), page)
        self.assertIn(reverse("forward-messages"), page)

    def test_source_cards_link_to_the_related_pages(self):
        for scope in ("real", "forward"):
            html = self.client.get(reverse(f"{scope}-sources")).content.decode()
            for name in ("history", "trades-positions", "trades-pending", "trades-cancelled", "messages"):
                self.assertIn(f'href="{reverse(f"{scope}-{name}")}"', html)

    def test_sources_row_has_a_green_or_red_switch_and_icon_buttons(self):
        make_source(name="On", mode="FORWARD", is_active=True)
        html = self.client.get(reverse("forward-sources")).content.decode()
        self.assertIn('aria-checked="true"', html)
        self.assertIn("bg-emerald-500", html)  # green switch while active
        self.assertIn("js-open-settings", html)  # settings gear
        self.assertIn('aria-label="Delete source"', html)  # trash icon
        self.assertNotIn(">Deactivate<", html)
        TelegramSource.objects.update(is_active=False)
        html = self.client.get(reverse("forward-sources")).content.decode()
        self.assertIn('aria-checked="false"', html)
        self.assertIn("bg-red-500", html)  # red switch while inactive

    def test_sidebar_is_narrow_and_content_uses_the_full_width(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("w-44", html)
        self.assertIn("md:pl-44", html)
        self.assertNotIn("w-60", html)
        self.assertNotIn("max-w-7xl", html.split("<main")[1].split(">")[0])


class TablesFitTheirCardTests(TestCase):
    """Tables must fit the page width: no horizontal scroll wrappers, fixed layout, breakable cells."""

    def setUp(self):
        get_user_model().objects.create_user("yasir", password="pw")
        self.client.login(username="yasir", password="pw")

    def test_no_table_page_uses_a_horizontal_scroll_wrapper(self):
        for scope in ("real", "forward"):
            for name in ("sources", "trades-positions", "trades-pending", "trades-cancelled", "history", "messages"):
                html = self.client.get(reverse(f"{scope}-{name}")).content.decode()
                self.assertNotIn("overflow-x-auto", html, f"{scope}-{name}")
                self.assertIn("table-fixed", html, f"{scope}-{name}")
                self.assertIn("[overflow-wrap:anywhere]", html, f"{scope}-{name}")

    def test_js_tables_size_columns_from_weights_and_do_not_force_nowrap(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parent.parent / "static/trading/js/common.js").read_text()
        self.assertIn("th.style.width", js)
        self.assertNotIn("overflow-x-auto", js)
        cell_lines = [line for line in js.splitlines() if 'el("th"' in line or 'el("td"' in line]
        self.assertTrue(cell_lines)
        self.assertFalse([line for line in cell_lines if "whitespace-nowrap" in line])


class LiveUpdateHooksTests(TestCase):
    """Counters and messages refresh without a page reload: the pages carry the hooks the live scripts use."""

    def setUp(self):
        get_user_model().objects.create_user("yasir", password="pw")
        self.client.login(username="yasir", password="pw")

    def test_sources_page_marks_every_counter_as_live(self):
        source = make_source(name="Alpha", mode="FORWARD")
        html = self.client.get(reverse("forward-sources")).content.decode()
        for key in ("t-trades", "t-open", "t-pending", "t-cancelled", "t-messages", "t-winrate", "t-wlb", "t-pnl", "t-avg"):
            self.assertIn(f'data-live="{key}"', html)
        for key in ("trades", "open", "pending", "cancelled", "messages", "winrate", "wlb", "pnl", "avg"):
            self.assertIn(f'data-live="{source.id}-{key}"', html)
        self.assertIn("/static/trading/js/live.js", html)
        self.assertIn("/static/trading/js/sources.js", html)

    def test_messages_page_has_row_ids_counter_and_live_scripts(self):
        from telegram_monitor.models import TelegramMessage

        source = make_source(mode="FORWARD")
        message = TelegramMessage.objects.create(source=source, telegram_message_id=777001, message_text="hello",
                                                 telegram_date=timezone.now(), parse_status="FAILED", error_code="MESSAGE_TOO_SHORT")
        html = self.client.get(reverse("forward-messages")).content.decode()
        self.assertIn(f'data-id="{message.id}"', html)
        self.assertIn('data-live="message-count"', html)
        self.assertIn('id="message-rows"', html)
        self.assertIn("/static/trading/js/messages.js", html)

    def test_every_trade_table_polls_every_five_seconds_and_pauses_when_hidden(self):
        from pathlib import Path

        js_dir = Path(__file__).resolve().parent.parent / "static/trading/js"
        for name in ("trades.js", "history.js"):
            text = (js_dir / name).read_text()
            self.assertNotIn("15000", text, name)
        self.assertIn("document.hidden", (js_dir / "common.js").read_text())
        self.assertIn("document.hidden", (js_dir / "live.js").read_text())
