from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class SplitSourcesByModeMigrationTests(TransactionTestCase):
    before = [("telegram_monitor", "0003_telegramsource_auto_trade")]
    after = [("telegram_monitor", "0005_source_unique_per_mode")]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def test_auto_trade_sources_become_real_and_every_source_is_also_forward(self):
        old = self._migrate(self.before)
        Source = old.get_model("telegram_monitor", "TelegramSource")
        Source.objects.create(name="Live", telegram_id=-1001, auto_trade=True)
        Source.objects.create(name="Sim", telegram_id=-1002, auto_trade=False, is_active=False)

        new = self._migrate(self.after)
        Source = new.get_model("telegram_monitor", "TelegramSource")
        got = sorted(Source.objects.values_list("telegram_id", "mode", "is_active"))
        self.assertEqual(got, sorted([(-1001, "FORWARD", True), (-1001, "REAL", True), (-1002, "FORWARD", False)]))
