import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class TradingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "trading"

    def ready(self) -> None:
        from telegram_monitor.apps import is_serving_process
        from telegram_monitor.signals import signal_committed
        from trading import dispatcher

        signal_committed.connect(dispatcher.on_signal_committed, dispatch_uid="trading.on_signal_committed")

        # Same guard as the Telegram listener: only the process that serves requests
        # (never migrate/test/shell, and not both processes of the autoreloader).
        if is_serving_process():
            from trading.services.engine import engine

            engine.start()
