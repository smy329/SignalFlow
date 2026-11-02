from django.contrib import admin

from trading.models import Trade, TradeOrder


class TradeOrderInline(admin.TabularInline):
    model = TradeOrder
    extra = 0
    can_delete = False
    readonly_fields = [
        "role", "kind", "side", "quantity", "price", "reduce_only", "exchange_id", "is_algo",
        "status", "executed_quantity", "avg_fill_price", "created_at",
    ]


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ["symbol", "side", "mode", "status", "reason_code", "quantity", "avg_entry_price", "created_at"]
    list_filter = ["mode", "status", "reason_code", "side"]
    search_fields = ["symbol", "reason_code", "reason_message"]
    ordering = ["-created_at"]
    readonly_fields = ["signal", "created_at", "updated_at"]
    inlines = [TradeOrderInline]
