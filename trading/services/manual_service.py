"""
Manual Trade: turns a pasted signal text plus the options of the Manual Trade page into a signal that the
trading engine handles exactly like a Telegram one (same planner, same orders, same monitoring).

A manual trade belongs to a built-in "Manual" source (one row per mode, never listened to). The strategy
chosen on the page is stored on the signal itself (`strategy_override`), so two manual trades placed back
to back can never overwrite each other's options.
"""
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional

from django.db import transaction
from django.utils import timezone

from runtime_settings import registry
from runtime_settings import service as runtime
from telegram_monitor import signals as signal_events
from telegram_monitor.models import MANUAL_CHANNEL, MANUAL_TELEGRAM_ID, ParsedSignal, TelegramMessage, TelegramSource
from telegram_monitor.services import message_status
from telegram_monitor.services.signal_parser import parse_signal
from trading.domain.strategy import SL_AFTER_FILLING_TP, SourceStrategy, select_entries, validate
from trading.domain.tp_stoploss import sl_options
from trading.domain.types import BUY
from trading.models import Trade
from trading.scopes import REAL, Scope
from trading.services.engine import engine

MODE_LABEL = {"LIVE": "Real", "PAPER": "Forward"}


class ManualInputError(ValueError):
    """The page's input cannot be traded. `fields` maps a form field name to its message."""

    def __init__(self, message: str, fields: Optional[dict] = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


@dataclass(frozen=True)
class ManualOptions:
    leverage: Decimal
    margin: Decimal
    margin_type: str
    entry_mode: str
    sl_mode: str
    sl_after_tp: int
    reversal_mode: str
    tp_split: tuple

    @property
    def strategy(self) -> SourceStrategy:
        return SourceStrategy(
            entry_mode=self.entry_mode, sl_mode=self.sl_mode, sl_after_tp=self.sl_after_tp,
            reversal=self.reversal_mode, default_tp=self.tp_split,
        )


def parse_options(raw: Mapping[str, Any]) -> ManualOptions:
    """Validate the option fields with the same rules as the Manual trading page of Settings."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name in runtime.MANUAL_FIELDS:
        if name == "sl_after_tp":
            continue
        try:
            values[name] = runtime.parse(registry.BY_KEY[f"manual.{name}"], raw.get(name, ""))
        except runtime.SettingError as exc:
            errors[name] = str(exc)
    if values.get("sl_mode") == SL_AFTER_FILLING_TP or "sl_mode" in errors:
        try:
            values["sl_after_tp"] = runtime.parse(registry.BY_KEY["manual.sl_after_tp"], raw.get("sl_after_tp", ""))
        except runtime.SettingError as exc:
            errors["sl_after_tp"] = str(exc)
    else:  # TP1_TOUCH never reads it, so a blank or odd value must not block the trade
        values["sl_after_tp"] = 1
    if errors:
        raise ManualInputError("Some of the trade options are not valid.", errors)

    options = ManualOptions(
        leverage=values["leverage"], margin=values["margin"], margin_type=values["margin_type"],
        entry_mode=values["entry_mode"], sl_mode=values["sl_mode"], sl_after_tp=values["sl_after_tp"],
        reversal_mode=values["reversal_mode"], tp_split=tuple(values["tp_split"]),
    )
    problems = validate(options.strategy)
    if problems:
        raise ManualInputError(problems[0])
    return options


def parse_sl_tp(raw: Any) -> Optional[int]:
    """The TP number the page chose for a signal without a stop-loss (None = none chosen)."""
    if raw in (None, ""):
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ManualInputError("The stop-loss option is not valid.", {"sl_tp": "The stop-loss option is not valid."})
    return raw


def _decimals(values) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


def _options_for(signal: dict) -> list:
    entries = _decimals(signal["entries"]) or _decimals([signal["entry"]])
    return sl_options(signal["direction"], entries, _decimals(signal["targets"]))


def _read_signal(text: str, options: ManualOptions) -> dict:
    """The parsed signal; its stoploss is None when the text has none."""
    text = (text or "").strip()
    if not text:
        raise ManualInputError("Paste a signal first.", {"text": "Paste a signal first."})
    result = parse_signal(
        text, channel=MANUAL_CHANNEL, leverage=options.leverage, margin=options.margin,
        margin_type=options.margin_type, rules=runtime.parser_rules(), require_stoploss=False,
    )
    if not result.success:
        _, reason = message_status.describe("FAILED", result.error_code or "")
        message = reason or result.error_message or "This text is not a valid signal."
        raise ManualInputError(message, {"text": message})
    return dict(result.signal)


def parse_text(text: str, options: ManualOptions, sl_tp: Optional[int] = None) -> dict:
    """The signal in `text` (direction, entries, targets and stop-loss all come from the text).

    A signal without a stop-loss needs `sl_tp`: the stop-loss then becomes 1:1 with that target (the price is
    worked out here from the signal, never taken from the browser). A stop-loss written in the text always wins.
    """
    signal = _read_signal(text, options)
    if signal["stoploss"] is None:
        if sl_tp is None:
            message = "This signal has no stop-loss. Choose a stop-loss option (TP1, TP2, ...)."
            raise ManualInputError(message, {"text": message})
        choices = _options_for(signal)
        if sl_tp > len(choices):
            raise ManualInputError(f"The signal has no TP{sl_tp}.", {"sl_tp": f"The signal has no TP{sl_tp}."})
        choice = choices[sl_tp - 1]
        if not choice.usable:
            raise ManualInputError(f"TP{sl_tp} cannot be used for the stop-loss: {choice.reason}.", {"sl_tp": choice.reason})
        signal["stoploss"] = choice.sl_price
    return signal


def analyze(text: str, options: ManualOptions) -> dict:
    """Does the signal need a stop-loss choice? If so, the options (one per target). Nothing is stored."""
    signal = _read_signal(text, options)
    if signal["stoploss"] is not None:
        return {"needs_stoploss": False}
    choices = _options_for(signal)
    return {
        "needs_stoploss": True,
        "symbol": signal["symbol"],
        "side": "LONG" if signal["direction"] == BUY else "SHORT",
        "entry": _plain(Decimal(str(signal["entry"]))),
        "options": [
            {"tp": c.tp, "tp_price": _plain(c.tp_price), "sl_price": _plain(c.sl_price),
             "percent": format(c.percent.quantize(Decimal("0.01")), "f"), "usable": c.usable, "reason": c.reason}
            for c in choices
        ],
    }


def unavailable_reason(scope: Scope) -> Optional[str]:
    """Why a manual trade cannot be taken in this world right now (None = it can)."""
    if not runtime.get(scope.config_flag):
        return f"'{scope.config_label}' is Off on the Settings page."
    if not engine.is_running:
        return "The trading engine is not running."
    if scope.trade_mode not in engine.gateways:
        if scope is REAL:
            return "Live trading is not connected: check the Binance API keys in .env and that the testnet setting matches them."
        return "The forward test is not connected yet (the engine has not reached Binance)."
    return None


def _plain(value) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def preview(text: str, options: ManualOptions, scope: Scope, sl_tp: Optional[int] = None) -> dict:
    """What would be traded, for the confirmation step. Nothing is stored."""
    signal = parse_text(text, options, sl_tp)
    reason = unavailable_reason(scope)
    if reason:
        raise ManualInputError(reason)
    entries = [Decimal(str(e)) for e in signal["entries"]] or [Decimal(str(signal["entry"]))]
    used = select_entries(signal["direction"], entries, options.entry_mode)
    return {
        "mode": scope.slug,
        "mode_label": "Real" if scope is REAL else "Forward",
        "symbol": signal["symbol"],
        "side": "LONG" if signal["direction"] == BUY else "SHORT",
        "entries": [_plain(e) for e in sorted(set(entries))],
        "entries_used": [_plain(e) for e in used],
        "targets": [_plain(t) for t in signal["targets"]],
        "stoploss": _plain(Decimal(str(signal["stoploss"]))),
        "leverage": f"{_plain(options.leverage)}x",
        "margin": f"{_plain(options.margin)} USDT",
        "margin_type": options.margin_type,
        "entry_mode": options.entry_mode,
        "sl_mode": options.sl_mode + (f" {options.sl_after_tp}" if options.sl_mode == SL_AFTER_FILLING_TP else ""),
        "reversal_mode": options.reversal_mode,
        "tp_split": ", ".join(str(p) for p in options.tp_split),
    }


def submit(text: str, options: ManualOptions, scope: Scope, sl_tp: Optional[int] = None) -> int:
    """Store the signal and hand it to the trading engine. Returns the signal id."""
    signal = parse_text(text, options, sl_tp)
    reason = unavailable_reason(scope)
    if reason:
        raise ManualInputError(reason)

    with transaction.atomic():
        source, _ = TelegramSource.objects.get_or_create(
            telegram_id=MANUAL_TELEGRAM_ID, mode=scope.source_mode,
            defaults={"name": MANUAL_CHANNEL, "is_active": False},  # inactive: the Telegram listener never sees it
        )
        message = TelegramMessage.objects.create(
            source=source, telegram_message_id=time.time_ns() // 1000, sender_name=MANUAL_CHANNEL,
            message_text=text.strip(), telegram_date=timezone.now(), parse_status=TelegramMessage.ParseStatus.SUCCESS,
        )
        created = ParsedSignal.objects.create(
            telegram_message=message, source=source, symbol=signal["symbol"], direction=signal["direction"],
            leverage=signal["leverage"], margin=signal["margin"], margin_type=signal["margin_type"],
            entry=signal["entry"], entries=signal["entries"], targets=signal["targets"],
            stoploss=signal["stoploss"], channel=MANUAL_CHANNEL, strategy_override=options.strategy.to_dict(),
        )
        transaction.on_commit(lambda: _notify(created.id), robust=True)
    return created.id


def _notify(signal_id: int) -> None:
    for _receiver, response in signal_events.signal_committed.send_robust(sender=ParsedSignal, signal_id=signal_id):
        if isinstance(response, Exception):
            raise response


def outcome(signal_id: int) -> dict:
    """How far a manual signal got: waiting for the engine, placing, placed, rejected or failed."""
    trade = (
        Trade.objects.filter(signal_id=signal_id, signal__source__telegram_id=MANUAL_TELEGRAM_ID).first()
    )
    if trade is None:
        return {"state": "waiting"}
    status = Trade.Status
    if trade.status == status.PLACING:
        return {"state": "placing"}
    base = {"mode_label": MODE_LABEL.get(trade.mode, trade.mode), "symbol": trade.symbol,
            "side": "LONG" if trade.side == "BUY" else "SHORT"}
    if trade.status in (status.REJECTED, status.FAILED):
        return {**base, "state": "rejected" if trade.status == status.REJECTED else "failed",
                "code": trade.reason_code, "message": trade.reason_message}
    tab = {status.OPEN: "positions", status.PENDING_ENTRY: "pending", status.CANCELLED: "cancelled"}.get(trade.status)
    return {
        **base, "state": "placed", "label": trade.get_status_display(), "tab": tab or "history",
        "quantity": _plain(trade.quantity) if trade.quantity is not None else None,
        "entries": [_plain(p) for p in trade.entry_prices],
        "stoploss": _plain(trade.stoploss) if trade.stoploss is not None else None,
        "targets": [_plain(t) for t in trade.targets],
    }
