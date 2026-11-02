import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from telegram_monitor.services.realtime import SIGNAL_GROUP

logger = logging.getLogger(__name__)


class SignalConsumer(AsyncJsonWebsocketConsumer):
    """One connection per open dashboard tab. Joins the shared `signals`
    group and forwards `signal.created` events verbatim — it never touches
    the database itself; all persistence happens in signal_service.py
    before a broadcast is ever sent."""

    group_name = SIGNAL_GROUP

    async def connect(self):
        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4001)
            return
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        logger.info("Dashboard WebSocket connected (%s)", self.channel_name)

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)
        logger.info("Dashboard WebSocket disconnected (%s): code=%s", self.channel_name, close_code)

    async def signal_created(self, event):
        """Handler name must match the `type` sent by realtime.broadcast_signal
        ("signal.created" -> "signal_created") — this is Channels' routing
        convention for group_send events."""
        await self.send_json({"type": "signal.created", "signal": event["signal"]})
