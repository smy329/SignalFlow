"""
Pure value types for the trading domain.

No Django ORM, no network, no python-binance: everything here can be imported
and unit-tested in isolation (same rule as telegram_monitor/services/signal_parser.py).
"""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional

BUY = "BUY"
SELL = "SELL"


def close_side(side: str) -> str:
    """Side of the order that closes a position opened with `side`."""
    return SELL if side == BUY else BUY


class EntryType(str, Enum):
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"


class OrderRole(str, Enum):
    ENTRY = "ENTRY"
    ENTRY2 = "ENTRY2"  # second entry order when a source trades "both entries"
    SL = "SL"
    TP1 = "TP1"
    TP2 = "TP2"
    TP3 = "TP3"
    TP4 = "TP4"
    TP5 = "TP5"
    TP6 = "TP6"
    TP7 = "TP7"
    TP8 = "TP8"


ALL_ROLE_VALUES = tuple(role.value for role in OrderRole)
ENTRY_ROLES = (OrderRole.ENTRY, OrderRole.ENTRY2)
TP_ROLES = (
    OrderRole.TP1, OrderRole.TP2, OrderRole.TP3, OrderRole.TP4,
    OrderRole.TP5, OrderRole.TP6, OrderRole.TP7, OrderRole.TP8,
)
MAX_TARGETS = len(TP_ROLES)


def tp_role(number: int) -> OrderRole:
    """TP1..TP8 for a 1-based target number."""
    return TP_ROLES[number - 1]


def tp_number(role: str) -> int:
    """1-based target number of a TPn role value (e.g. "TP3" -> 3)."""
    return int(role[2:])


class OrderKind(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"  # only the Trade Now button creates one (an entry at the current price)
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


@dataclass(frozen=True)
class SignalInput:
    """A parsed signal, decoupled from the ORM so the planner stays pure."""

    symbol: str
    side: str  # BUY | SELL
    entry: Decimal
    targets: tuple[Decimal, ...]
    stoploss: Decimal
    leverage: Decimal
    margin: Optional[Decimal]
    margin_type: str
    # Every entry the signal listed (the source's entry strategy picks from these).
    # Empty for signals saved before multi-entry support: `entry` is then the only one.
    entries: tuple[Decimal, ...] = ()

    @property
    def all_entries(self) -> tuple[Decimal, ...]:
        return self.entries or (self.entry,)


@dataclass(frozen=True)
class SymbolFilters:
    """Exchange rules for one symbol, already parsed into Decimals."""

    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class OrderSpec:
    """One order to place. `price` is the limit price for LIMIT orders and the
    trigger price for the *_MARKET conditional orders."""

    role: OrderRole
    kind: OrderKind
    side: str
    quantity: Decimal
    price: Decimal
    reduce_only: bool
    # Entry orders only: the unfilled entry is void once price reaches `guard_tp` (the signal's
    # TP1) or `guard_sl`. The forward-test gateway uses these to cancel at exact trade order.
    guard_tp: Optional[Decimal] = None
    guard_sl: Optional[Decimal] = None


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: str
    entry_type: EntryType
    leverage: int
    margin_type: str
    quantity: Decimal  # total planned size over all entries
    market_price: Decimal  # reference price the plan was validated against
    entries: tuple[OrderSpec, ...]  # one, or two when the source trades both entries
    stop_loss: OrderSpec
    take_profits: tuple[OrderSpec, ...]  # in target order, only the TPs that close some quantity
    targets: tuple[Decimal, ...] = ()  # every quantized signal target used (TP1 first), max 8

    @property
    def entry(self) -> OrderSpec:
        return self.entries[0]

    @property
    def exit_orders(self) -> tuple[OrderSpec, ...]:
        return (self.stop_loss, *self.take_profits)


@dataclass(frozen=True)
class PlanResult:
    """Planner outcome — exactly one of plan / error is set (mirrors ParseResult)."""

    success: bool
    plan: Optional[TradePlan] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def ok(cls, plan: TradePlan) -> "PlanResult":
        return cls(success=True, plan=plan)

    @classmethod
    def fail(cls, error_code: str, error_message: str) -> "PlanResult":
        return cls(success=False, error_code=error_code, error_message=error_message)
