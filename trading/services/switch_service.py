"""Runtime ON/OFF switches for taking new Real / Forward trades.

A scope takes new trades only when BOTH hold:
  * its Settings master flag allows it ("Allow live trading" / "Allow forward test"), and
  * its dashboard switch is ON (no stored row means ON, so upgrading changes nothing).
Switching OFF never touches pending or open trades: they are still managed to their end.
"""
from runtime_settings import service as runtime
from trading.models import TradingSwitch
from trading.scopes import SCOPES, Scope


def _allowed_by_config(scope: Scope) -> bool:
    return bool(runtime.get(scope.config_flag))


def is_enabled(scope: Scope) -> bool:
    row = TradingSwitch.objects.filter(slug=scope.slug).first()
    return row.enabled if row else True


def state(scope: Scope) -> dict:
    allowed, enabled = _allowed_by_config(scope), is_enabled(scope)
    return {"allowed": allowed, "enabled": enabled, "on": allowed and enabled}


def set_enabled(scope: Scope, enabled: bool) -> None:
    TradingSwitch.objects.update_or_create(slug=scope.slug, defaults={"enabled": enabled})


def switched_on_modes() -> set[str]:
    """Trade modes (LIVE / PAPER) whose dashboard switch is ON."""
    off = set(TradingSwitch.objects.filter(enabled=False).values_list("slug", flat=True))
    return {scope.trade_mode for slug, scope in SCOPES.items() if slug not in off}
