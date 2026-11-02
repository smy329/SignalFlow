"""
Real Binance USDT-M Futures gateway (python-binance 1.0.36, async client).

python-binance routes conditional order types (STOP_MARKET, TAKE_PROFIT_MARKET)
to Binance's algo endpoint automatically and returns an `algoId`. Algo orders
must therefore be *queried and cancelled through the algo endpoints by algoId*
(the old bot passed the algoId to the regular order endpoints, which never
found them). A triggered algo order exposes the real order as `actualOrderId`.

Also serves as the market-data source for the PaperGateway.
"""
import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Iterable, Optional

from binance import AsyncClient
from binance.exceptions import BinanceAPIException

from trading.domain.quantize import to_plain_str
from trading.domain.rules import PriceObservation, observe_price
from trading.domain.types import OrderKind, OrderSpec, SymbolFilters
from trading.exchange import base
from trading.exchange.base import AggTrade, Candle, GatewayError, OrderState, PlacedOrder, RateLimited, TradeHistoryExpired, MAX_TRADE_LOOKBACK_MS
from trading.exchange.filters import parse_exchange_info

logger = logging.getLogger(__name__)

# Defaults only: the live values come from the Settings page via apply_settings().
FILTERS_MAX_AGE_SECONDS = 600
LEVERAGE_CACHE_SECONDS = 3600
RATE_LIMIT_RETRY_SECONDS = 60
CANDLE_INTERVAL = "1m"
_KNOWN_STATUSES = {base.NEW, base.PARTIALLY_FILLED, base.FILLED, base.CANCELED, base.EXPIRED, base.REJECTED}
_ALGO_DEAD = {"CANCELED": base.CANCELED, "CANCELLED": base.CANCELED, "EXPIRED": base.EXPIRED, "REJECTED": base.REJECTED}
_NO_NEED_TO_CHANGE_MARGIN_TYPE = -4046
_TRADE_WINDOW_EXCEEDED = -4166
_TOO_MANY_REQUESTS = -1003


def _dec(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value))


def _first_nonzero(*values: Any) -> Decimal:
    for value in values:
        number = _dec(value)
        if number != 0:
            return number
    return Decimal("0")


def _state_from_order(order: dict, order_id: Optional[int] = None) -> OrderState:
    status = order.get("status", base.NEW)
    if status == "EXPIRED_IN_MATCH":
        status = base.EXPIRED
    if status not in _KNOWN_STATUSES:
        status = base.NEW
    avg = _dec(order.get("avgPrice"))
    return OrderState(
        status=status, executed_qty=_dec(order.get("executedQty")), avg_price=avg if avg > 0 else None, order_id=order_id
    )


class BinanceGateway:
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = False) -> None:
        self._api_key = api_key or None
        self._api_secret = api_secret or None
        self._testnet = testnet
        self.client: Optional[AsyncClient] = None
        self._filters: dict[str, SymbolFilters] = {}
        self._filters_loaded_at = 0.0
        self._time_offset_ms = 0
        self._leverage_cache: dict[str, tuple[float, int]] = {}
        # tunables (Settings page), refreshed by the engine through apply_settings()
        self.filters_cache_seconds = FILTERS_MAX_AGE_SECONDS
        self.leverage_cache_seconds = LEVERAGE_CACHE_SECONDS
        self.rate_limit_retry_seconds = RATE_LIMIT_RETRY_SECONDS
        self.candle_interval = CANDLE_INTERVAL
        self.trade_lookback_ms = MAX_TRADE_LOOKBACK_MS

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    @property
    def testnet(self) -> bool:
        return self._testnet

    def apply_settings(self, cfg) -> None:
        """Take over the tunable values of a fresh TradingSettings snapshot."""
        self.filters_cache_seconds = cfg.filters_cache_seconds
        self.leverage_cache_seconds = cfg.leverage_cache_seconds
        self.rate_limit_retry_seconds = cfg.rate_limit_retry_seconds
        self.candle_interval = cfg.candle_interval
        self.trade_lookback_ms = cfg.trade_lookback_ms

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        # Direct constructor (like the original bot), not AsyncClient.create(): create() first pings
        # the *spot* API, which a futures-only setup must not depend on.
        self.client = AsyncClient(self._api_key, self._api_secret, testnet=self._testnet)
        await self.sync_time()
        await self.refresh_filters()
        logger.info(
            "Binance Futures %s connected (%s), %d tradable symbols",
            "TESTNET" if self._testnet else "LIVE",
            "authenticated" if self.has_credentials else "public data only",
            len(self._filters),
        )

    async def verify_account(self) -> None:
        """Raise GatewayError unless the key can trade Futures and the account is in
        one-way position mode (the only mode the order flow supports)."""
        try:
            await self.client.futures_account_balance()
            mode = await self.client.futures_get_position_mode()
        except BinanceAPIException as exc:
            raise GatewayError(f"Binance rejected the API key [{exc.code}]: {exc.message}") from exc
        if mode.get("dualSidePosition") in (True, "true"):
            raise GatewayError("The account is in Hedge position mode; switch it to One-way mode to trade.")

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close_connection()
            self.client = None

    async def sync_time(self) -> None:
        server = (await self.client.futures_time())["serverTime"]
        self._time_offset_ms = int(server) - int(time.time() * 1000)

    def now_ms(self) -> int:
        """Binance server time (local clock corrected by the measured offset)."""
        return int(time.time() * 1000) + self._time_offset_ms

    # ---- symbol rules ------------------------------------------------------

    async def refresh_filters(self) -> None:
        self._filters = parse_exchange_info(await self.client.futures_exchange_info())
        self._filters_loaded_at = time.monotonic()

    async def refresh_filters_if_stale(self) -> None:
        if time.monotonic() - self._filters_loaded_at > self.filters_cache_seconds:
            await self.refresh_filters()
            await self.sync_time()

    def filters(self, symbol: str) -> Optional[SymbolFilters]:
        return self._filters.get(symbol)

    async def max_leverage(self, symbol: str) -> int:
        cached = self._leverage_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < self.leverage_cache_seconds:
            return cached[1]
        brackets = await self.client.futures_leverage_bracket(symbol=symbol)
        entry = brackets[0] if isinstance(brackets, list) else brackets
        leverage = int(entry["brackets"][0]["initialLeverage"])  # first bracket = highest leverage
        self._leverage_cache[symbol] = (time.monotonic(), leverage)
        return leverage

    # ---- market data -------------------------------------------------------

    async def last_price(self, symbol: str) -> Decimal:
        ticker = await self.client.futures_symbol_ticker(symbol=symbol)
        return _dec(ticker["price"])

    async def last_prices(self, symbols: Iterable[str]) -> dict[str, Decimal]:
        wanted = set(symbols)
        if not wanted:
            return {}
        tickers = await self.client.futures_symbol_ticker()  # every symbol in one call
        return {t["symbol"]: _dec(t["price"]) for t in tickers if t["symbol"] in wanted}

    async def current_candle(self, symbol: str) -> Candle:
        klines = await self.client.futures_klines(symbol=symbol, interval=self.candle_interval, limit=1)
        open_time, _open, high, low = klines[-1][:4]  # last kline = the ongoing candle
        return Candle(open_ms=int(open_time), high=_dec(high), low=_dec(low))

    async def observe_price(self, symbol: str, exchange_id: int, placed_ms: int) -> PriceObservation:
        last, candle = await asyncio.gather(self.last_price(symbol), self.current_candle(symbol))
        return observe_price(last, candle.open_ms, candle.high, candle.low, placed_ms)

    async def agg_trades(
        self, symbol: str, *, from_id: Optional[int] = None, start_ms: Optional[int] = None, limit: int = 1000
    ) -> list[AggTrade]:
        params: dict[str, Any] = {"symbol": symbol, "limit": limit}
        if from_id is not None:
            params["fromId"] = from_id
        elif start_ms is not None:
            earliest = int(time.time() * 1000) - self.trade_lookback_ms
            params["startTime"] = max(start_ms, earliest)  # clamp to the allowed window
        try:
            rows = await self.client.futures_aggregate_trades(**params)
        except BinanceAPIException as exc:
            if exc.code == _TRADE_WINDOW_EXCEEDED:
                raise TradeHistoryExpired(f"{symbol}: {exc.message}") from exc
            if exc.code == _TOO_MANY_REQUESTS or exc.status_code in (418, 429):
                raise RateLimited(f"{symbol}: {exc.message}", self.rate_limit_retry_seconds) from exc
            raise
        return [AggTrade(id=int(r["a"]), price=_dec(r["p"]), time_ms=int(r["T"])) for r in rows]

    async def sync_market(self, symbols: Iterable[str]) -> None:
        """Nothing to do: live orders are matched by the exchange itself."""

    # ---- trading -----------------------------------------------------------

    async def prepare_symbol(self, symbol: str, leverage: int, margin_type: str) -> int:
        try:
            await self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type.upper())
        except BinanceAPIException as exc:
            if exc.code != _NO_NEED_TO_CHANGE_MARGIN_TYPE:
                logger.warning("Margin type change on %s not applied: %s", symbol, exc)
        try:
            response = await self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
        except BinanceAPIException as exc:
            raise GatewayError(f"Could not set leverage on {symbol}: {exc}") from exc
        return int(response.get("leverage", leverage))

    async def place_order(self, symbol: str, spec: OrderSpec, trade_ref: int) -> PlacedOrder:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": spec.side,
            "type": spec.kind.value,
            "quantity": to_plain_str(spec.quantity),
        }
        if spec.kind is OrderKind.LIMIT:
            params.update(price=to_plain_str(spec.price), timeInForce="GTC")
        elif spec.kind is OrderKind.MARKET:
            pass  # no price: it fills at the market
        else:
            params["stopPrice"] = to_plain_str(spec.price)  # python-binance maps this to triggerPrice
        if spec.reduce_only:
            params["reduceOnly"] = "true"

        try:
            response = await self.client.futures_create_order(**params)
        except BinanceAPIException as exc:
            raise GatewayError(f"{spec.role.value} order rejected [{exc.code}]: {exc.message}") from exc

        if response.get("algoId") is not None:
            return PlacedOrder(exchange_id=int(response["algoId"]), is_algo=True)
        if response.get("orderId") is not None:
            return PlacedOrder(exchange_id=int(response["orderId"]), is_algo=False)
        raise GatewayError(f"{spec.role.value} order placed but the response had no id: {response}")

    async def get_order(self, symbol: str, exchange_id: int, is_algo: bool, trade_ref: int) -> OrderState:
        if not is_algo:
            return _state_from_order(await self.client.futures_get_order(symbol=symbol, orderId=exchange_id), exchange_id)

        algo = await self.client.futures_get_algo_order(algoId=exchange_id)
        actual_id = algo.get("actualOrderId")
        if actual_id:  # triggered: the real order carries the fill data
            return _state_from_order(await self.client.futures_get_order(symbol=symbol, orderId=int(actual_id)), int(actual_id))
        status = _ALGO_DEAD.get(str(algo.get("algoStatus", "")).upper(), base.NEW)
        return OrderState(status=status, executed_qty=Decimal("0"), avg_price=None)

    async def cancel_order(self, symbol: str, exchange_id: int, is_algo: bool) -> bool:
        """True if the exchange accepted the cancel. False is not fatal: the
        order may already be filled/cancelled, so callers re-read its state."""
        try:
            if is_algo:
                await self.client.futures_cancel_algo_order(algoId=exchange_id)
            else:
                await self.client.futures_cancel_order(symbol=symbol, orderId=exchange_id)
            return True
        except BinanceAPIException as exc:
            logger.warning("Cancel of %s order %s on %s refused: %s", "algo" if is_algo else "regular", exchange_id, symbol, exc)
            return False

    async def position_amount(self, symbol: str, trade_ref: int) -> Decimal:
        rows = await self.client.futures_position_information(symbol=symbol)
        return sum((abs(_dec(r.get("positionAmt"))) for r in rows), Decimal("0"))

    async def has_open_exposure(self, symbol: str) -> bool:
        """Any position or open (regular or conditional) order on the symbol,
        including ones this system did not create."""
        if await self.position_amount(symbol, 0) > 0:
            return True
        regular = await self.client.futures_get_open_orders(symbol=symbol)
        algo = await self.client.futures_get_open_algo_orders(symbol=symbol)
        algo_orders = algo if isinstance(algo, list) else algo.get("orders", [])
        return bool(regular) or bool(algo_orders)

    async def market_close(self, symbol: str, side: str, quantity: Decimal, trade_ref: int) -> None:
        try:
            await self.client.futures_create_order(
                symbol=symbol, side=side, type="MARKET", quantity=to_plain_str(quantity), reduceOnly="true"
            )
        except BinanceAPIException as exc:
            raise GatewayError(f"Market close of {symbol} failed [{exc.code}]: {exc.message}") from exc

    async def account_fills(self, symbol: str, since_ms: int) -> list[dict[str, Any]]:
        """The account's own fills on `symbol` since `since_ms`, with Binance's realized PnL and commission.
        Binance answers at most 7 days per request, so a longer trade is read window by window."""
        week_ms, fills, start, now = 7 * 24 * 3600 * 1000, [], since_ms, int(time.time() * 1000)
        while start <= now:
            rows = await self.client.futures_account_trades(symbol=symbol, startTime=start, endTime=min(start + week_ms - 1, now), limit=1000)
            fills += [
                {
                    "order_id": int(r["orderId"]), "side": r["side"], "price": _dec(r.get("price")), "qty": _dec(r.get("qty")),
                    "realized_pnl": _dec(r.get("realizedPnl")), "commission": _dec(r.get("commission")),
                    "commission_asset": str(r.get("commissionAsset", "")).upper(), "time": int(r["time"]),
                }
                for r in rows
            ]
            start += week_ms
        return sorted(fills, key=lambda f: f["time"])

    # ---- account view (Positions page) --------------------------------------

    async def position_snapshots(self) -> list[dict[str, Any]]:
        """Open positions with the numbers the Positions page shows."""
        risk_rows, account = await asyncio.gather(
            self.client.futures_position_information(), self.client.futures_account()
        )
        account_positions = {p["symbol"]: p for p in account.get("positions", [])}

        snapshots = []
        for row in risk_rows:
            amount = _dec(row.get("positionAmt"))
            if amount == 0:
                continue
            extra = account_positions.get(row["symbol"], {})
            snapshots.append(
                {
                    "symbol": row["symbol"],
                    "amount": amount,
                    "entry_price": _dec(row.get("entryPrice")),
                    "mark_price": _dec(row.get("markPrice")),
                    "liq_price": _dec(row.get("liquidationPrice")),
                    "unrealized_pnl": _first_nonzero(row.get("unRealizedProfit"), extra.get("unrealizedProfit")),
                    "notional": _first_nonzero(row.get("notional"), extra.get("notional")),
                    "leverage": int(_first_nonzero(extra.get("leverage"), row.get("leverage"))),
                    "initial_margin": _first_nonzero(
                        row.get("initialMargin"), extra.get("initialMargin"), extra.get("positionInitialMargin")
                    ),
                    "maint_margin": _first_nonzero(row.get("maintMargin"), extra.get("maintMargin")),
                }
            )
        return snapshots
