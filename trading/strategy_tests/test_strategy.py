"""Pure rules: entry selection, take-profit allocation, stop-loss trigger, validation, planner."""
from decimal import Decimal as D

from django.test import SimpleTestCase

from trading.domain import strategy as st
from trading.domain.planner import plan_exits, plan_trade
from trading.domain.types import OrderRole, SignalInput, SymbolFilters

FILTERS = SymbolFilters(tick_size=D("0.01"), step_size=D("0.001"), min_qty=D("0.001"), max_qty=D("1000"), min_notional=D("5"))


def signal(side="BUY", entries=("2000", "1990"), targets=("2020", "2040", "2060", "2080"), stoploss="1980", margin="30"):
    prices = tuple(D(e) for e in entries)
    return SignalInput(
        symbol="ETHUSDT", side=side, entry=max(prices) if side == "BUY" else min(prices),
        targets=tuple(D(t) for t in targets), stoploss=D(stoploss), leverage=D(10), margin=D(margin),
        margin_type="CROSSED", entries=prices,
    )


class EntrySelectionTests(SimpleTestCase):
    entries = [D("1"), D("2"), D("3")]

    def test_long_and_short_pick_opposite_ends_for_nearest_and_farthest(self):
        self.assertEqual(st.select_entries("BUY", self.entries, st.NEAREST), [D("3")])
        self.assertEqual(st.select_entries("BUY", self.entries, st.FARTHEST), [D("1")])
        self.assertEqual(st.select_entries("SELL", self.entries, st.NEAREST), [D("1")])
        self.assertEqual(st.select_entries("SELL", self.entries, st.FARTHEST), [D("3")])

    def test_both_takes_only_the_lowest_and_the_highest_nearest_first(self):
        self.assertEqual(st.select_entries("BUY", self.entries, st.BOTH), [D("3"), D("1")])
        self.assertEqual(st.select_entries("SELL", self.entries, st.BOTH), [D("1"), D("3")])

    def test_a_single_listed_entry_is_one_entry_in_every_mode(self):
        for mode in st.ENTRY_MODES:
            self.assertEqual(st.select_entries("BUY", [D("5")], mode), [D("5")])


class TakeProfitPercentTests(SimpleTestCase):
    def test_nothing_configured_is_the_default_60_40(self):
        self.assertEqual(st.effective_tp_percents(3, [None] * 8), [60, 40, 0])
        self.assertEqual(st.effective_tp_percents(1, [None] * 8), [100])

    def test_exact_100_is_used_as_given(self):
        self.assertEqual(st.effective_tp_percents(4, [10, 40, 50]), [10, 40, 50, 0])

    def test_less_than_100_is_completed_by_the_next_target(self):
        self.assertEqual(st.effective_tp_percents(3, [30]), [30, 70, 0])
        self.assertEqual(st.effective_tp_percents(4, [None, 60]), [0, 60, 40, 0])

    def test_the_last_target_of_the_signal_closes_the_rest(self):
        self.assertEqual(st.effective_tp_percents(2, [10, 40, 50]), [10, 90])
        self.assertEqual(st.effective_tp_percents(1, [30]), [100])
        self.assertEqual(st.effective_tp_percents(2, [None, None, 100]), [0, 100])

    def test_only_tp2_at_100_closes_everything_at_tp2(self):
        self.assertEqual(st.effective_tp_percents(5, [None, 100]), [0, 100, 0, 0, 0])


class AllocationTests(SimpleTestCase):
    def test_slices_add_up_to_the_quantity_and_follow_the_percentages(self):
        slices = st.allocate_tp_quantities(D("1.000"), [D(1), D(2), D(3)], [10, 40, 50], D("0.001"), D("0.001"))
        self.assertEqual([s[0] for s in slices], [1, 2, 3])
        self.assertEqual([s[2] for s in slices], [D("0.100"), D("0.400"), D("0.500")])
        self.assertEqual(sum(s[2] for s in slices), D("1.000"))

    def test_a_slice_below_min_qty_is_merged_into_the_next_one(self):
        slices = st.allocate_tp_quantities(D("3"), [D(1), D(2), D(3)], [10, 40, 50], D("1"), D("1"))
        self.assertEqual([(s[0], s[2]) for s in slices], [(2, D("1")), (3, D("2"))])

    def test_a_too_small_last_slice_is_merged_into_the_previous_one(self):
        slices = st.allocate_tp_quantities(D("2"), [D(1), D(2)], [60, 40], D("1"), D("1"))
        self.assertEqual([(s[0], s[2]) for s in slices], [(1, D("1")), (2, D("1"))])
        slices = st.allocate_tp_quantities(D("1.5"), [D(1), D(2)], [90, 10], D("0.5"), D("1"))
        self.assertEqual([(s[0], s[2]) for s in slices], [(1, D("1.5"))])

    def test_zero_percent_targets_get_no_order(self):
        slices = st.allocate_tp_quantities(D("1"), [D(1), D(2), D(3)], [0, 100, 0], D("0.001"), D("0.001"))
        self.assertEqual([(s[0], s[2]) for s in slices], [(2, D("1"))])


class StopLossRuleTests(SimpleTestCase):
    def test_after_tp_uses_the_chosen_target_when_it_exists(self):
        s = st.SourceStrategy(sl_mode=st.SL_AFTER_FILLING_TP, sl_after_tp=3)
        self.assertEqual(st.sl_trigger_target(s, [1, 2, 3, 4]), 3)

    def test_after_tp_falls_back_to_the_next_then_the_highest_existing_target(self):
        s = st.SourceStrategy(sl_mode=st.SL_AFTER_FILLING_TP, sl_after_tp=3)
        self.assertEqual(st.sl_trigger_target(s, [1, 2, 4]), 4)
        self.assertEqual(st.sl_trigger_target(s, [1, 2]), 2)

    def test_touch_mode_has_no_target(self):
        self.assertIsNone(st.sl_trigger_target(st.SourceStrategy(), [1, 2]))


class ValidationTests(SimpleTestCase):
    def test_default_strategy_is_valid(self):
        self.assertEqual(st.validate(st.SourceStrategy()), [])

    def test_total_over_100_is_refused_with_the_total_in_the_message(self):
        errors = st.validate(st.SourceStrategy(tp_percents=[60, 50]))
        self.assertTrue(any("110%" in e and "more than 100%" in e for e in errors))

    def test_percent_out_of_range_and_unknown_modes_are_refused(self):
        self.assertTrue(st.validate(st.SourceStrategy(tp_percents=[101])))
        self.assertTrue(st.validate(st.SourceStrategy(entry_mode="WHATEVER")))
        self.assertTrue(st.validate(st.SourceStrategy(sl_mode="WHATEVER")))

    def test_after_tp_needs_a_tp_that_closes_something_when_custom_percentages_are_set(self):
        self.assertTrue(st.validate(st.SourceStrategy(tp_percents=[50, 50], sl_mode=st.SL_AFTER_FILLING_TP, sl_after_tp=3)))
        self.assertTrue(st.validate(st.SourceStrategy(sl_mode=st.SL_AFTER_FILLING_TP, sl_after_tp=9)))
        self.assertEqual(st.validate(st.SourceStrategy(tp_percents=[50, 50], sl_mode=st.SL_AFTER_FILLING_TP, sl_after_tp=2)), [])
        self.assertEqual(st.validate(st.SourceStrategy(sl_mode=st.SL_AFTER_FILLING_TP, sl_after_tp=2)), [])  # default 60/40

    def test_round_trip_through_a_dict_and_tolerance_for_old_trades(self):
        s = st.SourceStrategy(entry_mode=st.BOTH, tp_percents=[10, 90], sl_mode=st.SL_AFTER_FILLING_TP, sl_after_tp=2)
        self.assertEqual(st.SourceStrategy.from_dict(s.to_dict()), s)
        self.assertEqual(st.SourceStrategy.from_dict({}), st.DEFAULT_STRATEGY)


class PlannerTests(SimpleTestCase):
    def plan(self, sig=None, **strategy):
        return plan_trade(sig or signal(), FILTERS, max_leverage=50, market_price=D("2010"), strategy=st.SourceStrategy(**strategy))

    def test_default_strategy_keeps_the_original_behaviour(self):
        plan = self.plan().plan
        self.assertEqual([e.price for e in plan.entries], [D("2000")])  # nearest = highest for a long
        self.assertEqual([(t.role, t.quantity) for t in plan.take_profits], [(OrderRole.TP1, D("0.090")), (OrderRole.TP2, D("0.060"))])
        self.assertEqual(plan.targets, (D("2020"), D("2040"), D("2060"), D("2080")))

    def test_farthest_long_takes_the_lowest_entry(self):
        self.assertEqual([e.price for e in self.plan(entry_mode=st.FARTHEST).plan.entries], [D("1990")])

    def test_both_entries_split_the_margin_equally_and_exits_cover_the_total(self):
        plan = self.plan(entry_mode=st.BOTH, tp_percents=[10, 40, 50]).plan
        self.assertEqual([(e.role, e.price, e.quantity) for e in plan.entries],
                         [(OrderRole.ENTRY, D("2000"), D("0.075")), (OrderRole.ENTRY2, D("1990"), D("0.075"))])
        self.assertEqual(plan.quantity, D("0.150"))
        self.assertEqual(plan.stop_loss.quantity, D("0.150"))
        self.assertEqual([(t.role.value, t.quantity) for t in plan.take_profits],
                         [("TP1", D("0.015")), ("TP2", D("0.060")), ("TP3", D("0.075"))])
        self.assertTrue(all(e.guard_tp == D("2020") and e.guard_sl == D("1980") for e in plan.entries))

    def test_an_entry_that_is_too_small_is_skipped_and_the_other_gets_the_full_margin(self):
        tiny = SymbolFilters(D("0.01"), D("0.001"), D("0.001"), D("1000"), D("6"))  # 15 USDT x10 = 150 each: fine...
        sig = signal(entries=("2000", "1990"), margin="1.2")  # 0.6 x10 = 6 USDT each: 0.003 ETH = 6.0 / 5.97
        result = plan_trade(sig, tiny, max_leverage=50, market_price=D("2010"), strategy=st.SourceStrategy(entry_mode=st.BOTH))
        # 1990 gives 0.003 * 1990 = 5.97 < 6 (skipped); 2000 gives 6.0 (kept) and then uses the full 1.2 margin
        self.assertEqual([e.price for e in result.plan.entries], [D("2000")])
        self.assertEqual(result.plan.quantity, D("0.006"))

    def test_when_neither_half_fits_the_nearest_entry_alone_gets_all_the_margin(self):
        strict = SymbolFilters(D("0.01"), D("0.001"), D("0.001"), D("1000"), D("10"))
        sig = signal(entries=("2000", "1990"), margin="1.5")  # half = 7.5 USDT notional < 10; full = 15
        result = plan_trade(sig, strict, max_leverage=50, market_price=D("2010"), strategy=st.SourceStrategy(entry_mode=st.BOTH))
        self.assertEqual([e.price for e in result.plan.entries], [D("2000")])

    def test_everything_too_small_is_rejected(self):
        result = self.plan(signal(margin="0.1"), entry_mode=st.BOTH)
        self.assertFalse(result.success)

    def test_entries_that_round_to_the_same_price_collapse_into_one_with_the_full_margin(self):
        sig = signal(entries=("2000.001", "2000.002"))
        plan = self.plan(sig, entry_mode=st.BOTH).plan
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(plan.quantity, D("0.150"))

    def test_an_entry_beyond_the_stop_loss_is_skipped_not_the_whole_signal(self):
        sig = signal(entries=("2000", "1970"), stoploss="1980")
        plan = self.plan(sig, entry_mode=st.BOTH).plan
        self.assertEqual([e.price for e in plan.entries], [D("2000")])

    def test_market_already_at_tp1_is_still_rejected(self):
        result = plan_trade(signal(), FILTERS, max_leverage=50, market_price=D("2025"), strategy=st.DEFAULT_STRATEGY)
        self.assertEqual(result.error_code, "PRICE_ALREADY_HIT_TP1")

    def test_targets_after_tp2_that_turn_around_are_dropped(self):
        plan = self.plan(signal(targets=("2020", "2040", "2060", "2050", "2100"))).plan
        self.assertEqual(plan.targets, (D("2020"), D("2040"), D("2060")))

    def test_at_most_eight_targets(self):
        targets = tuple(str(2020 + 20 * i) for i in range(10))
        self.assertEqual(len(self.plan(signal(targets=targets)).plan.targets), 8)

    def test_single_signal_entry_without_the_entries_list_still_plans(self):
        sig = SignalInput("ETHUSDT", "BUY", D("2000"), (D("2020"), D("2040")), D("1980"), D(10), D(30), "CROSSED")
        self.assertEqual([e.price for e in self.plan(sig, entry_mode=st.BOTH).plan.entries], [D("2000")])

    def test_plan_exits_for_a_resized_position_uses_the_same_percentages(self):
        _, tps = plan_exits("BUY", D("0.075"), D("1980"), [D("2020"), D("2040"), D("2060")], FILTERS, [10, 40, 50])
        self.assertEqual(sum(t.quantity for t in tps), D("0.075"))
        self.assertEqual([t.role.value for t in tps], ["TP1", "TP2", "TP3"])
