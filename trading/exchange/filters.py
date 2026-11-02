"""
Binance USDⓈ-M exchangeInfo -> pure SymbolFilters.

This is the only place that knows the exchangeInfo payload layout. Anything
that cannot be parsed safely yields None, and the planner then rejects the
symbol as not tradable instead of guessing.
"""
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from trading.domain.types import SymbolFilters

TRADING_STATUS = "TRADING"


def parse_exchange_info(exchange_info: Mapping[str, Any]) -> dict[str, SymbolFilters]:
    """symbol -> filters for every symbol currently in TRADING status."""
    result: dict[str, SymbolFilters] = {}
    for info in exchange_info.get("symbols", []):
        filters = parse_symbol_filters(info)
        if filters is not None:
            result[info["symbol"]] = filters
    return result


def parse_symbol_filters(symbol_info: Mapping[str, Any]) -> Optional[SymbolFilters]:
    if symbol_info.get("status") != TRADING_STATUS:
        return None

    by_type = {f.get("filterType"): f for f in symbol_info.get("filters", [])}
    price, lot, notional = by_type.get("PRICE_FILTER"), by_type.get("LOT_SIZE"), by_type.get("MIN_NOTIONAL")
    if not (price and lot and notional):
        return None

    try:
        tick_size = _dec(price["tickSize"])
        lot_rules = _merge_lot_filters(lot, by_type.get("MARKET_LOT_SIZE"))
        # Futures name the field "notional"; "minNotional" is the spot spelling.
        min_notional = _dec(notional.get("notional", notional.get("minNotional")))
    except (KeyError, InvalidOperation, TypeError):
        return None

    if lot_rules is None or tick_size <= 0:
        return None
    step_size, min_qty, max_qty = lot_rules
    if step_size <= 0:
        return None

    return SymbolFilters(
        tick_size=tick_size, step_size=step_size, min_qty=min_qty, max_qty=max_qty, min_notional=min_notional
    )


def _merge_lot_filters(lot: Mapping[str, Any], market: Optional[Mapping[str, Any]]):
    """Entries can be LIMIT or STOP_MARKET and exits are market orders, so
    quantities must satisfy LOT_SIZE and MARKET_LOT_SIZE together. Zero values
    in MARKET_LOT_SIZE mean "rule disabled"."""
    step, min_qty, max_qty = _dec(lot["stepSize"]), _dec(lot["minQty"]), _dec(lot["maxQty"])
    if not market:
        return step, min_qty, max_qty

    market_step = _dec(market.get("stepSize", "0"))
    min_qty = max(min_qty, _dec(market.get("minQty", "0")))
    market_max = _dec(market.get("maxQty", "0"))
    if market_max > 0:
        max_qty = min(max_qty, market_max)

    if market_step > 0 and market_step != step:
        larger, smaller = max(step, market_step), min(step, market_step)
        if larger % smaller != 0:
            return None  # steps that are not multiples of each other: refuse rather than guess
        step = larger
    return step, min_qty, max_qty


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))
