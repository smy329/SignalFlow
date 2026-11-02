"""Manual Trade: options validation, the signal text, engine behaviour, the pages and their separation from Telegram trades."""
import json
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from runtime_settings import service as runtime
from telegram_monitor.models import MANUAL_TELEGRAM_ID, ParsedSignal, TelegramMessage, TelegramSource
from trading.models import Trade, TradingSwitch
from trading.services import manual_service as ms
from trading.strategy_tests.support import Harness, make_signal, make_source

OPTIONS = {
    "leverage": "10", "margin": "3", "margin_type": "CROSSED", "entry_mode": "NEAREST", "sl_mode": "TP1_TOUCH",
    "sl_after_tp": "1", "reversal_mode": "ONE_WAY", "tp_split": "60, 40",
}
LONG = "$ETH | Long\n\nEntry: 1990-2000\nTARGETS:\n\n2020\n2040\n2060\nSTOPLOSS: 1980\nNfa. Manage your risk and size every position carefully."
SHORT = "$ETH | Short\n\nEntry: 2010-2020\nTARGETS:\n\n1990\n1970\n1950\nSTOPLOSS: 2040\nNfa. Manage your risk and size every position carefully."


def opts(**changes):
    return ms.parse_options({**OPTIONS, **changes})


def engine_stub():
    return SimpleNamespace(is_running=True, gateways={"PAPER": object(), "LIVE": object()})


class OptionTests(TestCase):
    def test_the_defaults_are_valid(self):
        o = opts()
        self.assertEqual((o.leverage, o.margin, o.margin_type, o.tp_split), (10, 3, "CROSSED", (60, 40)))

    def test_leverage_takes_numbers_only(self):
        for bad in ("10x", "10X", "abc", "0", "2001", ""):
            with self.assertRaises(ms.ManualInputError) as ctx:
                opts(leverage=bad)
            self.assertIn("leverage", ctx.exception.fields)

    def test_each_free_text_field_is_validated_next_to_its_own_field(self):
        for field, bad in (("margin", "0"), ("margin", "x"), ("tp_split", "70, 40"), ("tp_split", "a"), ("margin_type", "weird")):
            with self.assertRaises(ms.ManualInputError) as ctx:
                opts(**{field: bad})
            self.assertEqual(list(ctx.exception.fields), [field])

    def test_stop_loss_target_is_only_needed_for_after_tp(self):
        self.assertEqual(opts(sl_after_tp="").sl_after_tp, 1)  # TP1_TOUCH never reads it
        with self.assertRaises(ms.ManualInputError) as ctx:
            opts(sl_mode="AFTER_FILLING_TP", sl_after_tp="")
        self.assertIn("sl_after_tp", ctx.exception.fields)
        self.assertEqual(opts(sl_mode="AFTER_FILLING_TP", sl_after_tp="2").sl_after_tp, 2)


class SignalTextTests(TestCase):
    def test_the_text_decides_the_direction(self):
        self.assertEqual(ms.parse_text(LONG, opts())["direction"], "BUY")
        self.assertEqual(ms.parse_text(SHORT, opts())["direction"], "SELL")

    def test_the_example_signal_is_read_in_full(self):
        text = "$XAN | Long\n\nEntry: 0.0078-0.0080\nTARGETS:\n\n0.0086\n0.010\n0.013\n0.017\nSTOPLOSS: 0.0070\nNfa."
        signal = ms.parse_text(text, opts())
        self.assertEqual((signal["symbol"], signal["entries"], len(signal["targets"])), ("XANUSDT", [0.0078, 0.008], 4))

    def test_empty_and_directionless_text_is_refused_on_the_text_field(self):
        for bad in ("", "   ", "$ETH\nEntry: 1990-2000\nTARGETS:\n2020\n2040\nSTOPLOSS: 1980\nNfa. Manage your risk and size every position carefully."):
            with self.assertRaises(ms.ManualInputError) as ctx:
                ms.parse_text(bad, opts())
            self.assertIn("text", ctx.exception.fields)


class EngineBehaviourTests(TestCase):
    def place(self, h, text=LONG, **changes):
        with mock.patch.object(ms, "engine", engine_stub()):
            signal_id = ms.submit(text, opts(**changes), _scope("forward"))
        async_to_sync(h.executor.handle_signal)(signal_id)
        return signal_id, Trade.objects.get(signal_id=signal_id)

    def test_a_manual_trade_uses_the_options_from_the_page(self):
        h = Harness()
        signal_id, trade = self.place(h, entry_mode="BOTH", leverage="7", sl_mode="AFTER_FILLING_TP", sl_after_tp="2", tp_split="50, 50")
        signal = ParsedSignal.objects.get(pk=signal_id)
        self.assertEqual(signal.source.telegram_id, MANUAL_TELEGRAM_ID)
        self.assertFalse(signal.source.is_active)  # the Telegram listener never sees it
        self.assertEqual((trade.mode, trade.status, trade.leverage), ("PAPER", "PENDING_ENTRY", 7))
        self.assertEqual(len(trade.entry_prices), 2)  # BOTH
        self.assertEqual(trade.strategy["sl_mode"], "AFTER_FILLING_TP")
        self.assertEqual((trade.strategy["sl_after_tp"], trade.strategy["default_tp"]), (2, [50, 50]))

    def test_two_manual_trades_never_share_options(self):
        h = Harness()
        _, first = self.place(h, text=LONG, entry_mode="FARTHEST")
        _, second = self.place(h, text=LONG.replace("Long", "Long ").replace("1990-2000", "1991-2001"), entry_mode="NEAREST")
        first.refresh_from_db()
        self.assertEqual((first.strategy["entry_mode"], second.strategy["entry_mode"]), ("FARTHEST", "NEAREST"))

    def test_the_on_off_button_does_not_stop_a_manual_trade_but_stops_telegram_ones(self):
        TradingSwitch.objects.create(slug="forward", enabled=False)
        h = Harness()
        _, trade = self.place(h)
        self.assertEqual(trade.status, "PENDING_ENTRY")
        telegram = make_signal(make_source())
        async_to_sync(h.executor.handle_signal)(telegram.id)
        self.assertFalse(Trade.objects.filter(signal=telegram).exists())

    def test_the_settings_master_flag_still_applies(self):
        runtime.save_many({"trading.forward_enabled": "false"})
        with mock.patch.object(ms, "engine", engine_stub()), self.assertRaises(ms.ManualInputError) as ctx:
            ms.submit(LONG, opts(), _scope("forward"))
        self.assertIn("Allow forward test", str(ctx.exception))

    def test_one_way_reverses_the_previous_manual_trade_and_ignore_skips_the_new_signal(self):
        h = Harness()
        _, old = self.place(h, text=LONG)
        _, new = self.place(h, text=SHORT, reversal_mode="ONE_WAY")
        old.refresh_from_db()
        self.assertEqual((old.status, old.reason_code, new.status), ("CANCELLED", "REVERSED", "PENDING_ENTRY"))
        _, ignored = self.place(h, text=LONG, reversal_mode="IGNORE")
        self.assertEqual((ignored.status, ignored.reason_code), ("REJECTED", "REVERSAL_IGNORED"))

    def test_the_outcome_reports_placed_and_rejected(self):
        h = Harness()
        signal_id, _ = self.place(h)
        self.assertEqual(ms.outcome(signal_id)["state"], "placed")
        self.assertEqual(ms.outcome(signal_id)["tab"], "pending")
        rejected, _ = self.place(h, text=LONG, reversal_mode="IGNORE")  # opposite? no: same side, but one trade per coin only for live
        self.assertIn(ms.outcome(rejected)["state"], {"placed", "rejected"})
        self.assertEqual(ms.outcome(10**9), {"state": "waiting"})


def _scope(slug):
    from trading.scopes import SCOPES
    return SCOPES[slug]


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))

    def post(self, name, **body):
        return self.client.post(reverse(name), json.dumps(body), content_type="application/json")

    def body(self, **extra):
        return {"text": LONG, "mode": "forward", "options": OPTIONS, **extra}

    def test_every_manual_page_requires_login(self):
        self.client.logout()
        for name in ("manual-place", "manual-trades", "manual-history", "manual-api-pending", "manual-api-history"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
        self.assertEqual(self.post("manual-submit", **self.body()).status_code, 302)

    def test_the_place_page_starts_from_the_manual_settings(self):
        page = self.client.get(reverse("manual-place"))
        self.assertContains(page, 'id="signal-text"')
        self.assertEqual(page.context["config"]["defaults"], {**OPTIONS, "margin_type": "CROSSED"})
        self.assertNotContains(page, 'id="opt-direction"')  # no direction box: the text decides

    def test_changing_the_manual_settings_changes_the_form_and_its_version(self):
        before = self.client.get(reverse("manual-place")).context["config"]
        runtime.save_many({"manual.margin": "5", "manual.leverage": "20", "manual.tp_split": "50, 30, 20"})
        after = self.client.get(reverse("manual-place")).context["config"]
        self.assertEqual((after["defaults"]["margin"], after["defaults"]["leverage"], after["defaults"]["tp_split"]), ("5", "20", "50, 30, 20"))
        self.assertNotEqual(before["version"], after["version"])  # the page drops the remembered values on this

    def test_the_manual_trading_settings_page_exists_and_the_sidebar_links_everything(self):
        group = reverse("settings-group", args=["manual-trading"])
        self.assertContains(self.client.get(group), "Maximum entries")
        page = self.client.get(reverse("dashboard"))
        for name in ("manual-place", "manual-trades", "manual-history"):
            self.assertContains(page, reverse(name))
        self.assertContains(page, group)

    def test_a_real_trade_cannot_be_placed_without_the_confirmation_step(self):
        with mock.patch.object(ms, "engine", engine_stub()):
            response = self.post("manual-submit", **self.body(mode="real"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ParsedSignal.objects.count(), 0)

    def test_preview_shows_the_parsed_signal_and_the_entries_that_will_be_traded(self):
        runtime.save_many({"trading.real_enabled": "true"})
        with mock.patch.object(ms, "engine", engine_stub()):
            data = self.post("manual-preview", **self.body(mode="real", options={**OPTIONS, "entry_mode": "BOTH"})).json()
        preview = data["preview"]
        self.assertEqual((preview["symbol"], preview["side"], preview["mode_label"]), ("ETHUSDT", "LONG", "Real"))
        self.assertEqual((preview["entries_used"], preview["leverage"]), (["2000", "1990"], "10x"))  # nearest first
        self.assertEqual(ParsedSignal.objects.count(), 0)  # a preview stores nothing

    def test_errors_come_back_on_the_field_that_caused_them(self):
        bad = self.post("manual-submit", **self.body(options={**OPTIONS, "leverage": "10x"}))
        self.assertEqual((bad.status_code, list(bad.json()["fields"])), (400, ["leverage"]))
        text = self.post("manual-submit", **self.body(text="hello"))
        self.assertIn("text", text.json()["fields"])
        engine_off = self.post("manual-submit", **self.body())  # the engine is not running in tests
        self.assertEqual(engine_off.status_code, 400)
        self.assertIn("engine", engine_off.json()["message"])

    def test_placing_stores_a_manual_signal_and_the_result_endpoint_follows_it(self):
        with mock.patch.object(ms, "engine", engine_stub()):
            data = self.post("manual-submit", **self.body()).json()
        self.assertTrue(data["ok"])
        self.assertEqual(self.client.get(reverse("manual-result", args=[data["signal_id"]])).json(), {"state": "waiting"})
        h = Harness()
        async_to_sync(h.executor.handle_signal)(data["signal_id"])
        result = self.client.get(reverse("manual-result", args=[data["signal_id"]])).json()
        self.assertEqual((result["state"], result["mode_label"], result["url"]), ("placed", "Forward", reverse("manual-trades-pending")))

    def test_manual_trades_live_on_the_manual_pages_only(self):
        h = Harness()
        with mock.patch.object(ms, "engine", engine_stub()):
            signal_id = ms.submit(LONG, opts(), _scope("forward"))
        async_to_sync(h.executor.handle_signal)(signal_id)
        telegram = make_signal(make_source(), entries=("2001", "1991"))
        async_to_sync(h.executor.handle_signal)(telegram.id)

        manual = self.client.get(reverse("manual-api-pending")).json()["rows"]
        self.assertEqual([(r["mode"], r["channel"]) for r in manual], [("Forward", "Manual")])
        forward = self.client.get(reverse("forward-api-pending")).json()["rows"]
        self.assertEqual(len(forward), 1)
        self.assertNotEqual(forward[0]["channel"], "Manual")

    def test_the_manual_source_is_invisible_to_the_telegram_pages(self):
        with mock.patch.object(ms, "engine", engine_stub()):
            ms.submit(LONG, opts(), _scope("forward"))
        self.assertEqual(TelegramSource.objects.get(telegram_id=MANUAL_TELEGRAM_ID).name, "Manual")
        self.assertEqual(TelegramMessage.objects.count(), 1)
        self.assertEqual(self.client.get(reverse("forward-sources")).context["rows"], [])
        self.assertEqual(list(self.client.get(reverse("forward-messages")).context["rows"]), [])
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual((len(dashboard.context["sources"]), dashboard.context["signal_count"]), (0, 0))
        self.assertEqual(self.client.get(reverse("forward-sources")).context["totals"]["pending"], 0)
