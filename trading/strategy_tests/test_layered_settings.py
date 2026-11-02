"""v2.12: Real / Forward global settings, per-source overrides, and the AFTER_FILLING_TP rename."""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from runtime_settings import service as runtime
from telegram_monitor.models import TelegramSource
from trading.domain import strategy as st
from trading.services import trade_repository as repo
from trading.strategy_tests.support import make_source

D = Decimal


class PrecedenceTests(TestCase):
    def test_source_value_first_then_the_world_value(self):
        source = make_source(mode="REAL")
        runtime.save_many({"real.leverage": "25", "real.margin": "7", "real.margin_type": "ISOLATED"})
        values = runtime.source_signal_values(source)
        self.assertEqual((values.leverage, values.margin, values.margin_type), (D("25"), D("7"), "ISOLATED"))

        source.leverage, source.margin = D("5"), D("1.5")
        source.save()
        values = runtime.source_signal_values(source)
        self.assertEqual((values.leverage, values.margin, values.margin_type), (D("5"), D("1.5"), "ISOLATED"))

    def test_a_source_without_override_follows_the_global_live(self):
        source = make_source(mode="FORWARD")
        self.assertEqual(repo.strategy_from_source(source).entry_mode, "NEAREST")
        runtime.save_many({"forward.entry_mode": "FARTHEST", "forward.reversal_mode": "IGNORE", "forward.tp_split": "70, 30"})
        strategy = repo.strategy_from_source(source)
        self.assertEqual((strategy.entry_mode, strategy.reversal, strategy.default_tp), ("FARTHEST", st.REVERSAL_IGNORE, (70, 30)))

    def test_an_override_wins_over_the_global(self):
        source = make_source(mode="FORWARD", entry_mode="BOTH", sl_mode="AFTER_FILLING_TP", sl_after_tp=2)
        runtime.save_many({"forward.entry_mode": "FARTHEST", "forward.sl_mode": "TP1_TOUCH"})
        strategy = repo.strategy_from_source(source)
        self.assertEqual((strategy.entry_mode, strategy.sl_mode, strategy.sl_after_tp), ("BOTH", "AFTER_FILLING_TP", 2))

    def test_real_and_forward_globals_are_independent(self):
        real, forward = make_source(mode="REAL"), make_source(mode="FORWARD")
        runtime.save_many({"real.entry_mode": "BOTH", "real.leverage": "30"})
        self.assertEqual(repo.strategy_from_source(real).entry_mode, "BOTH")
        self.assertEqual(repo.strategy_from_source(forward).entry_mode, "NEAREST")
        self.assertEqual(runtime.source_signal_values(real).leverage, D("30"))
        self.assertEqual(runtime.source_signal_values(forward).leverage, D("10"))

    def test_the_duplicate_window_always_comes_from_the_world(self):
        source = make_source(mode="REAL", leverage=D("3"))
        runtime.save_many({"real.duplicate_window_seconds": "11"})
        self.assertEqual(runtime.source_signal_values(source).duplicate_window_seconds, 11)

    def test_a_blank_global_margin_stays_blank_for_a_source_without_override(self):
        source = make_source(mode="REAL")
        runtime.save_many({"real.margin": ""})
        self.assertIsNone(runtime.source_signal_values(source).margin)


class OldNameTests(SimpleTestCase):
    def test_the_old_stop_loss_mode_text_still_works_in_saved_trades(self):
        saved = {"entry_mode": "NEAREST", "sl_mode": "AFTER_TP", "sl_after_tp": 2}
        strategy = st.SourceStrategy.from_dict(saved)
        self.assertEqual(strategy.sl_mode, st.SL_AFTER_FILLING_TP)
        self.assertEqual(st.sl_trigger_target(strategy, [1, 2, 3]), 2)
        self.assertEqual(strategy.to_dict()["sl_mode"], "AFTER_FILLING_TP")
        self.assertEqual(st.SourceStrategy(sl_mode="AFTER_TP", sl_after_tp=2).sl_mode, "AFTER_FILLING_TP")
        self.assertEqual(st.validate(st.SourceStrategy(sl_mode="AFTER_TP", sl_after_tp=2)), [])

    def test_the_new_name(self):
        self.assertEqual(st.SL_AFTER_FILLING_TP, "AFTER_FILLING_TP")
        self.assertEqual(st.SL_MODES, ("TP1_TOUCH", "AFTER_FILLING_TP"))


class SourceSettingsOverrideViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester", password="pw"))
        self.source = make_source(mode="REAL")
        self.url = reverse("source-settings", args=[self.source.pk])

    def post(self, **fields):
        data = {"leverage": "", "margin": "", "margin_type": "", "entry_mode": "", "sl_mode": "", "sl_after_tp": "",
                "reversal_mode": "", **fields}
        return self.client.post(self.url, data, follow=True)

    def saved(self):
        return TelegramSource.objects.get(pk=self.source.pk)

    def test_every_field_blank_means_use_global(self):
        self.post()
        s = self.saved()
        self.assertTrue(all(getattr(s, n) is None for n in runtime.SOURCE_OVERRIDE_FIELDS))

    def test_overrides_are_saved_and_a_blank_resets_them(self):
        self.post(leverage="12.5", margin="4", margin_type="ISOLATED", entry_mode="BOTH", reversal_mode="IGNORE",
                  sl_mode="AFTER_FILLING_TP", sl_after_tp="2", tp1="50", tp2="50")
        s = self.saved()
        self.assertEqual((s.leverage, s.margin, s.margin_type), (D("12.5"), D("4"), "ISOLATED"))
        self.assertEqual((s.entry_mode, s.reversal_mode, s.sl_mode, s.sl_after_tp), ("BOTH", "IGNORE", "AFTER_FILLING_TP", 2))
        self.post()  # everything back to "Use global"
        s = self.saved()
        self.assertTrue(all(getattr(s, n) is None for n in runtime.SOURCE_OVERRIDE_FIELDS))
        self.assertIsNone(s.tp1_pct)

    def test_a_field_missing_from_the_post_is_left_alone(self):
        self.source.leverage = D("9")
        self.source.save()
        self.client.post(self.url, {"entry_mode": "BOTH"})
        s = self.saved()
        self.assertEqual((s.leverage, s.entry_mode), (D("9"), "BOTH"))

    def test_invalid_values_are_refused_and_nothing_changes(self):
        for bad in ({"leverage": "0"}, {"leverage": "2001"}, {"leverage": "x"}, {"margin": "-1"}, {"margin_type": "SPLIT"},
                    {"reversal_mode": "HEDGE"}, {"sl_after_tp": "9"}):
            self.post(entry_mode="BOTH", **bad)
            s = self.saved()
            self.assertIsNone(s.entry_mode, bad)
            self.assertIsNone(s.leverage, bad)

    def test_the_old_stop_loss_name_is_accepted_and_stored_as_the_new_one(self):
        self.post(sl_mode="AFTER_TP", sl_after_tp="1")
        self.assertEqual(self.saved().sl_mode, "AFTER_FILLING_TP")

    def test_validation_uses_the_global_values_the_source_would_follow(self):
        runtime.save_many({"real.sl_mode": "AFTER_FILLING_TP", "real.sl_after_tp": "3"})
        response = self.post(tp1="50", tp2="50")  # follows "move after TP3", but TP3 would close nothing
        self.assertContains(response, "TP3 closes nothing")
        self.assertIsNone(self.saved().tp1_pct)
        self.post(tp1="50", tp2="50", sl_after_tp="2")  # its own TP number fixes it
        self.assertEqual(self.saved().sl_after_tp, 2)

    def test_sources_page_shows_the_new_controls_and_world_globals(self):
        runtime.save_many({"real.entry_mode": "FARTHEST"})
        html = self.client.get(reverse("real-sources")).content.decode()
        self.assertIn('id="world-globals"', html)
        self.assertIn('"entry_mode": "FARTHEST"', html)
        self.assertIn("Use global", html)
        self.assertIn("all global", html)
        self.assertNotIn(">Stop-loss after TP<", html)


class RenameMigrationTests(TransactionTestCase):
    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_sources_keep_their_values_as_overrides_and_after_tp_is_renamed(self):
        old = self._migrate([("telegram_monitor", "0008_parsedsignal_strategy_override")])
        Source = old.get_model("telegram_monitor", "TelegramSource")
        Source.objects.create(name="A", telegram_id=-1, mode="REAL", entry_mode="BOTH", sl_mode="AFTER_TP", sl_after_tp=2,
                              reversal_mode="IGNORE", tp1_pct=30, tp2_pct=70)
        Source.objects.create(name="B", telegram_id=-2, mode="FORWARD")  # defaults: NEAREST / TP1_TOUCH / ONE_WAY / 1

        new = self._migrate([("telegram_monitor", "0010_rename_after_tp")])
        Source = new.get_model("telegram_monitor", "TelegramSource")
        a, b = Source.objects.get(telegram_id=-1), Source.objects.get(telegram_id=-2)
        self.assertEqual((a.entry_mode, a.sl_mode, a.sl_after_tp, a.reversal_mode, a.tp1_pct, a.tp2_pct),
                         ("BOTH", "AFTER_FILLING_TP", 2, "IGNORE", 30, 70))
        self.assertEqual((b.entry_mode, b.sl_mode, b.sl_after_tp, b.reversal_mode), ("NEAREST", "TP1_TOUCH", 1, "ONE_WAY"))
        self.assertTrue(a.leverage is None and a.margin is None and a.margin_type is None)  # new columns: use global

    def test_default_source_strategy_values_move_to_real_and_forward(self):
        before = [("runtime_settings", "0002_seed_from_env")]
        old = self._migrate(before)
        Setting = old.get_model("runtime_settings", "Setting")
        for key, value in {
            "strategy.entry_mode": "BOTH", "strategy.sl_mode": "AFTER_TP", "strategy.sl_after_tp": 2,
            "strategy.reversal_mode": "IGNORE", "strategy.tp_split": [50, 50],
            "manual.sl_mode": "AFTER_TP", "real.entry_mode": "FARTHEST", "real.leverage": "20",
        }.items():
            Setting.objects.create(key=key, value=json.dumps(value))

        new = self._migrate([("runtime_settings", "0003_layered_source_settings")])
        got = {r.key: json.loads(r.value) for r in new.get_model("runtime_settings", "Setting").objects.all()}
        self.assertFalse(any(k.startswith("strategy.") for k in got))
        for world in ("real", "forward"):
            self.assertEqual(got[f"{world}.sl_mode"], "AFTER_FILLING_TP")
            self.assertEqual((got[f"{world}.sl_after_tp"], got[f"{world}.reversal_mode"], got[f"{world}.tp_split"]), (2, "IGNORE", [50, 50]))
        self.assertEqual(got["forward.entry_mode"], "BOTH")
        self.assertEqual(got["real.entry_mode"], "FARTHEST")  # an existing value is never overwritten
        self.assertEqual(got["real.leverage"], "20")
        self.assertEqual(got["manual.sl_mode"], "AFTER_FILLING_TP")
