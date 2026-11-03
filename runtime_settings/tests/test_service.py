"""The Settings catalogue, validation, storage, seeding and the Settings page."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.html import escape

from runtime_settings import registry, service
from runtime_settings.models import Setting
from runtime_settings.service import SettingError
from telegram_monitor.services.signal_parser import ParserRules, parse_signal


REAL_OPTIONS = {  # the options every Real / Forward group post carries besides the four signal values
    "real.follow_mode": "SIGNAL_DIRECTION", "real.entry_mode": "NEAREST", "real.sl_mode": "TP1_TOUCH", "real.sl_after_tp": "1",
    "real.reversal_mode": "ONE_WAY", "real.tp_split": "60, 40",
}


def defn(key):
    return registry.BY_KEY[key]


class CatalogueTests(SimpleTestCase):
    def test_every_default_passes_its_own_validation(self):
        for d in registry.DEFINITIONS:
            if d.editable:
                with self.subTest(d.key):
                    service.parse(d, service.form_value(d, d.default))

    def test_real_and_forward_have_independent_signal_values_and_only_forward_has_a_fee(self):
        for name in ("leverage", "margin", "margin_type", "entry_mode", "sl_mode", "sl_after_tp", "reversal_mode",
                     "tp_split", "max_entries", "duplicate_window_seconds"):
            self.assertIn(f"real.{name}", registry.BY_KEY)
            self.assertIn(f"forward.{name}", registry.BY_KEY)
            if name not in ("duplicate_window_seconds", "max_entries"):  # the manual page has the same option set
                self.assertIn(f"manual.{name}", registry.BY_KEY)
        self.assertIn("forward.fee_percent", registry.BY_KEY)
        self.assertNotIn("real.fee_percent", registry.BY_KEY)

    def test_agreed_defaults(self):
        self.assertEqual(defn("real.leverage").default, Decimal("10"))
        self.assertEqual(defn("real.margin").default, Decimal("2"))
        self.assertEqual(defn("real.margin_type").default, "CROSSED")
        self.assertIs(defn("trading.real_enabled").default, False)  # safe fallback for live trading
        self.assertEqual(list(defn("real.tp_split").default), [60, 40])
        self.assertEqual(list(defn("forward.tp_split").default), [60, 40])

    def test_max_entries_is_read_only_and_session_name_is_not_listed(self):
        self.assertFalse(defn("real.max_entries").editable)
        self.assertFalse(defn("forward.max_entries").editable)
        self.assertFalse(any("session" in d.key for d in registry.DEFINITIONS))

    def test_default_source_strategy_group_is_gone_and_labels_are_renamed(self):
        self.assertFalse(any(d.key.startswith("strategy.") for d in registry.DEFINITIONS))
        self.assertNotIn("Default source strategy", registry.GROUPS)
        for world in ("real", "forward", "manual"):
            self.assertEqual(defn(f"{world}.sl_after_tp").label, "Move Stop-loss after TP")
            self.assertEqual(defn(f"{world}.sl_mode").choices, ("TP1_TOUCH", "AFTER_FILLING_TP"))

    def test_old_stop_loss_mode_name_is_still_understood(self):
        self.assertEqual(service.parse(defn("real.sl_mode"), "AFTER_TP"), "AFTER_FILLING_TP")
        self.assertEqual(service.parse(defn("manual.sl_mode"), "after_filling_tp"), "AFTER_FILLING_TP")

    def test_channel_and_bot_are_kept_as_editable_settings(self):
        self.assertTrue(defn("telegram.signals_channel").editable)
        self.assertTrue(defn("telegram.signal_sender_bot").editable)


class ParseTests(SimpleTestCase):
    def test_leverage_range_is_1_to_2000(self):
        d = defn("real.leverage")
        self.assertEqual(service.parse(d, "1"), Decimal("1"))
        self.assertEqual(service.parse(d, "2000"), Decimal("2000"))
        for bad in ("0", "0.5", "2001", "abc", ""):
            with self.subTest(bad), self.assertRaises(SettingError):
                service.parse(d, bad)

    def test_margin_must_be_positive_or_blank(self):
        d = defn("real.margin")
        self.assertEqual(service.parse(d, "2.5"), Decimal("2.5"))
        self.assertIsNone(service.parse(d, ""))
        for bad in ("0", "-1", "x"):
            with self.subTest(bad), self.assertRaises(SettingError):
                service.parse(d, bad)

    def test_choice_is_case_insensitive_and_strict(self):
        self.assertEqual(service.parse(defn("real.margin_type"), "isolated"), "ISOLATED")
        self.assertEqual(service.parse(defn("binance.candle_interval"), "5M"), "5m")
        with self.assertRaises(SettingError):
            service.parse(defn("real.margin_type"), "SPLIT")

    def test_bool(self):
        d = defn("trading.testnet")
        self.assertIs(service.parse(d, "1"), True)
        self.assertIs(service.parse(d, "False"), False)
        with self.assertRaises(SettingError):
            service.parse(d, "maybe")

    def test_keyword_lists_are_one_per_line_uppercased_deduplicated_and_never_empty(self):
        d = defn("parser.entry_keywords")
        self.assertEqual(service.parse(d, "entry\n  buy  zone \nENTRY\n\n"), ["ENTRY", "BUY ZONE"])
        with self.assertRaises(SettingError):
            service.parse(d, "  \n ")
        self.assertEqual(service.parse(defn("parser.skip_words"), ""), [])  # skip words may be empty

    def test_take_profit_split(self):
        d = defn("real.tp_split")
        self.assertEqual(service.parse(d, "60, 40"), [60, 40])
        self.assertEqual(service.parse(d, "50 30 10"), [50, 30, 10])
        for bad in ("", "60, 50", "0", "101", "a, b", "10," + ",".join(["5"] * 8)):
            with self.subTest(bad), self.assertRaises(SettingError):
                service.parse(d, bad)

    def test_integer_bounds(self):
        with self.assertRaises(SettingError):
            service.parse(defn("paper.page_size"), "1001")  # Binance allows at most 1000
        with self.assertRaises(SettingError):
            service.parse(defn("trading.poll_seconds"), "0")
        with self.assertRaises(SettingError):
            service.parse(defn("trading.poll_seconds"), "2.5")


class ParserRulesFromSettingsTests(SimpleTestCase):
    TEXT = "#ETHUSDT LONG\nAccumulate: 2000\nTargets: 2100 2200\nStop loss: 1900\n" + "x" * 60

    def test_a_custom_entry_keyword_is_understood(self):
        default = parse_signal(self.TEXT, leverage=10, margin=2, margin_type="CROSSED")
        self.assertEqual(default.error_code, "NO_ENTRY")  # "ACCUMULATE" is not a default entry keyword
        rules = ParserRules.from_words(entry_words=["ENTRY", "ACCUMULATE"])
        custom = parse_signal(self.TEXT, leverage=10, margin=2, margin_type="CROSSED", rules=rules)
        self.assertTrue(custom.success)
        self.assertEqual(custom.signal["entry"], 2000)

    def test_minimum_length_comes_from_the_rules(self):
        short = "#ETHUSDT LONG\nEntry: 2000\nTP: 2100\nSL: 1900"
        kw = dict(leverage=10, margin=2, margin_type="CROSSED")
        self.assertEqual(parse_signal(short, **kw).error_code, "MESSAGE_TOO_SHORT")
        self.assertTrue(parse_signal(short, rules=ParserRules.from_words(min_length=10), **kw).success)


class StorageTests(TestCase):
    def test_default_until_saved_then_the_saved_value(self):
        self.assertEqual(service.get("real.leverage"), Decimal("10"))
        service.save_many({"real.leverage": "25"})
        self.assertEqual(service.get("real.leverage"), Decimal("25"))
        self.assertEqual(service.get("forward.leverage"), Decimal("10"))  # independent of Real

    def test_a_bad_value_saves_nothing(self):
        with self.assertRaises(SettingError):
            service.save_many({"real.leverage": "30", "real.margin": "-5"})
        self.assertEqual(service.get("real.leverage"), Decimal("10"))
        self.assertFalse(Setting.objects.exists())

    def test_read_only_settings_cannot_be_saved(self):
        with self.assertRaises(SettingError):
            service.save_many({"real.max_entries": "5"})

    def test_reset_returns_to_the_default(self):
        service.save_many({"parser.entry_keywords": "ONLY"})
        self.assertEqual(service.get("parser.entry_keywords"), ["ONLY"])
        service.reset("parser.entry_keywords")
        self.assertIn("ENTRY", service.get("parser.entry_keywords"))
        self.assertTrue(service.is_default("parser.entry_keywords"))

    def test_typed_bundles(self):
        service.save_many({"forward.margin_type": "ISOLATED", "forward.duplicate_window_seconds": "0"})
        values = service.signal_values("forward")
        self.assertEqual((values.margin_type, values.duplicate_window_seconds), ("ISOLATED", 0))
        self.assertEqual(service.signal_values("real").margin_type, "CROSSED")

    def test_unreadable_stored_value_falls_back_to_the_default(self):
        Setting.objects.create(key="real.leverage", value="not json")
        self.assertEqual(service.get("real.leverage"), Decimal("10"))


class SeedFromEnvTests(TestCase):
    ENV = {
        "SIGNAL_LEVERAGE": "10", "SIGNAL_MARGIN": "2", "SIGNAL_MARGIN_TYPE": "CROSSED",
        "SIGNAL_DUPLICATE_WINDOW_SECONDS": "300", "CRYPTO_SIGNALS_CHANNEL": "-1003747099426",
        "CRYPTO_SIGNAL_SENDER_BOT": "123:abc", "BINANCE_TESTNET": "False", "TRADING_ENABLED": "True   ",
        "TRADING_POLL_SECONDS": "5", "FORWARD_TEST_ENABLED": "True", "FORWARD_TEST_FEE_PERCENT": "0.05",
        "TELEGRAM_LISTENER_ENABLED": "True",
    }

    def test_every_old_env_value_is_imported(self):
        seeded = service.seed_from_env(Setting, self.ENV)
        service.clear_cache()
        self.assertEqual(service.get("real.margin"), Decimal("2"))
        self.assertEqual(service.get("forward.margin"), Decimal("2"))  # one env value seeds both worlds
        self.assertIs(service.get("trading.real_enabled"), True)  # trailing spaces are tolerated
        self.assertEqual(service.get("telegram.signals_channel"), "-1003747099426")
        self.assertEqual(service.get("telegram.signal_sender_bot"), "123:abc")
        self.assertEqual(service.get("forward.fee_percent"), Decimal("0.05"))
        self.assertIn("real.leverage", seeded)
        self.assertIn("forward.leverage", seeded)

    def test_seeding_never_overwrites_and_skips_invalid_values(self):
        service.save_many({"real.leverage": "50"})
        service.seed_from_env(Setting, {**self.ENV, "SIGNAL_MARGIN_TYPE": "SPLIT"})
        self.assertEqual(service.get("real.leverage"), Decimal("50"))
        self.assertEqual(service.get("real.margin_type"), "CROSSED")  # invalid env value ignored -> default

    def test_empty_margin_in_env_means_not_set(self):
        service.seed_from_env(Setting, {"SIGNAL_MARGIN": ""})
        self.assertIsNone(service.get("real.margin"))


class SettingsPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

    def _url(self, group):
        return reverse("settings-group", args=[registry.slug_of(group)])

    def test_requires_login_and_every_group_is_in_the_sidebar(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("settings")).status_code, 302)
        self.assertEqual(self.client.get(self._url(registry.G_REAL)).status_code, 302)
        self.client.force_login(get_user_model().objects.create_user("t2", password="pw"))
        page = self.client.get(reverse("dashboard"))
        for group in registry.GROUPS:
            self.assertContains(page, self._url(group))
            self.assertContains(page, escape(group))

    def test_settings_opens_the_first_group(self):
        self.assertRedirects(self.client.get(reverse("settings")), self._url(registry.GROUPS[0]))

    def test_each_group_has_its_own_page_with_only_its_own_settings(self):
        for group in registry.GROUPS:
            page = self.client.get(self._url(group))
            self.assertEqual(page.status_code, 200)
            own = {d.key for d in registry.DEFINITIONS if d.group == group}
            for d in registry.DEFINITIONS:
                if d.key in own:
                    self.assertContains(page, escape(d.label))
                else:
                    self.assertNotContains(page, f'id="f-{d.key}"')

    def test_unknown_group_is_404(self):
        self.assertEqual(self.client.get(reverse("settings-group", args=["nope"])).status_code, 404)
        self.assertEqual(self.client.post(reverse("settings-save", args=["nope"]), {}).status_code, 404)

    def test_saving_a_group_applies_the_values(self):
        slug = registry.slug_of(registry.G_REAL)
        response = self.client.post(reverse("settings-save", args=[slug]), {
            "trading.real_enabled": ["0", "1"], "real.leverage": "40", "real.margin": "5",
            "real.margin_type": "ISOLATED", "real.duplicate_window_seconds": "60", **REAL_OPTIONS,
            "real.entry_mode": "BOTH", "real.sl_mode": "AFTER_FILLING_TP", "real.sl_after_tp": "2",
            "real.reversal_mode": "IGNORE", "real.tp_split": "50, 50",
        })
        self.assertRedirects(response, self._url(registry.G_REAL))
        values = service.signal_values("real")
        self.assertEqual((values.leverage, values.margin, values.margin_type, values.duplicate_window_seconds),
                         (Decimal("40"), Decimal("5"), "ISOLATED", 60))
        self.assertIs(service.get("trading.real_enabled"), True)
        self.assertEqual(service.signal_values("forward").leverage, Decimal("10"))
        self.assertEqual(
            [service.get(f"real.{n}") for n in ("entry_mode", "sl_mode", "sl_after_tp", "reversal_mode", "tp_split")],
            ["BOTH", "AFTER_FILLING_TP", 2, "IGNORE", [50, 50]],
        )
        self.assertEqual(service.get("forward.entry_mode"), "NEAREST")  # Forward test keeps its own, independent values

    def test_an_invalid_field_blocks_the_whole_group_and_is_shown(self):
        slug = registry.slug_of(registry.G_REAL)
        response = self.client.post(reverse("settings-save", args=[slug]), {
            "real.leverage": "5000", "real.margin": "5", "real.margin_type": "ISOLATED", "real.duplicate_window_seconds": "60",
            **REAL_OPTIONS,
        })
        self.assertContains(response, "Leverage must be at most 2000")
        self.assertContains(response, 'name="real.leverage"')  # re-rendered on the same group page
        self.assertEqual(service.get("real.margin"), Decimal("2"))  # nothing saved

    def test_reset_button(self):
        service.save_many({"real.leverage": "40"})
        response = self.client.post(reverse("settings-reset"), {"key": "real.leverage"})
        self.assertRedirects(response, self._url(registry.G_REAL))
        self.assertEqual(service.get("real.leverage"), Decimal("10"))

    def test_saving_requires_post(self):
        self.assertEqual(self.client.get(reverse("settings-save", args=["real-trading"])).status_code, 405)
