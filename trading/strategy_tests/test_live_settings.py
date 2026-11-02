"""Settings are live in the trading side, and a settings change never touches open trades."""
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from runtime_settings import service as runtime
from telegram_monitor.models import TelegramSource
from trading.config import TradingSettings
from trading.domain import strategy as st
from trading.models import Trade
from trading.services import trade_repository as repo
from trading.services.engine import TradingEngine
from trading.services.gateways import gateway_for
from trading.strategy_tests.support import Harness, make_signal, make_source, settings_for_tests

D = Decimal


class GatewayForTests(TestCase):
    def test_a_live_trade_is_managed_on_its_own_environment(self):
        main, test = object(), object()
        gateways = {"LIVE": test, "LIVE@MAINNET": main, "LIVE@TESTNET": test}
        self.assertIs(gateway_for(gateways, "LIVE", False), main)
        self.assertIs(gateway_for(gateways, "LIVE", True), test)

    def test_never_falls_back_to_the_other_environment(self):
        gateways = {"LIVE": "x", "LIVE@TESTNET": "x"}
        self.assertIsNone(gateway_for(gateways, "LIVE", False))  # mainnet trade, only testnet connected

    def test_a_single_unlabelled_live_gateway_and_paper(self):
        self.assertEqual(gateway_for({"LIVE": "g"}, "LIVE", True), "g")
        self.assertEqual(gateway_for({"PAPER": "p"}, "PAPER"), "p")


class EngineEnvironmentTests(TestCase):
    """Switching "Use Binance testnet" must not strand or close a live trade that is already open."""

    def reconcile(self, engine, **kw):
        calls = []
        cfg = TradingSettings(**{**dict(
            trading_enabled=True, forward_test_enabled=False, poll_seconds=5, fee_rate=D("0"),
            api_key="k", api_secret="s", testnet=False), **kw})
        engine.settings = cfg

        async def fake_connect(key, secret, testnet):
            gateway = MagicMock(name=f"gw-{testnet}")
            gateway.verify_account = AsyncMock()
            gateway.testnet = testnet
            calls.append(testnet)
            return gateway

        engine._connect = fake_connect
        async_to_sync(engine._reconcile)(cfg)
        return calls

    def test_open_mainnet_trade_keeps_its_gateway_after_switching_to_testnet(self):
        source = make_source(mode="REAL")
        Trade.objects.create(signal=make_signal(source), mode="LIVE", symbol="ETHUSDT", side="BUY", status="OPEN", testnet=False)
        engine = TradingEngine()

        self.reconcile(engine, testnet=False)
        mainnet = engine.gateways["LIVE@MAINNET"]

        calls = self.reconcile(engine, testnet=True)  # the user flips the setting
        self.assertEqual(calls, [True])  # only the new environment had to connect
        self.assertIs(engine.gateways["LIVE@MAINNET"], mainnet)  # still there for the open trade
        self.assertIs(gateway_for(engine.gateways, "LIVE", False), mainnet)
        self.assertIs(engine.gateways["LIVE"], engine.gateways["LIVE@TESTNET"])  # NEW trades go to the testnet

    def test_without_open_trades_nothing_extra_is_connected(self):
        engine = TradingEngine()
        self.reconcile(engine, testnet=True)
        self.assertNotIn("LIVE@MAINNET", engine.gateways)

    def test_forward_test_switched_on_later_creates_the_paper_gateway_without_a_restart(self):
        engine = TradingEngine()
        self.reconcile(engine, forward_test_enabled=False)
        self.assertNotIn("PAPER", engine.gateways)
        self.reconcile(engine, forward_test_enabled=True)
        self.assertIn("PAPER", engine.gateways)
        self.reconcile(engine, forward_test_enabled=False)  # switching off never removes it (open paper trades)
        self.assertIn("PAPER", engine.gateways)

    def test_new_live_trade_records_its_environment(self):
        class Gw:
            testnet = True
        trade_id, _ = repo.begin_trade(make_signal(make_source(mode="REAL")).id, "LIVE", "ETHUSDT", "BUY", testnet=Gw.testnet)
        self.assertTrue(Trade.objects.get(pk=trade_id).testnet)


class TradingSettingsLoadTests(TestCase):
    def test_snapshot_follows_the_settings_page(self):
        runtime.save_many({
            "trading.real_enabled": True, "trading.testnet": True, "trading.poll_seconds": 9,
            "forward.fee_percent": "0.1", "trading.max_concurrent_trades": 3, "paper.page_size": 500,
            "binance.candle_interval": "5m", "paper.trade_lookback_minutes": 60,
        })
        cfg = TradingSettings.load()
        self.assertTrue(cfg.trading_enabled and cfg.testnet)
        self.assertEqual((cfg.poll_seconds, cfg.max_concurrent_trades, cfg.paper_page_size), (9, 3, 500))
        self.assertEqual(cfg.fee_rate, D("0.001"))
        self.assertEqual((cfg.candle_interval, cfg.trade_lookback_ms), ("5m", 3_600_000))

    def test_poll_interval_never_goes_below_the_minimum(self):
        runtime.save_many({"trading.poll_seconds": 2, "trading.min_poll_seconds": 4})
        self.assertEqual(TradingSettings.load().poll_seconds, 4)

    def test_defaults(self):
        cfg = TradingSettings.load()
        self.assertEqual((cfg.poll_seconds, cfg.max_concurrent_trades, cfg.reconnect_seconds), (5, 8, 30))
        self.assertFalse(cfg.trading_enabled)  # live trading is off until switched on
        self.assertTrue(cfg.forward_test_enabled)


class SettingsReachTheEngineTests(TestCase):
    def test_executor_follows_a_settings_update_for_new_signals_only(self):
        harness = Harness()
        first = harness.open(make_signal(make_source(mode="FORWARD")))  # takes the trade while forward is allowed
        harness.executor.update_settings(replace(settings_for_tests(), forward_test_enabled=False))
        signal2 = make_signal(make_source(mode="FORWARD"))
        async_to_sync(harness.executor.handle_signal)(signal2.id)
        self.assertFalse(Trade.objects.filter(signal=signal2).exists())  # new trades stop...
        harness.run(2010)
        first.refresh_from_db()
        self.assertIn(first.status, ("PENDING_ENTRY", "OPEN"))  # ...the open one is still managed, not closed


class StrategyDefaultsTests(TestCase):
    def test_default_take_profit_split_is_configurable_and_snapshotted_per_trade(self):
        self.assertEqual(st.effective_tp_percents(3, [None] * 8), [60, 40, 0])
        self.assertEqual(st.effective_tp_percents(3, [None] * 8, (50, 30, 20)), [50, 30, 20])
        self.assertEqual(st.effective_tp_percents(2, [None] * 8, (50, 30, 20)), [50, 50])  # remainder to the last target
        self.assertEqual(st.effective_tp_percents(1, [None] * 8, (50, 30, 20)), [100])

        runtime.save_many({"forward.tp_split": "70, 30"})
        source = make_source()
        strategy = repo.strategy_from_source(source)
        self.assertEqual(strategy.default_tp, (70, 30))
        self.assertEqual(st.SourceStrategy.from_dict(strategy.to_dict()).default_tp, (70, 30))
        self.assertEqual(st.SourceStrategy.from_dict({"entry_mode": "NEAREST"}).default_tp, (60, 40))  # old trades

        runtime.save_many({"forward.tp_split": "10, 90"})
        self.assertEqual(st.SourceStrategy.from_dict(strategy.to_dict()).default_tp, (70, 30))  # open trade unaffected

    def test_sources_that_set_their_own_percentages_ignore_the_default(self):
        self.assertEqual(st.effective_tp_percents(3, [20, 30, 50], (60, 40)), [20, 30, 50])


class NewSourceDefaultsTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

    def test_new_sources_override_nothing_and_follow_their_world_live(self):
        from unittest.mock import patch

        with patch("trading.views_sources.listener_manager") as listener:
            listener.resolve_entity.return_value = {"name": "New chat", "username": None}
            self.client.post(reverse("forward-source-create"), {"name": "", "telegram_id": "-100555"})
        source = TelegramSource.objects.get(telegram_id=-100555)
        for name in runtime.SOURCE_OVERRIDE_FIELDS:
            self.assertIsNone(getattr(source, name), name)
        runtime.save_many({"forward.entry_mode": "BOTH", "forward.sl_mode": "AFTER_FILLING_TP", "forward.sl_after_tp": "2"})
        strategy = repo.strategy_from_source(source)  # changed AFTER the source exists: still followed
        self.assertEqual((strategy.entry_mode, strategy.sl_mode, strategy.sl_after_tp), ("BOTH", "AFTER_FILLING_TP", 2))

    def test_re_adding_a_known_chat_keeps_its_own_overrides(self):
        existing = make_source(telegram_id=-100556, entry_mode="FARTHEST")
        runtime.save_many({"forward.entry_mode": "BOTH"})
        from unittest.mock import patch

        with patch("trading.views_sources.listener_manager") as listener:
            listener.resolve_entity.return_value = {"name": "Renamed", "username": None}
            self.client.post(reverse("forward-source-create"), {"name": "", "telegram_id": "-100556"})
        existing.refresh_from_db()
        self.assertEqual((existing.entry_mode, existing.name), ("FARTHEST", "Renamed"))
