"""A Binance network hiccup is retried quietly on the next poll instead of logged as an ERROR traceback."""
from unittest.mock import patch

import aiohttp
from asgiref.sync import async_to_sync
from django.test import TestCase

from trading.strategy_tests.support import Harness, make_signal, make_source


class NetworkErrorTests(TestCase):
    def _harness_with_open_trade(self):
        h = Harness()
        trade = h.open(make_signal(make_source()))
        return h, trade

    def _poll_failing_with(self, h, exc):
        with patch.object(h.monitor, "_step", side_effect=exc):
            with self.assertLogs("trading.services.monitor_service", level="WARNING") as logs:
                async_to_sync(h.monitor.poll_once)()
        return logs

    def test_a_timeout_is_a_one_line_warning_not_an_error(self):
        h, trade = self._harness_with_open_trade()
        logs = self._poll_failing_with(h, TimeoutError())
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIn("will retry", logs.output[0])

    def test_aiohttp_client_errors_are_treated_the_same_way(self):
        h, trade = self._harness_with_open_trade()
        logs = self._poll_failing_with(h, aiohttp.ClientConnectionError("reset"))
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])

    def test_a_real_bug_is_still_logged_as_an_error(self):
        h, trade = self._harness_with_open_trade()
        with patch.object(h.monitor, "_step", side_effect=ValueError("bug")):
            with self.assertLogs("trading.services.monitor_service", level="ERROR"):
                async_to_sync(h.monitor.poll_once)()
