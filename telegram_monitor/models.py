from django.core.validators import MaxValueValidator
from django.db import models

# The built-in source that owns every manually placed trade (one row per mode, never listened to).
MANUAL_TELEGRAM_ID = 0
MANUAL_CHANNEL = "Manual"


class TelegramSource(models.Model):
    """A Telegram group/channel that the authenticated user account monitors."""

    class Mode(models.TextChoices):
        REAL = "REAL", "Real"  # signals are traded live on Binance (also needs "Allow live trading" in Settings)
        FORWARD = "FORWARD", "Forward"  # signals are only simulated (paper trading)

    name = models.CharField(max_length=255, blank=True)
    telegram_id = models.BigIntegerField(db_index=True)
    # The same Telegram chat can be a Real and a Forward source at once; the two never share trades.
    mode = models.CharField(max_length=8, choices=Mode.choices, default=Mode.FORWARD, db_index=True)
    username = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # --- trade strategy of this source (edited from the Sources page; read by trading.services.trade_repository) ---
    # Values mirror trading.domain.strategy; they are only applied to trades planned after a change.
    class EntryMode(models.TextChoices):
        NEAREST = "NEAREST", "Nearest entry"  # long: highest entry, short: lowest entry
        FARTHEST = "FARTHEST", "Farthest entry"  # long: lowest entry, short: highest entry
        BOTH = "BOTH", "Both entries"  # lowest + highest, margin split equally

    class SlMode(models.TextChoices):
        TP1_TOUCH = "TP1_TOUCH", "When price touches TP1"
        AFTER_FILLING_TP = "AFTER_FILLING_TP", "After a chosen TP is filled"

    class ReversalMode(models.TextChoices):
        ONE_WAY = "ONE_WAY", "One-Way Mode"  # opposite signal: close this source's trade on the coin, take the new one
        IGNORE = "IGNORE", "Ignore"  # opposite signal: skip it, the active trade carries on
        HEDGE = "HEDGE", "Hedge Mode (upcoming)"  # not selectable yet; reserved for a later version

    # Every value below is an OVERRIDE of this source's world (Settings > Real trading / Forward test).
    # NULL (blank for the take-profit percentages) = "Use global": the world's current value is read live.
    leverage = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, default=None)
    margin = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True, default=None)
    margin_type = models.CharField(max_length=30, null=True, blank=True, default=None)
    entry_mode = models.CharField(max_length=8, choices=EntryMode.choices, null=True, blank=True, default=None)
    # Percent of the position closed at each target; blank = 0 %. All blank = the world's default split.
    tp1_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    tp2_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    tp3_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    tp4_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    tp5_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    tp6_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    tp7_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    tp8_pct = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MaxValueValidator(100)])
    sl_mode = models.CharField(max_length=20, choices=SlMode.choices, null=True, blank=True, default=None)
    sl_after_tp = models.PositiveSmallIntegerField(null=True, blank=True, default=None)  # "Move Stop-loss after TP" (1-8)

    reversal_mode = models.CharField(max_length=8, choices=ReversalMode.choices, null=True, blank=True, default=None)

    TP_PCT_FIELDS = tuple(f"tp{n}_pct" for n in range(1, 9))

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["telegram_id", "mode"], name="unique_source_telegram_id_per_mode"),
        ]

    def __str__(self) -> str:
        return self.name or str(self.telegram_id)


class TelegramMessage(models.Model):
    """A single text (or captioned-media) message captured after listener startup."""

    class ParseStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        ERROR = "ERROR", "Error"

    source = models.ForeignKey(TelegramSource, on_delete=models.CASCADE, related_name="messages")
    telegram_message_id = models.BigIntegerField()
    sender_id = models.BigIntegerField(null=True, blank=True)
    sender_name = models.CharField(max_length=255, blank=True)
    sender_username = models.CharField(max_length=255, blank=True)
    message_text = models.TextField(blank=True)
    reply_to_message_id = models.BigIntegerField(null=True, blank=True)
    telegram_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Signal parsing status (see services/signal_service.py) ---
    parse_status = models.CharField(max_length=10, choices=ParseStatus.choices, default=ParseStatus.PENDING)
    error_code = models.CharField(max_length=50, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-telegram_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "telegram_message_id"],
                name="unique_source_telegram_message",
            )
        ]
        indexes = [
            models.Index(fields=["source", "telegram_message_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.source}: {self.message_text[:40]}"


class ParsedSignal(models.Model):
    """A successfully parsed crypto trading signal, tied 1:1 to the
    TelegramMessage it was extracted from."""

    class Direction(models.TextChoices):
        BUY = "BUY", "Buy / Long"
        SELL = "SELL", "Sell / Short"

    telegram_message = models.OneToOneField(
        TelegramMessage, on_delete=models.CASCADE, related_name="parsed_signal"
    )
    source = models.ForeignKey(TelegramSource, on_delete=models.CASCADE, related_name="signals")

    symbol = models.CharField(max_length=30, db_index=True)
    direction = models.CharField(max_length=10, choices=Direction.choices, db_index=True)
    leverage = models.DecimalField(max_digits=10, decimal_places=2)
    margin = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    margin_type = models.CharField(max_length=30)

    # 15 decimal places: some low-cap crypto pairs price at 1e-10 or smaller.
    entry = models.DecimalField(max_digits=30, decimal_places=15, db_index=True)
    # Every entry price the signal listed (`entry` is the one the original bot would trade). Empty on old rows.
    entries = models.JSONField(default=list)
    targets = models.JSONField(default=list)
    stoploss = models.DecimalField(max_digits=30, decimal_places=15)

    channel = models.CharField(max_length=255, blank=True)
    # Manual trades only: the strategy chosen on the Manual Trade page (trading.domain.strategy dict).
    # Empty = use the source's own strategy, as every Telegram signal does.
    strategy_override = models.JSONField(null=True, blank=True, default=None)

    parsed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-parsed_at"]
        indexes = [
            # Backs the duplicate-detection window query in signal_service.py.
            models.Index(fields=["source", "symbol", "direction", "entry", "parsed_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.symbol} {self.direction} @ {self.entry}"
