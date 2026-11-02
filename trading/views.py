import json
import logging
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from runtime_settings import service as runtime
from telegram_monitor.models import MANUAL_CHANNEL
from trading.scopes import REAL, SCOPES, Scope
from trading.services import monitor_service, position_service, report_service
from trading.services import trade_repository as repo
from trading.services.engine import EngineNotReady, engine

logger = logging.getLogger(__name__)


def _scope(slug: str) -> Scope:
    if slug not in SCOPES:
        raise Http404
    return SCOPES[slug]


def _enabled(scope: Scope) -> bool:
    if scope is REAL:
        return engine.is_running and engine.has_live
    return engine.is_running and repo.MODE_PAPER in engine.gateways


def _off_message(scope: Scope) -> str:
    if scope is REAL:
        return "Live trading is off: set BINANCE_API_KEY and BINANCE_API_SECRET in .env (and check the Binance testnet setting matches the keys)."
    return "The forward test is not running ('Allow forward test' is Off in Settings, or the engine has not connected to Binance yet)."


def _market_prices(symbols: set[str]) -> dict[str, Decimal]:
    """Current prices, always from the real Binance market (never the testnet)."""
    if not symbols or not engine.is_running:
        return {}
    try:
        return engine.run(engine.market_gateway.last_prices(symbols), timeout=engine.view_timeout)
    except Exception:  # noqa: BLE001 - the tables then show "—" for prices
        logger.exception("Could not fetch market prices")
        return {}


def _fee_rate() -> Decimal:
    return engine.settings.fee_rate if engine.settings is not None else Decimal("0")


# ---- pages --------------------------------------------------------------------


# tab slug -> (label, text shown when the table is empty)
TRADE_TABS = {
    "positions": ("Positions", "NO OPEN POSITIONS"),
    "pending": ("Pending", "NO PENDING ORDERS"),
    "cancelled": ("Cancelled", "NO CANCELLED ENTRIES"),
}


@login_required
def trades_redirect(request, scope: str):
    return redirect(f"{_scope(scope).slug}-trades-positions")


@login_required
def trades_page(request, scope: str, tab: str):
    sc = _scope(scope)
    if tab not in TRADE_TABS:
        raise Http404
    tabs = [{"slug": t, "label": label, "url_name": f"{sc.slug}-trades-{t}"} for t, (label, _) in TRADE_TABS.items()]
    context = {
        "scope": sc, "tab": tab, "tabs": tabs, "empty_text": TRADE_TABS[tab][1],
        "nav_scope": sc.slug, "nav_item": "trades",
    }
    return render(request, "trading/trades.html", context)


@login_required
def history_page(request, scope: str):
    sc = _scope(scope)
    return render(request, "trading/history.html", {"scope": sc, "nav_scope": sc.slug, "nav_item": "history"})


# ---- JSON APIs (polled by the tables) ------------------------------------------


@login_required
@require_GET
def positions_api(request, scope: str):
    sc = _scope(scope)
    if not _enabled(sc):
        return JsonResponse({"enabled": False, "rows": [], "message": _off_message(sc)})

    if sc is REAL:  # the live account itself, including positions opened by hand
        try:
            snapshots = engine.run(engine.live_gateway.position_snapshots(), timeout=engine.view_timeout)  # network only
            rows = position_service.rows_from(snapshots, repo.active_live_trades_by_symbol())
        except EngineNotReady:
            return JsonResponse({"enabled": True, "rows": [], "message": "Connecting to Binance..."}, status=503)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load positions")
            return JsonResponse({"enabled": True, "rows": [], "message": f"Could not load positions: {exc}"}, status=502)
        return JsonResponse({"enabled": True, "rows": [r for r in rows if r["channel"] != MANUAL_CHANNEL]})

    trades = repo.trade_rows(sc.trade_mode, repo.ST_OPEN)
    rows = report_service.position_rows(trades, _market_prices(report_service.symbols(trades)), _fee_rate())
    return JsonResponse({"enabled": True, "rows": rows})


@login_required
@require_GET
def pending_api(request, scope: str):
    sc = _scope(scope)
    trades = repo.trade_rows(sc.trade_mode, repo.ST_PENDING)
    rows = report_service.pending_rows(trades, _market_prices(report_service.symbols(trades)))
    return JsonResponse({"enabled": _enabled(sc), "rows": rows, "message": "" if _enabled(sc) else _off_message(sc)})


@login_required
@require_GET
def history_api(request, scope: str):
    sc = _scope(scope)
    rows = report_service.history_rows(repo.trade_rows(sc.trade_mode, repo.ST_CLOSED, limit=runtime.get("display.history_limit")))
    return JsonResponse({"enabled": _enabled(sc), "rows": rows, "message": "" if _enabled(sc) else _off_message(sc)})


@login_required
@require_GET
def cancelled_api(request, scope: str):
    sc = _scope(scope)
    rows = report_service.cancelled_rows(repo.trade_rows(sc.trade_mode, repo.ST_CANCELLED, limit=runtime.get("display.history_limit")))
    return JsonResponse({"enabled": _enabled(sc), "rows": rows, "message": "" if _enabled(sc) else _off_message(sc)})


# ---- Close button (Positions tables) ---------------------------------------------

# monitor_service outcome -> (HTTP status, message). CLOSED is handled separately.
_CLOSE_FAILURES = {
    monitor_service.ALREADY_OVER: (409, "This trade is already closed."),
    monitor_service.PLACING: (409, "This trade is still being placed. Try again in a moment."),
    monitor_service.NO_GATEWAY: (503, "Not connected to the exchange this trade runs on, so it cannot be closed now."),
    monitor_service.FAILED: (
        502, "The exchange did not close the position. The trade stays open and its stop-loss is re-placed by the monitor if it was cancelled.",
    ),
}


async def _scoped_trade(request, trade_id: int, mode: "str | None", manual: bool):
    """(trade info, None) for a logged-in user and a trade that belongs on the page the button is on, else
    (None, response). A trade that does not belong there is a 404, so a button can never act on a trade of another page."""
    user = await request.auser()
    if not user.is_authenticated:
        return None, redirect_to_login(request.get_full_path())
    info = await sync_to_async(repo.trade_scope)(trade_id)
    if info is None or info["manual"] != manual or (mode is not None and info["mode"] != mode):
        return None, JsonResponse({"ok": False, "message": "Trade not found."}, status=404)
    return info, None


def _label(info: dict) -> str:
    return f"{info['symbol']} {'LONG' if info['side'] == 'BUY' else 'SHORT'}"


async def close_trade_response(request, trade_id: int, *, mode: "str | None", manual: bool):
    """Close one open trade at market, or cancel the entry orders of a pending one (the Close button)."""
    info, error = await _scoped_trade(request, trade_id, mode, manual)
    if error is not None:
        return error
    if info["mode"] == repo.MODE_LIVE:  # real money is never one click: the page asks first and says so
        try:
            confirmed = json.loads(request.body or b"{}").get("confirmed") is True
        except (ValueError, AttributeError):
            confirmed = False
        if not confirmed:
            return JsonResponse({"ok": False, "message": "Confirm the close first."}, status=400)
    label = _label(info)
    try:
        outcome = await engine.close_trade(trade_id)
    except EngineNotReady:
        return JsonResponse({"ok": False, "message": "The trading engine is not ready yet."}, status=503)
    except TimeoutError:
        logger.exception("Closing trade %s timed out", trade_id)
        return JsonResponse(
            {"ok": False, "message": f"No answer in time while closing {label}. Check Positions and History before trying again."},
            status=504,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Closing trade %s failed", trade_id)
        return JsonResponse({"ok": False, "message": f"Could not close {label}: {exc}"}, status=502)
    if outcome == monitor_service.CLOSED:
        return JsonResponse({"ok": True, "message": f"{label} closed at market."})
    if outcome == monitor_service.CANCELLED:
        return JsonResponse({"ok": True, "message": f"{label} pending entry cancelled."})
    status, message = _CLOSE_FAILURES.get(outcome, (502, "The trade could not be closed."))
    return JsonResponse({"ok": False, "message": message}, status=status)


# ---- Trade Now button (Pending tables) --------------------------------------------

_PRICE_REASONS = {
    "PRICE_HIT_TP1": "price has already reached TP1",
    "PRICE_HIT_SL": "price has already reached the stop-loss",
    "PRICE_HIT_TP1_AND_SL": "price has already reached TP1 and the stop-loss",
}
_TRADE_NOW_FAILURES = {
    monitor_service.NOT_PENDING: (409, "This trade is no longer pending: its entry has already filled."),
    monitor_service.ALREADY_OVER: (409, "This trade is already closed or cancelled."),
    monitor_service.PLACING: (409, "This trade is still being placed. Try again in a moment."),
    monitor_service.NO_GATEWAY: (503, "Not connected to the exchange this trade runs on, so it cannot be traded now."),
    monitor_service.TOO_SMALL: (409, "At the current price the size is below the exchange minimum, so nothing was traded."),
    monitor_service.FAILED: (
        502, "The market order could not be placed. The entry order was put back if it had been cancelled; check Pending before trying again.",
    ),
}


async def trade_now_response(request, trade_id: int, *, mode: "str | None", manual: bool):
    """Trade a pending trade at the current market price now. No confirmation, for Real as well."""
    info, error = await _scoped_trade(request, trade_id, mode, manual)
    if error is not None:
        return error
    label = _label(info)
    try:
        outcome, detail = await engine.trade_now(trade_id)
    except EngineNotReady:
        return JsonResponse({"ok": False, "message": "The trading engine is not ready yet."}, status=503)
    except TimeoutError:
        logger.exception("Trade now for trade %s timed out", trade_id)
        return JsonResponse(
            {"ok": False, "message": f"No answer in time while trading {label}. Check Pending and Positions before trying again."},
            status=504,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Trade now for trade %s failed", trade_id)
        return JsonResponse({"ok": False, "message": f"Could not trade {label}: {exc}"}, status=502)
    if outcome == monitor_service.TRADED:
        return JsonResponse({"ok": True, "message": f"{label} traded at market."})
    if outcome == monitor_service.CANCELLED:
        why = _PRICE_REASONS.get(detail, detail)
        return JsonResponse({"ok": False, "message": f"{label} was not traded: {why}. The trade was cancelled."}, status=409)
    status, message = _TRADE_NOW_FAILURES.get(outcome, (502, "The trade could not be traded now."))
    return JsonResponse({"ok": False, "message": message}, status=status)


@require_POST
async def close_api(request, scope: str, trade_id: int):
    sc = _scope(scope)
    return await close_trade_response(request, trade_id, mode=sc.trade_mode, manual=False)


@require_POST
async def trade_now_api(request, scope: str, trade_id: int):
    sc = _scope(scope)
    return await trade_now_response(request, trade_id, mode=sc.trade_mode, manual=False)
