from django.db import models
from django.db.models import Q

from trading.domain.types import EntryType, OrderKind, OrderRole

# A trade in one of these states still owns (or is about to own) exchange orders.
ACTIVE_STATUSES = ("PLACING", "PENDING_ENTRY", "OPEN")


def _choices(enum_cls):
    return [(member.value, member.value) for member in enum_cls]


class Trade(models.Model):
    """One attempt to trade a ParsedSignal, either for real (LIVE) or simulated
    (PAPER, the forward test). Rejected/failed attempts are kept with a reason
    for debugging (admin only) — the dashboard shows only trades actually taken."""

    class Mode(models.TextChoices):
        LIVE = "LIVE", "Live"
        PAPER = "PAPER", "Paper (forward test)"

    class Status(models.TextChoices):
        PLACING = "PLACING", "Placing orders"
        PENDING_ENTRY = "PENDING_ENTRY", "Entry order pending"
        OPEN = "OPEN", "Position open"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled before entry"
        REJECTED = "REJECTED", "Rejected before any order"
        FAILED = "FAILED", "Failed while placing orders"

    class Side(models.TextChoices):
        BUY = "BUY", "Buy / Long"
        SELL = "SELL", "Sell / Short"

    signal = models.ForeignKey("telegram_monitor.ParsedSignal", on_delete=models.CASCADE, related_name="trades")
    mode = models.CharField(max_length=8, choices=Mode.choices, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLACING, db_index=True)

    symbol = models.CharField(max_length=30, db_index=True)
    side = models.CharField(max_length=4, choices=Side.choices)
    # The Binance environment a LIVE trade was opened on. The "Use Binance testnet" setting only decides
    # where NEW trades go; this trade keeps being managed on its own environment. False for PAPER trades.
    testnet = models.BooleanField(default=False)

    # --- plan snapshot (empty for REJECTED trades, which never got a plan) ---
    entry_type = models.CharField(max_length=16, choices=_choices(EntryType), blank=True)
    leverage = models.PositiveSmallIntegerField(null=True, blank=True)
    margin_type = models.CharField(max_length=10, blank=True)
    quantity = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)
    entry_price = models.DecimalField(max_digits=30, decimal_places=15, null=True, blank=True)  # first (nearest) entry
    entry_prices = models.JSONField(default=list)  # every planned entry price, as strings (one or two)
    stoploss = models.DecimalField(max_digits=30, decimal_places=15, null=True, blank=True)
    targets = models.JSONField(default=list)  # every quantized signal target used (TP1 first, max 8), as strings
    # The source's strategy as it was when this trade was planned (see trading.domain.strategy).
    strategy = models.JSONField(default=dict)

    # --- execution state ---
    filled_quantity = models.DecimalField(max_digits=30, decimal_places=10, default=0)
    avg_entry_price = models.DecimalField(max_digits=30, decimal_places=15, null=True, blank=True)

    # --- results / forward-test bookkeeping ---
    exit_price = models.DecimalField(max_digits=30, decimal_places=15, null=True, blank=True)  # average closing price
    realized_pnl = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)  # net of fees: Binance's own figure for LIVE trades, an estimate for PAPER
    fees_paid = models.DecimalField(max_digits=30, decimal_places=10, null=True, blank=True)
    paper_cursor = models.BigIntegerField(null=True, blank=True)  # last aggTrade id replayed (PAPER only)

    # Why the trade was rejected / failed / cancelled / closed (e.g. BELOW_MIN_NOTIONAL, PRICE_HIT_TP1, SL_HIT).
    reason_code = models.CharField(max_length=50, blank=True)
    reason_message = models.TextField(blank=True)

    placed_at = models.DateTimeField(null=True, blank=True)  # entry order placed
    opened_at = models.DateTimeField(null=True, blank=True)  # entry filled
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["signal", "mode"], name="uniq_trade_per_signal_mode"),
            # Database-level guard: never two active LIVE trades on one symbol,
            # even if two signals race. PAPER trades are independent by design.
            models.UniqueConstraint(
                fields=["symbol"],
                condition=Q(mode="LIVE", status__in=ACTIVE_STATUSES),
                name="uniq_active_live_trade_per_symbol",
            ),
        ]
        indexes = [models.Index(fields=["mode", "status"])]

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def __str__(self) -> str:
        return f"[{self.mode}] {self.symbol} {self.side} ({self.status})"


class TradeOrder(models.Model):
    """One order on the exchange (or simulated) belonging to a Trade. The SL is
    replaced after TP1, so a trade can hold several rows for the same role."""

    class Status(models.TextChoices):
        NEW = "NEW", "New"
        PARTIALLY_FILLED = "PARTIALLY_FILLED", "Partially filled"
        FILLED = "FILLED", "Filled"
        CANCELED = "CANCELED", "Canceled"
        EXPIRED = "EXPIRED", "Expired"
        REJECTED = "REJECTED", "Rejected"

    trade = models.ForeignKey(Trade, on_delete=models.CASCADE, related_name="orders")
    role = models.CharField(max_length=8, choices=_choices(OrderRole))
    kind = models.CharField(max_length=24, choices=_choices(OrderKind))
    side = models.CharField(max_length=4, choices=Trade.Side.choices)
    quantity = models.DecimalField(max_digits=30, decimal_places=10)
    price = models.DecimalField(max_digits=30, decimal_places=15)  # limit price, or trigger price for *_MARKET
    reduce_only = models.BooleanField(default=False)

    # Binance orderId, or algoId for conditional orders (is_algo=True); the two id spaces are separate.
    exchange_id = models.BigIntegerField(null=True, blank=True)
    is_algo = models.BooleanField(default=False)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    executed_quantity = models.DecimalField(max_digits=30, decimal_places=10, default=0)
    avg_fill_price = models.DecimalField(max_digits=30, decimal_places=15, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["trade", "role"])]

    def __str__(self) -> str:
        return f"{self.trade_id}:{self.role} {self.kind} {self.status}"


class TradingSwitch(models.Model):
    """Runtime ON/OFF for taking NEW trades per scope ("real" / "forward"). No row means ON.
    OFF never touches trades that are already pending or open: those keep being managed."""

    slug = models.CharField(max_length=16, unique=True)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.slug}: {'ON' if self.enabled else 'OFF'}"
