"""
Profit-and-loss maths for simulated (forward-test) trades. Pure, no I/O.

Fees are charged on the notional of every fill at one flat rate. For a trade
that is still open, only fees already incurred are counted (no exit fee for
the part of the position that has not been closed yet).
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from trading.domain.types import BUY

ZERO = Decimal("0")


@dataclass(frozen=True)
class Fill:
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class Pnl:
    gross: Decimal
    fees: Decimal

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees


def average_price(fills: Sequence[Fill]) -> Optional[Decimal]:
    quantity = sum((f.quantity for f in fills), ZERO)
    if quantity <= 0:
        return None
    return sum((f.quantity * f.price for f in fills), ZERO) / quantity


def trade_pnl(
    side: str,
    entry_fills: Sequence[Fill],
    exit_fills: Sequence[Fill],
    fee_rate: Decimal,
    mark_price: Optional[Decimal] = None,
) -> Pnl:
    """Realized PnL of the exit fills, plus unrealized PnL of whatever is left
    when `mark_price` is given."""
    entry = average_price(entry_fills)
    if entry is None:
        return Pnl(ZERO, ZERO)

    direction = 1 if side == BUY else -1
    gross = sum(((f.price - entry) * f.quantity * direction for f in exit_fills), ZERO)

    if mark_price is not None:
        remaining = sum((f.quantity for f in entry_fills), ZERO) - sum((f.quantity for f in exit_fills), ZERO)
        if remaining > 0:
            gross += (mark_price - entry) * remaining * direction

    notional = sum((f.quantity * f.price for f in (*entry_fills, *exit_fills)), ZERO)
    return Pnl(gross=gross, fees=notional * fee_rate)
