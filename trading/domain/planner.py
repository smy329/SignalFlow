"""
Pure trade planner: signal + symbol rules + market price + source strategy ->
TradePlan or a rejection.

No Django, no network, no python-binance. It decides *what* to trade; placing
the orders is execution_service's job. Rejections are returned, never raised,
so one bad signal can never take the engine down.

Rules (carried over from the original bot unless noted):
  - an entry is a STOP_MARKET when it lies beyond the market in the trade
    direction (BUY above / SELL below), otherwise a LIMIT GTC
  - a market that already reached TP1 or the stop-loss does not stop the plan:
    it is returned as unplaceable, so the caller can record the trade as
    cancelled (visible in the Cancelled tab) without placing any order
  - quantity = margin * leverage / entry, leverage clamped to the symbol max
  - quantity is floored to stepSize (never rounded up) and checked against
    minQty / maxQty / minNotional; too small -> never bumped up
  - SL / TP1 (and TP2) must be on the correct side of the entry
Per-source strategy (see trading.domain.strategy):
  - the source picks which signal entries are traded: nearest, farthest or both
    (both = margin split equally; an entry that cannot be traded is skipped and the
    remaining one uses the full margin)
  - the position is closed over up to 8 take-profits by percentage
"""
from decimal import Decimal
from itertools import pairwise
from typing import Optional, Sequence

from trading.domain.quantize import floor_to_step, round_to_tick
from trading.domain.strategy import (
    DEFAULT_STRATEGY,
    DEFAULT_TP_PERCENTS,
    SourceStrategy,
    allocate_tp_quantities,
    effective_tp_percents,
    select_entries,
)
from trading.domain.types import (
    BUY,
    ENTRY_ROLES,
    MAX_TARGETS,
    EntryType,
    OrderKind,
    OrderRole,
    OrderSpec,
    PlanResult,
    SignalInput,
    SymbolFilters,
    TradePlan,
    close_side,
    tp_role,
)

# Rejection codes — plain strings so they map onto Trade.reason_code.
SYMBOL_NOT_TRADABLE = "SYMBOL_NOT_TRADABLE"
MISSING_MARGIN = "MISSING_MARGIN"
INVALID_INPUT = "INVALID_INPUT"
INVALID_LEVELS = "INVALID_LEVELS"
PRICE_ALREADY_HIT_TP1 = "PRICE_ALREADY_HIT_TP1"
PRICE_ALREADY_HIT_SL = "PRICE_ALREADY_HIT_SL"
BELOW_MIN_QTY = "BELOW_MIN_QTY"
ABOVE_MAX_QTY = "ABOVE_MAX_QTY"
BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"

_SIDES = ("BUY", "SELL")


def plan_trade(
    signal: SignalInput,
    filters: Optional[SymbolFilters],
    *,
    max_leverage: int,
    market_price: Decimal,
    strategy: SourceStrategy = DEFAULT_STRATEGY,
) -> PlanResult:
    if filters is None:
        return PlanResult.fail(SYMBOL_NOT_TRADABLE, f"{signal.symbol} is not tradable on Binance Futures.")

    failure = _check_inputs(signal, max_leverage, market_price)
    if failure:
        return failure

    side = signal.side
    stoploss = round_to_tick(signal.stoploss, filters.tick_size)
    targets = _usable_targets(side, [round_to_tick(t, filters.tick_size) for t in signal.targets[:MAX_TARGETS]])

    candidates = _unique(
        round_to_tick(e, filters.tick_size) for e in select_entries(signal.entry_side or side, signal.all_entries, strategy.entry_mode)
    )
    valid, failure = [], None
    for entry in candidates:
        problem = _check_levels(side, entry, stoploss, targets)
        if problem:
            failure = failure or problem  # this entry is skipped; the others may still be fine
        else:
            valid.append(entry)
    if not valid:
        return failure

    unreachable = _check_market(side, market_price, stoploss, targets[0])
    # A market that already reached TP1 / the stop-loss does not reject the signal: the plan is still
    # built (its levels are shown on the cancelled trade) and returned as unplaceable at the end.

    leverage = int(min(signal.leverage, Decimal(max_leverage)))
    sized, failure = _size_entries(valid, signal.margin, leverage, filters)
    if failure:
        return failure

    specs = tuple(
        OrderSpec(
            role, OrderKind(_choose_entry_type(side, price, market_price).value), side, quantity, price,
            reduce_only=False, guard_tp=targets[0], guard_sl=stoploss,
        )
        for role, (price, quantity) in zip(ENTRY_ROLES, sized)
    )
    total = sum(q for _, q in sized)
    stop_loss, take_profits = plan_exits(side, total, stoploss, targets, filters, strategy.tp_percents, strategy.default_tp)

    plan = TradePlan(
        symbol=signal.symbol,
        side=side,
        entry_type=EntryType(specs[0].kind.value),
        leverage=leverage,
        margin_type=signal.margin_type.upper(),
        quantity=total,
        market_price=market_price,
        entries=specs,
        stop_loss=stop_loss,
        take_profits=take_profits,
        targets=tuple(targets),
    )
    if unreachable is not None:
        return PlanResult.unplaceable(plan, unreachable.error_code, unreachable.error_message)
    return PlanResult.ok(plan)


def plan_exits(
    side: str,
    quantity: Decimal,
    stoploss: Decimal,
    targets: Sequence[Decimal],
    filters: SymbolFilters,
    tp_percents: Sequence[Optional[int]] = (),
    default_tp: Sequence[int] = DEFAULT_TP_PERCENTS,
) -> tuple[OrderSpec, tuple[OrderSpec, ...]]:
    """Stop-loss and take-profit orders for `quantity`. Prices must already be
    tick-quantized. Also used to re-place exits when the filled size changes."""
    exit_side = close_side(side)
    targets = list(targets)[:MAX_TARGETS]
    percents = effective_tp_percents(len(targets), tp_percents, default_tp)
    slices = allocate_tp_quantities(quantity, targets, percents, filters.step_size, filters.min_qty)
    stop_loss = OrderSpec(OrderRole.SL, OrderKind.STOP_MARKET, exit_side, quantity, stoploss, reduce_only=True)
    take_profits = tuple(
        OrderSpec(tp_role(number), OrderKind.TAKE_PROFIT_MARKET, exit_side, size, price, reduce_only=True)
        for number, price, size in slices
    )
    return stop_loss, take_profits


# ---- validation helpers ----------------------------------------------------


def _check_inputs(signal: SignalInput, max_leverage: int, market_price: Decimal) -> Optional[PlanResult]:
    if signal.margin is None or signal.margin <= 0:
        return PlanResult.fail(MISSING_MARGIN, "The margin setting is not a positive value.")
    if signal.side not in _SIDES:
        return PlanResult.fail(INVALID_INPUT, f"Unknown side {signal.side!r}.")
    if not signal.targets:
        return PlanResult.fail(INVALID_INPUT, "Signal has no targets.")
    if min(*signal.all_entries, signal.stoploss, market_price, *signal.targets) <= 0:
        return PlanResult.fail(INVALID_INPUT, "Entry, stop-loss, targets and market price must be positive.")
    if signal.leverage < 1 or max_leverage < 1:
        return PlanResult.fail(INVALID_INPUT, "Leverage must be at least 1.")
    return None


def _check_levels(side: str, entry: Decimal, stoploss: Decimal, targets: Sequence[Decimal]) -> Optional[PlanResult]:
    """BUY needs SL < entry < TP1 < TP2; SELL needs the reverse."""
    ladder = [stoploss, entry, *targets]
    ordered = all(a < b for a, b in pairwise(ladder)) if side == BUY else all(a > b for a, b in pairwise(ladder))
    if ordered:
        return None
    expected = "SL < entry < TP1 < TP2" if side == BUY else "SL > entry > TP1 > TP2"
    got = f"SL={_fmt(stoploss)}, entry={_fmt(entry)}, targets={[_fmt(t) for t in targets]}"
    return PlanResult.fail(INVALID_LEVELS, f"{side} requires {expected}; got {got}.")


def _check_market(side: str, market: Decimal, stoploss: Decimal, tp1: Decimal) -> Optional[PlanResult]:
    tp_hit, sl_hit = (market >= tp1, market <= stoploss) if side == BUY else (market <= tp1, market >= stoploss)
    if tp_hit:
        return PlanResult.fail(PRICE_ALREADY_HIT_TP1, f"Market {_fmt(market)} already reached TP1 {_fmt(tp1)}.")
    if sl_hit:
        return PlanResult.fail(PRICE_ALREADY_HIT_SL, f"Market {_fmt(market)} already reached SL {_fmt(stoploss)}.")
    return None


def _check_size(quantity: Decimal, entry: Decimal, filters: SymbolFilters) -> Optional[PlanResult]:
    if quantity <= 0 or quantity < filters.min_qty:
        return PlanResult.fail(
            BELOW_MIN_QTY, f"Quantity {_fmt(quantity)} is below the minimum {_fmt(filters.min_qty)}."
        )
    if quantity > filters.max_qty:
        return PlanResult.fail(
            ABOVE_MAX_QTY, f"Quantity {_fmt(quantity)} is above the maximum {_fmt(filters.max_qty)}."
        )
    notional = quantity * entry
    if notional < filters.min_notional:
        return PlanResult.fail(
            BELOW_MIN_NOTIONAL,
            f"Notional {_fmt(notional)} USDT is below the minimum {_fmt(filters.min_notional)} USDT.",
        )
    return None


# ---- construction helpers --------------------------------------------------


def _choose_entry_type(side: str, entry: Decimal, market: Decimal) -> EntryType:
    beyond_market = entry > market if side == BUY else entry < market
    return EntryType.STOP_MARKET if beyond_market else EntryType.LIMIT


def _unique(values) -> list[Decimal]:
    return list(dict.fromkeys(values))


def _usable_targets(side: str, targets: list[Decimal]) -> list[Decimal]:
    """The first two targets are kept as given (a bad TP2 rejects the signal, as always).
    From TP3 on, targets are only used while they keep moving away from the entry."""
    kept = targets[:2]
    for target in targets[2:]:
        if (target <= kept[-1]) if side == BUY else (target >= kept[-1]):
            break
        kept.append(target)
    return kept


def _size_entries(
    entries: list[Decimal], margin: Decimal, leverage: int, filters: SymbolFilters
) -> tuple[list[tuple[Decimal, Decimal]], Optional[PlanResult]]:
    """[(price, quantity)] for the entries that can be traded, margin split equally.
    An entry that is too small / too large is skipped and the rest share the full margin."""

    def attempt(prices: list[Decimal], per_entry: Decimal):
        sized, first_failure = [], None
        for price in prices:
            quantity = floor_to_step(per_entry * leverage / price, filters.step_size)
            problem = _check_size(quantity, price, filters)
            if problem:
                first_failure = first_failure or problem
            else:
                sized.append((price, quantity))
        return sized, first_failure

    sized, failure = attempt(entries, margin / len(entries))
    if len(sized) == len(entries):
        return sized, None
    if sized:  # skip the entries that failed; the remaining ones use the full margin
        resized, failure = attempt([p for p, _ in sized], margin / len(sized))
        return (resized, None) if resized else ([], failure)
    if len(entries) > 1:  # none fits half the margin: the nearest entry alone gets all of it
        alone, alone_failure = attempt(entries[:1], margin)
        if alone:
            return alone, None
    return [], failure


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")
