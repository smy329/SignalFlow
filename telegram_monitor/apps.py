import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)


def is_serving_process() -> bool:
    """True only in the single process that actually serves requests under
    `runserver` (see TelegramMonitorConfig._should_start_listener for the
    reasoning). Shared with the trading engine so both background workers
    start exactly once and never during migrate/test/shell."""
    is_runserver = len(sys.argv) > 1 and sys.argv[1] == "runserver"
    if not is_runserver:
        return False

    reloader_child = os.environ.get("RUN_MAIN") == "true"
    noreload = "--noreload" in sys.argv
    return reloader_child or noreload


def wait_for_apps_ready(timeout: float = 30.0) -> None:
    """Block a background worker thread until Django has finished app initialization.

    The workers are started from AppConfig.ready(), i.e. while apps are still
    loading. Touching the database at that point triggers Django's "Accessing the
    database during app initialization" RuntimeWarning, so each worker thread calls
    this before its first query."""
    import time

    from django.apps import apps

    deadline = time.monotonic() + timeout
    while not apps.ready and time.monotonic() < deadline:
        time.sleep(0.05)


class TelegramMonitorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "telegram_monitor"

    def ready(self) -> None:
        if not self._should_start_listener():
            return

        # Imported here (not at module level) so management commands that
        # never reach this branch (migrate, test, shell, ...) don't pay the
        # Telethon import cost and can't accidentally trigger a connection.
        from telegram_monitor.services.telegram_listener import listener_manager

        listener_manager.start()

    @staticmethod
    def _should_start_listener() -> bool:
        """Guards against: management commands, tests, and the dev-server
        autoreloader starting the listener twice.

        `runserver` launches a parent "watcher" process plus a child process
        that actually serves requests; Django sets RUN_MAIN=true only in
        that child. So:
          - reloader ON  (default): only the RUN_MAIN=true child starts it.
          - reloader OFF (--noreload): there is no watcher/child split and
            RUN_MAIN is never set, so we fall back to detecting `runserver`
            directly in argv for that single process.
        Every other command (migrate, test, shell, makemigrations, ...)
        never matches either branch and is left untouched.
        """
        # Whether the listener is switched on is a runtime setting ("Telegram listener" on the Settings
        # page): the started thread follows it, so it can be turned on and off without a restart.
        return is_serving_process()
