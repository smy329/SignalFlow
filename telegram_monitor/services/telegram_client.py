"""
Telethon client construction.

Kept separate from listener/lifecycle logic so the client can be reused
(e.g. from views, for entity resolution) without importing the whole
background-listener machinery.
"""
import logging

from django.conf import settings
from telethon import TelegramClient

logger = logging.getLogger(__name__)


class TelegramConfigError(Exception):
    """Raised when required Telegram credentials are missing."""


def build_client() -> TelegramClient:
    """Create a TelegramClient bound to the persistent local session file.

    Raises TelegramConfigError if API_ID / API_HASH are not configured.
    """
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        raise TelegramConfigError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in the environment (.env)."
        )

    settings.TELEGRAM_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_path = settings.TELEGRAM_SESSIONS_DIR / settings.TELEGRAM_SESSION_NAME

    return TelegramClient(
        str(session_path),
        int(settings.TELEGRAM_API_ID),
        settings.TELEGRAM_API_HASH,
    )
