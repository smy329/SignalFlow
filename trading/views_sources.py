"""Sources pages: the Real and the Forward source lists are independent."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from runtime_settings import service as runtime
from telegram_monitor.models import MANUAL_TELEGRAM_ID, TelegramSource
from telegram_monitor.services import message_service
from telegram_monitor.services.telegram_client import TelegramConfigError
from telegram_monitor.services.telegram_listener import listener_manager
from runtime_settings import registry
from trading.domain.strategy import (
    OPPOSITE_DIRECTION,
    REVERSAL_IGNORE,
    SL_AFTER_FILLING_TP,
    SourceStrategy,
    validate as validate_strategy,
)
from trading.scopes import REAL, SCOPE_BY_SOURCE_MODE, SCOPES
from trading.services import switch_service
from trading.services import trade_repository as repo


def _scope(slug: str):
    if slug not in SCOPES:
        raise Http404
    return SCOPES[slug]


def _back(request, source: TelegramSource):
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(f"{SCOPE_BY_SOURCE_MODE[source.mode].slug}-sources")


_ENTRY_LABELS = {"NEAREST": "Nearest entry", "FARTHEST": "Farthest entry", "BOTH": "Both entries"}


def _blank(value) -> str:
    return "" if value is None else str(value)


def _plain(value) -> str:
    return format(value.normalize(), "f") if hasattr(value, "normalize") else str(value)


def _settings_attrs(source: TelegramSource) -> dict:
    """What the Sources page needs to show and edit one source's settings. The *_own values are the source's
    overrides ("" = use global); the summary shows what the source actually trades with."""
    strategy = repo.strategy_from_source(source)
    if strategy.uses_default_tp:
        tps = "default " + "/".join(str(p) for p in strategy.default_tp)
    else:
        last = max(i for i, p in enumerate(strategy.tp_percents) if p)
        tps = "/".join(str(p or 0) for p in strategy.tp_percents[: last + 1])
    stop = f"SL to entry after TP{strategy.sl_after_tp}" if strategy.sl_mode == SL_AFTER_FILLING_TP else "SL to entry at TP1"
    opposite = strategy.follow == OPPOSITE_DIRECTION
    values = runtime.source_signal_values(source)
    size = f"{_plain(values.leverage)}x · " + (f"{_plain(values.margin)} USDT" if values.margin is not None else "no margin") + f" · {values.margin_type}"
    custom = sum(runtime.is_overridden(source, n) for n in runtime.SOURCE_OVERRIDE_FIELDS) + int(not strategy.uses_default_tp)
    return {
        "summary": ("Opposite to signals · " if opposite else "")
        + f"{size} · {_ENTRY_LABELS.get(strategy.entry_mode, strategy.entry_mode)} · "
        + ("TP = signal SL, SL = signal TP1" if opposite else f"TP {tps} · {stop}")
        + (" · Ignores reversals" if strategy.reversal == REVERSAL_IGNORE else " · One-Way reversal")
        + (f" · {custom} custom" if custom else " · all global"),
        "leverage": _blank(source.leverage and _plain(source.leverage)),
        "margin": _blank(source.margin and _plain(source.margin)),
        "margin_type": _blank(source.margin_type),
        "follow_mode": _blank(source.follow_mode),
        "entry_mode": _blank(source.entry_mode),
        "tp_csv": ",".join("" if p is None else str(p) for p in (getattr(source, f) for f in TelegramSource.TP_PCT_FIELDS)),
        "sl_mode": _blank(source.sl_mode),
        "sl_after_tp": _blank(source.sl_after_tp),
        "reversal_mode": _blank(source.reversal_mode),
    }


def _world_globals(scope) -> dict:
    """The global values of one world as plain strings, for the 'Use global (currently ...)' hints in the modal."""
    values = runtime.world_values(scope.slug)
    return {
        "leverage": _plain(values["leverage"]),
        "margin": "" if values["margin"] is None else _plain(values["margin"]),
        "margin_type": values["margin_type"],
        "follow_mode": values["follow_mode"],
        "entry_mode": values["entry_mode"],
        "sl_mode": values["sl_mode"],
        "sl_after_tp": values["sl_after_tp"],
        "reversal_mode": values["reversal_mode"],
        "tp_split": list(values["tp_split"]),
        "max_entries": runtime.get(f"{scope.slug}.max_entries"),
    }


def _wants_json(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@login_required
def sources_page(request, scope: str):
    sc = _scope(scope)
    per_source, totals = repo.source_stats(sc.trade_mode)
    messages_by_source = message_service.message_counts_by_source(sc.source_mode)
    rows = [
        {
            "source": s,
            "stats": {**(per_source.get(s.id) or repo.empty_stats()), "messages": messages_by_source.get(s.id, 0)},
            "settings": _settings_attrs(s),
        }
        for s in TelegramSource.objects.filter(mode=sc.source_mode).exclude(telegram_id=MANUAL_TELEGRAM_ID)
    ]
    totals["messages"] = sum(messages_by_source.values())
    return render(
        request,
        "trading/sources.html",
        {"scope": sc, "rows": rows, "totals": totals, "nav_scope": sc.slug, "nav_item": "sources",
         "world_globals": _world_globals(sc), "world_settings_url": reverse("settings-group", args=[registry.slug_of(registry.G_REAL if sc.slug == "real" else registry.G_FORWARD)])},
    )


@login_required
def source_create(request, scope: str):
    sc = _scope(scope)
    context = {"scope": sc, "nav_scope": sc.slug, "nav_item": "sources"}
    if request.method != "POST":
        return render(request, "telegram_monitor/source_form.html", context)

    name = request.POST.get("name", "").strip()
    try:
        telegram_id = int(request.POST.get("telegram_id", "").strip())
    except ValueError:
        messages.error(request, "Telegram Group/Channel ID must be a number, e.g. -1001234567890.")
        return render(request, "telegram_monitor/source_form.html", context)

    try:
        info = listener_manager.resolve_entity(telegram_id)
    except TelegramConfigError as exc:
        messages.error(request, str(exc))
        return render(request, "telegram_monitor/source_form.html", context)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Could not access that chat: {exc}")
        return render(request, "telegram_monitor/source_form.html", context)

    fields = {"name": name or info["name"], "username": info.get("username"), "is_active": True}
    source = TelegramSource.objects.filter(telegram_id=telegram_id, mode=sc.source_mode).first()
    if source is not None:  # re-adding a known chat: keep the overrides this source already has
        for field, value in fields.items():
            setattr(source, field, value)
        source.save()
    else:  # a new source overrides nothing: every value follows its world (Settings > Real trading / Forward test)
        source = TelegramSource.objects.create(telegram_id=telegram_id, mode=sc.source_mode, **fields)
    messages.success(request, f"Source '{source.name}' added to {sc.label} and now being monitored.")
    return redirect(f"{sc.slug}-sources")


@login_required
@require_POST
def source_toggle(request, pk: int):
    source = get_object_or_404(TelegramSource, pk=pk)
    source.is_active = not source.is_active
    source.save(update_fields=["is_active", "updated_at"])
    if _wants_json(request):  # the toggle switch on the Sources page flips without a page reload
        return JsonResponse({"is_active": source.is_active})
    return _back(request, source)


def _override(request, source, name, parse):
    """The value to store for one overridable field. Field missing from the form = keep what the source has;
    blank = "Use global" (None); otherwise parse() validates it (raises SettingError)."""
    if name not in request.POST:
        return getattr(source, name)
    raw = request.POST.get(name, "").strip()
    return None if raw == "" else parse(raw)


@login_required
@require_POST
def source_settings(request, pk: int):
    """Save the overrides of one source (position size, entry / take-profit / stop-loss / reversal). A blank
    field means "use the global value". Only trades created afterwards use the new values."""
    source = get_object_or_404(TelegramSource, pk=pk)
    world = runtime.world_of(source)
    by_key = registry.BY_KEY

    def one(name):
        defn = by_key[f"{world}.{name}"]
        return lambda raw: runtime.parse(defn, raw)

    try:
        percents = [_whole_percent(request.POST.get(f"tp{n}", "")) for n in range(1, 9)]
    except ValueError:
        messages.error(request, "Take-profit percentages must be whole numbers.")
        return _back(request, source)
    try:
        new = {name: _override(request, source, name, one(name)) for name in runtime.SOURCE_OVERRIDE_FIELDS}
    except runtime.SettingError as exc:
        messages.error(request, f"Settings not saved. {exc}")
        return _back(request, source)

    # Validate what the source would actually trade with: its overrides on top of the world's values.
    glob = runtime.world_values(world)
    pick = lambda name: glob[name] if new[name] is None else new[name]  # noqa: E731
    strategy = SourceStrategy(
        entry_mode=pick("entry_mode"), tp_percents=percents, sl_mode=pick("sl_mode"), sl_after_tp=pick("sl_after_tp"),
        reversal=pick("reversal_mode"), default_tp=tuple(glob["tp_split"]), follow=pick("follow_mode"),
    )
    errors = validate_strategy(strategy)
    if errors:
        messages.error(request, "Settings not saved. " + " ".join(errors))
        return _back(request, source)

    for name, value in new.items():
        setattr(source, name, value)
    for field, value in zip(TelegramSource.TP_PCT_FIELDS, percents):
        setattr(source, field, value)
    source.save(update_fields=[*runtime.SOURCE_OVERRIDE_FIELDS, *TelegramSource.TP_PCT_FIELDS, "updated_at"])
    messages.success(request, f"Settings of '{source.name}' saved. They apply to new trades only.")
    return _back(request, source)


def _whole_percent(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError(raw)
    return int(raw)


@login_required
@require_POST
def source_delete(request, pk: int):
    source = get_object_or_404(TelegramSource, pk=pk)
    # Deleting cascades to the source's signals and trades; never orphan live exchange orders.
    if source.mode == REAL.source_mode and repo.has_active_trades(source.id, REAL.trade_mode):
        messages.error(
            request,
            f"'{source.name}' still has live trades in progress. Deactivate it to stop new trades, then delete it once they are closed.",
        )
        return _back(request, source)

    name, back = source.name, _back(request, source)
    source.delete()
    messages.success(request, f"Source '{name}' and its trades were deleted.")
    return back


@login_required
@require_POST
def switch_set(request, scope: str):
    sc = _scope(scope)
    if not switch_service.state(sc)["allowed"]:
        messages.error(request, f"{sc.label} is disabled in Settings ('{sc.config_label}' is Off) and cannot be switched on here.")
    else:
        enabled = request.POST.get("enabled") == "1"
        switch_service.set_enabled(sc, enabled)
        messages.success(
            request,
            f"{sc.label} are ON: new signals are traded again." if enabled
            else f"{sc.label} are OFF: no new trades will be taken. Pending and open trades keep being managed.",
        )
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(f"{sc.slug}-sources")
