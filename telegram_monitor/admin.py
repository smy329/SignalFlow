from django.contrib import admin

from telegram_monitor.models import ParsedSignal, TelegramMessage, TelegramSource


@admin.register(TelegramSource)
class TelegramSourceAdmin(admin.ModelAdmin):
    list_display = ["name", "telegram_id", "mode", "username", "is_active", "created_at"]
    list_filter = ["mode", "is_active"]
    search_fields = ["name", "telegram_id", "username"]
    ordering = ["-created_at"]


@admin.register(TelegramMessage)
class TelegramMessageAdmin(admin.ModelAdmin):
    list_display = [
        "source",
        "sender_name",
        "telegram_message_id",
        "parse_status",
        "error_code",
        "telegram_date",
    ]
    list_filter = ["source", "parse_status", "telegram_date"]
    search_fields = ["message_text", "sender_name", "sender_username", "telegram_message_id", "error_code"]
    ordering = ["-telegram_date"]
    readonly_fields = ["parse_status", "error_code", "error_message"]


@admin.register(ParsedSignal)
class ParsedSignalAdmin(admin.ModelAdmin):
    list_display = ["symbol", "direction", "entry", "stoploss", "source", "channel", "parsed_at"]
    list_filter = ["source", "direction", "symbol"]
    search_fields = ["symbol", "channel"]
    ordering = ["-parsed_at"]
    readonly_fields = ["telegram_message", "parsed_at", "created_at", "updated_at"]
