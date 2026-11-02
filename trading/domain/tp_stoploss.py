"""
Stop-loss options for a signal that has none (Manual Trade only).

Option N puts the stop-loss as far from the entry as TP N is, on the other side: a 1:1 risk / reward
against that target. For both directions the price is 2 * entry - TP N.

The entry is always the NEAREST entry of the signal (long = highest, short = lowest), whatever the Entry mode
is. Margin mode does not matter here: nothing is checked against liquidation.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

from trading.domain.types import BUY


@dataclass(frozen=True)
class SlOption:
    tp: int                    # 1-based target number
    tp_price: Decimal
    sl_price: Decimal
    percent: Decimal           # distance of TP N from the entry, in % of the entry (also the SL distance)
    reason: Optional[str]      # why this option cannot be used (None = usable)

    @property
    def usable(self) -> bool:
        return self.reason is None


def nearest_entry(side: str, entries: Sequence[Decimal]) -> Decimal:
    return max(entries) if side == BUY else min(entries)


def sl_options(side: str, entries: Sequence[Decimal], targets: Sequence[Decimal]) -> list[SlOption]:
    """One option per target, in the signal's order."""
    entry = nearest_entry(side, entries)
    options = []
    for number, target in enumerate(targets, start=1):
        distance = target - entry if side == BUY else entry - target
        sl = 2 * entry - target
        percent = (distance / entry * 100) if entry else Decimal(0)
        reason = None
        if distance <= 0:
            reason = "TP is not beyond the entry"
        elif sl <= 0:
            reason = "SL would be zero or negative"
        options.append(SlOption(number, target, sl, percent, reason))
    return options
