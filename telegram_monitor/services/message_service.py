"""
All synchronous ORM access used by the (async) Telegram listener lives here,
isolated behind plain functions so it can be safely wrapped with
`sync_to_async` from async callbacks — never call Django ORM directly from
an `async def` handler.
"""
import logging
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from telegram_monitor.models import MANUAL_TELEGRAM_ID, TelegramMessage, TelegramSource

logger = logging.getLogger(__name__)


def get_active_sources() -> list[TelegramSource]:
    return list(TelegramSource.objects.filter(is_active=True))


def get_active_source_ids() -> set[int]:
    return set(TelegramSource.objects.filter(is_active=True).values_list("telegram_id", flat=True))


def get_active_sources_by_telegram_id(telegram_id: int) -> list[TelegramSource]:
    """A chat can be both a Real and a Forward source; each one gets its own copy of the message."""
    return list(TelegramSource.objects.filter(telegram_id=telegram_id, is_active=True))


def message_counts_by_source(mode: str) -> dict[int, int]:
    """{source_id: number of messages received} for the sources of one mode (Real / Forward)."""
    rows = TelegramMessage.objects.filter(source__mode=mode).exclude(source__telegram_id=MANUAL_TELEGRAM_ID).values_list("source_id").annotate(n=Count("id"))
    return dict(rows)


def messages_for_mode(mode: str):
    """Newest-first queryset of every message received by the sources of one mode."""
    return TelegramMessage.objects.filter(source__mode=mode).exclude(source__telegram_id=MANUAL_TELEGRAM_ID).select_related("source").order_by("-telegram_date", "-id")


def create_source(
    telegram_id: int, name: str = "", username: Optional[str] = None, mode: str = TelegramSource.Mode.FORWARD
) -> TelegramSource:
    source, _ = TelegramSource.objects.update_or_create(
        telegram_id=telegram_id,
        mode=mode,
        defaults={"name": name, "username": username, "is_active": True},
    )
    return source


def save_message(
    *,
    source: TelegramSource,
    telegram_message_id: int,
    sender_id: Optional[int],
    sender_name: str,
    sender_username: str,
    message_text: str,
    reply_to_message_id: Optional[int],
    telegram_date,
) -> Optional[TelegramMessage]:
    """Persist a message. Returns None (instead of raising) on duplicates.

    Wrapped in its own `atomic()` savepoint: catching IntegrityError without
    one would otherwise poison any surrounding transaction (e.g. ATOMIC_REQUESTS,
    or a caller's own `atomic()` block) and make it unusable until rollback.
    """
    try:
        with transaction.atomic():
            return TelegramMessage.objects.create(
                source=source,
                telegram_message_id=telegram_message_id,
                sender_id=sender_id,
                sender_name=sender_name,
                sender_username=sender_username,
                message_text=message_text,
                reply_to_message_id=reply_to_message_id,
                telegram_date=telegram_date or timezone.now(),
            )
    except IntegrityError:
        logger.info("Duplicate message ignored: source=%s telegram_message_id=%s", source.telegram_id, telegram_message_id)
        return None
