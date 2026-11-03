"""
All database access for the trading engine (sync ORM; callers wrap it in
sync_to_async, like telegram_monitor.services.message_service).

Returns plain snapshot dataclasses so async code never touches ORM objects.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from runtime_settings import service as runtime
from telegram_monitor.models import MANUAL_TELEGRAM_ID, ParsedSignal, TelegramSource
from trading.domain.quantize import to_decimal
from trading.domain.strategy import DEFAULT_STRATEGY, OPPOSITE_DIRECTION, SourceStrategy, apply_follow
from trading.domain.types import ENTRY_ROLES, OrderKind, OrderRole, OrderSpec, SignalInput, TradePlan
from trading.exchange.base import OrderState, PlacedOrder
from trading.models import ACTIVE_STATUSES, Trade, TradeOrder

MODE_LIVE = Trade.Mode.LIVE.value
MODE_PAPER = Trade.Mode.PAPER.value
ST_PLACING = Trade.Status.PLACING.value
ST_PENDING = Trade.Status.PENDING_ENTRY.value
ST_OPEN = Trade.Status.OPEN.value
ST_CLOSED = Trade.Status.CLOSED.value
ST_CANCELLED = Trade.Status.CANCELLED.value
ST_REJECTED = Trade.Status.REJECTED.value
ST_FAILED = Trade.Status.FAILED.value
ENTRY_ROLE_VALUES = tuple(r.value for r in ENTRY_ROLES)


@dataclass(frozen=True)
class SignalData:
    signal_id: int
    input: SignalInput
    channel: str
    source_mode: str  # TelegramSource.mode: REAL -> live trade, FORWARD -> paper trade
    source_id: int = 0
    strategy: SourceStrategy = DEFAULT_STRATEGY  # the source's entry / take-profit / stop-loss settings
    manual: bool = False  # placed from the Manual Trade page: not subject to the Real / Forward ON/OFF buttons


@dataclass
class OrderSnap:
    id: int
    role: str
    kind: str
    side: str
    quantity: Decimal
    price: Decimal
    reduce_only: bool
    exchange_id: Optional[int]
    is_algo: bool
    status: str
    executed_quantity: Decimal
    avg_fill_price: Optional[Decimal]
    created_ms: int = 0

    def spec(self) -> OrderSpec:
        return OrderSpec(OrderRole(self.role), OrderKind(self.kind), self.side, self.quantity, self.price, self.reduce_only)


@dataclass
class TradeSnap:
    id: int
    mode: str
    symbol: str
    side: str
    status: str
    quantity: Optional[Decimal]
    entry_price: Optional[Decimal]
    stoploss: Optional[Decimal]
    targets: list[Decimal]
    filled_quantity: Decimal
    avg_entry_price: Optional[Decimal]
    placed_at: Optional[datetime]
    paper_cursor: Optional[int]
    orders: list[OrderSnap] = field(default_factory=list)
    entry_prices: list[Decimal] = field(default_factory=list)
    strategy: SourceStrategy = DEFAULT_STRATEGY
    testnet: bool = False  # LIVE trades: the Binance environment the trade was opened on

    def rows(self, *roles: str) -> list[OrderSnap]:
        """Order rows that reached the exchange, oldest first."""
        return [o for o in self.orders if o.role in roles and o.exchange_id is not None]


# ---- reading ----------------------------------------------------------------


def is_opposite(strategy: Optional[dict]) -> bool:
    """Was this trade planned as the mirror image of its signal? (From the strategy saved with the trade.)"""
    return SourceStrategy.from_dict(strategy).follow == OPPOSITE_DIRECTION


def strategy_from_source(source: TelegramSource, default_tp: Optional[tuple] = None) -> SourceStrategy:
    """The strategy of a source as a pure domain value: its own override where it has one, else the global
    value of its world (Real trading / Forward test). `default_tp` is the take-profit split used when the
    source sets no percentages (None = the world's value now)."""
    if default_tp is None:
        default_tp = runtime.default_tp_split(runtime.world_of(source))
    return SourceStrategy(
        entry_mode=runtime.effective(source, "entry_mode"),
        tp_percents=tuple(getattr(source, name) for name in TelegramSource.TP_PCT_FIELDS),
        sl_mode=runtime.effective(source, "sl_mode"),
        sl_after_tp=runtime.effective(source, "sl_after_tp"),
        default_tp=tuple(default_tp),
        reversal=runtime.effective(source, "reversal_mode"),
        follow=runtime.effective(source, "follow_mode"),
    )


def load_signal(signal_id: int) -> Optional[SignalData]:
    s = ParsedSignal.objects.select_related("source").filter(pk=signal_id).first()
    if s is None:
        return None
    # A manual trade carries the strategy chosen on its page; Telegram signals use their source's.
    strategy = SourceStrategy.from_dict(s.strategy_override) if s.strategy_override else strategy_from_source(s.source)
    signal = SignalInput(
        symbol=s.symbol,
        side=s.direction,
        entry=to_decimal(s.entry),
        targets=tuple(to_decimal(t) for t in s.targets),
        stoploss=to_decimal(s.stoploss),
        leverage=to_decimal(s.leverage),
        margin=None if s.margin is None else to_decimal(s.margin),
        margin_type=s.margin_type,
        entries=tuple(to_decimal(e) for e in s.entries),
    )
    return SignalData(
        signal_id=s.id,
        # From here on the trade works with what is to be traded: for an "opposite" source the mirror image of the
        # signal (the stored signal itself is never changed).
        input=apply_follow(signal, strategy.follow),
        channel=s.channel,
        source_mode=s.source.mode,
        source_id=s.source_id,
        strategy=strategy,
        manual=s.source.telegram_id == MANUAL_TELEGRAM_ID,
    )


def _snap(trade: Trade) -> TradeSnap:
    return TradeSnap(
        id=trade.id,
        mode=trade.mode,
        symbol=trade.symbol,
        side=trade.side,
        status=trade.status,
        quantity=trade.quantity,
        entry_price=trade.entry_price,
        stoploss=trade.stoploss,
        targets=[Decimal(t) for t in trade.targets],
        filled_quantity=trade.filled_quantity,
        avg_entry_price=trade.avg_entry_price,
        placed_at=trade.placed_at,
        paper_cursor=trade.paper_cursor,
        entry_prices=[Decimal(p) for p in trade.entry_prices],
        strategy=SourceStrategy.from_dict(trade.strategy),
        testnet=trade.testnet,
        orders=[
            OrderSnap(
                id=o.id, role=o.role, kind=o.kind, side=o.side, quantity=o.quantity, price=o.price,
                reduce_only=o.reduce_only, exchange_id=o.exchange_id, is_algo=o.is_algo, status=o.status,
                executed_quantity=o.executed_quantity, avg_fill_price=o.avg_fill_price,
                created_ms=int(o.created_at.timestamp() * 1000),
            )
            for o in trade.orders.all()
        ],
    )


def snapshot_trades(*statuses: str) -> list[TradeSnap]:
    trades = Trade.objects.filter(status__in=statuses).prefetch_related("orders").order_by("id")
    return [_snap(t) for t in trades]


def snapshot_trade(trade_id: int) -> Optional[TradeSnap]:
    trade = Trade.objects.filter(pk=trade_id).prefetch_related("orders").first()
    return _snap(trade) if trade is not None else None


def trade_scope(trade_id: int) -> Optional[dict]:
    """Which page a trade belongs to: its mode and whether it came from the Manual Trade page."""
    row = Trade.objects.filter(pk=trade_id).values("mode", "signal__source__telegram_id", "symbol", "side", "status").first()
    if row is None:
        return None
    return {
        "mode": row["mode"], "manual": row["signal__source__telegram_id"] == MANUAL_TELEGRAM_ID,
        "symbol": row["symbol"], "side": row["side"], "status": row["status"],
    }


def signal_has_trade(signal_id: int, mode: str) -> bool:
    return Trade.objects.filter(signal_id=signal_id, mode=mode).exists()


def opposite_active_trades(source_id: int, mode: str, symbol: str, side: str) -> list[tuple[int, str]]:
    """(trade id, status) of the active trades this source already has on `symbol` in the direction opposite to `side`."""
    rows = Trade.objects.filter(
        mode=mode, symbol=symbol, status__in=ACTIVE_STATUSES, signal__source_id=source_id
    ).exclude(side=side)
    return list(rows.order_by("id").values_list("id", "status"))


def snapshot_active_trades() -> list[TradeSnap]:
    """Trades the monitor manages (orders already placed)."""
    return snapshot_trades(ST_PENDING, ST_OPEN)


def snapshot_placing_trades() -> list[TradeSnap]:
    return snapshot_trades(ST_PLACING)


def active_live_environments() -> set[bool]:
    """Binance environments (testnet True/False) that still have a live trade in progress."""
    return set(Trade.objects.filter(mode=MODE_LIVE, status__in=ACTIVE_STATUSES).values_list("testnet", flat=True))


def has_active_paper_trades() -> bool:
    return Trade.objects.filter(mode=MODE_PAPER, status__in=ACTIVE_STATUSES).exists()


def active_live_trades_by_symbol() -> dict[str, dict]:
    trades = Trade.objects.filter(mode=MODE_LIVE, status=ST_OPEN).select_related("signal")
    return {
        t.symbol: {
            "trade_id": t.id,
            "channel": t.signal.channel,
            "opposite": is_opposite(t.strategy),
            "stoploss": t.stoploss,
            "targets": [Decimal(x) for x in t.targets],
            "opened_at": t.opened_at,
        }
        for t in trades
    }


# ---- writing ----------------------------------------------------------------


def begin_trade(
    signal_id: int, mode: str, symbol: str, side: str, testnet: bool = False
) -> tuple[Optional[int], Optional[str]]:
    """(trade_id, None): ready to place orders. (trade_id, code): recorded as
    rejected. (None, None): this signal already has a trade in this mode."""
    if Trade.objects.filter(signal_id=signal_id, mode=mode).exists():
        return None, None

    def rejected() -> tuple[int, str]:
        trade = Trade.objects.create(
            signal_id=signal_id, mode=mode, symbol=symbol, side=side, status=ST_REJECTED, testnet=testnet,
            reason_code="ACTIVE_TRADE_EXISTS", reason_message=f"There is already an active live trade on {symbol}.",
        )
        return trade.id, "ACTIVE_TRADE_EXISTS"

    if mode == MODE_LIVE and Trade.objects.filter(mode=MODE_LIVE, symbol=symbol, status__in=ACTIVE_STATUSES).exists():
        return rejected()
    try:
        with transaction.atomic():
            return Trade.objects.create(signal_id=signal_id, mode=mode, symbol=symbol, side=side, testnet=testnet).id, None
    except IntegrityError:  # lost a race: either the same signal twice, or another live trade on the symbol
        if Trade.objects.filter(signal_id=signal_id, mode=mode).exists():
            return None, None
        return rejected()


def _finish(trade_id: int, status: str, code: str, message: str, **extra) -> None:
    Trade.objects.filter(pk=trade_id).update(
        status=status, reason_code=code, reason_message=message, updated_at=timezone.now(), **extra
    )


def reject_trade(trade_id: int, code: str, message: str) -> None:
    _finish(trade_id, ST_REJECTED, code, message)


def fail_trade(trade_id: int, code: str, message: str) -> None:
    _finish(trade_id, ST_FAILED, code, message, closed_at=timezone.now())


def save_plan(trade_id: int, plan: TradePlan, strategy: SourceStrategy = DEFAULT_STRATEGY) -> None:
    Trade.objects.filter(pk=trade_id).update(
        entry_type=plan.entry_type.value,
        leverage=plan.leverage,
        margin_type=plan.margin_type,
        quantity=plan.quantity,
        entry_price=plan.entry.price,
        entry_prices=[str(e.price) for e in plan.entries],
        stoploss=plan.stop_loss.price,
        targets=[str(t) for t in plan.targets],
        strategy=strategy.to_dict(),
        updated_at=timezone.now(),
    )


def add_order(trade_id: int, spec: OrderSpec) -> int:
    return TradeOrder.objects.create(
        trade_id=trade_id, role=spec.role.value, kind=spec.kind.value, side=spec.side,
        quantity=spec.quantity, price=spec.price, reduce_only=spec.reduce_only,
    ).id


def set_order_placed(order_id: int, placed: PlacedOrder) -> None:
    TradeOrder.objects.filter(pk=order_id).update(
        exchange_id=placed.exchange_id, is_algo=placed.is_algo, updated_at=timezone.now()
    )


def set_order_status(order_id: int, status: str) -> None:
    TradeOrder.objects.filter(pk=order_id).update(status=status, updated_at=timezone.now())


def update_order_state(order_id: int, state: OrderState) -> None:
    TradeOrder.objects.filter(pk=order_id).update(
        status=state.status, executed_quantity=state.executed_qty, avg_fill_price=state.avg_price,
        updated_at=timezone.now(),
    )


def mark_pending_entry(trade_id: int, placed_at: datetime) -> None:
    Trade.objects.filter(pk=trade_id).update(status=ST_PENDING, placed_at=placed_at, updated_at=timezone.now())


def open_trade(trade_id: int, filled_quantity: Decimal, avg_price: Decimal, opened_at: datetime) -> None:
    Trade.objects.filter(pk=trade_id).update(
        status=ST_OPEN, filled_quantity=filled_quantity, avg_entry_price=avg_price, opened_at=opened_at,
        updated_at=timezone.now(),
    )


def update_fills(trade_id: int, filled_quantity: Decimal, avg_price: Decimal) -> None:
    """The position of an already open trade grew (a second entry filled)."""
    Trade.objects.filter(pk=trade_id).update(
        filled_quantity=filled_quantity, avg_entry_price=avg_price, updated_at=timezone.now()
    )


def close_trade(
    trade_id: int, status: str, code: str, message: str, closed_at: datetime,
    realized_pnl: Optional[Decimal] = None, fees: Optional[Decimal] = None, exit_price: Optional[Decimal] = None,
) -> None:
    _finish(trade_id, status, code, message, closed_at=closed_at, realized_pnl=realized_pnl, fees_paid=fees, exit_price=exit_price)


def update_paper_cursors(symbol: str, cursor: Optional[int]) -> None:
    if cursor is not None:
        Trade.objects.filter(mode=MODE_PAPER, symbol=symbol, status__in=ACTIVE_STATUSES).update(paper_cursor=cursor)


# ---- reports (Positions / Pending / History / Sources pages) ------------------------


def trade_rows(mode: Optional[str], status: str, limit: Optional[int] = None, manual: bool = False) -> list[dict]:
    """Plain rows for the trade tables of one mode. Closed rows only include trades whose
    position was actually opened; entries cancelled before filling are their own status.
    `manual=False` leaves out trades placed from the Manual Trade page, `manual=True` returns only those
    (and `mode=None` then means both Real and Forward)."""
    qs = Trade.objects.filter(status=status).select_related("signal").prefetch_related("orders")
    if mode is not None:
        qs = qs.filter(mode=mode)
    qs = qs.filter(signal__source__telegram_id=MANUAL_TELEGRAM_ID) if manual else qs.exclude(signal__source__telegram_id=MANUAL_TELEGRAM_ID)
    if status in (ST_CLOSED, ST_CANCELLED):
        qs = qs.filter(opened_at__isnull=False) if status == ST_CLOSED else qs
        qs = qs.order_by("-closed_at")
    elif status == ST_OPEN:
        qs = qs.order_by("-opened_at")
    else:
        qs = qs.order_by("-placed_at")
    if limit:
        qs = qs[:limit]

    rows = []
    for t in qs:
        orders = [o for o in t.orders.all() if o.executed_quantity > 0]
        rows.append(
            {
                "id": t.id,
                "mode": t.mode,
                "recorded_exit_price": t.exit_price,
                "opposite": is_opposite(t.strategy),
                "channel": t.signal.channel,
                "symbol": t.symbol,
                "side": t.side,
                "status": t.status,
                "reason": t.reason_code,
                "leverage": t.leverage,
                "quantity": t.quantity,
                "planned_entry": t.entry_price,
                "planned_entries": [Decimal(x) for x in t.entry_prices] or ([t.entry_price] if t.entry_price else []),
                "stoploss": t.stoploss,
                "targets": [Decimal(x) for x in t.targets],
                "opened_at": t.opened_at,
                "closed_at": t.closed_at,
                "realized_pnl": t.realized_pnl,
                "fees": t.fees_paid,
                "entry_fills": [(o.executed_quantity, o.avg_fill_price or o.price) for o in orders if o.role in ENTRY_ROLE_VALUES],
                "exit_fills": [(o.executed_quantity, o.avg_fill_price or o.price) for o in orders if o.role not in ENTRY_ROLE_VALUES],
            }
        )
    return rows


_SUMMARY_AGGREGATES = {
    "trades": Count("id"),
    "wins": Count("id", filter=Q(realized_pnl__gt=0)),
    "losses": Count("id", filter=Q(realized_pnl__lt=0)),
    "total_pnl": Sum("realized_pnl"),
}


def _summary_dict(agg: dict) -> dict:
    trades, wins = agg["trades"], agg["wins"]
    total = agg["total_pnl"] or Decimal("0")
    return {
        "trades": trades,
        "wins": wins,
        "losses": agg["losses"],
        "breakeven": trades - wins - agg["losses"],
        "win_rate": float(wins * 100 / trades) if trades else None,
        "total_pnl": total,
        "avg_pnl": (total / trades) if trades else None,
    }


_NO_TRADES = {"trades": 0, "wins": 0, "losses": 0, "total_pnl": None}


def source_stats(mode: str) -> tuple[dict[int, dict], dict]:
    """({source_id: stats}, totals) for one mode. `trades` counts closed trades, `open` open
    positions, `pending` entries still waiting to fill and `cancelled` entries cancelled before
    they filled. Sources without any of these are simply absent from the dict."""
    telegram = Trade.objects.exclude(signal__source__telegram_id=MANUAL_TELEGRAM_ID)  # manual trades are not a source
    closed = telegram.filter(mode=mode, status=ST_CLOSED, opened_at__isnull=False, realized_pnl__isnull=False)
    counts = {
        status: dict(telegram.filter(mode=mode, status=status).values_list("signal__source_id").annotate(n=Count("id")))
        for status in (ST_OPEN, ST_PENDING, ST_CANCELLED)
    }
    per_source = {
        r["signal__source_id"]: _summary_dict(r)
        for r in closed.values("signal__source_id").annotate(**_SUMMARY_AGGREGATES)
    }
    for source_id in {*counts[ST_OPEN], *counts[ST_PENDING], *counts[ST_CANCELLED]}:
        per_source.setdefault(source_id, _summary_dict(_NO_TRADES))
    for source_id, stats in per_source.items():
        stats["open"] = counts[ST_OPEN].get(source_id, 0)
        stats["pending"] = counts[ST_PENDING].get(source_id, 0)
        stats["cancelled"] = counts[ST_CANCELLED].get(source_id, 0)
    totals = {
        **_summary_dict(closed.aggregate(**_SUMMARY_AGGREGATES)),
        "open": sum(counts[ST_OPEN].values()),
        "pending": sum(counts[ST_PENDING].values()),
        "cancelled": sum(counts[ST_CANCELLED].values()),
    }
    return per_source, totals


def empty_stats() -> dict:
    return {**_summary_dict(_NO_TRADES), "open": 0, "pending": 0, "cancelled": 0}


def has_active_trades(source_id: int, mode: str) -> bool:
    return Trade.objects.filter(signal__source_id=source_id, mode=mode, status__in=ACTIVE_STATUSES).exists()
