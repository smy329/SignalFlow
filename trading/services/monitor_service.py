"""
The monitor: every few seconds it advances every active trade.

A trade has one or two entry orders (a source may trade "both entries").

PENDING_ENTRY  read the entries first. Nothing filled -> cancel the unfilled entries once price
               reached TP1 or the SL. As soon as an entry is filled the trade is OPEN; the
               exits are resized to what actually filled.
OPEN           SL filled -> cancel everything else, close.  All TPs filled -> cancel SL, close.
               A second entry that fills later grows the position and the exits are resized.
               The SL moves to the average entry once the source's SL rule fires (price touches
               the signal's TP1, or a chosen TP is filled); a second entry still waiting is
               cancelled then. Position gone -> clean up.  No live SL -> re-place it (or close).
Exits are tracked by algoId (conditional orders live on Binance's algo endpoints), which is
what the old monitor got wrong.
"""
import asyncio
import logging
import time
from decimal import Decimal
from typing import Mapping, Optional

import aiohttp
from asgiref.sync import sync_to_async
from django.utils import timezone

from trading.config import TradingSettings
from trading.domain.exchange_result import ExchangeResult, exchange_result
from trading.domain.pnl import Fill, average_price, trade_pnl
from trading.domain.planner import plan_exits
from trading.domain.quantize import floor_to_step, round_to_tick
from trading.domain.rules import PriceObservation, pending_entry_cancel_reason, price_reached
from trading.domain.strategy import SL_AFTER_FILLING_TP, sl_trigger_target
from trading.domain.types import ENTRY_ROLES, TP_ROLES, OrderKind, OrderRole, OrderSpec, close_side, tp_number
from trading.exchange import base
from trading.exchange.base import ExchangeGateway
from trading.services import trade_repository as repo
from trading.services.gateways import gateway_for
from trading.services.trade_repository import OrderSnap, TradeSnap

logger = logging.getLogger(__name__)
sa = sync_to_async

SL = OrderRole.SL.value
ENTRY_ROLES_V = tuple(r.value for r in ENTRY_ROLES)
TP_ROLES_V = tuple(r.value for r in TP_ROLES)
EXIT_ROLES = (SL, *TP_ROLES_V)
WORKING = base.WORKING_STATUSES
DEAD = base.DEAD_STATUSES

MANUAL_CLOSE_CODE = "CLOSED_MANUALLY"  # history reason of a trade closed with the Close button
# outcomes of close_now()
CLOSED, CANCELLED, ALREADY_OVER, PLACING, NO_GATEWAY, FAILED = (
    "CLOSED", "CANCELLED", "ALREADY_OVER", "PLACING", "NO_GATEWAY", "FAILED",
)
# extra outcomes of trade_now()
TRADED, NOT_PENDING, TOO_SMALL = "TRADED", "NOT_PENDING", "TOO_SMALL"


class MonitorService:
    def __init__(self, gateways: Mapping[str, ExchangeGateway], settings: TradingSettings) -> None:
        self._gateways = gateways
        self._settings = settings
        self._paper_sync_resume_at = 0.0
        self._trade_locks: dict[int, asyncio.Lock] = {}  # one step / one reversal at a time per trade
        self._reversed: set[int] = set()  # trades closed by a reversal: a stale poll snapshot must not touch them

    def _lock(self, trade_id: int) -> asyncio.Lock:
        return self._trade_locks.setdefault(trade_id, asyncio.Lock())

    def update_settings(self, settings: TradingSettings) -> None:
        """Called by the engine when the Settings page changed something (poll interval, concurrency, ...)."""
        self._settings = settings

    async def run_forever(self) -> None:
        logger.info("Trade monitor started (every %ss)", self._settings.poll_seconds)
        while True:
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("Monitor poll failed")
            await asyncio.sleep(max(0.5, self._settings.poll_seconds - (time.monotonic() - started)))

    async def poll_once(self) -> None:
        trades = await sa(repo.snapshot_active_trades)()
        await self._sync_paper_market(trades)

        active_ids = {t.id for t in trades}  # forget locks / reversal marks of trades that are over
        self._reversed &= active_ids
        for trade_id in [i for i in self._trade_locks if i not in active_ids]:
            del self._trade_locks[trade_id]

        gate = asyncio.Semaphore(max(1, self._settings.max_concurrent_trades))

        async def guarded(trade: TradeSnap) -> None:
            async with gate:
                gateway = gateway_for(self._gateways, trade.mode, trade.testnet)
                if gateway is None:
                    logger.warning(
                        "%s trade %s is active but no %s gateway is available%s",
                        trade.mode, trade.id, trade.mode, " for its environment (testnet)" if trade.testnet else "",
                    )
                    return
                async with self._lock(trade.id):
                    if trade.id in self._reversed:  # closed by a reversal while this poll was running
                        return
                    try:
                        await self._step(gateway, trade)
                    except (TimeoutError, aiohttp.ClientError, ConnectionError) as exc:
                        # Network hiccup talking to Binance: nothing is lost, the next poll retries.
                        logger.warning(
                            "Network problem managing %s trade %s (%s): %s; will retry on the next poll",
                            trade.mode, trade.id, trade.symbol, type(exc).__name__,
                        )
                    except Exception:  # noqa: BLE001 - one trade must not stop the others
                        logger.exception("Error managing %s trade %s (%s)", trade.mode, trade.id, trade.symbol)

        await asyncio.gather(*(guarded(t) for t in trades))

    async def _sync_paper_market(self, trades: list[TradeSnap]) -> None:
        paper = self._gateways.get(repo.MODE_PAPER)
        if paper is None or time.monotonic() < self._paper_sync_resume_at:
            return
        symbols = {t.symbol for t in trades if t.mode == repo.MODE_PAPER}
        try:
            await paper.sync_market(symbols)
            for symbol in symbols:
                await sa(repo.update_paper_cursors)(symbol, paper.cursor_for(symbol))
        except base.RateLimited as exc:
            self._paper_sync_resume_at = time.monotonic() + exc.retry_after_s
            logger.warning("Binance rate limit hit; forward-test sync paused for %.0fs", exc.retry_after_s)
        except Exception:  # noqa: BLE001 - retried on the next poll from the saved cursor
            logger.exception("Forward-test market sync failed")

    # ---- per-trade state machine ---------------------------------------------

    async def _step(self, gateway: ExchangeGateway, trade: TradeSnap) -> None:
        if trade.status == repo.ST_PENDING:
            await self._step_pending(gateway, trade)
        elif trade.status == repo.ST_OPEN:
            await self._step_open(gateway, trade)

    # -- entries --

    @staticmethod
    def _entry_rows(trade: TradeSnap) -> list[OrderSnap]:
        return trade.rows(*ENTRY_ROLES_V)

    @staticmethod
    def _fills(entries: list[OrderSnap]) -> tuple[Decimal, Optional[Decimal]]:
        """(total filled quantity, quantity-weighted average price) over every entry order."""
        filled = sum((o.executed_quantity for o in entries), Decimal("0"))
        if filled <= 0:
            return filled, None
        return filled, sum((o.executed_quantity * (o.avg_fill_price or o.price) for o in entries), Decimal("0")) / filled

    async def _refresh_working(self, gateway, trade: TradeSnap, orders: list[OrderSnap]) -> None:
        for order in orders:
            if order.status in WORKING:
                await self._refresh(gateway, trade, order)

    async def _entry_void_reason(self, gateway, trade: TradeSnap, working: list[OrderSnap]) -> Optional[str]:
        """Price reached TP1 or the SL while an entry was still unfilled -> that entry is void."""
        observation = await self._observe(gateway, trade, working[0])
        return pending_entry_cancel_reason(trade.side, trade.targets[0], trade.stoploss, observation)

    async def _observe(self, gateway, trade: TradeSnap, order: OrderSnap) -> PriceObservation:
        placed_ms = int(trade.placed_at.timestamp() * 1000) if trade.placed_at else 0
        return await gateway.observe_price(trade.symbol, order.exchange_id, placed_ms)

    async def _step_pending(self, gateway: ExchangeGateway, trade: TradeSnap) -> None:
        entries = self._entry_rows(trade)
        if not entries:
            logger.error("Trade %s is pending but has no entry order on the exchange", trade.id)
            return
        await self._refresh_working(gateway, trade, entries)
        if await self._settle_entries(gateway, trade, None):
            return

        working = [o for o in entries if o.status in WORKING]
        reason = await self._entry_void_reason(gateway, trade, working)
        if reason:
            await self._cancel_entries(gateway, trade, working)
            await self._settle_entries(gateway, trade, reason)

    async def _settle_entries(self, gateway, trade: TradeSnap, reason: Optional[str]) -> bool:
        """Move a pending trade on when its entries have settled. True when it did (opened or cancelled)."""
        entries = self._entry_rows(trade)
        working = [o for o in entries if o.status in WORKING]
        filled, avg = self._fills(entries)

        if filled > 0 and not any(o.status == base.PARTIALLY_FILLED for o in entries):
            await self._open_trade(gateway, trade, filled, avg)
            return True
        if filled == 0 and not working:  # every entry died unfilled
            if reason is None:  # cancelled without us asking (by hand, or by the forward-test engine the
                # moment price reached TP1/SL): name the real reason when the price data explains it.
                observation = await self._observe(gateway, trade, entries[0])
                reason = pending_entry_cancel_reason(trade.side, trade.targets[0], trade.stoploss, observation)
                reason = reason or f"ENTRY_{entries[0].status}"
            await self._abort_pending(gateway, trade, reason)
            return True
        if reason is not None and not working:  # cancel requested, an entry kept a partial fill
            logger.warning("Trade %s: entry still working after cancel request, retrying next poll", trade.id)
        return False

    async def _cancel_entries(self, gateway, trade: TradeSnap, working: list[OrderSnap]) -> None:
        await self._cancel_orders(gateway, trade, working)  # the exchange has the last word: refreshed if it filled

    async def _abort_pending(self, gateway, trade: TradeSnap, reason: str) -> None:
        await self._cancel_working(gateway, trade, *EXIT_ROLES)
        await self._close(trade, repo.ST_CANCELLED, reason)
        logger.info("Trade %s (%s) cancelled before entry: %s", trade.id, trade.symbol, reason)

    async def _open_trade(self, gateway, trade: TradeSnap, filled: Decimal, avg: Decimal) -> None:
        """Entries are filled (fully, or the unfilled rest was cancelled): the trade is open."""
        if trade.status == repo.ST_PENDING:
            await sa(repo.open_trade)(trade.id, filled, avg, timezone.now())
            logger.info("Trade %s (%s) entry filled: %s @ %s", trade.id, trade.symbol, filled, avg)
        else:
            await sa(repo.update_fills)(trade.id, filled, avg)
            logger.info("Trade %s (%s) position is now %s @ %s", trade.id, trade.symbol, filled, avg)
        trade.status, trade.filled_quantity, trade.avg_entry_price = repo.ST_OPEN, filled, avg
        if await self._align_exits(gateway, trade, filled):
            await self._step_open(gateway, trade)

    async def _align_exits(self, gateway, trade: TradeSnap, filled: Decimal) -> bool:
        """Make the SL / TPs cover exactly `filled`. New exits are placed first, then the old ones
        are removed. False when the filled part cannot be protected (it is then closed)."""
        old_exits = [o for o in trade.rows(*EXIT_ROLES) if o.status in WORKING]
        sl = next((o for o in reversed(old_exits) if o.role == SL), None)
        if sl is not None and sl.quantity == filled:
            return True
        if any(o.status == base.FILLED for o in trade.rows(*TP_ROLES_V)):
            logger.warning("Trade %s: size changed to %s after a take-profit filled; exits left as they are", trade.id, filled)
            return True

        filters = gateway.filters(trade.symbol)
        try:
            if filters is None or filled < filters.min_qty:
                raise ValueError(f"filled {filled} is below the minimum order size")
            stop_loss, take_profits = plan_exits(
                trade.side, filled, trade.stoploss, trade.targets, filters, trade.strategy.tp_percents,
                trade.strategy.default_tp
            )
            for spec in (stop_loss, *take_profits):
                await self._place(gateway, trade, spec)
        except Exception as exc:  # noqa: BLE001
            logger.error("Trade %s: cannot protect the filled %s (%s), closing it", trade.id, filled, exc)
            await self._cancel_orders(gateway, trade, [o for o in trade.rows(*EXIT_ROLES) if o.status in WORKING])
            await self._wrap_up(gateway, trade, repo.ST_CLOSED, "FILL_UNPROTECTED", flatten=True)
            return False
        await self._cancel_orders(gateway, trade, old_exits)
        logger.info("Trade %s: exits resized for %s", trade.id, filled)
        return True

    # -- open trades --

    async def _step_open(self, gateway: ExchangeGateway, trade: TradeSnap) -> None:
        entries = self._entry_rows(trade)
        await self._refresh_working(gateway, trade, [*entries, *trade.rows(*EXIT_ROLES)])

        sl_rows, tp_rows = trade.rows(SL), trade.rows(*TP_ROLES_V)
        filled_sl = next((o for o in sl_rows if o.status == base.FILLED), None)
        if filled_sl is not None:
            code = "SL_AT_ENTRY_HIT" if filled_sl.price != trade.stoploss else "SL_HIT"
            await self._wrap_up(gateway, trade, repo.ST_CLOSED, code)
            return

        live_tps = [o for o in tp_rows if o.status not in DEAD]
        if live_tps and all(o.status == base.FILLED for o in live_tps):
            await self._wrap_up(gateway, trade, repo.ST_CLOSED, "TP_COMPLETED")
            return

        if not await self._manage_late_entries(gateway, trade, entries):
            return

        if not self._sl_moved(trade) and await self._sl_rule_fired(gateway, trade):
            if await self._move_sl_to_entry(gateway, trade):
                await self._cancel_working(gateway, trade, *ENTRY_ROLES_V)  # no more averaging after the SL is at entry

        await self._audit_protection(gateway, trade)

    async def _manage_late_entries(self, gateway, trade: TradeSnap, entries: list[OrderSnap]) -> bool:
        """The second entry of an open trade: void it when price reached TP1 / SL, and grow the exits
        when it fills. False when the trade was closed because the bigger position cannot be protected."""
        working = [o for o in entries if o.status in WORKING]
        if working:
            reason = await self._entry_void_reason(gateway, trade, working)
            if reason:
                logger.info("Trade %s: unfilled entry cancelled (%s)", trade.id, reason)
                await self._cancel_entries(gateway, trade, working)

        filled, avg = self._fills(entries)
        if filled > trade.filled_quantity and not any(o.status == base.PARTIALLY_FILLED for o in entries):
            await sa(repo.update_fills)(trade.id, filled, avg)
            trade.filled_quantity, trade.avg_entry_price = filled, avg
            logger.info("Trade %s (%s) position is now %s @ %s", trade.id, trade.symbol, filled, avg)
            return await self._align_exits(gateway, trade, filled)
        return True

    async def _sl_rule_fired(self, gateway, trade: TradeSnap) -> bool:
        """Is it time to move the SL to entry? Decided by the source's SL strategy."""
        strategy = trade.strategy
        if strategy.sl_mode == SL_AFTER_FILLING_TP:
            present = sorted({tp_number(o.role) for o in trade.rows(*TP_ROLES_V)})
            target = sl_trigger_target(strategy, present)
            return target is not None and any(
                o.status == base.FILLED for o in trade.rows(*TP_ROLES_V) if tp_number(o.role) == target
            )
        # SL_TP1_TOUCH: forced as soon as price touches the signal's TP1 level, even if TP1 closes nothing.
        anchor = next((o for o in reversed(trade.rows(SL)) if o.status in WORKING), None)
        if anchor is None or not trade.targets:
            return False
        return price_reached(trade.side, trade.targets[0], await self._observe(gateway, trade, anchor))

    async def _move_sl_to_entry(self, gateway, trade: TradeSnap) -> bool:
        """SL to the actual average entry price for what is left. New SL first, then the old one
        is cancelled, so there is never a gap. True when the move happened."""
        filters = gateway.filters(trade.symbol)
        remaining = await gateway.position_amount(trade.symbol, trade.id)
        if filters is None or remaining <= 0:
            return False
        entry_price = round_to_tick(trade.avg_entry_price or trade.entry_price, filters.tick_size)
        spec = OrderSpec(
            OrderRole.SL, OrderKind.STOP_MARKET, close_side(trade.side),
            floor_to_step(remaining, filters.step_size), entry_price, reduce_only=True,
        )
        old_sl = [o for o in trade.rows(SL) if o.status in WORKING]
        try:
            await self._place(gateway, trade, spec)
        except Exception as exc:  # noqa: BLE001 - the old SL keeps protecting; retried next poll
            logger.warning("Trade %s: could not move SL to entry %s (%s), will retry", trade.id, entry_price, exc)
            return False
        await self._cancel_orders(gateway, trade, old_sl)
        logger.info("Trade %s (%s): SL moved to entry %s for %s", trade.id, trade.symbol, entry_price, spec.quantity)
        return True

    async def _audit_protection(self, gateway, trade: TradeSnap) -> None:
        remaining = await gateway.position_amount(trade.symbol, trade.id)
        working_sl = [o for o in trade.rows(SL) if o.status in WORKING]

        if remaining <= 0:  # closed some other way (manually, liquidation, fills we could not see)
            await self._wrap_up(gateway, trade, repo.ST_CLOSED, "CLOSED_EXTERNALLY")
            return
        if len(working_sl) > 1:  # a cancel failed after a move: keep only the newest
            await self._cancel_orders(gateway, trade, working_sl[:-1])
        elif not working_sl:
            await self._replace_missing_sl(gateway, trade, remaining)

    async def _replace_missing_sl(self, gateway, trade: TradeSnap, remaining) -> None:
        filters = gateway.filters(trade.symbol)
        price = (trade.avg_entry_price or trade.entry_price) if self._sl_moved(trade) else trade.stoploss
        try:
            if filters is None:
                raise ValueError("symbol rules unavailable")
            spec = OrderSpec(
                OrderRole.SL, OrderKind.STOP_MARKET, close_side(trade.side),
                floor_to_step(remaining, filters.step_size), round_to_tick(price, filters.tick_size), reduce_only=True,
            )
            await self._place(gateway, trade, spec)
            logger.warning("Trade %s had no live stop-loss: re-placed it at %s", trade.id, spec.price)
        except Exception as exc:  # noqa: BLE001
            logger.error("Trade %s: cannot restore the stop-loss (%s), closing the position", trade.id, exc)
            await self._wrap_up(gateway, trade, repo.ST_CLOSED, "PROTECTION_LOST", flatten=True)

    @staticmethod
    def _sl_moved(trade: TradeSnap) -> bool:
        """True once a stop-loss at the entry price has been placed (any SL row
        whose price differs from the original stop-loss)."""
        return any(o.price != trade.stoploss for o in trade.rows(SL))

    # ---- reversal (One-Way Mode) ---------------------------------------------------

    async def reverse_close(self, trade_id: int) -> bool:
        """The trade's source sent the opposite signal: cancel what is working and flatten the position.
        True when the trade is no longer active. On a failure the trade is left to the normal monitor
        (which re-places a missing stop-loss), so a position is never abandoned unprotected."""
        ok, _ = await self._close_at_market(trade_id, "REVERSED")
        return ok

    async def close_now(self, trade_id: int) -> str:
        """The Close button: cancel the trade's entry / take-profit / stop-loss orders and close what is left of
        the position at market, exactly like a reversal. A pending trade (no entry filled yet) just has its
        entry orders cancelled and moves to Cancelled; an entry that filled meanwhile is closed at market.
        Returns one of CLOSED, CANCELLED, ALREADY_OVER, PLACING, NO_GATEWAY, FAILED."""
        _, outcome = await self._close_at_market(trade_id, MANUAL_CLOSE_CODE)
        return outcome

    async def trade_now(self, trade_id: int) -> tuple[str, str]:
        """The Trade Now button on a pending trade: the nearest entry is bought / sold at the current market
        price instead of waiting for its limit price. A second entry order stays where it is.

        The size is worked out again at the market price from the margin share of that entry. An entry that is
        already partly filled keeps what it has and only the missing rest is bought at market. If price has
        already reached TP1 or the stop-loss, nothing is traded and the trade is cancelled with that reason.
        Returns (outcome, detail): TRADED, CANCELLED (detail = reason code), NOT_PENDING, ALREADY_OVER, PLACING,
        NO_GATEWAY, TOO_SMALL or FAILED."""
        async with self._lock(trade_id):
            trade = await sa(repo.snapshot_trade)(trade_id)
            if trade is None:
                return ALREADY_OVER, ""
            if trade.status == repo.ST_PLACING:
                return PLACING, ""
            if trade.status == repo.ST_OPEN:
                return NOT_PENDING, ""
            if trade.status != repo.ST_PENDING:
                return ALREADY_OVER, ""
            gateway = gateway_for(self._gateways, trade.mode, trade.testnet)
            if gateway is None:
                return NO_GATEWAY, ""
            try:
                return await self._trade_now(gateway, trade)
            except Exception:  # noqa: BLE001
                logger.exception("Trade now: could not trade %s (%s)", trade.id, trade.symbol)
                return FAILED, ""

    async def _trade_now(self, gateway, trade: TradeSnap) -> tuple[str, str]:
        entries = self._entry_rows(trade)
        await self._refresh_working(gateway, trade, entries)
        if await self._settle_entries(gateway, trade, None):  # it settled while we looked
            return (NOT_PENDING if trade.status == repo.ST_OPEN else ALREADY_OVER), ""

        working = [o for o in entries if o.status in WORKING]
        reason = await self._entry_void_reason(gateway, trade, working)
        if reason:  # price already reached TP1 / SL: no trade
            await self._cancel_entries(gateway, trade, working)
            await self._settle_entries(gateway, trade, reason)
            return (CANCELLED, reason) if trade.status == repo.ST_CANCELLED else (NOT_PENDING, "")

        target = min(working, key=lambda o: ENTRY_ROLES_V.index(o.role))  # the nearest entry (ENTRY before ENTRY2)
        filters = gateway.filters(trade.symbol)
        market = await gateway.last_price(trade.symbol)
        if filters is None:
            return FAILED, ""
        # same margin share, current price: planned quantity x planned price / market price
        total = floor_to_step(target.quantity * target.price / market, filters.step_size)

        def size_ok(quantity: Decimal) -> bool:
            return filters.min_qty <= quantity <= filters.max_qty and quantity * market >= filters.min_notional

        if target.executed_quantity <= 0 and not size_ok(total):
            return TOO_SMALL, ""  # checked before anything is cancelled

        original = target.spec()
        await self._cancel_orders(gateway, trade, [target])
        await self._refresh(gateway, trade, target)  # what the entry really filled before it was cancelled
        buy = total - target.executed_quantity
        if buy > 0 and not size_ok(buy):
            buy = Decimal("0")  # a partly filled entry whose rest is too small: keep what it has
        if buy > 0:
            spec = OrderSpec(
                OrderRole(target.role), OrderKind.MARKET, trade.side, buy, market, reduce_only=False,
                guard_tp=trade.targets[0] if trade.targets else None, guard_sl=trade.stoploss,
            )
            try:
                snap = await self._place(gateway, trade, spec)
                await self._refresh(gateway, trade, snap)
            except Exception:  # noqa: BLE001 - put the limit entry back so the trade is not left without one
                logger.exception("Trade now: market order failed for trade %s", trade.id)
                if target.executed_quantity <= 0:
                    try:
                        await self._place(gateway, trade, original)
                    except Exception:  # noqa: BLE001
                        logger.exception("Trade now: could not restore the entry of trade %s", trade.id)
                return FAILED, ""
        await self._settle_entries(gateway, trade, None)  # opens the trade now instead of on the next poll
        return TRADED, ""

    async def _close_at_market(self, trade_id: int, code: str) -> tuple[bool, str]:
        """(trade is no longer active, outcome). `code` is the history reason of the closed trade."""
        async with self._lock(trade_id):
            trade = await sa(repo.snapshot_trade)(trade_id)
            if trade is None or trade.status not in (repo.ST_PENDING, repo.ST_OPEN):
                over = trade is None or trade.status != repo.ST_PLACING  # already over: nothing to close
                return over, ALREADY_OVER if over else PLACING
            gateway = gateway_for(self._gateways, trade.mode, trade.testnet)
            if gateway is None:
                logger.error("Close: no %s gateway to close trade %s", trade.mode, trade.id)
                return False, NO_GATEWAY
            self._reversed.add(trade.id)  # a stale poll snapshot must not touch the trade while / after it is closed
            try:
                await self._refresh_working(gateway, trade, trade.rows(*ENTRY_ROLES_V, *EXIT_ROLES))
                filled, _ = self._fills(self._entry_rows(trade))
                price = None
                if filled > 0:
                    price = await gateway.last_price(trade.symbol)  # the market close is not a recorded fill
                status = repo.ST_CLOSED if filled > 0 else repo.ST_CANCELLED
                await self._wrap_up(gateway, trade, status, code, flatten=True, close_price=price)
            except Exception:  # noqa: BLE001
                self._reversed.discard(trade.id)
                logger.exception("Close (%s): could not close trade %s (%s)", code, trade.id, trade.symbol)
                return False, FAILED
            logger.info("Trade %s (%s) closed: %s", trade.id, trade.symbol, code)
            return True, CLOSED if status == repo.ST_CLOSED else CANCELLED

    # ---- exchange helpers -----------------------------------------------------

    async def _refresh(self, gateway, trade: TradeSnap, order: OrderSnap) -> base.OrderState:
        state = await gateway.get_order(trade.symbol, order.exchange_id, order.is_algo, trade.id)
        await sa(repo.update_order_state)(order.id, state)
        order.status, order.executed_quantity, order.avg_fill_price = state.status, state.executed_qty, state.avg_price
        return state

    async def _place(self, gateway, trade: TradeSnap, spec: OrderSpec) -> OrderSnap:
        row_id = await sa(repo.add_order)(trade.id, spec)
        try:
            placed = await gateway.place_order(trade.symbol, spec, trade.id)
        except Exception:
            await sa(repo.set_order_status)(row_id, base.REJECTED)
            raise
        await sa(repo.set_order_placed)(row_id, placed)
        snap = OrderSnap(
            id=row_id, role=spec.role.value, kind=spec.kind.value, side=spec.side, quantity=spec.quantity,
            price=spec.price, reduce_only=spec.reduce_only, exchange_id=placed.exchange_id, is_algo=placed.is_algo,
            status=base.NEW, executed_quantity=Decimal("0"), avg_fill_price=None,
        )
        trade.orders.append(snap)
        return snap

    async def _cancel_orders(self, gateway, trade: TradeSnap, orders: list[OrderSnap]) -> None:
        for order in orders:
            if await gateway.cancel_order(trade.symbol, order.exchange_id, order.is_algo):
                await sa(repo.set_order_status)(order.id, base.CANCELED)
                order.status = base.CANCELED
            else:  # already filled / gone: record what the exchange says
                await self._refresh(gateway, trade, order)

    async def _cancel_working(self, gateway, trade: TradeSnap, *roles: str) -> None:
        await self._cancel_orders(gateway, trade, [o for o in trade.rows(*roles) if o.status in WORKING])

    async def _close_position(self, gateway, trade: TradeSnap) -> None:
        remaining = await gateway.position_amount(trade.symbol, trade.id)
        if remaining > 0:
            await gateway.market_close(trade.symbol, close_side(trade.side), remaining, trade.id)

    async def _wrap_up(
        self, gateway, trade: TradeSnap, status: str, code: str, *, flatten: bool = False,
        close_price: Optional[Decimal] = None,
    ) -> None:
        """Cancel everything still working and close the trade. `flatten` closes what is left of the
        position at market. It also happens whenever an entry was still working: an entry that fills
        while it is being cancelled must never leave an unprotected position behind."""
        had_entries = any(o.status in WORKING for o in self._entry_rows(trade))
        await self._cancel_working(gateway, trade, *ENTRY_ROLES_V, *EXIT_ROLES)
        if flatten or had_entries:
            await self._close_position(gateway, trade)
        exact = None
        if status == repo.ST_CLOSED and trade.mode == repo.MODE_LIVE:
            exact = await self._exchange_result(gateway, trade)  # Binance's own numbers for the trade
            if exact is not None and code == "CLOSED_EXTERNALLY":
                code = await self._closing_code(gateway, trade, exact) or code
        await self._close(trade, status, code, close_price=close_price, exact=exact)

    async def _exchange_result(self, gateway, trade: TradeSnap) -> Optional[ExchangeResult]:
        """The closed trade as Binance recorded it (closing price, realized PnL, commissions). None when
        Binance cannot say (then the estimate from our own order rows is used)."""
        fetch = getattr(gateway, "account_fills", None)
        if fetch is None or trade.placed_at is None:
            return None
        since_ms = int(trade.placed_at.timestamp() * 1000) - 5000
        for attempt in range(4):  # a fill can take a moment to appear in the account's trade list
            try:
                result = exchange_result(trade.side, await fetch(trade.symbol, since_ms), self._settings.fee_rate)
            except Exception as exc:  # noqa: BLE001 - never block closing a trade on a reporting call
                logger.warning("Trade %s: could not read the account's fills (%s); estimating the result", trade.id, exc)
                return None
            if result is not None and result.fully_closed:
                return result
            await asyncio.sleep(1.0)
        logger.warning("Trade %s: the account's fills do not show the whole position closed; estimating the result", trade.id)
        return None

    async def _closing_code(self, gateway, trade: TradeSnap, result: ExchangeResult) -> Optional[str]:
        """What really closed a trade we found already closed: our stop-loss, our take-profits, or
        something else (None: closed by hand, liquidated, or unknown)."""
        stops, take_profits = {}, set()
        for order in trade.rows(SL, *TP_ROLES_V):
            try:
                order_id = (await gateway.get_order(trade.symbol, order.exchange_id, order.is_algo, trade.id)).order_id
            except Exception:  # noqa: BLE001
                continue
            if order_id is None:
                continue
            if order.role == SL:
                stops[order_id] = order
            else:
                take_profits.add(order_id)
        closing = set(result.closing_order_ids)
        hit = [stops[i] for i in closing if i in stops]
        if hit:
            return "SL_AT_ENTRY_HIT" if hit[-1].price != trade.stoploss else "SL_HIT"
        return "TP_COMPLETED" if closing and closing <= take_profits else None

    async def _close(
        self, trade: TradeSnap, status: str, code: str, *, close_price: Optional[Decimal] = None,
        exact: Optional[ExchangeResult] = None,
    ) -> None:
        # A live trade is priced from Binance's own fills when they could be read (`exact`); otherwise, and for
        # paper trades, net PnL (estimated fees) comes from the recorded fills.
        entry = [Fill(o.executed_quantity, o.avg_fill_price or o.price) for o in self._entry_rows(trade) if o.executed_quantity > 0]
        exits = [Fill(o.executed_quantity, o.avg_fill_price or o.price) for o in trade.rows(*EXIT_ROLES) if o.executed_quantity > 0]
        if close_price is not None:  # a market close has no order row: price what is left of the position at `close_price`
            left = sum((f.quantity for f in entry), Decimal("0")) - sum((f.quantity for f in exits), Decimal("0"))
            if left > 0:
                exits.append(Fill(left, close_price))
        if exact is not None:
            pnl, fees, exit_price = exact.net, exact.fees, exact.exit_price
        else:
            result = trade_pnl(trade.side, entry, exits, self._settings.fee_rate)
            pnl, fees, exit_price = result.net, result.fees, average_price(exits)
        await sa(repo.close_trade)(trade.id, status, code, "", timezone.now(), pnl, fees, exit_price)
        trade.status = status
        logger.info("Trade %s (%s %s) %s: %s (net PnL %.4f USDT)", trade.id, trade.mode, trade.symbol, status, code, pnl)
