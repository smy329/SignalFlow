"""Numbers shown on the Positions page. Pure, no I/O."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class PositionInput:
    amount: Decimal  # signed: > 0 long, < 0 short
    mark_price: Decimal
    notional: Decimal
    unrealized_pnl: Decimal
    leverage: int
    initial_margin: Decimal
    maint_margin: Decimal


@dataclass(frozen=True)
class PositionMetrics:
    side: str  # LONG | SHORT
    size_usdt: Decimal
    margin: Decimal
    roi_pct: Optional[Decimal]
    margin_ratio_pct: Optional[Decimal]


def position_metrics(p: PositionInput) -> PositionMetrics:
    """
    margin        the position's initial margin (notional / leverage when the exchange omits it)
    ROI           unrealized PnL / margin
    margin ratio  the position's maintenance margin / (its margin + unrealized PnL), capped at
                  100% — at 100% the position is at its liquidation point
    """
    size = abs(p.notional) if p.notional != 0 else abs(p.amount) * p.mark_price
    margin = p.initial_margin
    if margin <= 0 and p.leverage > 0:
        margin = size / p.leverage

    roi = p.unrealized_pnl / margin * HUNDRED if margin > 0 else None

    ratio = None
    if p.maint_margin > 0:
        equity = margin + p.unrealized_pnl
        ratio = HUNDRED if equity <= 0 else min(HUNDRED, p.maint_margin / equity * HUNDRED)

    return PositionMetrics(
        side="LONG" if p.amount > 0 else "SHORT", size_usdt=size, margin=margin, roi_pct=roi, margin_ratio_pct=ratio
    )
