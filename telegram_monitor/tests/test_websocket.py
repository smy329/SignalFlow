"""
WebSocket tests for the realtime signal channel. Uses Channels' in-memory
test utilities — no real network socket, no browser.
"""
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, User
from django.test import TransactionTestCase
from django.utils import timezone

from telegram_monitor.consumers import SignalConsumer
from telegram_monitor.models import TelegramMessage, TelegramSource
from telegram_monitor.services import signal_service

VALID_SIGNAL_TEXT = (
    "BTCUSDT LONG\n\n"
    "This is a strong setup forming with good volume confirmation across the board on the daily chart today.\n\n"
    "Entry: 100000\n\nTP1: 101000\nTP2: 102000\n\nSL: 98000"
)


async def make_communicator(user):
    communicator = WebsocketCommunicator(SignalConsumer.as_asgi(), "/ws/signals/")
    communicator.scope["user"] = user
    connected, _ = await communicator.connect()
    return communicator, connected


class WebSocketConnectionTests(TransactionTestCase):
    async def test_authenticated_user_can_connect(self):
        user = await sync_to_async(User.objects.create_user)(username="dev", password="pass12345")
        communicator, connected = await make_communicator(user)
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_anonymous_user_is_rejected(self):
        communicator, connected = await make_communicator(AnonymousUser())
        self.assertFalse(connected)

    async def test_client_disconnect_is_clean(self):
        user = await sync_to_async(User.objects.create_user)(username="dev2", password="pass12345")
        communicator, connected = await make_communicator(user)
        self.assertTrue(connected)
        await communicator.disconnect()  # must not raise


class WebSocketBroadcastTests(TransactionTestCase):
    async def _connected_communicator(self, username="dev3"):
        user = await sync_to_async(User.objects.create_user)(username=username, password="pass12345")
        communicator, connected = await make_communicator(user)
        assert connected
        return communicator

    async def test_valid_signal_is_broadcast_with_expected_payload(self):
        communicator = await self._connected_communicator()

        source = await sync_to_async(TelegramSource.objects.create)(name="Live Channel", telegram_id=-100999)
        message = await sync_to_async(TelegramMessage.objects.create)(
            source=source, telegram_message_id=1, message_text=VALID_SIGNAL_TEXT, telegram_date=timezone.now()
        )
        await sync_to_async(signal_service.process_message)(message)

        event = await communicator.receive_json_from(timeout=5)
        self.assertEqual(event["type"], "signal.created")
        signal = event["signal"]
        self.assertEqual(signal["symbol"], "BTCUSDT")
        self.assertEqual(signal["direction"], "BUY")
        self.assertEqual(signal["source"]["name"], "Live Channel")
        self.assertIn("targets", signal)

        await communicator.disconnect()

    async def test_invalid_message_produces_no_broadcast(self):
        communicator = await self._connected_communicator(username="dev4")

        source = await sync_to_async(TelegramSource.objects.create)(name="Chatty", telegram_id=-100888)
        message = await sync_to_async(TelegramMessage.objects.create)(
            source=source, telegram_message_id=1, message_text="Just saying good morning to everyone here today!",
            telegram_date=timezone.now(),
        )
        await sync_to_async(signal_service.process_message)(message)

        self.assertTrue(await communicator.receive_nothing(timeout=1))
        await communicator.disconnect()

    async def test_duplicate_signal_produces_no_second_broadcast(self):
        communicator = await self._connected_communicator(username="dev5")
        source = await sync_to_async(TelegramSource.objects.create)(name="Dup Channel", telegram_id=-100777)

        first = await sync_to_async(TelegramMessage.objects.create)(
            source=source, telegram_message_id=1, message_text=VALID_SIGNAL_TEXT, telegram_date=timezone.now()
        )
        await sync_to_async(signal_service.process_message)(first)
        await communicator.receive_json_from(timeout=5)  # the first, real broadcast

        second = await sync_to_async(TelegramMessage.objects.create)(
            source=source, telegram_message_id=2, message_text=VALID_SIGNAL_TEXT, telegram_date=timezone.now()
        )
        await sync_to_async(signal_service.process_message)(second)

        self.assertTrue(await communicator.receive_nothing(timeout=1))
        await communicator.disconnect()
