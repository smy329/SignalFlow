"""Reversal mode of a source: an opposite signal on a coin the same source already trades."""
from decimal import Decimal as D

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from runtime_settings import service as runtime
from telegram_monitor.models import TelegramSource
from trading.config import TradingSettings
from trading.domain import strategy as st
from trading.models import Trade
from trading.services.execution_service import ExecutionService
from trading.services.monitor_service import MonitorService
from trading.strategy_tests.support import Harness, make_signal, make_source, working


def short_signal(source, **kw):
    fields = dict(side="SELL", entries=("2010", "2020"), targets=("1990", "1970", "1950"), stoploss="2040")
    return make_signal(source, **{**fields, **kw})


def open_long(h, source):
    """A long that has filled (entry 2000) and then drifted to 1995: a small loss."""
    trade = h.open(make_signal(source))
    h.run(2005, 2000, 1995)
    trade.refresh_from_db()
    assert trade.status == "OPEN", trade.status
    return trade


class OneWayReversalTests(TestCase):
    def test_an_open_trade_is_closed_and_the_new_signal_is_traded(self):
        h, source = Harness(), make_source()
        old = open_long(h, source)

        new = h.open(short_signal(source))

        old.refresh_from_db()
        self.assertEqual((old.status, old.reason_code), ("CLOSED", "REVERSED"))
        self.assertLess(old.realized_pnl, 0)  # long from 2000 closed near 1995: the loss is recorded, not zero
        self.assertEqual(working(old, "SL") + working(old, "TP1") + working(old, "TP2"), [])
        self.assertEqual(async_to_sync(h.paper.position_amount)("ETHUSDT", old.id), D("0"))
        self.assertEqual((new.side, new.status), ("SELL", "PENDING_ENTRY"))

    def test_a_pending_entry_is_cancelled_and_the_new_signal_is_traded(self):
        h, source = Harness(), make_source()
        old = h.open(make_signal(source))  # limit buy at 2000, never reached
        self.assertEqual(old.status, "PENDING_ENTRY")

        new = h.open(short_signal(source))

        old.refresh_from_db()
        self.assertEqual((old.status, old.reason_code), ("CANCELLED", "REVERSED"))
        self.assertEqual(new.status, "PENDING_ENTRY")

    def test_the_monitor_never_overwrites_a_reversed_trade(self):
        h, source = Harness(), make_source()
        old = open_long(h, source)
        h.open(short_signal(source))
        h.run(1990, 1985, polls=3)
        old.refresh_from_db()
        self.assertEqual((old.status, old.reason_code), ("CLOSED", "REVERSED"))

    def test_the_same_signal_handled_twice_reverses_once(self):
        h, source = Harness(), make_source()
        old = open_long(h, source)
        signal = short_signal(source)
        h.open(signal)
        async_to_sync(h.executor.handle_signal)(signal.id)
        self.assertEqual(Trade.objects.filter(mode="PAPER").count(), 2)
        old.refresh_from_db()
        self.assertEqual(old.reason_code, "REVERSED")

    def test_a_signal_that_could_not_be_traded_keeps_the_old_trade(self):
        h, source = Harness(), make_source()
        old = open_long(h, source)

        refused = h.open(short_signal(source, stoploss="1900"))  # stop-loss on the wrong side of a short

        old.refresh_from_db()
        self.assertEqual(old.status, "OPEN")
        self.assertEqual((refused.status, refused.reason_code), ("CANCELLED", "INVALID_LEVELS"))
        self.assertNotEqual(refused.reason_code, "REVERSAL_IGNORED")
        self.assertIn("kept", refused.reason_message)

    def test_a_signal_that_can_no_longer_be_entered_keeps_the_old_trade(self):
        h, source = Harness(), make_source()
        old = open_long(h, source)
        h.run(1985)  # below the short's TP1 (1990): the new signal can no longer be entered

        refused = h.open(short_signal(source))

        old.refresh_from_db()
        refused.refresh_from_db()
        self.assertEqual(old.status, "OPEN")
        self.assertEqual((refused.status, refused.reason_code), ("CANCELLED", "PRICE_HIT_TP1"))
        self.assertEqual(refused.entry_price, D("2010"))  # the plan is kept for the Cancelled tab
        self.assertIn("kept", refused.reason_message)

    def test_only_this_sources_own_trades_are_closed(self):
        h = Harness()
        a, b = make_source(), make_source()
        old = open_long(h, a)

        other = h.open(short_signal(b))

        old.refresh_from_db()
        self.assertEqual(old.status, "OPEN")
        self.assertEqual(other.status, "PENDING_ENTRY")

    def test_a_signal_in_the_same_direction_is_not_a_reversal(self):
        h, source = Harness(), make_source()
        old = open_long(h, source)
        again = h.open(make_signal(source, entries=("1990", "1980"), stoploss="1950", targets=("2000", "2010")))
        old.refresh_from_db()
        self.assertEqual(old.status, "OPEN")
        self.assertEqual(again.side, "BUY")


class IgnoreReversalTests(TestCase):
    def test_the_opposite_signal_is_skipped_and_the_trade_carries_on(self):
        h, source = Harness(), make_source(reversal_mode="IGNORE")
        old = open_long(h, source)

        skipped = h.open(short_signal(source))

        old.refresh_from_db()
        self.assertEqual(old.status, "OPEN")
        self.assertEqual((skipped.status, skipped.reason_code), ("REJECTED", "REVERSAL_IGNORED"))
        self.assertEqual(len(working(old, "SL")), 1)  # still protected


class LiveModeTests(TestCase):
    """REAL sources: the one-live-trade-per-coin rule must let the reversal through, and only the reversal."""

    def setUp(self):
        self.h = Harness()
        cfg = TradingSettings(
            trading_enabled=True, forward_test_enabled=True, poll_seconds=5, fee_rate=D("0.0005"),
            api_key="k", api_secret="s", testnet=False,
        )
        gateways = {"LIVE": self.h.paper}  # the paper gateway stands in for Binance: orders are simulated
        self.executor = ExecutionService(gateways, cfg)
        self.executor.attach_monitor(MonitorService(gateways, cfg))

    def handle(self, signal) -> Trade:
        async_to_sync(self.executor.handle_signal)(signal.id)
        return Trade.objects.get(signal=signal, mode="LIVE")

    def test_the_live_trade_is_replaced_by_the_reversal(self):
        source = make_source(mode="REAL")
        old = self.handle(make_signal(source))
        self.assertEqual(old.status, "PENDING_ENTRY")

        new = self.handle(short_signal(source))

        old.refresh_from_db()
        self.assertEqual((old.status, old.reason_code), ("CANCELLED", "REVERSED"))
        self.assertEqual((new.side, new.status), ("SELL", "PENDING_ENTRY"))

    def test_another_sources_live_trade_is_untouched_and_the_signal_is_rejected_as_before(self):
        a, b = make_source(mode="REAL"), make_source(mode="REAL")
        old = self.handle(make_signal(a))

        rejected = self.handle(short_signal(b))

        old.refresh_from_db()
        self.assertEqual(old.status, "PENDING_ENTRY")
        self.assertEqual((rejected.status, rejected.reason_code), ("REJECTED", "ACTIVE_TRADE_EXISTS"))

    def test_ignore_keeps_the_live_trade(self):
        source = make_source(mode="REAL", reversal_mode="IGNORE")
        old = self.handle(make_signal(source))
        rejected = self.handle(short_signal(source))
        old.refresh_from_db()
        self.assertEqual(old.status, "PENDING_ENTRY")
        self.assertEqual((rejected.status, rejected.reason_code), ("REJECTED", "REVERSAL_IGNORED"))


class DomainTests(TestCase):
    def test_one_way_is_the_default_and_survives_a_snapshot(self):
        self.assertEqual(st.SourceStrategy().reversal, "ONE_WAY")
        source = make_source()
        self.assertIsNone(source.reversal_mode)  # a source stores no value of its own: "Use global"
        self.assertEqual(runtime.effective(source, "reversal_mode"), "ONE_WAY")  # the world's default
        self.assertEqual(st.SourceStrategy.from_dict({}).reversal, "ONE_WAY")  # trades saved before this feature
        self.assertEqual(st.SourceStrategy.from_dict(st.SourceStrategy(reversal="IGNORE").to_dict()).reversal, "IGNORE")

    def test_hedge_is_not_selectable_yet(self):
        self.assertEqual(st.validate(st.SourceStrategy(reversal="ONE_WAY")), [])
        self.assertEqual(st.validate(st.SourceStrategy(reversal="IGNORE")), [])
        self.assertIn("not available yet", " ".join(st.validate(st.SourceStrategy(reversal="HEDGE"))))
        self.assertTrue(st.validate(st.SourceStrategy(reversal="NONSENSE")))


class SettingsViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))
        self.source = make_source()
        self.url = reverse("source-settings", args=[self.source.pk])

    def post(self, **fields):
        data = {"entry_mode": "NEAREST", "sl_mode": "TP1_TOUCH", "sl_after_tp": "1", **fields}
        return self.client.post(self.url, data, follow=True)

    def saved(self) -> str:
        return TelegramSource.objects.get(pk=self.source.pk).reversal_mode

    def test_ignore_can_be_saved(self):
        self.post(reversal_mode="IGNORE")
        self.assertEqual(self.saved(), "IGNORE")

    def test_hedge_is_refused_and_nothing_changes(self):
        response = self.post(reversal_mode="HEDGE")
        self.assertIsNone(self.saved())  # still "Use global"
        self.assertContains(response, "choose one of ONE_WAY, IGNORE")  # the settings parser refuses it first

    def test_a_form_without_the_field_keeps_the_current_value(self):
        self.post(reversal_mode="IGNORE")
        self.post()
        self.assertEqual(self.saved(), "IGNORE")

    def test_the_sources_page_offers_the_modes_with_hedge_disabled(self):
        for slug in ("forward", "real"):
            response = self.client.get(reverse(f"{slug}-sources"))
            self.assertContains(response, 'value="ONE_WAY"')
            self.assertContains(response, 'value="HEDGE" disabled')
            self.assertContains(response, "Upcoming")
