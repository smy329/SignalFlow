"""Receiver for telegram_monitor's signal_committed event. Must return instantly."""
import logging

logger = logging.getLogger(__name__)


def on_signal_committed(sender, signal_id: int, **kwargs) -> None:
    from trading.services.engine import engine

    if engine.is_running:
        engine.submit_signal(signal_id)
    else:
        logger.debug("Trading engine not running, signal %s not queued for trading.", signal_id)
