"""End to end on the forward-test gateway: signal -> planned orders -> monitor reacts to scripted prices."""
from decimal import Decimal as D

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from trading.domain import strategy as st
from trading.exchange.paper_gateway import PaperGateway
from trading.models import Trade
from trading.services.engine import TradingEngine
from trading.strategy_tests.support import Harness, make_signal, make_source, orders, working


def qty_sum(rows):
    return sum((o.quantity for o in rows), D("0"))


def tp_rows(trade, status=None):
    rows = [o for o in trade.orders.order_by("id") if o.role.startswith("TP")]
    return [o for o in rows if status is None or o.status == status]


class DefaultStrategyTests(TestCase):
    """A source nobody touched trades exactly as before: one entry, 60/40, SL to entry at TP1."""

    def test_full_life_of_a_default_trade(self):
        h = Harness()
        trade = h.open(make_signal(make_source()))
        self.assertEqual(trade.status, "PENDING_ENTRY")
        self.assertEqual([o.role for o in trade.orders.order_by("id")], ["ENTRY", "SL", "TP1", "TP2"])
        self.assertEqual(trade.orders.get(role="ENTRY").price, D("2000"))
        self.assertEqual([o.quantity for o in tp_rows(trade)], [D("0.090"), D("0.060")])

        h.run(2005, 2000)
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.filled_quantity, trade.avg_entry_price), ("OPEN", D("0.150"), D("2000")))

        h.run(2020)  # TP1 fills and the SL moves to the entry
        self.assertEqual(len(tp_rows(trade, "FILLED")), 1)
        live_sl = working(trade, "SL")
        self.assertEqual([(o.price, o.quantity) for o in live_sl], [(D("2000"), D("0.060"))])

        h.run(2040)
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "TP_COMPLETED"))
        self.assertGreater(trade.realized_pnl, 0)

    def test_the_strategy_is_snapshotted_on_the_trade(self):
        source = make_source(entry_mode="BOTH", tp1_pct=10, tp2_pct=90)
        trade = Harness().open(make_signal(source))
        snapshot = st.SourceStrategy.from_dict(trade.strategy)
        self.assertEqual((snapshot.entry_mode, snapshot.tp_percents[:2]), ("BOTH", (10, 90)))
        self.assertEqual([D(t) for t in trade.targets], [D("2020"), D("2040"), D("2060")])
        self.assertEqual([D(p) for p in trade.entry_prices], [D("2000"), D("1990")])

    def test_a_settings_change_does_not_touch_a_trade_that_already_started(self):
        source = make_source()
        h = Harness()
        trade = h.open(make_signal(source))
        source.entry_mode, source.tp1_pct, source.tp2_pct = "BOTH", 100, None
        source.save()
        h.run(2000)
        trade.refresh_from_db()
        self.assertEqual(st.SourceStrategy.from_dict(trade.strategy).entry_mode, "NEAREST")
        self.assertEqual([o.quantity for o in tp_rows(trade)], [D("0.090"), D("0.060")])


class EntryStrategyTests(TestCase):
    def test_farthest_entry_places_the_lowest_entry_for_a_long(self):
        trade = Harness().open(make_signal(make_source(entry_mode="FARTHEST")))
        self.assertEqual([o.price for o in orders(trade, "ENTRY")], [D("1990")])
        self.assertEqual(orders(trade, "ENTRY2"), [])

    def test_a_short_takes_the_opposite_ends(self):
        sig = dict(side="SELL", entries=("2020", "2030"), targets=("1990", "1970"), stoploss="2050")
        near = Harness("2010").open(make_signal(make_source(entry_mode="NEAREST"), **sig))
        far = Harness("2010").open(make_signal(make_source(entry_mode="FARTHEST"), **sig))
        self.assertEqual(near.orders.get(role="ENTRY").price, D("2020"))
        self.assertEqual(far.orders.get(role="ENTRY").price, D("2030"))

    def test_both_places_two_limit_orders_with_the_margin_split_equally(self):
        trade = Harness().open(make_signal(make_source(entry_mode="BOTH")))
        entry, entry2 = orders(trade, "ENTRY")[0], orders(trade, "ENTRY2")[0]
        self.assertEqual((entry.price, entry.quantity, entry.kind), (D("2000"), D("0.075"), "LIMIT"))
        self.assertEqual((entry2.price, entry2.quantity, entry2.kind), (D("1990"), D("0.075"), "LIMIT"))
        self.assertEqual(working(trade, "SL")[0].quantity, D("0.150"))
        self.assertEqual(qty_sum(tp_rows(trade)), D("0.150"))

    def test_one_fill_protects_only_what_filled_and_the_second_fill_grows_the_exits(self):
        h = Harness()
        trade = h.open(make_signal(make_source(entry_mode="BOTH", tp1_pct=10, tp2_pct=40, tp3_pct=50)))

        h.run(2005, 2000)  # only the first entry fills
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.filled_quantity, trade.avg_entry_price), ("OPEN", D("0.075"), D("2000")))
        self.assertEqual(working(trade, "SL")[0].quantity, D("0.075"))
        live_tps = [o for o in tp_rows(trade) if o.status == "NEW"]
        self.assertEqual(qty_sum(live_tps), D("0.075"))
        self.assertEqual(len(working(trade, "ENTRY2")), 1)

        h.run(1995, 1990)  # the second one fills later
        trade.refresh_from_db()
        self.assertEqual((trade.filled_quantity, trade.avg_entry_price), (D("0.150"), D("1995")))
        self.assertEqual([o.quantity for o in working(trade, "SL")], [D("0.150")])
        self.assertEqual(qty_sum([o for o in tp_rows(trade) if o.status == "NEW"]), D("0.150"))
        self.assertEqual([o.quantity for o in tp_rows(trade, "NEW")], [D("0.015"), D("0.060"), D("0.075")])

    def test_price_reaching_tp1_cancels_the_second_entry_that_never_filled(self):
        h = Harness()
        trade = h.open(make_signal(make_source(entry_mode="BOTH")))
        h.run(2000)
        h.run(2020)
        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(orders(trade, "ENTRY2")[0].status, "CANCELED")
        self.assertEqual(working(trade, "SL")[0].price, D("2000"))  # and the SL moved to entry for the first leg

    def test_neither_entry_filled_when_price_runs_to_tp1_cancels_the_trade(self):
        h = Harness()
        trade = h.open(make_signal(make_source(entry_mode="BOTH")))
        h.run(2015, 2020)
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CANCELLED", "PRICE_HIT_TP1"))
        self.assertTrue(all(o.status == "CANCELED" for o in trade.orders.all()))

    def test_both_filled_then_the_stop_loss_closes_everything_with_both_fills_counted(self):
        h = Harness()
        trade = h.open(make_signal(make_source(entry_mode="BOTH")))
        h.run(2000, 1990)
        h.run(1980)
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "SL_HIT"))
        self.assertLess(trade.realized_pnl, 0)
        self.assertEqual(sum(o.executed_quantity for o in trade.orders.filter(role__startswith="ENTRY")), D("0.150"))

    def test_the_unfilled_second_entry_is_cancelled_when_the_sl_moves_to_entry(self):
        """TP1 level touched while the second entry is still waiting, for a source whose
        entries sit far apart: the guard of the entry itself is not what cancels it here."""
        h = Harness()
        trade = h.open(make_signal(make_source(entry_mode="BOTH", sl_mode="AFTER_FILLING_TP", sl_after_tp=1, tp1_pct=50, tp2_pct=50)))
        h.run(2000)
        h.run(2020)  # TP1 touched: entry 2 voided; TP1 filled: SL moves
        trade.refresh_from_db()
        self.assertEqual(orders(trade, "ENTRY2")[0].status, "CANCELED")
        self.assertEqual(working(trade, "SL")[0].price, D("2000"))


class TakeProfitStrategyTests(TestCase):
    def test_custom_percentages_place_one_order_per_target_with_the_requested_sizes(self):
        trade = Harness().open(make_signal(make_source(tp1_pct=10, tp2_pct=40, tp3_pct=50)))
        self.assertEqual([(o.role, o.quantity, o.price) for o in tp_rows(trade)],
                         [("TP1", D("0.015"), D("2020")), ("TP2", D("0.060"), D("2040")), ("TP3", D("0.075"), D("2060"))])

    def test_the_last_target_closes_what_is_left_when_the_signal_has_fewer_targets(self):
        trade = Harness().open(make_signal(make_source(tp1_pct=10, tp2_pct=40, tp3_pct=50), targets=("2020", "2040")))
        self.assertEqual([(o.role, o.quantity) for o in tp_rows(trade)], [("TP1", D("0.015")), ("TP2", D("0.135"))])

    def test_only_tp2_at_100_percent_closes_everything_at_tp2(self):
        h = Harness()
        trade = h.open(make_signal(make_source(tp2_pct=100)))
        self.assertEqual([(o.role, o.quantity) for o in tp_rows(trade)], [("TP2", D("0.150"))])
        h.run(2000, 2040)
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "TP_COMPLETED"))

    def test_percentages_below_100_are_completed_by_the_next_target(self):
        trade = Harness().open(make_signal(make_source(tp1_pct=30)))
        self.assertEqual([(o.role, o.quantity) for o in tp_rows(trade)], [("TP1", D("0.045")), ("TP2", D("0.105"))])

    def test_eight_targets_can_be_used(self):
        targets = tuple(str(2020 + 20 * i) for i in range(8))
        source = make_source(**{f"tp{n}_pct": 10 for n in range(1, 8)}, tp8_pct=30)
        trade = Harness().open(make_signal(source, targets=targets))
        self.assertEqual([o.role for o in tp_rows(trade)], [f"TP{n}" for n in range(1, 9)])
        self.assertEqual(qty_sum(tp_rows(trade)), D("0.150"))


class StopLossStrategyTests(TestCase):
    def test_touching_tp1_moves_the_sl_even_when_tp1_closes_nothing(self):
        h = Harness()
        trade = h.open(make_signal(make_source(tp2_pct=100)))
        h.run(2000)
        self.assertEqual(working(trade, "SL")[0].price, D("1980"))
        h.run(2020)  # TP1 level touched, no TP order exists there
        trade.refresh_from_db()
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(tp_rows(trade, "FILLED"), [])
        self.assertEqual([(o.price, o.quantity) for o in working(trade, "SL")], [(D("2000"), D("0.150"))])
        h.run(2040)
        trade.refresh_from_db()
        self.assertEqual(trade.reason_code, "TP_COMPLETED")

    def test_after_tp_waits_for_the_chosen_target_to_fill(self):
        h = Harness()
        source = make_source(tp1_pct=10, tp2_pct=40, tp3_pct=50, sl_mode="AFTER_FILLING_TP", sl_after_tp=2)
        trade = h.open(make_signal(source))
        h.run(2000)
        h.run(2020)  # TP1 filled: not yet
        self.assertEqual(working(trade, "SL")[0].price, D("1980"))
        h.run(2040)  # TP2 filled: now
        self.assertEqual([(o.price, o.quantity) for o in working(trade, "SL")], [(D("2000"), D("0.075"))])
        h.run(2060)
        trade.refresh_from_db()
        self.assertEqual(trade.reason_code, "TP_COMPLETED")

    def test_after_tp_with_a_target_the_signal_does_not_have_uses_the_last_one(self):
        h = Harness()
        source = make_source(tp1_pct=50, tp2_pct=50, sl_mode="AFTER_FILLING_TP", sl_after_tp=2)
        trade = h.open(make_signal(source, targets=("2020", "2040", "2060")))
        h.run(2000, 2020)
        self.assertEqual(working(trade, "SL")[0].price, D("1980"))

    def test_sl_at_entry_hit_after_the_move(self):
        h = Harness()
        trade = h.open(make_signal(make_source()))
        h.run(2000, 2020)
        h.run(2000)  # back to entry: the moved SL fills
        trade.refresh_from_db()
        self.assertEqual((trade.status, trade.reason_code), ("CLOSED", "SL_AT_ENTRY_HIT"))


class RestoreTests(TestCase):
    def test_restart_restores_both_entries_with_their_guard_levels(self):
        h = Harness()
        trade = h.open(make_signal(make_source(entry_mode="BOTH")))
        Trade.objects.filter(pk=trade.pk).update(status="PENDING_ENTRY")
        engine = TradingEngine()
        engine._market = h.market
        fresh = PaperGateway(h.market)
        async_to_sync(engine._restore_paper_state)(fresh)
        entries = [o for o in fresh._orders.values() if o.spec.role.value.startswith("ENTRY")]
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(o.spec.guard_tp == D("2020.00") and o.spec.guard_sl == D("1980.00") for o in entries))
        # and a restored entry still voids when price runs to TP1
        h.market.clock = int(timezone.now().timestamp() * 1000) + 60_000  # restored orders carry their DB timestamps
        h.market.push(2025)
        async_to_sync(fresh.sync_market)({"ETHUSDT"})
        self.assertTrue(all(o.status == "CANCELED" for o in entries))
