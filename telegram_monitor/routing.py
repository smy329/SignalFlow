from django.urls import re_path

from telegram_monitor import consumers

websocket_urlpatterns = [
    re_path(r"^ws/signals/$", consumers.SignalConsumer.as_asgi()),
]
