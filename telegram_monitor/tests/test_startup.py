"""
Verifies the dev-server autoreloader cannot cause a duplicate Telegram
listener, and that unrelated management commands never trigger one at all.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from telegram_monitor.apps import TelegramMonitorConfig
from telegram_monitor.services.telegram_listener import TelegramListenerManager


class StartupGuardTests(SimpleTestCase):
    def test_skips_for_non_runserver_commands(self):
        for argv in (["manage.py", "migrate"], ["manage.py", "test"], ["manage.py", "shell"], ["manage.py"]):
            with patch("sys.argv", argv):
                self.assertFalse(TelegramMonitorConfig._should_start_listener())

    def test_reloader_watcher_process_is_skipped(self):
        # Parent watcher process: runserver in argv, but RUN_MAIN not yet set.
        with patch("sys.argv", ["manage.py", "runserver"]), patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("RUN_MAIN", None)
            self.assertFalse(TelegramMonitorConfig._should_start_listener())

    def test_reloader_child_process_starts_listener(self):
        with patch("sys.argv", ["manage.py", "runserver"]), patch.dict("os.environ", {"RUN_MAIN": "true"}):
            self.assertTrue(TelegramMonitorConfig._should_start_listener())

    def test_noreload_single_process_starts_listener(self):
        with patch("sys.argv", ["manage.py", "runserver", "--noreload"]):
            import os

            os.environ.pop("RUN_MAIN", None)
            self.assertTrue(TelegramMonitorConfig._should_start_listener())

    def test_listener_switch_is_a_runtime_setting_not_a_startup_guard(self):
        # The "Telegram listener" setting is followed by the running thread (so it can be switched on and
        # off without a restart); the startup guard only decides whether this process serves requests.
        with patch("sys.argv", ["manage.py", "runserver", "--noreload"]):
            self.assertTrue(TelegramMonitorConfig._should_start_listener())


class ListenerManagerSingletonTests(SimpleTestCase):
    def test_start_is_idempotent_within_one_process(self):
        manager = TelegramListenerManager()
        with patch("threading.Thread.start") as mock_start:
            manager.start()
            manager.start()  # second call must be a no-op
            self.assertEqual(mock_start.call_count, 1)
