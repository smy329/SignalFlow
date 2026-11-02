"""
Listener tests. Telethon itself is never contacted: we call the manager's
internal handler directly with fake event objects, so these tests need no
real Telegram account or network access.
"""
import inspect
from datetime import datetime, timezone as dt_timezone

from django.test import TestCase
from django.utils import timezone

from telegram_monitor.models import TelegramMessage, TelegramSource
from telegram_monitor.services import telegram_listener
from telegram_monitor.services.telegram_listener import TelegramListenerManager


class FakeSender:
    def __init__(self, first_name="Alice", username="alice"):
        self.first_name = first_name
        self.last_name = ""
        self.username = username
        self.title = ""


class FakeMessage:
    def __init__(self, msg_id, text, date=None, reply_to=None):
        self.id = msg_id
        self.message = text
        self.date = date or datetime.now(dt_timezone.utc)
        self.reply_to_msg_id = reply_to


class FakeEvent:
    def __init__(self, chat_id, message: FakeMessage, sender=None):
        self.chat_id = chat_id
        self.message = message
        self.sender_id = 555
        self._sender = sender or FakeSender()

    async def get_sender(self):
        return self._sender


class ListenerHandlerTests(TestCase):
    def setUp(self):
        self.manager = TelegramListenerManager()
        self.source = TelegramSource.objects.create(name="Group A", telegram_id=-100111, is_active=True)

    async def test_new_text_message_is_saved(self):
        event = FakeEvent(self.source.telegram_id, FakeMessage(1, "Hello from Telegram"))
        await self.manager._handle_new_message(event)
        exists = await TelegramMessage.objects.filter(source=self.source, telegram_message_id=1).aexists()
        self.assertTrue(exists)

    async def test_media_caption_is_saved_as_text(self):
        event = FakeEvent(self.source.telegram_id, FakeMessage(2, "Check out this photo"))
        await self.manager._handle_new_message(event)
        msg = await TelegramMessage.objects.aget(source=self.source, telegram_message_id=2)
        self.assertEqual(msg.message_text, "Check out this photo")

    async def test_message_without_text_is_ignored(self):
        event = FakeEvent(self.source.telegram_id, FakeMessage(3, ""))
        await self.manager._handle_new_message(event)
        exists = await TelegramMessage.objects.filter(source=self.source, telegram_message_id=3).aexists()
        self.assertFalse(exists)

    async def test_unknown_source_is_ignored(self):
        event = FakeEvent(-1009999999999, FakeMessage(4, "hi"))
        await self.manager._handle_new_message(event)
        self.assertEqual(await TelegramMessage.objects.acount(), 0)

    async def test_inactive_source_is_ignored(self):
        self.source.is_active = False
        await self.source.asave(update_fields=["is_active"])
        event = FakeEvent(self.source.telegram_id, FakeMessage(5, "hi"))
        await self.manager._handle_new_message(event)
        self.assertEqual(await TelegramMessage.objects.acount(), 0)

    async def test_duplicate_message_does_not_raise_or_duplicate(self):
        event = FakeEvent(self.source.telegram_id, FakeMessage(6, "first"))
        await self.manager._handle_new_message(event)
        await self.manager._handle_new_message(event)  # duplicate telegram_message_id
        count = await TelegramMessage.objects.filter(source=self.source, telegram_message_id=6).acount()
        self.assertEqual(count, 1)

    async def test_multiple_sources_are_isolated(self):
        source_b = await TelegramSource.objects.acreate(name="Group B", telegram_id=-100222, is_active=True)
        await self.manager._handle_new_message(FakeEvent(self.source.telegram_id, FakeMessage(7, "for A")))
        await self.manager._handle_new_message(FakeEvent(source_b.telegram_id, FakeMessage(7, "for B")))
        count_a = await TelegramMessage.objects.filter(source=self.source, telegram_message_id=7).acount()
        count_b = await TelegramMessage.objects.filter(source=source_b, telegram_message_id=7).acount()
        self.assertEqual(count_a, 1)
        self.assertEqual(count_b, 1)

    async def test_same_chat_as_real_and_forward_source_is_saved_for_both(self):
        real = await TelegramSource.objects.acreate(name="Group A", telegram_id=-100111, mode="REAL")
        await self.manager._handle_new_message(FakeEvent(self.source.telegram_id, FakeMessage(10, "shared")))
        sources = {m.source_id async for m in TelegramMessage.objects.filter(telegram_message_id=10)}
        self.assertEqual(sources, {self.source.id, real.id})

    async def test_inactive_mode_does_not_block_the_other_mode(self):
        real = await TelegramSource.objects.acreate(name="Group A", telegram_id=-100111, mode="REAL", is_active=False)
        await self.manager._handle_new_message(FakeEvent(self.source.telegram_id, FakeMessage(11, "only forward")))
        sources = {m.source_id async for m in TelegramMessage.objects.filter(telegram_message_id=11)}
        self.assertEqual(sources, {self.source.id})
        self.assertNotIn(real.id, sources)

    async def test_source_reactivation_is_picked_up_without_restart(self):
        self.source.is_active = False
        await self.source.asave(update_fields=["is_active"])
        event = FakeEvent(self.source.telegram_id, FakeMessage(8, "ignored while inactive"))
        await self.manager._handle_new_message(event)
        self.assertEqual(await TelegramMessage.objects.acount(), 0)

        self.source.is_active = True
        await self.source.asave(update_fields=["is_active"])
        event2 = FakeEvent(self.source.telegram_id, FakeMessage(9, "captured once active"))
        await self.manager._handle_new_message(event2)
        exists = await TelegramMessage.objects.filter(telegram_message_id=9).aexists()
        self.assertTrue(exists)


class NoHistoricalSyncTests(TestCase):
    """Structural guarantee: the listener module must never call Telethon's
    history-fetching API. New messages only ever arrive through the
    `events.NewMessage` handler registered in `_main`."""

    def test_iter_messages_is_never_used(self):
        source = inspect.getsource(telegram_listener)
        self.assertNotIn("iter_messages", source)

    def test_only_new_message_event_is_registered(self):
        source = inspect.getsource(telegram_listener)
        self.assertIn("events.NewMessage()", source)
