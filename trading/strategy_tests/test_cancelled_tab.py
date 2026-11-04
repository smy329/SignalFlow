"""The Cancelled tab lists every trade that never traded: cancelled entries, rejected signals and
trades that failed while placing."""
from django.test import TestCase

from trading.models import Trade
from trading.services import report_service
from trading.services import trade_repository as repo
from trading.strategy_tests.support import Harness, make_signal, make_source


class CancelledTabTests(TestCase):
    def test_cancelled_rejected_and_failed_all_show_with_their_state(self):
        h, source = Harness(), make_source()
        cancelled = Harness("2025").open(make_signal(make_source()))  # TP1 already reached: never placed
        rejected = h.open(make_signal(source, stoploss="2050"))  # stop-loss above a long entry: invalid plan
        signal = make_signal(source)
        failed = Trade.objects.create(
            signal=signal, mode="PAPER", symbol="ETHUSDT", side="BUY", status="FAILED",
            reason_code="ORDER_PLACEMENT_FAILED", reason_message="boom",
        )

        rows = repo.trade_rows("PAPER", repo.UNTRADED_STATUSES)
        rendered = report_service.cancelled_rows(rows)
        by_id = {row["id"]: row for row in rendered}

        self.assertEqual(
            {by_id[t.id]["status"] for t in (cancelled, rejected, failed)}, {"CANCELLED", "REJECTED", "FAILED"}
        )
        self.assertEqual(by_id[cancelled.id]["result"], "PRICE_HIT_TP1")
        self.assertEqual(by_id[rejected.id]["result"], "INVALID_LEVELS")
        self.assertEqual(by_id[failed.id]["result"], "ORDER_PLACEMENT_FAILED")
        self.assertIsNotNone(by_id[failed.id]["closed_at"])  # finished_at stands in for a missing closed_at

    def test_a_filled_and_closed_trade_is_not_in_the_cancelled_tab(self):
        h, source = Harness(), make_source()
        trade = h.open(make_signal(source))
        h.run(2000, 2020)  # entry fills, TP1 closes it
        trade.refresh_from_db()
        self.assertEqual(trade.status, "CLOSED")

        ids = {row["id"] for row in repo.trade_rows("PAPER", repo.UNTRADED_STATUSES)}
        self.assertNotIn(trade.id, ids)

    def test_a_single_status_still_returns_only_that_status(self):
        Harness("2025").open(make_signal(make_source()))  # CANCELLED
        Harness().open(make_signal(make_source(), stoploss="2050"))  # REJECTED

        statuses = {row["status"] for row in repo.trade_rows("PAPER", repo.ST_CANCELLED)}
        self.assertEqual(statuses, {"CANCELLED"})
