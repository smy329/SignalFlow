"""
Realtime signal delivery to connected dashboard WebSocket clients via
Django Channels. Local development uses the in-memory channel layer (see
CHANNEL_LAYERS in settings.py) — a production multi-process deployment
must switch to a shared backend such as channels_redis.
"""
import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from telegram_monitor.models import ParsedSignal
from telegram_monitor.serializers import ParsedSignalSerializer

logger = logging.getLogger(__name__)

SIGNAL_GROUP = "signals"


def broadcast_signal(signal: ParsedSignal) -> None:
    """Push a newly-committed signal to every connected dashboard client.

    Only ever call this from `transaction.on_commit(...)` — never before
    the signal's transaction has committed (see signal_service.py).
    """
    layer = get_channel_layer()
    if layer is None:
        logger.warning("No channel layer configured — skipping realtime broadcast.")
        return

    payload = ParsedSignalSerializer(signal).data
    try:
        async_to_sync(layer.group_send)(SIGNAL_GROUP, {"type": "signal.created", "signal": payload})
        logger.info("Signal %s broadcast to dashboard clients", signal.id)
    except Exception:  # noqa: BLE001 - realtime delivery must never break persistence
        logger.exception("Failed to broadcast signal %s", signal.id)
