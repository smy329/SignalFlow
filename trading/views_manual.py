"""Manual Trade pages: place a trade from pasted signal text, and list manual trades (Real and Forward together)."""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from runtime_settings import registry
from runtime_settings import service as runtime
from telegram_monitor.models import MANUAL_CHANNEL
from trading import views as trading_views
from trading.scopes import REAL, SCOPES
from trading.services import manual_service, position_service, report_service
from trading.services import trade_repository as repo
from trading.services.engine import EngineNotReady, engine
from trading.views import TRADE_TABS

logger = logging.getLogger(__name__)


class _ManualScope:
    """Stands in for a Scope in the shared Trades / History templates."""

    slug = "manual"
    label = "Manual Trades"
    manual = True


MANUAL = _ManualScope()


# ---- place a trade ---------------------------------------------------------------


@login_required
def place_page(request):
    choices = {name: list(registry.BY_KEY[f"manual.{name}"].choices) for name in ("margin_type", "entry_mode", "sl_mode", "reversal_mode")}
    context = {
        "config": {"defaults": runtime.manual_defaults(), "version": runtime.manual_version(), "choices": choices},
        "max_entries": runtime.get("manual.max_entries"),
        "nav_scope": "manual", "nav_item": "place",
    }
    return render(request, "trading/manual.html", context)


def _read(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise manual_service.ManualInputError("The request could not be read.") from None
    scope = SCOPES.get(str(body.get("mode", "")).lower())
    if scope is None:
        raise manual_service.ManualInputError("Choose Real or Forward.")
    options = manual_service.parse_options(body.get("options") or {})
    return body, scope, options


def _read_options(request):
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        raise manual_service.ManualInputError("The request could not be read.") from None
    return body, manual_service.parse_options(body.get("options") or {})


def _error(exc: manual_service.ManualInputError) -> JsonResponse:
    return JsonResponse({"ok": False, "message": str(exc), "fields": exc.fields}, status=400)


@login_required
@require_POST
def analyze(request):
    """Called right after a paste: does this signal need a stop-loss choice, and which options does it have?"""
    try:
        body, options = _read_options(request)
        return JsonResponse({"ok": True, **manual_service.analyze(body.get("text", ""), options)})
    except manual_service.ManualInputError as exc:
        return _error(exc)


@login_required
@require_POST
def preview(request):
    try:
        body, scope, options = _read(request)
        sl_tp = manual_service.parse_sl_tp(body.get("sl_tp"))
        return JsonResponse({"ok": True, "preview": manual_service.preview(body.get("text", ""), options, scope, sl_tp)})
    except manual_service.ManualInputError as exc:
        return _error(exc)


@login_required
@require_POST
def place(request):
    try:
        body, scope, options = _read(request)
        text = body.get("text", "")
        sl_tp = manual_service.parse_sl_tp(body.get("sl_tp"))
        # A Real order is confirmed first, except when the signal has no stop-loss and the click on a TP option
        # is the decision (that click places the trade). A stop-loss written in the text never skips the confirmation.
        one_click = sl_tp is not None and manual_service.analyze(text, options)["needs_stoploss"]
        if scope is REAL and body.get("confirmed") is not True and not one_click:
            raise manual_service.ManualInputError("Confirm the trade first.")
        signal_id = manual_service.submit(text, options, scope, sl_tp)
    except manual_service.ManualInputError as exc:
        return _error(exc)
    return JsonResponse({"ok": True, "signal_id": signal_id})


@login_required
@require_GET
def result(request, signal_id: int):
    data = manual_service.outcome(signal_id)
    if data.get("state") == "placed":
        data["url"] = reverse("manual-history") if data["tab"] == "history" else reverse(f"manual-trades-{data['tab']}")
    return JsonResponse(data)


# ---- manual trades lists ----------------------------------------------------------


@login_required
def trades_redirect(request):
    return redirect("manual-trades-positions")


@login_required
def trades_page(request, tab: str):
    if tab not in TRADE_TABS:
        raise Http404
    tabs = [{"slug": t, "label": label, "url_name": f"manual-trades-{t}"} for t, (label, _) in TRADE_TABS.items()]
    context = {"scope": MANUAL, "tab": tab, "tabs": tabs, "empty_text": TRADE_TABS[tab][1], "nav_scope": "manual", "nav_item": "trades"}
    return render(request, "trading/trades.html", context)


@login_required
def history_page(request):
    return render(request, "trading/history.html", {"scope": MANUAL, "nav_scope": "manual", "nav_item": "history"})


def _tag(raw: list[dict], built: list[dict]) -> list[dict]:
    """Add the Real / Forward label; `built` is in the same order as `raw`."""
    return [{**row, "mode": manual_service.MODE_LABEL.get(r["mode"], r["mode"])} for r, row in zip(raw, built)]


def _json(rows: list[dict], message: str = "") -> JsonResponse:
    return JsonResponse({"enabled": True, "rows": rows, "message": message})


@login_required
@require_GET
def positions_api(request):
    paper = repo.trade_rows(repo.MODE_PAPER, repo.ST_OPEN, manual=True)
    rows = _tag(paper, report_service.position_rows(paper, trading_views._market_prices(report_service.symbols(paper)), trading_views._fee_rate()))
    message = ""
    if repo.trade_rows(repo.MODE_LIVE, repo.ST_OPEN, manual=True):  # the exchange's own numbers for live positions
        try:
            snapshots = engine.run(engine.live_gateway.position_snapshots(), timeout=engine.view_timeout)  # network only
            live = position_service.rows_from(snapshots, repo.active_live_trades_by_symbol())
            rows += [{**r, "mode": "Real"} for r in live if r["channel"] == MANUAL_CHANNEL]
        except (EngineNotReady, KeyError):
            message = "Connecting to Binance..."
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load manual live positions")
            message = f"Could not load live positions: {exc}"
    return _json(rows, message)


@login_required
@require_GET
def pending_api(request):
    raw = repo.trade_rows(None, repo.ST_PENDING, manual=True)
    return _json(_tag(raw, report_service.pending_rows(raw, trading_views._market_prices(report_service.symbols(raw)))))


@login_required
@require_GET
def cancelled_api(request):
    raw = repo.trade_rows(None, repo.ST_CANCELLED, limit=runtime.get("display.history_limit"), manual=True)
    return _json(_tag(raw, report_service.cancelled_rows(raw)))


@login_required
@require_GET
def history_api(request):
    raw = repo.trade_rows(None, repo.ST_CLOSED, limit=runtime.get("display.history_limit"), manual=True)
    return _json(_tag(raw, report_service.history_rows(raw)))


@require_POST
async def close_api(request, trade_id: int):
    return await trading_views.close_trade_response(request, trade_id, mode=None, manual=True)


@require_POST
async def trade_now_api(request, trade_id: int):
    return await trading_views.trade_now_response(request, trade_id, mode=None, manual=True)
