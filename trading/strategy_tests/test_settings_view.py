"""The Sources page: icons, the on/off switch and saving a source's settings."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from telegram_monitor.models import TelegramSource
from trading.strategy_tests.support import make_source


class SourceSettingsViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))
        self.source = make_source()
        self.url = reverse("source-settings", args=[self.source.pk])

    def post(self, **fields):
        data = {"entry_mode": "NEAREST", "sl_mode": "TP1_TOUCH", "sl_after_tp": "1", **fields}
        return self.client.post(self.url, data, follow=True)

    def saved(self) -> TelegramSource:
        return TelegramSource.objects.get(pk=self.source.pk)

    def test_new_sources_override_nothing(self):
        s = self.saved()
        self.assertTrue(all(getattr(s, f) is None for f in ("leverage", "margin", "margin_type", "entry_mode", "sl_mode",
                                                            "sl_after_tp", "reversal_mode", *TelegramSource.TP_PCT_FIELDS)))

    def test_saves_entry_mode_percentages_and_stop_loss_rule(self):
        response = self.post(entry_mode="BOTH", tp1="10", tp2="40", tp3="50", sl_mode="AFTER_FILLING_TP", sl_after_tp="3")
        s = self.saved()
        self.assertEqual((s.entry_mode, s.sl_mode, s.sl_after_tp), ("BOTH", "AFTER_FILLING_TP", 3))
        self.assertEqual([s.tp1_pct, s.tp2_pct, s.tp3_pct, s.tp4_pct], [10, 40, 50, None])
        self.assertContains(response, "saved")

    def test_blank_fields_are_stored_as_blank_and_can_clear_a_previous_value(self):
        self.post(tp2="100")
        self.assertEqual(self.saved().tp2_pct, 100)
        self.post()
        self.assertIsNone(self.saved().tp2_pct)

    def test_a_total_over_100_is_refused_and_nothing_changes(self):
        response = self.post(entry_mode="BOTH", tp1="60", tp2="50")
        self.assertContains(response, "110%")
        self.assertIsNone(self.saved().entry_mode)
        self.assertIsNone(self.saved().tp1_pct)

    def test_decimals_and_text_are_refused(self):
        for bad in ("12.5", "abc", "-5"):
            response = self.post(tp1=bad)
            self.assertContains(response, "whole numbers")
            self.assertIsNone(self.saved().tp1_pct)

    def test_a_percentage_above_100_is_refused(self):
        self.post(tp1="101")
        self.assertIsNone(self.saved().tp1_pct)

    def test_stop_loss_after_a_tp_that_closes_nothing_is_refused(self):
        response = self.post(tp1="50", tp2="50", sl_mode="AFTER_FILLING_TP", sl_after_tp="3")
        self.assertContains(response, "TP3 closes nothing")
        self.assertIsNone(self.saved().sl_mode)

    def test_unknown_modes_are_refused(self):
        self.post(entry_mode="ALL")
        self.post(sl_mode="LATER")
        self.assertEqual((self.saved().entry_mode, self.saved().sl_mode), (None, None))

    def test_settings_belong_to_one_source_only(self):
        other = make_source()
        self.post(entry_mode="FARTHEST", tp1="100")
        other.refresh_from_db()
        self.assertEqual((other.entry_mode, other.tp1_pct), (None, None))

    def test_saving_requires_login_and_post(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {}).status_code, 302)
        self.client.force_login(get_user_model().objects.get(username="tester"))
        self.assertEqual(self.client.get(self.url).status_code, 405)


class SourcesPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))

    def test_page_has_icons_a_switch_a_settings_modal_and_no_text_buttons(self):
        make_source(name="Alpha")
        html = self.client.get(reverse("forward-sources")).content.decode()
        for needle in ('id="settings-modal"', 'role="switch"', "js-open-settings", 'aria-label="Delete source"',
                       'name="tp1"', 'name="tp8"', 'name="entry_mode"', 'name="sl_mode"', 'name="sl_after_tp"',
                       'name="leverage"', 'name="margin"', 'name="margin_type"', 'name="reversal_mode"',
                       'id="world-globals"', "Move Stop-loss after TP"):
            self.assertIn(needle, html)
        self.assertNotIn(">Deactivate<", html)
        self.assertNotIn(">Delete<", html)

    def test_each_row_carries_its_current_settings_for_the_modal(self):
        make_source(name="Alpha", entry_mode="BOTH", tp1_pct=10, tp2_pct=90, sl_mode="AFTER_FILLING_TP", sl_after_tp=2)
        html = self.client.get(reverse("forward-sources")).content.decode()
        self.assertIn('data-entry-mode="BOTH"', html)
        self.assertIn('data-tp="10,90,,,,,,"', html)
        self.assertIn('data-sl-mode="AFTER_FILLING_TP"', html)
        self.assertIn("Both entries · TP 10/90 · SL to entry after TP2", html)
        self.assertIn('data-leverage=""', html)  # nothing overridden for the position size: follows the world

    def test_real_and_forward_pages_both_have_the_controls(self):
        make_source(mode="REAL", name="R")
        make_source(mode="FORWARD", name="F")
        for scope in ("real", "forward"):
            self.assertIn("js-open-settings", self.client.get(reverse(f"{scope}-sources")).content.decode())

    def test_switch_flips_a_source_and_answers_json_for_the_page_script(self):
        source = make_source()
        response = self.client.post(
            reverse("source-toggle", args=[source.pk]), HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(response.json(), {"is_active": False})
        source.refresh_from_db()
        self.assertFalse(source.is_active)
        # without the header it is still the old redirect
        self.assertEqual(self.client.post(reverse("source-toggle", args=[source.pk])).status_code, 302)
