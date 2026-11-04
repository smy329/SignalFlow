"""
Turns a committed ParsedSignal into orders.

For every enabled mode (PAPER = forward test, LIVE = real Binance) it:
  guards -> plans (pure planner) -> prepares the symbol -> places
  ENTRY, then SL, then TPs (the order your old bot proved works).
If any order fails, everything already placed is cancelled and an entry that
managed to fill is closed at market: a position is never left unprotected.
Rejections are recorded (visible in admin) and never raised.
"""
import asyncio
import contextlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Collection, Mapping

from asgiref.sync import sync_to_async

from trading.config import TradingSettings
from trading.domain.planner import PRICE_ALREADY_HIT_SL, PRICE_ALREADY_HIT_TP1, plan_trade
from trading.domain.rules import PRICE_HIT_SL, PRICE_HIT_TP1
from trading.domain.strategy import REVERSAL_IGNORE, REVERSAL_ONE_WAY, SourceStrategy
from trading.domain.types import ALL_ROLE_VALUES, ENTRY_ROLES, OrderSpec, PlanResult, TradePlan, close_side
from trading.exchange import base
from trading.exchange.base import ExchangeGateway, PlacedOrder
from trading.scopes import TRADE_MODE_BY_SOURCE_MODE
from trading.services import switch_service
from trading.services.gateways import gateway_for
from trading.services import trade_repository as repo

logger = logging.getLogger(__name__)
sa = sync_to_async

EXPOSURE_ON_SYMBOL = "EXPOSURE_ON_SYMBOL"
REVERSAL_IGNORED = "REVERSAL_IGNORED"
PREPARE_FAILED = "PREPARE_FAILED"
ORDER_PLACEMENT_FAILED = "ORDER_PLACEMENT_FAILED"

# A plan the planner refused because TP1 / the stop-loss was reached before any entry: no order is
# placed and the trade is recorded as cancelled (it then shows in the Cancelled tab), not rejected.
_VOID_REASONS = {PRICE_ALREADY_HIT_TP1: PRICE_HIT_TP1, PRICE_ALREADY_HIT_SL: PRICE_HIT_SL}


@dataclass(frozen=True)
class _Placed:
    row_id: int
    result: PlacedOrder
    spec: OrderSpec


@dataclass(frozen=True)
class _Refusal:
    """Why a signal will not be traded. `cancelled=True` records it as Cancelled (the dashboard shows
    it, with `plan` when the planner still built one); otherwise it is recorded as Rejected."""

    code: str
    message: str
    plan: "TradePlan | None" = None
    cancelled: bool = False


class ExecutionService:
    def __init__(self, gateways: Mapping[str, ExchangeGateway], settings: TradingSettings) -> None:
        self._gateways = gateways
        self._settings = settings
        self._live_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._monitor = None  # set by the engine: closes the old trade when a source reverses (One-Way Mode)

    def attach_monitor(self, monitor) -> None:
        self._monitor = monitor

    def update_settings(self, settings: TradingSettings) -> None:
        """Called by the engine when the Settings page changed something."""
        self._settings = settings

    def modes_for(self, data: repo.SignalData, switched_on: Collection[str]) -> list[str]:
        """A Real source only ever trades live, a Forward source only ever trades on paper,
        and only while the Settings master flag AND the dashboard switch of that mode are on.
        A manual trade is the exception to the second part: the person chose it, so the ON/OFF button
        (which is about automatic Telegram signals) does not apply; the Settings master flag still does."""
        mode = TRADE_MODE_BY_SOURCE_MODE[data.source_mode]
        if mode == repo.MODE_LIVE:
            enabled = self._settings.trading_enabled
        else:
            enabled = self._settings.forward_test_enabled
        return [mode] if enabled and (data.manual or mode in switched_on) and mode in self._gateways else []

    async def handle_signal(self, signal_id: int) -> None:
        data = await sa(repo.load_signal)(signal_id)
        if data is None:
            return
        modes = self.modes_for(data, await sa(switch_service.switched_on_modes)())
        if modes:
            await asyncio.gather(*(self._run_mode(mode, data) for mode in modes))

    async def recover_interrupted_trades(self) -> None:
        """A trade still PLACING at startup was interrupted mid-placement (crash/restart).
        Cancel whatever it managed to place, flatten any position, and mark it failed."""
        for trade in await sa(repo.snapshot_placing_trades)():
            gateway = gateway_for(self._gateways, trade.mode, trade.testnet)
            unsafe = gateway is None and trade.mode == repo.MODE_LIVE and bool(trade.rows(*repo.ENTRY_ROLE_VALUES))
            if gateway is not None:
                for order in trade.rows(*ALL_ROLE_VALUES):
                    try:
                        await gateway.cancel_order(trade.symbol, order.exchange_id, order.is_algo)
                    except Exception:  # noqa: BLE001
                        unsafe = True
                        logger.exception("Recovery: could not cancel order %s of trade %s", order.exchange_id, trade.id)
                if trade.mode == repo.MODE_LIVE and trade.rows(*repo.ENTRY_ROLE_VALUES):
                    try:
                        position = await gateway.position_amount(trade.symbol, trade.id)
                        if position > 0:
                            await gateway.market_close(trade.symbol, close_side(trade.side), position, trade.id)
                    except Exception:  # noqa: BLE001
                        unsafe = True
                        logger.exception("Recovery: could not flatten %s for trade %s", trade.symbol, trade.id)
            if unsafe:
                logger.critical("MANUAL ATTENTION: interrupted trade %s on %s may have left orders or a position", trade.id, trade.symbol)
            await sa(repo.fail_trade)(trade.id, "INTERRUPTED" + ("_UNSAFE" if unsafe else ""), "The process stopped while orders were being placed.")

    async def _run_mode(self, mode: str, data: repo.SignalData) -> None:
        try:
            # Live trades on one symbol are serialized; paper trades are independent.
            lock = self._live_locks[data.input.symbol] if mode == repo.MODE_LIVE else contextlib.nullcontext()
            async with lock:
                await self._execute(mode, data)
        except Exception:  # noqa: BLE001 - one signal must never take the engine down
            logger.exception("Unhandled error executing %s trade for signal %s", mode, data.signal_id)

    async def _execute(self, mode: str, data: repo.SignalData) -> None:
        gateway = self._gateways[mode]
        signal = data.input

        # A live trade remembers the Binance environment it is opened on and is managed there until it ends.
        testnet = mode == repo.MODE_LIVE and bool(getattr(gateway, "testnet", False))
        refusal = await self._reverse_opposite(mode, gateway, data)
        trade_id, rejected = await sa(repo.begin_trade)(data.signal_id, mode, signal.symbol, signal.side, testnet=testnet)
        if trade_id is not None and refusal is not None:  # also replaces begin_trade's generic "active trade exists"
            if refusal.cancelled:
                if refusal.plan is not None:
                    await sa(repo.save_plan)(trade_id, refusal.plan, data.strategy)
                await sa(repo.cancel_trade)(trade_id, refusal.code, refusal.message)
            else:
                await sa(repo.reject_trade)(trade_id, refusal.code, refusal.message)
            rejected = refusal.code
        if trade_id is None or rejected:
            logger.info("%s %s signal %s skipped (%s)", mode, signal.symbol, data.signal_id, rejected or "already handled")
            return

        try:
            plan = await self._prepare(gateway, mode, trade_id, signal, data.strategy)
        except Exception as exc:  # noqa: BLE001 - nothing was placed yet
            logger.exception("Preparing %s trade %s failed", mode, trade_id)
            await sa(repo.fail_trade)(trade_id, PREPARE_FAILED, str(exc))
            return
        if plan is None:
            return  # rejection already recorded

        await sa(repo.save_plan)(trade_id, plan, data.strategy)
        await self._place_all(gateway, mode, trade_id, plan)

    async def _reverse_opposite(
        self, mode: str, gateway: ExchangeGateway, data: repo.SignalData
    ) -> "_Refusal | None":
        """A signal opposite to a trade this same source already has active on the coin.

        ONE_WAY: close that trade fully, then let the new signal trade as usual (close and reverse). The new
                 signal is planned first: if it could not be traded anyway, the old trade is kept and the new
                 signal is recorded as Cancelled (visible in the dashboard) instead of closing for nothing.
        IGNORE:  skip the new signal; the old trade carries on.
        Returns why the new signal will not be traded, None to carry on normally. If the old trade cannot be
        closed the signal carries on too, and the normal guards (one live trade / no exposure per symbol)
        still decide.
        """
        signal, strategy = data.input, data.strategy
        if strategy.reversal not in (REVERSAL_ONE_WAY, REVERSAL_IGNORE):
            return None
        if await sa(repo.signal_has_trade)(data.signal_id, mode):  # handled before: nothing to reverse
            return None
        old = await sa(repo.opposite_active_trades)(data.source_id, mode, signal.symbol, signal.side)
        if not old:
            return None

        if strategy.reversal == REVERSAL_IGNORE:
            return _Refusal(
                REVERSAL_IGNORED, f"Ignored: this source already has an active opposite trade on {signal.symbol}."
            )
        if self._monitor is None:
            return None
        closable = [trade_id for trade_id, status in old if status != repo.ST_PLACING]
        if not closable:
            return None

        try:  # would the new signal trade at all? Never close a position for a signal that cannot open
            await gateway.refresh_filters_if_stale()
            filters = gateway.filters(signal.symbol)
            max_leverage = await gateway.max_leverage(signal.symbol) if filters else 1
            result = plan_trade(
                signal, filters, max_leverage=max_leverage, market_price=await gateway.last_price(signal.symbol),
                strategy=strategy,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reversal pre-check failed for signal %s", data.signal_id)
            return _Refusal(PREPARE_FAILED, f"Existing {signal.symbol} trade kept (reversal skipped): {exc}")
        if not result.success:
            message = f"Existing {signal.symbol} trade kept (reversal skipped): {result.error_message}"
            code = _VOID_REASONS.get(result.error_code, result.error_code or "") if result.void else (result.error_code or "")
            return _Refusal(code, message, result.plan if result.void else None, cancelled=True)

        for trade_id in closable:
            if not await self._monitor.reverse_close(trade_id):
                logger.error("Reversal: old trade %s on %s could not be closed", trade_id, signal.symbol)
        return None

    async def _refuse(self, trade_id: int, result: PlanResult, strategy: SourceStrategy) -> None:
        """Record a planner refusal. A plan the planner completed but would not place (TP1 / the stop-loss
        was already reached) keeps its levels on the trade and is cancelled, so the Cancelled tab shows it;
        anything else is rejected."""
        if result.void and result.plan is not None:
            await sa(repo.save_plan)(trade_id, result.plan, strategy)
            await sa(repo.cancel_trade)(
                trade_id, _VOID_REASONS.get(result.error_code, result.error_code or ""), result.error_message or ""
            )
        else:
            await sa(repo.reject_trade)(trade_id, result.error_code, result.error_message)

    async def _prepare(
        self, gateway: ExchangeGateway, mode: str, trade_id: int, signal, strategy: SourceStrategy
    ) -> "TradePlan | None":
        await gateway.refresh_filters_if_stale()
        filters = gateway.filters(signal.symbol)
        market = await gateway.last_price(signal.symbol)
        max_leverage = await gateway.max_leverage(signal.symbol) if filters else 1

        if mode == repo.MODE_LIVE and await gateway.has_open_exposure(signal.symbol):
            await sa(repo.reject_trade)(
                trade_id, EXPOSURE_ON_SYMBOL, f"The account already has a position or open orders on {signal.symbol}."
            )
            return None

        result = plan_trade(signal, filters, max_leverage=max_leverage, market_price=market, strategy=strategy)
        if not result.success:
            await self._refuse(trade_id, result, strategy)
            return None

        applied = await gateway.prepare_symbol(signal.symbol, result.plan.leverage, result.plan.margin_type)
        if applied != result.plan.leverage:  # exchange applied a different leverage: size the trade for it
            result = plan_trade(signal, filters, max_leverage=applied, market_price=market, strategy=strategy)
            if not result.success:
                await self._refuse(trade_id, result, strategy)
                return None
        return result.plan

    async def _place_all(self, gateway: ExchangeGateway, mode: str, trade_id: int, plan: TradePlan) -> None:
        placed: list[_Placed] = []
        pending_row = None
        placed_ms = gateway.now_ms()
        try:
            for spec in (*plan.entries, plan.stop_loss, *plan.take_profits):
                pending_row = await sa(repo.add_order)(trade_id, spec)
                result = await gateway.place_order(plan.symbol, spec, trade_id)
                await sa(repo.set_order_placed)(pending_row, result)
                placed.append(_Placed(pending_row, result, spec))
                pending_row = None
        except Exception as exc:  # noqa: BLE001 - GatewayError or anything unexpected
            logger.error("%s trade %s: order placement failed: %s", mode, trade_id, exc)
            await self._rollback(gateway, trade_id, plan, placed, pending_row, exc)
            return

        await sa(repo.mark_pending_entry)(trade_id, datetime.fromtimestamp(placed_ms / 1000, tz=timezone.utc))
        logger.info("%s trade %s placed: %s %s qty %s", mode, trade_id, plan.side, plan.symbol, plan.quantity)

    async def _rollback(
        self, gateway: ExchangeGateway, trade_id: int, plan: TradePlan, placed: list[_Placed],
        pending_row: "int | None", cause: Exception,
    ) -> None:
        unsafe = False
        if pending_row is not None:
            await sa(repo.set_order_status)(pending_row, base.REJECTED)

        for item in reversed(placed):
            try:
                if await gateway.cancel_order(plan.symbol, item.result.exchange_id, item.result.is_algo):
                    await sa(repo.set_order_status)(item.row_id, base.CANCELED)
            except Exception:  # noqa: BLE001
                unsafe = True
                logger.exception("Rollback: could not cancel %s order %s", item.spec.role.value, item.result.exchange_id)

        entries = [p for p in placed if p.spec.role in ENTRY_ROLES]
        try:  # an entry may have filled before it could be cancelled
            filled = False
            for entry in entries:
                state = await gateway.get_order(plan.symbol, entry.result.exchange_id, entry.result.is_algo, trade_id)
                filled = filled or state.executed_qty > 0
            if filled:
                position = await gateway.position_amount(plan.symbol, trade_id)
                if position > 0:
                    await gateway.market_close(plan.symbol, close_side(plan.side), position, trade_id)
        except Exception:  # noqa: BLE001
            unsafe = True
            logger.exception("Rollback: could not verify/close the entries of trade %s", trade_id)

        if unsafe:
            logger.critical("MANUAL ATTENTION: trade %s on %s may have left orders or a position open", trade_id, plan.symbol)
        code = ORDER_PLACEMENT_FAILED + ("_UNSAFE" if unsafe else "")
        await sa(repo.fail_trade)(trade_id, code, str(cause))
