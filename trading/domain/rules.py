"""
Pure trade-management rules used by the monitor (and, later, the forward test).
No Django, no network.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from trading.domain.types import BUY

PRICE_HIT_TP1 = "PRICE_HIT_TP1"
PRICE_HIT_SL = "PRICE_HIT_SL"
PRICE_HIT_TP1_AND_SL = "PRICE_HIT_TP1_AND_SL"


@dataclass(frozen=True)
class PriceObservation:
    """What one monitor poll saw for a symbol. `high`/`low` are the ongoing
    1-minute candle's range and are only set when that candle is eligible
    (see `observe_price`)."""

    last: Decimal
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None


def candle_is_eligible(candle_open_ms: int, order_placed_ms: int) -> bool:
    """A candle's high/low may only count if the candle opened at or after the
    order was placed. The ongoing candle can start up to 60s before the order
    existed, and its range would include price action from before the signal."""
    return candle_open_ms >= order_placed_ms


def observe_price(
    last: Decimal,
    candle_open_ms: int,
    candle_high: Decimal,
    candle_low: Decimal,
    order_placed_ms: int,
) -> PriceObservation:
    if candle_is_eligible(candle_open_ms, order_placed_ms):
        return PriceObservation(last=last, high=candle_high, low=candle_low)
    return PriceObservation(last=last)


def price_reached(side: str, level: Decimal, observation: PriceObservation) -> bool:
    """True once price has touched `level` in the trade direction (BUY: up to it, SELL: down to it)."""
    if side == BUY:
        high = observation.last if observation.high is None else max(observation.last, observation.high)
        return high >= level
    low = observation.last if observation.low is None else min(observation.last, observation.low)
    return low <= level


def pending_entry_cancel_reason(
    side: str, take_profit_1: Decimal, stoploss: Decimal, observation: PriceObservation
) -> Optional[str]:
    """Reason to cancel a still-unfilled entry, or None to keep waiting.

    The entry must be cancelled once price has reached TP1 or the stop-loss
    before the entry filled. Callers must check the entry order's status first
    and only apply this rule while it is genuinely still unfilled.
    """
    high = observation.last if observation.high is None else max(observation.last, observation.high)
    low = observation.last if observation.low is None else min(observation.last, observation.low)

    if side == BUY:
        hit_tp1, hit_sl = high >= take_profit_1, low <= stoploss
    else:
        hit_tp1, hit_sl = low <= take_profit_1, high >= stoploss

    if hit_tp1 and hit_sl:
        return PRICE_HIT_TP1_AND_SL
    if hit_tp1:
        return PRICE_HIT_TP1
    if hit_sl:
        return PRICE_HIT_SL
    return None
