"""Live Positions page: real Binance positions merged with our trade records."""
from typing import Any, Optional

from asgiref.sync import sync_to_async

from runtime_settings import service as runtime
from trading.domain.positions import PositionInput, position_metrics
from trading.exchange.binance_gateway import BinanceGateway
from trading.services import trade_repository as repo


def _num(value, places=None) -> Optional[float]:
    """Round for display. `places` is a number, or the name of a Settings decimals value
    ("amount" is the default; "percent" and "pnl" are the other two)."""
    if value is None:
        return None
    return round(float(value), runtime.places(places or "amount") if not isinstance(places, int) else places)


async def build_positions(gateway: BinanceGateway) -> list[dict[str, Any]]:
    """Snapshots + our trade records in one coroutine. The Django views do NOT use this: a view that waits
    on the engine loop must not make the engine reach the database (see `rows_from`)."""
    snapshots = await gateway.position_snapshots()
    # rows_from reads the Settings table (display decimals): do all database work in one sync_to_async call.
    # Calling it straight from the coroutine would open (and leak) a database connection in the event-loop thread.
    return await sync_to_async(lambda: rows_from(snapshots, repo.active_live_trades_by_symbol()))()


def rows_from(snapshots: list[dict[str, Any]], trades: dict[str, dict]) -> list[dict[str, Any]]:
    """The Positions rows. Plain synchronous code (settings and trade records are read by the caller), so the
    views can fetch the snapshots from the engine loop, which only talks to Binance, and build the rows in
    their own thread. Reading the database from the engine loop while a sync view waits for it deadlocks:
    the engine's sync_to_async call inherits the request's thread and that thread is the one blocked waiting."""
    rows = []
    for snap in snapshots:
        metrics = position_metrics(
            PositionInput(
                amount=snap["amount"], mark_price=snap["mark_price"], notional=snap["notional"],
                unrealized_pnl=snap["unrealized_pnl"], leverage=snap["leverage"],
                initial_margin=snap["initial_margin"], maint_margin=snap["maint_margin"],
            )
        )
        ours = trades.get(snap["symbol"])  # manual positions have no trade record
        rows.append(
            {
                "trade_id": ours["trade_id"] if ours else None,  # None: opened by hand on Binance, nothing to close here
                "channel": ours["channel"] if ours else "—",
                "symbol": snap["symbol"],
                "side": metrics.side,
                "leverage": snap["leverage"],
                "margin": _num(metrics.margin, "percent"),
                "margin_ratio": _num(metrics.margin_ratio_pct, "percent"),
                "entry_price": _num(snap["entry_price"]),
                "mark_price": _num(snap["mark_price"]),
                "liq_price": _num(snap["liq_price"]) or None,
                "size_usdt": _num(metrics.size_usdt, "percent"),
                "pnl": _num(snap["unrealized_pnl"], "pnl"),
                "roi": _num(metrics.roi_pct, "percent"),
                "pnl_pct": _num(metrics.roi_pct, "percent"),  # % of margin, same key the forward rows use
                "stoploss": _num(ours["stoploss"]) if ours else None,
                "targets": [_num(t) for t in ours["targets"]] if ours else [],
                "opened_at": ours["opened_at"].isoformat() if ours and ours["opened_at"] else None,
            }
        )
    return rows
