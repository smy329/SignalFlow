"""
Paper (forward-test) gateway.

Simulates orders against *real* Binance market data. Nothing is ever sent to
the exchange. Every real aggregated trade (price + timestamp, in exact
exchange order) that happened after an order was placed is replayed through
that order, so the first level actually crossed in the real sequence decides
the outcome — no "SL first" or "TP first" assumption is ever needed.

Fill model (mirrors how the live orders behave):
  LIMIT entry            fills at its limit price once a trade prints at or
                         through it (touch = fill; queue position is unknowable)
  STOP_MARKET            triggers when BUY: price >= trigger, SELL: price <= trigger
  TAKE_PROFIT_MARKET     triggers when BUY: price <= trigger, SELL: price >= trigger
  triggered market order fills at the price of the trade that triggered it
Reduce-only orders only fill while their trade has an open position.
Each paper trade has its own position, so trades on one symbol never interfere.
"""
import itertools
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from trading.domain.rules import PriceObservation, pending_entry_cancel_reason
from trading.domain.types import BUY, ENTRY_ROLES, OrderKind, OrderRole, OrderSpec, SymbolFilters
from trading.exchange import base
from trading.exchange.base import AggTrade, GatewayError, OrderState, PlacedOrder, TradeHistoryExpired

logger = logging.getLogger(__name__)

# Defaults only: the live values come from the Settings page via apply_settings().
TRADES_PAGE_SIZE = 1000
MAX_PAGES_PER_SYNC = 6  # ~120 weight per 5s cycle; keeps well under Binance's 2400/min
ZERO = Decimal("0")


@dataclass
class _SimOrder:
    id: int
    trade_ref: int
    symbol: str
    spec: OrderSpec
    placed_ms: int
    status: str = base.NEW
    executed_qty: Decimal = ZERO
    avg_price: Optional[Decimal] = None
    high: Optional[Decimal] = None  # extremes of real trades seen since placement
    low: Optional[Decimal] = None


def _triggered(spec: OrderSpec, price: Decimal) -> bool:
    if spec.kind is OrderKind.LIMIT:
        return price <= spec.price if spec.side == BUY else price >= spec.price
    if spec.kind is OrderKind.STOP_MARKET:
        return price >= spec.price if spec.side == BUY else price <= spec.price
    return price <= spec.price if spec.side == BUY else price >= spec.price  # TAKE_PROFIT_MARKET


class PaperGateway:
    def __init__(self, market) -> None:
        """`market` supplies real prices/rules: a BinanceGateway (or a test double)."""
        self._market = market
        self._orders: dict[int, _SimOrder] = {}
        self._positions: dict[int, Decimal] = {}  # trade_ref -> open size
        self._cursors: dict[str, int] = {}  # symbol -> next aggTrade id to request
        self._last_tick: dict[str, Decimal] = {}
        self._ids = itertools.count(int(time.time() * 1000))
        self.page_size = TRADES_PAGE_SIZE
        self.pages_per_sync = MAX_PAGES_PER_SYNC
        self._pages_left = self.pages_per_sync

    def apply_settings(self, cfg) -> None:
        """Take over the tunable values of a fresh TradingSettings snapshot."""
        self.page_size = cfg.paper_page_size
        self.pages_per_sync = cfg.paper_pages_per_sync

    # ---- market data: straight from the real exchange ----------------------

    def filters(self, symbol: str) -> Optional[SymbolFilters]:
        return self._market.filters(symbol)

    def now_ms(self) -> int:
        return self._market.now_ms()

    async def refresh_filters_if_stale(self) -> None:
        await self._market.refresh_filters_if_stale()

    async def max_leverage(self, symbol: str) -> int:
        return await self._market.max_leverage(symbol)

    async def last_price(self, symbol: str) -> Decimal:
        return await self._market.last_price(symbol)

    async def last_prices(self, symbols: Iterable[str]) -> dict[str, Decimal]:
        return await self._market.last_prices(symbols)

    async def prepare_symbol(self, symbol: str, leverage: int, margin_type: str) -> int:
        return leverage

    async def has_open_exposure(self, symbol: str) -> bool:
        return False  # paper trades are independent of each other and of the real account

    # ---- simulated orders ---------------------------------------------------

    async def place_order(self, symbol: str, spec: OrderSpec, trade_ref: int) -> PlacedOrder:
        if spec.kind is OrderKind.MARKET:  # fills at once at the current price
            price = self._last_tick.get(symbol)
            if price is None:
                price = await self._market.last_price(symbol)
            order = _SimOrder(next(self._ids), trade_ref, symbol, spec, placed_ms=self._market.now_ms())
            self._orders[order.id] = order
            self._fill(order, price)
            return PlacedOrder(exchange_id=order.id, is_algo=False)
        if spec.kind is not OrderKind.LIMIT:
            await self._reject_if_would_trigger_immediately(symbol, spec)
        order = _SimOrder(next(self._ids), trade_ref, symbol, spec, placed_ms=self._market.now_ms())
        self._orders[order.id] = order
        return PlacedOrder(exchange_id=order.id, is_algo=False)

    async def _reject_if_would_trigger_immediately(self, symbol: str, spec: OrderSpec) -> None:
        """Binance refuses a conditional order whose trigger price is already
        reached (error -2021); the simulation refuses it too."""
        price = self._last_tick.get(symbol)
        if price is None:
            price = await self._market.last_price(symbol)
        if _triggered(spec, price):
            raise GatewayError(f"{spec.role.value} order would immediately trigger (trigger {spec.price}, price {price})")

    async def get_order(self, symbol: str, exchange_id: int, is_algo: bool, trade_ref: int) -> OrderState:
        order = self._orders[exchange_id]
        return OrderState(order.status, order.executed_qty, order.avg_price)

    async def cancel_order(self, symbol: str, exchange_id: int, is_algo: bool) -> bool:
        order = self._orders.get(exchange_id)
        if order is None or order.status not in base.WORKING_STATUSES:
            return False
        order.status = base.CANCELED
        return True

    async def position_amount(self, symbol: str, trade_ref: int) -> Decimal:
        return self._positions.get(trade_ref, ZERO)

    async def market_close(self, symbol: str, side: str, quantity: Decimal, trade_ref: int) -> None:
        self._positions[trade_ref] = max(ZERO, self._positions.get(trade_ref, ZERO) - quantity)

    async def observe_price(self, symbol: str, exchange_id: int, placed_ms: int) -> PriceObservation:
        order = self._orders[exchange_id]
        last = self._last_tick.get(symbol)
        if last is None:
            last = await self._market.last_price(symbol)
        return PriceObservation(last=last, high=order.high, low=order.low)

    # ---- replaying real trades ----------------------------------------------

    async def sync_market(self, symbols: Iterable[str]) -> None:
        """Replay every real trade since the last sync through the working orders."""
        self._pages_left = self.pages_per_sync  # shared budget across all symbols this cycle
        wanted = set(symbols)
        for stale in [s for s in self._cursors if s not in wanted]:
            del self._cursors[stale]  # nothing to replay: restart from "now" when orders appear again
        for symbol in wanted:
            working = [o for o in self._orders.values() if o.symbol == symbol and o.status in base.WORKING_STATUSES]
            if working:
                await self._replay(symbol, working)

    async def _replay(self, symbol: str, working: list[_SimOrder]) -> None:
        cursor = self._cursors.get(symbol)
        start_ms = min(o.placed_ms for o in working)
        while self._pages_left > 0:  # budget exhausted -> resume next cycle from the saved cursor
            self._pages_left -= 1
            try:
                if cursor is None:
                    trades = await self._market.agg_trades(symbol, start_ms=start_ms, limit=self.page_size)
                else:
                    trades = await self._market.agg_trades(symbol, from_id=cursor, limit=self.page_size)
            except TradeHistoryExpired:
                if cursor is None:
                    raise
                logger.warning("%s: paper cursor %s is outside the lookup window; resyncing from the window start", symbol, cursor)
                self._cursors.pop(symbol, None)
                cursor = None
                continue
            for trade in trades:
                self._apply_trade(symbol, trade)
            if trades:
                cursor = trades[-1].id + 1
                self._cursors[symbol] = cursor
            if len(trades) < self.page_size:
                break

    def _apply_trade(self, symbol: str, trade: AggTrade) -> None:
        self._last_tick[symbol] = trade.price
        for order in sorted((o for o in self._orders.values() if o.symbol == symbol), key=lambda o: o.id):
            if order.status not in base.WORKING_STATUSES or trade.time_ms < order.placed_ms:
                continue  # a trade that happened before the order existed can never touch it
            order.high = trade.price if order.high is None else max(order.high, trade.price)
            order.low = trade.price if order.low is None else min(order.low, trade.price)
            if _triggered(order.spec, trade.price):
                self._fill(order, trade.price)
        self._cancel_entries_that_missed(symbol, trade)

    def _cancel_entries_that_missed(self, symbol: str, trade: AggTrade) -> None:
        """An entry still unfilled after this trade is cancelled if the price has
        reached its TP1 or stop-loss (the same rule the monitor applies to live
        trades, but evaluated at exact trade order instead of every few seconds).
        A trade that fills the entry wins: the entry is checked first. An entry is also
        void once the stop-loss of its trade has been hit: nothing may open afterwards."""
        for entry in [o for o in self._orders.values() if o.symbol == symbol and o.spec.role in ENTRY_ROLES]:
            if entry.status not in base.WORKING_STATUSES or trade.time_ms < entry.placed_ms:
                continue
            siblings = [o for o in self._orders.values() if o.trade_ref == entry.trade_ref]
            if any(o.spec.role is OrderRole.SL and o.status == base.FILLED for o in siblings):
                entry.status = base.CANCELED
                continue
            guard_tp, guard_sl = entry.spec.guard_tp, entry.spec.guard_sl
            if guard_tp is None or guard_sl is None:  # an order created without guard levels: use its sibling exits
                working = [o for o in siblings if o.status in base.WORKING_STATUSES]
                tp1 = next((o for o in working if o.spec.role is OrderRole.TP1), None)
                sl = next((o for o in working if o.spec.role is OrderRole.SL), None)
                if tp1 is None or sl is None:
                    continue
                guard_tp, guard_sl = tp1.spec.price, sl.spec.price
            reason = pending_entry_cancel_reason(entry.spec.side, guard_tp, guard_sl, PriceObservation(last=trade.price))
            if reason:
                entry.status = base.CANCELED

    def _fill(self, order: _SimOrder, tick_price: Decimal) -> None:
        spec = order.spec
        if spec.reduce_only:
            quantity = min(spec.quantity, self._positions.get(order.trade_ref, ZERO))
            if quantity <= 0:
                return  # nothing to reduce: the order stays pending, exactly like an unfillable reduce-only
            self._positions[order.trade_ref] -= quantity
        else:
            quantity = spec.quantity
            self._positions[order.trade_ref] = self._positions.get(order.trade_ref, ZERO) + quantity
        order.status = base.FILLED
        order.executed_qty = quantity
        order.avg_price = spec.price if spec.kind is OrderKind.LIMIT else tick_price

    # ---- restart support ------------------------------------------------------

    def restore_order(
        self,
        *,
        exchange_id: int,
        trade_ref: int,
        symbol: str,
        spec: OrderSpec,
        placed_ms: int,
        status: str,
        executed_qty: Decimal,
        avg_price: Optional[Decimal],
    ) -> None:
        """Rebuild a simulated order (and its trade's position) from the database."""
        self._orders[exchange_id] = _SimOrder(
            exchange_id, trade_ref, symbol, spec, placed_ms, status, executed_qty, avg_price
        )
        if status == base.FILLED:
            delta = -executed_qty if spec.reduce_only else executed_qty
            self._positions[trade_ref] = self._positions.get(trade_ref, ZERO) + delta
        self._ids = itertools.count(max(exchange_id + 1, next(self._ids)))

    def cursor_for(self, symbol: str) -> Optional[int]:
        return self._cursors.get(symbol)

    def set_cursor(self, symbol: str, cursor: int) -> None:
        self._cursors[symbol] = cursor
