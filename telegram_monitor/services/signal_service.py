"""
Orchestrates: parser output -> validation -> duplicate check -> persistence
-> realtime broadcast. This is the only module that connects the pure
`signal_parser` to Django ORM/Channels — the parser itself never touches
either (see signal_parser.py docstring).
"""
import logging
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from runtime_settings import service as runtime
from telegram_monitor import signals as signal_events
from telegram_monitor.models import ParsedSignal, TelegramMessage, TelegramSource
from telegram_monitor.services import realtime
from telegram_monitor.services.signal_parser import parse_signal

logger = logging.getLogger(__name__)

DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"


def process_message(message: TelegramMessage) -> Optional[ParsedSignal]:
    """Parse one already-saved TelegramMessage and persist the outcome.

    Called synchronously (via sync_to_async) from the Telegram listener.
    Never raises: any unexpected failure is recorded on the message as
    parse_status=ERROR instead of propagating, so one bad message can never
    take down the listener (rule: invalid messages must not crash it).
    """
    try:
        return _process_message_inner(message)
    except Exception as exc:  # noqa: BLE001 - listener must survive this
        logger.exception("Unexpected error processing message %s for signals", message.id)
        _mark(message, TelegramMessage.ParseStatus.ERROR, "PARSER_ERROR", str(exc))
        return None


def _process_message_inner(message: TelegramMessage) -> Optional[ParsedSignal]:
    channel_name = message.source.name or str(message.source.telegram_id)

    # A source uses its own leverage / margin / margin type when it sets them, else those of its world
    # (Real trading / Forward test); the duplicate window always comes from its world.
    values = runtime.source_signal_values(message.source)

    result = parse_signal(
        message.message_text,
        channel=channel_name,
        leverage=values.leverage,
        margin=values.margin,
        margin_type=values.margin_type,
        rules=runtime.parser_rules(),
    )

    if not result.success:
        logger.info("Message %s rejected by parser: %s", message.id, result.error_code)
        _mark(message, TelegramMessage.ParseStatus.FAILED, result.error_code or "", result.error_message or "")
        return None

    data = result.signal

    with transaction.atomic():
        # Lock the source row so two near-simultaneous messages from the
        # *same* source can never race past the duplicate check together.
        # Different sources are never blocked by each other's locks.
        TelegramSource.objects.select_for_update().get(pk=message.source_id)

        if _is_duplicate(message.source_id, data["symbol"], data["direction"], data["entry"], values.duplicate_window_seconds):
            logger.warning(
                "Duplicate signal ignored: source=%s symbol=%s direction=%s entry=%s",
                message.source_id, data["symbol"], data["direction"], data["entry"],
            )
            _mark(
                message,
                TelegramMessage.ParseStatus.FAILED,
                DUPLICATE_SIGNAL,
                "An identical signal from this source was already received recently.",
            )
            return None

        signal = ParsedSignal.objects.create(
            telegram_message=message,
            source_id=message.source_id,
            symbol=data["symbol"],
            direction=data["direction"],
            leverage=data["leverage"],
            margin=data["margin"],
            margin_type=data["margin_type"],
            entry=data["entry"],
            entries=data["entries"],
            targets=data["targets"],
            stoploss=data["stoploss"],
            channel=data["channel"],
        )
        _mark(message, TelegramMessage.ParseStatus.SUCCESS)

        # Only fires after this transaction actually commits, so the
        # frontend can never receive a signal that failed to persist.
        # robust=True: a failing broadcast must never stop the trading hook (and vice versa).
        transaction.on_commit(lambda: realtime.broadcast_signal(signal), robust=True)
        transaction.on_commit(lambda: _notify_signal_committed(signal.id), robust=True)

    logger.info(
        "Signal parsed and saved: %s %s @ %s (message %s)", data["symbol"], data["direction"], data["entry"], message.id
    )
    return signal


def _notify_signal_committed(signal_id: int) -> None:
    """Tell other apps (trading) a signal is safely persisted. Receivers must
    be non-blocking; failures are logged and never reach the listener."""
    for receiver, response in signal_events.signal_committed.send_robust(sender=ParsedSignal, signal_id=signal_id):
        if isinstance(response, Exception):
            logger.error("signal_committed receiver %r failed", receiver, exc_info=response)


def _is_duplicate(source_id: int, symbol: str, direction: str, entry, window_seconds: int) -> bool:
    cutoff = timezone.now() - timedelta(seconds=window_seconds)
    return ParsedSignal.objects.filter(
        source_id=source_id, symbol=symbol, direction=direction, entry=entry, parsed_at__gte=cutoff
    ).exists()


def _mark(message: TelegramMessage, status: str, error_code: str = "", error_message: str = "") -> None:
    message.parse_status = status
    message.error_code = error_code
    message.error_message = error_message
    message.save(update_fields=["parse_status", "error_code", "error_message"])
