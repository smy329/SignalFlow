"""
Reading, validating and storing settings.

Every value lives in the `Setting` table as JSON text. A key with no row simply has
its registry default, so adding a new setting later needs no data migration.

Reads go through a tiny in-process cache (RUNTIME_SETTINGS_CACHE_SECONDS, default 2 s)
that is cleared the moment a value is saved, so a change is visible to the very next
read in this process and to any other process within a couple of seconds.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from django.conf import settings as django_settings
from django.db import DatabaseError, transaction

from runtime_settings import registry
from runtime_settings.registry import (
    BOOL, CHOICE, DECIMAL, INT, INTLIST, TEXT, WORDS, SettingDef,
)
from telegram_monitor.services.signal_parser import ParserRules
from trading.domain.strategy import LEGACY_SL_MODES
from trading.domain.types import MAX_TARGETS

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


class SettingError(ValueError):
    """A submitted value is not valid for its setting (the message is shown to the user)."""


# ---- parsing / validation ------------------------------------------------------------------


def _bounds(defn: SettingDef, value) -> None:
    unit = f" {defn.unit}" if defn.unit else ""
    if defn.min is not None:
        too_low = value <= defn.min if defn.min_exclusive else value < defn.min
        if too_low:
            word = "greater than" if defn.min_exclusive else "at least"
            raise SettingError(f"{defn.label} must be {word} {_plain(defn.min)}{unit}.")
    if defn.max is not None and value > defn.max:
        raise SettingError(f"{defn.label} must be at most {_plain(defn.max)}{unit}.")


def _plain(value) -> str:
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def parse(defn: SettingDef, raw: Any) -> Any:
    """Turn user input (a form string, or an already typed value) into a validated value."""
    kind = defn.kind
    if kind == BOOL:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise SettingError(f"{defn.label}: choose On or Off.")

    if kind == WORDS:
        lines = raw if isinstance(raw, (list, tuple)) else str(raw or "").splitlines()
        words = list(dict.fromkeys(" ".join(str(line).split()).upper() for line in lines if str(line).strip()))
        if not words and not defn.optional:
            raise SettingError(f"{defn.label}: enter at least one word (one per line).")
        return words

    if kind == INTLIST:
        parts = raw if isinstance(raw, (list, tuple)) else str(raw or "").replace(",", " ").split()
        try:
            numbers = [int(str(p).strip()) for p in parts]
        except ValueError:
            raise SettingError(f"{defn.label}: use whole numbers separated by commas, e.g. 60, 40.") from None
        if not numbers:
            raise SettingError(f"{defn.label}: enter at least one percentage.")
        if len(numbers) > MAX_TARGETS:
            raise SettingError(f"{defn.label}: at most {MAX_TARGETS} percentages.")
        if any(not 0 <= n <= 100 for n in numbers):
            raise SettingError(f"{defn.label}: each percentage must be between 0 and 100.")
        if not 0 < sum(numbers) <= 100:
            raise SettingError(f"{defn.label}: the percentages total {sum(numbers)}%; they must total between 1% and 100%.")
        return numbers

    text = "" if raw is None else str(raw).strip()
    if text == "":
        if defn.optional:
            return "" if kind == TEXT else None
        raise SettingError(f"{defn.label} is required.")

    if kind == TEXT:
        return text

    if kind == CHOICE:
        text = LEGACY_SL_MODES.get(text.upper(), text)  # "AFTER_TP" is still accepted (renamed AFTER_FILLING_TP)
        for choice in defn.choices:
            if choice.lower() == text.lower():
                return choice
        raise SettingError(f"{defn.label}: choose one of {', '.join(defn.choices)}.")

    if kind == INT:
        try:
            value = int(text)
        except ValueError:
            raise SettingError(f"{defn.label} must be a whole number.") from None
        _bounds(defn, value)
        return value

    if kind == DECIMAL:
        try:
            value = Decimal(text)
        except InvalidOperation:
            raise SettingError(f"{defn.label} must be a number.") from None
        if not value.is_finite():
            raise SettingError(f"{defn.label} must be a number.")
        _bounds(defn, value)
        return value

    raise SettingError(f"{defn.label}: unsupported setting type.")  # pragma: no cover


def to_storage(defn: SettingDef, value: Any) -> str:
    if value is not None and defn.kind == DECIMAL:
        value = str(value)
    return json.dumps(value)


def from_storage(defn: SettingDef, text: str) -> Any:
    value = json.loads(text)
    if value is not None and defn.kind == DECIMAL:
        value = Decimal(str(value))
    if defn.kind in (WORDS, INTLIST):
        value = list(value)
    if defn.kind == CHOICE:
        value = LEGACY_SL_MODES.get(value, value)
    return value


def form_value(defn: SettingDef, value: Any) -> Any:
    """How a value is shown in its form field."""
    if value is None:
        return ""
    if defn.kind == WORDS:
        return "\n".join(value)
    if defn.kind == INTLIST:
        return ", ".join(str(v) for v in value)
    if defn.kind == DECIMAL:
        return format(value.normalize(), "f") if value else "0"
    return value


# ---- storage + cache -----------------------------------------------------------------------

_lock = threading.Lock()
_cache: Optional[tuple[float, dict[str, str]]] = None


def _ttl() -> float:
    return float(getattr(django_settings, "RUNTIME_SETTINGS_CACHE_SECONDS", 2.0))


def clear_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def _rows() -> dict[str, str]:
    global _cache
    ttl = _ttl()
    now = time.monotonic()
    with _lock:
        if ttl > 0 and _cache is not None and now - _cache[0] < ttl:
            return _cache[1]
    from runtime_settings.models import Setting

    try:
        rows = dict(Setting.objects.values_list("key", "value"))
    except DatabaseError:  # table not migrated yet: behave as "all defaults"
        logger.warning("Settings table is not available yet; using defaults.")
        return {}
    with _lock:
        _cache = (now, rows)
    return rows


def get(key: str) -> Any:
    """The current value of one setting (its default when nothing is stored)."""
    defn = registry.BY_KEY[key]
    raw = _rows().get(key)
    if raw is not None:
        try:
            return from_storage(defn, raw)
        except (ValueError, InvalidOperation, TypeError):
            logger.error("Stored value of %s is unreadable; using the default.", key)
    default = defn.default
    return list(default) if isinstance(default, (list, tuple)) else default


def all_values() -> dict[str, Any]:
    return {d.key: get(d.key) for d in registry.DEFINITIONS}


def is_default(key: str) -> bool:
    default = registry.BY_KEY[key].default
    return get(key) == (list(default) if isinstance(default, (list, tuple)) else default)


def save_many(updates: Mapping[str, Any]) -> None:
    """Validate every value first, then store them all (or none)."""
    from runtime_settings.models import Setting

    prepared = {}
    for key, raw in updates.items():
        defn = registry.BY_KEY[key]
        if not defn.editable:
            raise SettingError(f"{defn.label} cannot be changed.")
        prepared[key] = to_storage(defn, parse(defn, raw))
    with transaction.atomic():
        for key, text in prepared.items():
            Setting.objects.update_or_create(key=key, defaults={"value": text})
    clear_cache()


def reset(key: str) -> None:
    from runtime_settings.models import Setting

    registry.BY_KEY[key]  # KeyError for unknown keys
    Setting.objects.filter(key=key).delete()
    clear_cache()


def seed_from_env(model, environ: Mapping[str, str]) -> list[str]:
    """First-run import of the old .env values. Never overwrites an existing row.
    Returns the keys that were seeded."""
    existing = set(model.objects.values_list("key", flat=True))
    seeded = []
    for defn in registry.DEFINITIONS:
        if not defn.env or defn.key in existing or defn.env not in environ:
            continue
        raw = environ[defn.env]
        try:
            value = parse(defn, raw)
        except SettingError as exc:
            logger.warning("Not importing %s=%r from .env: %s", defn.env, raw, exc)
            continue
        model.objects.create(key=defn.key, value=to_storage(defn, value))
        seeded.append(defn.key)
    return seeded


# ---- typed bundles used by the rest of the project -----------------------------------------


@dataclass(frozen=True)
class SignalValues:
    leverage: Decimal
    margin: Optional[Decimal]
    margin_type: str
    duplicate_window_seconds: int


def signal_values(scope: str) -> SignalValues:
    """The signal values of one trading world: scope is "real" or "forward"."""
    return SignalValues(
        leverage=get(f"{scope}.leverage"),
        margin=get(f"{scope}.margin"),
        margin_type=get(f"{scope}.margin_type"),
        duplicate_window_seconds=get(f"{scope}.duplicate_window_seconds"),
    )


MANUAL_FIELDS = ("leverage", "margin", "margin_type", "entry_mode", "sl_mode", "sl_after_tp", "reversal_mode", "tp_split")


def manual_defaults() -> dict[str, Any]:
    """The Manual trading settings, as form-ready strings (what the Manual Trade page starts from)."""
    return {name: str(form_value(registry.BY_KEY[f"manual.{name}"], get(f"manual.{name}"))) for name in MANUAL_FIELDS}


def manual_version() -> str:
    """Changes whenever the Manual trading page of Settings is saved (even with unchanged values), so the
    Manual Trade form knows to replace its remembered values with the Settings ones."""
    from runtime_settings.models import Setting

    latest = Setting.objects.filter(key__startswith="manual.").order_by("-updated_at").values_list("updated_at", flat=True).first()
    return latest.isoformat() if latest else "default"


def parser_rules() -> ParserRules:
    try:
        return ParserRules.from_words(
            min_length=get("parser.min_message_length"),
            skip_words=get("parser.skip_words"),
            entry_words=get("parser.entry_keywords"),
            target_words=get("parser.target_keywords"),
            stoploss_words=get("parser.stoploss_keywords"),
        )
    except ValueError:  # an empty keyword list sneaked in: never stop parsing because of it
        logger.error("Parser keyword settings are invalid; using the built-in defaults.")
        return ParserRules()


def default_tp_split(scope: str = "real") -> tuple[int, ...]:
    """The default take-profit split of one world ("real" or "forward")."""
    return tuple(get(f"{scope}.tp_split"))


# ---- per-source overrides ------------------------------------------------------------------
# A source stores None for every value it does not override ("Use global"); the value of its world
# (Real trading / Forward test page of Settings) is then read live. Source value first, world value second.

SOURCE_OVERRIDE_FIELDS = ("leverage", "margin", "margin_type", "entry_mode", "sl_mode", "sl_after_tp", "reversal_mode")


def world_of(source) -> str:
    """"real" or "forward": the world whose global values the source falls back to."""
    return "real" if str(source.mode).upper() == "REAL" else "forward"


def effective(source, name: str) -> Any:
    """The value a source trades with for one of SOURCE_OVERRIDE_FIELDS."""
    own = getattr(source, name)
    return get(f"{world_of(source)}.{name}") if own is None else own


def is_overridden(source, name: str) -> bool:
    return getattr(source, name) is not None


def source_signal_values(source) -> SignalValues:
    """Leverage / margin / margin type of a source's new signals (its own, else its world's)."""
    base = signal_values(world_of(source))
    return SignalValues(
        leverage=effective(source, "leverage"),
        margin=effective(source, "margin"),
        margin_type=effective(source, "margin_type"),
        duplicate_window_seconds=base.duplicate_window_seconds,
    )


def world_values(scope: str) -> dict[str, Any]:
    """Every global value a source of this world falls back to, keyed by field name."""
    return {name: get(f"{scope}.{name}") for name in (*SOURCE_OVERRIDE_FIELDS, "tp_split")}


def places(kind: str) -> int:
    """Decimal places for display rounding: kind is "amount", "percent" or "pnl"."""
    return int(get(f"display.{kind}_decimals"))
