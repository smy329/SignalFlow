"""Signal following strategy: trade the signal as posted, or its exact mirror image (Opposite to signal's direction)."""
from decimal import Decimal as D
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from runtime_settings import registry
from runtime_settings import service as runtime
from telegram_monitor.models import ParsedSignal, TelegramSource
from trading.domain import strategy as st
from trading.domain.planner import PRICE_ALREADY_HIT_SL, PRICE_ALREADY_HIT_TP1, plan_trade
from trading.domain.types import SignalInput, SymbolFilters
from trading.services import trade_repository as repo
from trading.strategy_tests.support import Harness, make_signal, make_source, working

OPPOSITE = st.SourceStrategy(follow=st.OPPOSITE_DIRECTION)
FILTERS = SymbolFilters(D("0.01"), D("0.001"), D("0.001"), D("1000"), D("5"))


def long_signal(**kw) -> SignalInput:
    fields = dict(symbol="ETHUSDT", side="BUY", entry=D("2000"), targets=(D("2020"), D("2040"), D("2060")), stoploss=D("1980"),
                  leverage=D("10"), margin=D("30"), margin_type="CROSSED", entries=(D("2000"), D("1990")))
    return SignalInput(**{**fields, **kw})


def plan(signal, market="2010", strategy=OPPOSITE):
    return plan_trade(st.apply_follow(signal, strategy.follow), FILTERS, max_leverage=50, market_price=D(market), strategy=strategy)


class MirrorImageTests(TestCase):
    def test_following_the_signal_changes_nothing(self):
        signal = long_signal()
        self.assertIs(st.apply_follow(signal, st.SIGNAL_DIRECTION), signal)

    def test_the_mirror_image_of_a_long_signal(self):
        mirrored = st.apply_follow(long_signal(), st.OPPOSITE_DIRECTION)
        self.assertEqual((mirrored.side, mirrored.targets, mirrored.stoploss), ("SELL", (D("1980"),), D("2020")))
        self.assertEqual((mirrored.entries, mirrored.entry_side), ((D("2000"), D("1990")), "BUY"))

    def test_the_mirror_image_of_a_short_signal(self):
        short = long_signal(side="SELL", entry=D("2010"), entries=(D("2010"), D("2020")), targets=(D("1990"), D("1970")), stoploss=D("2040"))
        mirrored = st.apply_follow(short, st.OPPOSITE_DIRECTION)
        self.assertEqual((mirrored.side, mirrored.targets, mirrored.stoploss, mirrored.entry_side), ("BUY", (D("2040"),), D("1990"), "SELL"))

    def test_the_trade_has_one_take_profit_for_the_whole_position_and_the_signals_tp1_as_stop_loss(self):
        result = plan(long_signal())
        self.assertTrue(result.success, result)
        p = result.plan
        self.assertEqual(p.side, "SELL")
        self.assertEqual((p.stop_loss.price, p.stop_loss.side), (D("2020"), "BUY"))
        self.assertEqual([(tp.price, tp.quantity, tp.role.value) for tp in p.take_profits], [(D("1980"), p.quantity, "TP1")])
        self.assertEqual(p.targets, (D("1980"),))

    def test_entries_are_picked_relative_to_the_signals_direction(self):
        entries = lambda mode: [e.price for e in plan(long_signal(), strategy=st.SourceStrategy(follow=st.OPPOSITE_DIRECTION, entry_mode=mode)).plan.entries]  # noqa: E731
        self.assertEqual(entries(st.NEAREST), [D("2000")])  # the long signal's own nearest entry, not the lowest
        self.assertEqual(entries(st.FARTHEST), [D("1990")])
        self.assertEqual(sorted(entries(st.BOTH)), [D("1990"), D("2000")])

    def test_the_entry_waits_at_the_signals_entry_price(self):
        below = plan(long_signal(), market="2010").plan  # market above the entry: the short triggers when price falls to it
        above = plan(long_signal(), market="1995").plan  # market below the entry: a limit sell waits for price to rise to it
        self.assertEqual((below.entries[0].kind.value, above.entries[0].kind.value), ("STOP_MARKET", "LIMIT"))

    def test_the_market_checks_use_the_mirrored_levels(self):
        self.assertEqual(plan(long_signal(), market="1975").error_code, PRICE_ALREADY_HIT_TP1)  # already at the signal's SL = our TP
        self.assertEqual(plan(long_signal(), market="2025").error_code, PRICE_ALREADY_HIT_SL)  # already above the signal's TP1 = our SL

    def test_a_signal_without_targets_is_left_alone_for_the_planner_to_reject(self):
        signal = long_signal(targets=())
        self.assertIs(st.apply_follow(signal, st.OPPOSITE_DIRECTION), signal)

    def test_the_strategy_round_trips_and_old_snapshots_follow_the_signal(self):
        self.assertEqual(st.SourceStrategy.from_dict(OPPOSITE.to_dict()).follow, st.OPPOSITE_DIRECTION)
        self.assertEqual(st.SourceStrategy.from_dict({"entry_mode": "NEAREST"}).follow, st.SIGNAL_DIRECTION)
        self.assertEqual(st.SourceStrategy.from_dict(None).follow, st.SIGNAL_DIRECTION)
        self.assertTrue(repo.is_opposite(OPPOSITE.to_dict()))
        self.assertFalse(repo.is_opposite({}))
        self.assertTrue(st.validate(st.SourceStrategy(follow="SIDEWAYS")))


class SettingsLayeringTests(TestCase):
    def test_real_and_forward_have_the_option_defaulting_to_the_signal_and_manual_does_not(self):
        for world in ("real", "forward"):
            defn = registry.BY_KEY[f"{world}.follow_mode"]
            self.assertEqual((defn.label, defn.default, runtime.get(f"{world}.follow_mode")), ("Signal following strategy", "SIGNAL_DIRECTION", "SIGNAL_DIRECTION"))
        self.assertNotIn("manual.follow_mode", registry.BY_KEY)

    def test_a_source_follows_its_world_unless_it_has_its_own_value(self):
        real, forward = make_source(mode=TelegramSource.Mode.REAL), make_source(mode=TelegramSource.Mode.FORWARD)
        own = make_source(follow_mode="OPPOSITE_DIRECTION")
        follow = lambda source: repo.strategy_from_source(source).follow  # noqa: E731
        self.assertEqual((follow(real), follow(forward), follow(own)), ("SIGNAL_DIRECTION",) * 2 + ("OPPOSITE_DIRECTION",))
        runtime.save_many({"real.follow_mode": "OPPOSITE_DIRECTION"})
        self.assertEqual((follow(real), follow(forward)), ("OPPOSITE_DIRECTION", "SIGNAL_DIRECTION"))  # Real only
        own.follow_mode = "SIGNAL_DIRECTION"
        self.assertEqual(follow(own), "SIGNAL_DIRECTION")  # its own value beats the world's

    def test_a_signal_is_stored_as_posted_and_traded_mirrored(self):
        source = make_source(follow_mode="OPPOSITE_DIRECTION")
        data = repo.load_signal(make_signal(source).id)
        self.assertEqual((data.input.side, data.strategy.follow), ("SELL", "OPPOSITE_DIRECTION"))
        self.assertEqual(ParsedSignal.objects.get(source=source).direction, "BUY")


class OppositeTradeFlowTests(TestCase):
    def setUp(self):
        self.h = Harness()
        self.source = make_source(follow_mode="OPPOSITE_DIRECTION")

    def test_a_long_signal_becomes_a_pending_short_with_the_mirrored_exits(self):
        trade = self.h.open(make_signal(self.source))
        self.assertEqual((trade.status, trade.side, trade.stoploss, [D(t) for t in trade.targets]), ("PENDING_ENTRY", "SELL", D("2020"), [D("1980")]))
        self.assertEqual(trade.strategy["follow"], "OPPOSITE_DIRECTION")
        self.assertEqual(trade.signal.direction, "BUY")

    def test_the_short_is_taken_when_price_falls_to_the_signals_entry_and_closes_at_the_signals_stop_loss(self):
        trade = self.h.open(make_signal(self.source))
        self.h.run(2005, 2000, 1995)  # the sell-stop at the signal's entry (2000) triggers
        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        self.h.run(1990, 1980, 1975)  # the signal's stop-loss price = our take-profit
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "TP_COMPLETED"))
        self.assertGreater(trade.realized_pnl, 0)

    def test_it_is_stopped_out_at_the_signals_tp1(self):
        trade = self.h.open(make_signal(self.source))
        self.h.run(2005, 2000, 1995, 2010, 2020, 2025)
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "SL_HIT"))
        self.assertLess(trade.realized_pnl, 0)

    def test_the_stop_loss_is_not_moved_when_price_nears_the_only_target(self):
        trade = self.h.open(make_signal(self.source))
        self.h.run(2005, 2000, 1995, 1985, 1982)  # touching "TP1" (our only take-profit) must not move the stop-loss to entry
        sl = working(trade, "SL")
        self.assertEqual([o.price for o in sl], [D("2020")])

    def test_a_new_signal_that_mirrors_to_the_other_side_reverses_the_active_trade(self):
        old = self.h.open(make_signal(self.source))  # a short (from a long signal)
        new = self.h.open(make_signal(self.source, side="SELL", entries=("2010", "2020"), targets=("1990", "1970"), stoploss="2040"))
        old.refresh_from_db()
        self.assertEqual((old.status, old.reason_code), ("CANCELLED", "REVERSED"))
        self.assertEqual((new.side, new.status), ("BUY", "PENDING_ENTRY"))

    def test_a_normal_source_is_unchanged(self):
        trade = self.h.open(make_signal(make_source()))
        self.assertEqual((trade.side, trade.stoploss, [D(t) for t in trade.targets]), ("BUY", D("1980"), [D("2020"), D("2040"), D("2060")]))


class OppositePagesTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))
        self.h = Harness()

    def test_flipped_trades_carry_the_opposite_marker_on_the_pages(self):
        flipped = self.h.open(make_signal(make_source(follow_mode="OPPOSITE_DIRECTION")))
        normal = self.h.open(make_signal(make_source(name="Normal")))
        rows = {r["id"]: r for r in self.client.get(reverse("forward-api-pending")).json()["rows"]}
        self.assertEqual((rows[flipped.id]["opposite"], rows[flipped.id]["side"]), (True, "SHORT"))
        self.assertFalse(rows[normal.id]["opposite"])
        self.h.run(2005, 2000, 1995)  # the flipped short fills
        with mock.patch("trading.views._enabled", return_value=True):  # the engine is not running in tests
            positions = {r["id"]: r for r in self.client.get(reverse("forward-api-positions")).json()["rows"]}
        self.assertTrue(positions[flipped.id]["opposite"])
        self.assertFalse(positions[normal.id]["opposite"])

    def test_the_sources_page_shows_and_saves_the_option(self):
        source = make_source()
        page = self.client.get(reverse("forward-sources"))
        self.assertContains(page, 'name="follow_mode"')
        self.assertEqual(page.context["world_globals"]["follow_mode"], "SIGNAL_DIRECTION")
        url = reverse("source-settings", args=[source.pk])
        self.client.post(url, {"follow_mode": "OPPOSITE_DIRECTION"})
        source.refresh_from_db()
        self.assertEqual(source.follow_mode, "OPPOSITE_DIRECTION")
        self.assertContains(self.client.get(reverse("forward-sources")), "Opposite to signals")
        self.client.post(url, {"follow_mode": "SIDEWAYS"})  # invalid: nothing changes
        source.refresh_from_db()
        self.assertEqual(source.follow_mode, "OPPOSITE_DIRECTION")
        self.client.post(url, {"follow_mode": ""})  # Use global
        source.refresh_from_db()
        self.assertIsNone(source.follow_mode)

    def test_the_settings_pages_offer_it_for_real_and_forward_only(self):
        for group, expected in ((registry.G_REAL, True), (registry.G_FORWARD, True), (registry.G_MANUAL, False)):
            page = self.client.get(reverse("settings-group", args=[registry.slug_of(group)]))
            self.assertEqual("Signal following strategy" in page.content.decode(), expected, group)
