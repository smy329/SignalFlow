"""Rows for the Positions / Pending / History tables of one mode (Real or Forward).

Pure assembly: the caller supplies the DB rows (trade_repository.trade_rows) and
real-market prices; the only thing read here is the display-rounding setting (cached).
"""
from decimal import Decimal
from typing import Any, Mapping, Optional

from runtime_settings import service as runtime
from trading.domain.pnl import Fill, average_price, trade_pnl

ZERO = Decimal("0")


def _num(value, places=None) -> Optional[float]:
    """Round for display. `places` is a number, or the name of a Settings decimals value
    ("amount" is the default; "percent" and "pnl" are the other two)."""
    if value is None:
        return None
    return round(float(value), runtime.places(places or "amount") if not isinstance(places, int) else places)


def _margin(quantity: Optional[Decimal], price: Optional[Decimal], leverage: Optional[int]) -> Optional[Decimal]:
    return (quantity * price / leverage) if quantity and price and leverage else None


def _planned_prices(r: dict) -> list[Decimal]:
    return list(r.get("planned_entries") or ([r["planned_entry"]] if r["planned_entry"] else []))


def _planned_price(r: dict) -> Optional[Decimal]:
    """One reference price for a not-yet-filled trade. Entries share the margin equally, so the
    quantity-weighted average of two entries is their harmonic mean."""
    prices = _planned_prices(r)
    return len(prices) / sum(1 / p for p in prices) if prices else None


def _pct(pnl: Optional[Decimal], margin: Optional[Decimal]) -> Optional[float]:
    return _num(pnl / margin * 100, "percent") if pnl is not None and margin else None


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _common(r: dict) -> dict[str, Any]:
    return {
        "id": r["id"],
        "trade_id": r["id"],
        "channel": r["channel"],
        "opposite": bool(r.get("opposite")),  # a mirror-image trade of its signal
        "symbol": r["symbol"],
        "side": "LONG" if r["side"] == "BUY" else "SHORT",
        "status": r.get("status"),
        "leverage": r["leverage"],
        "stoploss": _num(r["stoploss"]),
        "targets": [_num(t) for t in r["targets"]],
    }


def _fills(pairs) -> list[Fill]:
    return [Fill(q, p) for q, p in pairs]


def symbols(rows: list[dict]) -> set[str]:
    return {r["symbol"] for r in rows}


def position_rows(rows: list[dict], prices: Mapping[str, Decimal], fee_rate: Decimal) -> list[dict]:
    out = []
    for r in rows:
        entry, exits = _fills(r["entry_fills"]), _fills(r["exit_fills"])
        mark = prices.get(r["symbol"])
        entry_price = average_price(entry)
        margin = _margin(sum((f.quantity for f in entry), ZERO), entry_price, r["leverage"])
        pnl = trade_pnl(r["side"], entry, exits, fee_rate, mark_price=mark).net if mark is not None else None
        out.append(
            {
                **_common(r),
                "entry_price": _num(entry_price),
                "margin": _num(margin, "pnl"),
                "mark_price": _num(mark),
                "pnl": _num(pnl, "pnl"),
                "pnl_pct": _pct(pnl, margin),
                "opened_at": _iso(r["opened_at"]),
            }
        )
    return out


def pending_rows(rows: list[dict], prices: Mapping[str, Decimal]) -> list[dict]:
    return [
        {
            **_common(r),
            "current_price": _num(prices.get(r["symbol"])),
            "entry_price": _num(r["planned_entry"]),
            "entry_prices": [_num(p) for p in _planned_prices(r)],
            "margin": _num(_margin(r["quantity"], _planned_price(r), r["leverage"]), "pnl"),
        }
        for r in rows
    ]


def cancelled_rows(rows: list[dict]) -> list[dict]:
    """Every trade that never traded: entries cancelled before they filled (e.g. price reached TP1 / SL
    first), and signals rejected or failed before any order was placed."""
    return [
        {
            **_common(r),
            "entry_price": _num(r["planned_entry"]),
            "entry_prices": [_num(p) for p in _planned_prices(r)],
            "margin": _num(_margin(r["quantity"], _planned_price(r), r["leverage"]), "pnl"),
            "result": r["reason"] or None,
            "closed_at": _iso(r.get("finished_at") or r["closed_at"]),
        }
        for r in rows
    ]


def history_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        entry, exits = _fills(r["entry_fills"]), _fills(r["exit_fills"])
        entry_price = average_price(entry)
        margin = _margin(sum((f.quantity for f in entry), ZERO), entry_price, r["leverage"])
        pnl = r["realized_pnl"]
        out.append(
            {
                **_common(r),
                "entry_price": _num(entry_price),
                "margin": _num(margin, "pnl"),
                "exit_price": _num(r.get("recorded_exit_price") or average_price(exits)),
                "result": r["reason"] or None,
                "pnl": _num(pnl, "pnl"),
                "pnl_pct": _pct(pnl, margin),
                "opened_at": _iso(r["opened_at"]),
                "closed_at": _iso(r["closed_at"]),
            }
        )
    return out
