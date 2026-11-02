from rest_framework import serializers

from telegram_monitor.models import ParsedSignal, TelegramMessage, TelegramSource


class TelegramSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = TelegramSource
        fields = ["id", "name", "telegram_id", "username", "is_active", "mode", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]


class TelegramMessageSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name", read_only=True)

    class Meta:
        model = TelegramMessage
        fields = [
            "id",
            "source",
            "source_name",
            "telegram_message_id",
            "sender_id",
            "sender_name",
            "sender_username",
            "message_text",
            "reply_to_message_id",
            "telegram_date",
            "created_at",
            "parse_status",
            "error_code",
            "error_message",
        ]


class TelegramSourceMiniSerializer(serializers.ModelSerializer):
    """Nested, frontend-friendly source representation for signal payloads."""

    class Meta:
        model = TelegramSource
        fields = ["id", "name"]


class ParsedSignalSerializer(serializers.ModelSerializer):
    source = TelegramSourceMiniSerializer(read_only=True)
    telegram_message_id = serializers.IntegerField(source="telegram_message.telegram_message_id", read_only=True)

    class Meta:
        model = ParsedSignal
        fields = [
            "id",
            "symbol",
            "direction",
            "leverage",
            "margin",
            "margin_type",
            "entry",
            "entries",
            "targets",
            "stoploss",
            "channel",
            "source",
            "telegram_message_id",
            "created_at",
        ]
