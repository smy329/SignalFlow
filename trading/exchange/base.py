"""
The exchange boundary.

`ExchangeGateway` is the only thing the execution and monitor services know
about an exchange. Two implementations exist:
  - BinanceGateway  real Binance USDT-M Futures (live trading + market data)
  - PaperGateway    simulated orders replayed against real Binance prices
                    (forward test) — same interface, so the exact same
                    execution/monitor code runs for both.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional, Protocol

from trading.domain.rules import PriceObservation
from trading.domain.types import OrderSpec, SymbolFilters

# Order statuses shared by both gateways (Binance's names).
NEW = "NEW"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELED = "CANCELED"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"

WORKING_STATUSES = (NEW, PARTIALLY_FILLED)
DEAD_STATUSES = (CANCELED, EXPIRED, REJECTED)


class GatewayError(Exception):
    """An exchange call failed in a way the caller must handle."""


@dataclass(frozen=True)
class PlacedOrder:
    exchange_id: int  # Binance orderId, or algoId for conditional orders
    is_algo: bool


@dataclass(frozen=True)
class OrderState:
    status: str
    executed_qty: Decimal
    avg_price: Optional[Decimal]
    order_id: Optional[int] = None  # the exchange's id of the order that actually traded (a triggered algo order's real order)


@dataclass(frozen=True)
class Candle:
    open_ms: int
    high: Decimal
    low: Decimal


@dataclass(frozen=True)
class AggTrade:
    id: int
    price: Decimal
    time_ms: int


class ExchangeGateway(Protocol):
    def filters(self, symbol: str) -> Optional[SymbolFilters]: ...

    def now_ms(self) -> int: ...

    async def refresh_filters_if_stale(self) -> None: ...

    async def max_leverage(self, symbol: str) -> int: ...

    async def last_price(self, symbol: str) -> Decimal: ...

    async def last_prices(self, symbols: Iterable[str]) -> dict[str, Decimal]: ...

    async def observe_price(self, symbol: str, exchange_id: int, placed_ms: int) -> PriceObservation: ...

    async def prepare_symbol(self, symbol: str, leverage: int, margin_type: str) -> int: ...

    async def place_order(self, symbol: str, spec: OrderSpec, trade_ref: int) -> PlacedOrder: ...

    async def get_order(self, symbol: str, exchange_id: int, is_algo: bool, trade_ref: int) -> OrderState: ...

    async def cancel_order(self, symbol: str, exchange_id: int, is_algo: bool) -> bool: ...

    async def position_amount(self, symbol: str, trade_ref: int) -> Decimal: ...

    async def has_open_exposure(self, symbol: str) -> bool: ...

    async def market_close(self, symbol: str, side: str, quantity: Decimal, trade_ref: int) -> None: ...

    async def account_fills(self, symbol: str, since_ms: int) -> list[dict]: ...  # live only; see trading.domain.exchange_result

    async def sync_market(self, symbols: Iterable[str]) -> None: ...

class TradeHistoryExpired(GatewayError):
    """Requested trades are older than the exchange's lookup window."""

class RateLimited(GatewayError):
    """Exchange asked us to slow down; retry after `retry_after_s`."""
    def __init__(self, message: str, retry_after_s: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s

# Binance only serves aggTrades from the last 2 days (small safety margin)
MAX_TRADE_LOOKBACK_MS = 2 * 24 * 3600 * 1000 - 5 * 60 * 1000