"""
The result of a live trade as Binance itself recorded it. Pure, no I/O.

`fills` are the account's fills on the symbol since the trade was placed, one dict per fill with
`order_id, side, price, qty, realized_pnl, commission, commission_asset`. Binance's `realized_pnl` is the
price profit of a closing fill (0 on an entry fill) and the commission is charged separately, so the net
result is the sum of the first minus the sum of the second. That is the "Realized PNL" of Binance's
Position History.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Sequence

ZERO = Decimal("0")
STABLE_COMMISSION_ASSETS = frozenset({"USDT", "USDC", "BUSD"})


@dataclass(frozen=True)
class ExchangeResult:
    entry_quantity: Decimal
    closed_quantity: Decimal
    exit_price: Optional[Decimal]
    gross: Decimal
    fees: Decimal
    closing_order_ids: frozenset

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees

    @property
    def fully_closed(self) -> bool:
        return self.entry_quantity > 0 and self.closed_quantity >= self.entry_quantity * Decimal("0.999")


def exchange_result(side: str, fills: Sequence[dict], fee_rate: Decimal) -> Optional[ExchangeResult]:
    """None when the fills hold no entry or no closing fill. A commission paid in another asset (BNB) cannot
    be added to a USDT result, so that fill's fee is estimated with `fee_rate` on its notional."""
    entries = [f for f in fills if f["side"] == side]
    closing = [f for f in fills if f["side"] != side]
    if not entries or not closing:
        return None
    fees = ZERO
    for fill in fills:
        if fill["commission_asset"] in STABLE_COMMISSION_ASSETS:
            fees += fill["commission"]
        else:
            fees += fill["price"] * fill["qty"] * fee_rate
    closed = sum((f["qty"] for f in closing), ZERO)
    return ExchangeResult(
        entry_quantity=sum((f["qty"] for f in entries), ZERO),
        closed_quantity=closed,
        exit_price=sum((f["price"] * f["qty"] for f in closing), ZERO) / closed if closed > 0 else None,
        gross=sum((f["realized_pnl"] for f in fills), ZERO),
        fees=fees,
        closing_order_ids=frozenset(f["order_id"] for f in closing),
    )
