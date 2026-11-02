"""Manual Trade, signals without a stop-loss: SL options 1:1 with each TP (maths, analyze, one-click placing)."""
import json
from decimal import Decimal as D
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from runtime_settings import service as runtime
from telegram_monitor.models import ParsedSignal
from trading.domain.tp_stoploss import sl_options
from trading.models import Trade
from trading.services import manual_service as ms
from trading.strategy_tests.support import Harness
from trading.strategy_tests.test_manual import OPTIONS, engine_stub, opts

XAN = "$XAN | Long\n\nEntry: 0.0078-0.0080\nTARGETS:\n\n0.0086\n0.010\n0.013\n0.017\nNfa. Manage your risk and size every position carefully."
LONG_NO_SL = "$ETH | Long\n\nEntry: 1990-2000\nTARGETS:\n\n2020\n2040\n2060\nNfa. Manage your risk and size every position carefully."
SHORT_NO_SL = "$ETH | Short\n\nEntry: 2010-2020\nTARGETS:\n\n1990\n1970\n1950\nNfa. Manage your risk and size every position carefully."
LONG_WITH_SL = "$ETH | Long\n\nEntry: 1990-2000\nTARGETS:\n\n2020\n2040\n2060\nSTOPLOSS: 1980\nNfa. Manage your risk and size every position carefully."
EIGHT_TPS = "$ETH | Long\n\nEntry: 2000\nTARGETS:\n\n2010\n2020\n2030\n2040\n2050\n2060\n2070\n2080\nNfa. Manage your risk and size every position carefully."


class MathTests(TestCase):
    def test_long_options_mirror_each_target_below_the_nearest_entry(self):
        out = sl_options("BUY", [D("0.0078"), D("0.0080")], [D("0.0086"), D("0.0100"), D("0.0130"), D("0.0170")])
        self.assertEqual([o.sl_price for o in out[:3]], [D("0.0074"), D("0.0060"), D("0.0030")])
        self.assertEqual([o.percent for o in out[:3]], [D("7.5"), D("25"), D("62.5")])
        self.assertTrue(all(o.usable for o in out[:3]))
        self.assertEqual(out[3].reason, "SL would be zero or negative")  # TP4 is more than 100% above the entry

    def test_short_options_mirror_each_target_above_the_nearest_entry(self):
        out = sl_options("SELL", [D("2010"), D("2020")], [D("1990"), D("1970")])
        self.assertEqual([o.sl_price for o in out], [D("2030"), D("2050")])  # nearest entry of a short = lowest

    def test_a_short_can_always_use_every_target_that_is_below_the_entry(self):
        self.assertTrue(all(o.usable for o in sl_options("SELL", [D("1")], [D("0.5"), D("0.1"), D("0.0001")])))

    def test_one_option_per_target_whatever_the_count(self):
        self.assertEqual(len(sl_options("BUY", [D("2000")], [D(2000 + 10 * i) for i in range(1, 9)])), 8)

    def test_a_target_on_the_wrong_side_of_the_entry_cannot_be_used(self):
        out = sl_options("BUY", [D("2000")], [D("1990"), D("2010")])
        self.assertEqual((out[0].usable, out[1].usable), (False, True))


class AnalyzeAndPlaceTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))

    def post(self, name, **body):
        return self.client.post(reverse(name), json.dumps(body), content_type="application/json")

    def body(self, text=LONG_NO_SL, **extra):
        return {"text": text, "mode": "forward", "options": OPTIONS, **extra}

    # -- analyze ------------------------------------------------------------------

    def test_analyze_lists_one_option_per_target_with_server_side_prices(self):
        data = self.post("manual-analyze", **self.body(text=XAN)).json()
        self.assertTrue(data["needs_stoploss"])
        self.assertEqual((data["symbol"], data["side"], data["entry"]), ("XANUSDT", "LONG", "0.008"))  # nearest entry, whatever the Entry mode
        self.assertEqual([(o["tp"], o["sl_price"], o["percent"], o["usable"]) for o in data["options"]], [
            (1, "0.0074", "7.50", True), (2, "0.006", "25.00", True), (3, "0.003", "62.50", True), (4, "-0.001", "112.50", False)])
        self.assertEqual(data["options"][3]["reason"], "SL would be zero or negative")

    def test_the_entry_mode_does_not_change_the_options(self):
        both = self.post("manual-analyze", **self.body(options={**OPTIONS, "entry_mode": "BOTH"})).json()
        far = self.post("manual-analyze", **self.body(options={**OPTIONS, "entry_mode": "FARTHEST"})).json()
        self.assertEqual(both["options"], far["options"])
        self.assertEqual(both["options"][0]["sl_price"], "1980")  # nearest entry 2000, TP1 2020

    def test_eight_targets_give_eight_options(self):
        self.assertEqual(len(self.post("manual-analyze", **self.body(text=EIGHT_TPS)).json()["options"]), 8)

    def test_a_signal_with_a_stop_loss_needs_no_choice(self):
        self.assertEqual(self.post("manual-analyze", **self.body(text=LONG_WITH_SL)).json(), {"ok": True, "needs_stoploss": False})

    def test_analyze_refuses_text_that_is_not_a_signal_and_stores_nothing(self):
        bad = self.post("manual-analyze", **self.body(text="hello"))
        self.assertEqual(bad.status_code, 400)
        self.assertIn("text", bad.json()["fields"])
        self.assertEqual(self.post("manual-analyze", **self.body(text="  ")).status_code, 400)
        self.assertEqual(ParsedSignal.objects.count(), 0)

    def test_analyze_requires_login(self):
        self.client.logout()
        self.assertEqual(self.post("manual-analyze", **self.body()).status_code, 302)

    # -- placing ------------------------------------------------------------------

    def test_without_a_choice_a_signal_with_no_stop_loss_is_refused(self):
        with mock.patch.object(ms, "engine", engine_stub()):
            response = self.post("manual-submit", **self.body())
        self.assertEqual(response.status_code, 400)
        self.assertIn("no stop-loss", response.json()["message"])
        self.assertEqual(ParsedSignal.objects.count(), 0)

    def test_a_forward_click_places_the_trade_with_the_stop_loss_worked_out_by_the_server(self):
        h = Harness()
        with mock.patch.object(ms, "engine", engine_stub()):
            data = self.post("manual-submit", **self.body(sl_tp=2)).json()
        self.assertTrue(data["ok"])
        self.assertEqual(ParsedSignal.objects.get(pk=data["signal_id"]).stoploss, D("1960"))  # nearest entry 2000, TP2 2040
        async_to_sync(h.executor.handle_signal)(data["signal_id"])
        self.assertEqual(Trade.objects.get(signal_id=data["signal_id"]).stoploss, D("1960"))

    def test_a_short_places_with_the_stop_loss_above_the_entry(self):
        with mock.patch.object(ms, "engine", engine_stub()):
            data = self.post("manual-submit", **self.body(text=SHORT_NO_SL, sl_tp=1)).json()
        self.assertEqual(ParsedSignal.objects.get(pk=data["signal_id"]).stoploss, D("2030"))

    def test_a_real_click_places_without_the_confirmation_dialog(self):
        runtime.save_many({"trading.real_enabled": "true"})
        with mock.patch.object(ms, "engine", engine_stub()):
            data = self.post("manual-submit", **self.body(mode="real", sl_tp=1)).json()
        self.assertTrue(data["ok"])
        self.assertEqual(ParsedSignal.objects.get(pk=data["signal_id"]).stoploss, D("1980"))

    def test_a_stop_loss_in_the_text_always_wins_and_a_real_trade_keeps_its_confirmation(self):
        with mock.patch.object(ms, "engine", engine_stub()):
            real = self.post("manual-submit", **self.body(text=LONG_WITH_SL, mode="real", sl_tp=1))
            fwd = self.post("manual-submit", **self.body(text=LONG_WITH_SL, sl_tp=3)).json()
        self.assertEqual(real.status_code, 400)  # sl_tp must not be a way around the confirmation
        self.assertEqual(ParsedSignal.objects.get(pk=fwd["signal_id"]).stoploss, D("1980"))  # the text's own SL

    def test_options_that_cannot_work_are_refused_by_the_server(self):
        with mock.patch.object(ms, "engine", engine_stub()):
            impossible = self.post("manual-submit", **self.body(text=XAN, sl_tp=4))
            missing = self.post("manual-submit", **self.body(sl_tp=9))
            for bad in (0, -1, "2", True, 1.5):
                self.assertEqual(self.post("manual-submit", **self.body(sl_tp=bad)).status_code, 400, bad)
        self.assertEqual((impossible.status_code, missing.status_code), (400, 400))
        self.assertIn("zero or negative", impossible.json()["message"])
        self.assertEqual(ParsedSignal.objects.count(), 0)

    def test_telegram_signals_without_a_stop_loss_are_still_refused(self):
        from telegram_monitor.services.signal_parser import NO_STOPLOSS, parse_signal
        result = parse_signal(LONG_NO_SL, "chan", leverage=10, margin=3, margin_type="CROSSED")
        self.assertEqual((result.success, result.error_code), (False, NO_STOPLOSS))
        with_flag = parse_signal(LONG_NO_SL, "chan", leverage=10, margin=3, margin_type="CROSSED", require_stoploss=False)
        self.assertEqual((with_flag.success, with_flag.signal["stoploss"]), (True, None))

    def test_the_page_carries_the_panel_and_the_analyze_url(self):
        page = self.client.get(reverse("manual-place"))
        self.assertContains(page, 'id="sl-panel"')
        self.assertContains(page, reverse("manual-analyze"))
