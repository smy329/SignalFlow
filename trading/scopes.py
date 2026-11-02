"""The two independent trading worlds and how their names map onto each other.

Real     sources -> LIVE  trades (orders on Binance)
Forward  sources -> PAPER trades (simulated against real market prices)
"""
from dataclasses import dataclass

from telegram_monitor.models import TelegramSource
from trading.models import Trade


@dataclass(frozen=True)
class Scope:
    slug: str  # used in URLs
    label: str
    source_mode: str  # TelegramSource.mode
    trade_mode: str  # Trade.mode
    config_flag: str  # Settings-page key that must be On for the scope to be allowed at all
    config_label: str  # that setting's label on the Settings page, for messages


REAL = Scope("real", "Real Trades", TelegramSource.Mode.REAL.value, Trade.Mode.LIVE.value, "trading.real_enabled", "Allow live trading")
FORWARD = Scope("forward", "Forward Trades", TelegramSource.Mode.FORWARD.value, Trade.Mode.PAPER.value, "trading.forward_enabled", "Allow forward test")

SCOPES = {s.slug: s for s in (REAL, FORWARD)}
TRADE_MODE_BY_SOURCE_MODE = {s.source_mode: s.trade_mode for s in SCOPES.values()}
SCOPE_BY_SOURCE_MODE = {s.source_mode: s for s in SCOPES.values()}
