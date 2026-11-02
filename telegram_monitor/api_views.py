import logging

from rest_framework import status, viewsets
from rest_framework.response import Response

from telegram_monitor.models import MANUAL_TELEGRAM_ID, ParsedSignal, TelegramMessage, TelegramSource
from telegram_monitor.serializers import (
    ParsedSignalSerializer,
    TelegramMessageSerializer,
    TelegramSourceSerializer,
)
from telegram_monitor.services.telegram_client import TelegramConfigError
from telegram_monitor.services.telegram_listener import listener_manager

logger = logging.getLogger(__name__)


class TelegramSourceViewSet(viewsets.ModelViewSet):
    """CRUD for monitored sources. Creation resolves + verifies the entity
    against Telegram before saving (see perform_create)."""

    queryset = TelegramSource.objects.exclude(telegram_id=MANUAL_TELEGRAM_ID)
    serializer_class = TelegramSourceSerializer

    def create(self, request, *args, **kwargs):
        telegram_id = request.data.get("telegram_id")
        name = request.data.get("name", "")
        mode = request.data.get("mode", TelegramSource.Mode.FORWARD)
        if mode not in TelegramSource.Mode.values:
            return Response({"detail": f"mode must be one of {TelegramSource.Mode.values}."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            telegram_id = int(telegram_id)
        except (TypeError, ValueError):
            return Response({"detail": "telegram_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            info = listener_manager.resolve_entity(telegram_id)
        except TelegramConfigError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as exc:  # noqa: BLE001 - surface Telethon errors to the user
            logger.warning("Failed to resolve Telegram source %s: %s", telegram_id, exc)
            return Response({"detail": f"Could not access that chat: {exc}"}, status=status.HTTP_400_BAD_REQUEST)

        source, _ = TelegramSource.objects.update_or_create(
            telegram_id=telegram_id,
            mode=mode,
            defaults={
                "name": name or info["name"],
                "username": info.get("username"),
                "is_active": True,
            },
        )
        serializer = self.get_serializer(source)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TelegramMessageViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only. Supports `?source_id=` and `?after_id=` polling params."""

    serializer_class = TelegramMessageSerializer

    def get_queryset(self):
        qs = TelegramMessage.objects.select_related("source").order_by("telegram_message_id")
        source_id = self.request.query_params.get("source_id")
        after_id = self.request.query_params.get("after_id")
        if source_id:
            qs = qs.filter(source_id=source_id)
        if after_id:
            qs = qs.filter(id__gt=after_id)
        return qs


class ParsedSignalViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only. Supports `?source_id=`, `?symbol=`, `?direction=`."""

    serializer_class = ParsedSignalSerializer

    def get_queryset(self):
        qs = ParsedSignal.objects.exclude(source__telegram_id=MANUAL_TELEGRAM_ID).select_related("source", "telegram_message").order_by("-parsed_at")
        source_id = self.request.query_params.get("source_id")
        symbol = self.request.query_params.get("symbol")
        direction = self.request.query_params.get("direction")
        if source_id:
            qs = qs.filter(source_id=source_id)
        if symbol:
            qs = qs.filter(symbol__iexact=symbol)
        if direction:
            qs = qs.filter(direction__iexact=direction)
        return qs
