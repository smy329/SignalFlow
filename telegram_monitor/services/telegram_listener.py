"""
Background Telegram listener.

Runs Telethon in a dedicated thread with its own asyncio event loop so the
Django HTTP server (WSGI, single-threaded per request) is never blocked by
`client.run_until_disconnected()`.

Only one instance of this manager should ever be started per Django
process — see `telegram_monitor.apps.TelegramMonitorConfig.ready()` for the
startup guard that protects against the dev-server autoreloader spawning it
twice.
"""
import asyncio
import atexit
import logging
import threading

from asgiref.sync import sync_to_async
from django.utils import timezone
from telethon import events

from runtime_settings import service as runtime
from telegram_monitor.apps import wait_for_apps_ready
from telegram_monitor.services import message_service, signal_service
from telegram_monitor.services.telegram_client import build_client

logger = logging.getLogger(__name__)


class TelegramListenerManager:
    """Owns the background thread, event loop, and Telethon client."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._startup_time = None
        self._reconnect_task: asyncio.Task | None = None
        self._stopping = False  # process shutdown: the supervisor must not reconnect

    # ---- public API -----------------------------------------------------

    def start(self) -> None:
        """Idempotent: safe to call more than once, only the first call wins."""
        with self._lock:
            if self._thread is not None:
                logger.info("Telegram listener already running, skipping duplicate start.")
                return
            self._thread = threading.Thread(
                target=self._run_thread, name="telegram-listener", daemon=True
            )
            self._thread.start()
        atexit.register(self.stop)

    def resolve_entity(self, telegram_id: int, timeout: float | None = None) -> dict:
        """Resolve a Telegram id via the already-connected client (sync-callable).

        Used by the "Add Source" view to confirm the account can access the
        chat before saving it. Runs on the listener's own event loop so we
        never open a second connection against the same session file.
        """
        timeout = timeout or runtime.get("telegram.entity_lookup_timeout_seconds")
        if not self._started.wait(timeout=timeout) or self._loop is None:
            raise RuntimeError("Telegram listener is not ready yet. Try again shortly.")

        fut = asyncio.run_coroutine_threadsafe(self._resolve_entity_async(telegram_id), self._loop)
        return fut.result(timeout=timeout)

    async def _resolve_entity_async(self, telegram_id: int) -> dict:
        entity = await self._client.get_entity(telegram_id)
        title = getattr(entity, "title", None)
        name = title or _display_name(entity) or str(telegram_id)
        username = getattr(entity, "username", None)
        return {"name": name, "username": username}

    def stop(self) -> None:
        if self._loop is None or self._client is None:
            return
        logger.info("Telegram listener shutting down...")
        self._stopping = True
        try:
            fut = asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
            fut.result(timeout=runtime.get("engine.ready_timeout_seconds"))
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.exception("Error while disconnecting Telegram client.")

    # ---- thread / event-loop bootstrap -----------------------------------

    def _run_thread(self) -> None:
        wait_for_apps_ready()  # no DB access while Django is still initializing apps
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            logger.exception("Telegram listener crashed.")

    async def _main(self) -> None:
        """Supervisor: follows the "Telegram listener" setting. Turning it off disconnects the
        client; turning it on connects again. No restart, and open trades are not involved."""
        logger.info("Telegram listener supervisor started")
        retry_auth_at = 0.0
        while True:
            try:
                enabled = await sync_to_async(runtime.get)("telegram.listener_enabled") and not self._stopping
                if enabled and self._client is None and self._loop.time() >= retry_auth_at:
                    if not await self._start_client():
                        retry_auth_at = self._loop.time() + 30  # do not hammer a failing login / network
                elif not enabled and self._client is not None:
                    await self._stop_client()
            except Exception:  # noqa: BLE001 - the supervisor itself must never die
                logger.exception("Telegram listener supervisor error")
                retry_auth_at = self._loop.time() + 30
            await asyncio.sleep(2)

    async def _start_client(self) -> bool:
        logger.info("Telegram listener starting")
        client = build_client()
        await client.connect()  # non-interactive: never block the server on input()
        if not await client.is_user_authorized():
            logger.error(
                "Telegram session is not authorized. Stop the server and run: "
                "python manage.py telegram_login"
            )
            await client.disconnect()
            return False
        logger.info("Telegram authenticated / session loaded")

        active_count = len(await sync_to_async(message_service.get_active_sources)())
        logger.info("Loaded %d active Telegram source(s)", active_count)

        client.add_event_handler(self._on_new_message, events.NewMessage())
        self._client = client
        self._startup_time = timezone.now()
        self._started.set()
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(client))
        logger.info("Telegram listener ready")
        return True

    async def _stop_client(self) -> None:
        logger.info("Telegram listener switched off in Settings: disconnecting")
        client, task = self._client, self._reconnect_task
        self._client, self._reconnect_task = None, None
        self._started.clear()
        if task is not None:
            task.cancel()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("Error while disconnecting Telegram client.")

    async def _reconnect_loop(self, client) -> None:
        """Keep the listener alive across transient network failures."""
        while True:
            try:
                await client.run_until_disconnected()
                break  # clean disconnect (shutdown, or switched off in Settings)
            except (ConnectionError, OSError):
                wait = await sync_to_async(runtime.get)("telegram.reconnect_wait_seconds")
                logger.warning("Telegram connection lost, reconnecting in %ss...", wait)
                await asyncio.sleep(wait)
                try:
                    await client.connect()
                except Exception:  # noqa: BLE001
                    logger.exception("Reconnect attempt failed.")
        if self._client is client:  # ended on its own (not switched off): let the supervisor start a fresh client
            self._client, self._reconnect_task = None, None
            self._started.clear()

    # ---- message handling -------------------------------------------------

    async def _on_new_message(self, event) -> None:
        try:
            await self._handle_new_message(event)
        except Exception:  # noqa: BLE001 - one bad message must not kill listener
            logger.exception("Error processing incoming Telegram message.")

    async def _handle_new_message(self, event) -> None:
        sources = await sync_to_async(message_service.get_active_sources_by_telegram_id)(event.chat_id)
        if not sources:
            return  # unknown or inactive chat — ignore

        text = (event.message.message or "").strip()
        if not text:
            return  # sticker/photo/video/voice/poll/service msg without caption — ignore

        sender = await event.get_sender()
        sender_name = _display_name(sender)
        sender_username = getattr(sender, "username", "") or ""

        # Real and Forward sources of the same chat each keep their own message/signal copy
        # and never block one another.
        for source in sources:
            try:
                await self._ingest(event, source, text, sender_name, sender_username)
            except Exception:  # noqa: BLE001
                logger.exception("Error processing message %s for source %s", event.message.id, source.id)

    async def _ingest(self, event, source, text: str, sender_name: str, sender_username: str) -> None:
        message = await sync_to_async(message_service.save_message)(
            source=source,
            telegram_message_id=event.message.id,
            sender_id=event.sender_id,
            sender_name=sender_name,
            sender_username=sender_username,
            message_text=text,
            reply_to_message_id=event.message.reply_to_msg_id,
            telegram_date=event.message.date,
        )
        if message is None:
            return  # duplicate Telegram message — never re-run signal parsing on it

        logger.info("Saved Telegram message %s from source %s (%s)", message.telegram_message_id, source.telegram_id, source.mode)

        # Hand off to the signal pipeline: parse -> validate -> duplicate
        # check -> persist -> broadcast. Isolated in its own service so a
        # bad message/parse failure can never crash this listener.
        await sync_to_async(signal_service.process_message)(message)


def _display_name(sender) -> str:
    if sender is None:
        return ""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    title = getattr(sender, "title", "") or ""  # channels/groups posting as themselves
    return (f"{first} {last}".strip() or title).strip()


# Module-level singleton — imported by apps.py and reused by views if ever needed.
listener_manager = TelegramListenerManager()
