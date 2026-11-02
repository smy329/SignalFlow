"""
The catalogue of every setting that can be changed from the Settings page.

One `SettingDef` per setting: where it lives on the page, its type, default, limits
and (optionally) the .env variable it is seeded from on the first migrate.
Nothing here touches the database; `service.py` stores and reads the values.

Keys are dotted: "<area>.<name>". Anything that exists once per trading world uses
the scope as its area: "real.leverage" and "forward.leverage" are independent values.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from telegram_monitor.services.signal_parser import (
    DEFAULT_ENTRY_WORDS,
    DEFAULT_MIN_MESSAGE_LENGTH,
    DEFAULT_SKIP_WORDS,
    DEFAULT_STOPLOSS_WORDS,
    DEFAULT_TARGET_WORDS,
)
from trading.domain.strategy import DEFAULT_TP_PERCENTS, ENTRY_MODES, MAX_ENTRIES, REVERSAL_MODES, SL_MODES
from trading.domain.types import MAX_TARGETS

# value kinds
INT, DECIMAL, BOOL, TEXT, CHOICE, WORDS, INTLIST = "int", "decimal", "bool", "text", "choice", "words", "intlist"

MARGIN_TYPES = ("CROSSED", "ISOLATED")
CANDLE_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h")


@dataclass(frozen=True)
class SettingDef:
    key: str
    group: str
    label: str
    kind: str
    default: Any
    help: str = ""
    unit: str = ""
    env: Optional[str] = None  # .env variable used to seed this setting once, on migrate
    min: Optional[Any] = None
    max: Optional[Any] = None
    min_exclusive: bool = False
    choices: tuple = ()
    optional: bool = False  # blank is allowed and means "not set" (None)
    editable: bool = True  # False: shown read-only (structural limits)


# ---- groups, in page order -----------------------------------------------------------------

G_REAL = "Real trading"
G_FORWARD = "Forward test"
G_MANUAL = "Manual trading"
G_ENGINE = "Trading engine"
G_BINANCE = "Binance & paper trading"
G_TELEGRAM = "Telegram"
G_PARSER = "Signal parser"
G_DISPLAY = "Display"

GROUPS = (G_REAL, G_FORWARD, G_MANUAL, G_ENGINE, G_BINANCE, G_TELEGRAM, G_PARSER, G_DISPLAY)

GROUP_HELP = {
    G_REAL: "Signals from Real sources are traded live on Binance with these values. Every Real source can override each "
    "of them on the Sources page; a source that does not follows these values live. A change only affects trades "
    "created afterwards: open and pending trades keep the values they started with.",
    G_FORWARD: "Signals from Forward sources are simulated (paper trades) with these values. Independent of the Real "
    "values. Every Forward source can override each of them on the Sources page; a source that does not follows these "
    "values live. A change only affects trades created afterwards.",
    G_MANUAL: "Starting values of the Manual Trade page (sidebar > Manual > Place trade). Saving this page puts these values "
    "on that page's form; open trades are never changed.",
    G_ENGINE: "How the trading engine runs. Open trades are never closed or cancelled by changing anything here.",
    G_BINANCE: "Exchange data caches, limits and the paper-trade replay.",
    G_TELEGRAM: "The Telegram listener. Switching it off disconnects the listener; open trades keep being managed.",
    G_PARSER: "How message text is turned into a signal. Keywords are plain words, one per line; a space inside a "
    "phrase matches any amount of whitespace.",
    G_DISPLAY: "List sizes and how many decimals the tables show. Display only; nothing stored or traded changes.",
}


ENTRY_HELP = "NEAREST: long = highest entry, short = lowest. FARTHEST: the opposite. BOTH: lowest + highest, margin split equally."
SL_MODE_HELP = (
    "TP1_TOUCH: stop-loss moves to entry when price touches TP1. "
    "AFTER_FILLING_TP: stop-loss moves to entry after a chosen TP order is filled."
)
SL_AFTER_HELP = "Target number whose fill moves the stop-loss. Used when the stop-loss mode is AFTER_FILLING_TP."


def _scoped(scope: str, group: str, *, leverage_default=Decimal("10")) -> list[SettingDef]:
    """The values every trading world has (Real and Forward each get their own, independent copy).
    A source of that world may override each of them except the maximum number of entries."""
    return [
        SettingDef(
            f"{scope}.leverage", group, "Leverage", DECIMAL, leverage_default, unit="x", env="SIGNAL_LEVERAGE",
            min=Decimal("1"), max=Decimal("2000"),
            help="Applied to every new signal. The exchange may cap it per symbol; the trade is then sized for the cap.",
        ),
        SettingDef(
            f"{scope}.margin", group, "Margin per trade", DECIMAL, Decimal("2"), unit="USDT", env="SIGNAL_MARGIN",
            min=Decimal("0"), min_exclusive=True, optional=True,
            help="Margin used for each trade. Leave blank to reject every signal (nothing is traded without a margin).",
        ),
        SettingDef(
            f"{scope}.margin_type", group, "Margin type", CHOICE, "CROSSED", env="SIGNAL_MARGIN_TYPE",
            choices=MARGIN_TYPES,
        ),
        SettingDef(
            f"{scope}.entry_mode", group, "Entry mode", CHOICE, "NEAREST", choices=tuple(ENTRY_MODES),
            help=ENTRY_HELP,
        ),
        SettingDef(
            f"{scope}.sl_mode", group, "Stop-loss mode", CHOICE, "TP1_TOUCH", choices=tuple(SL_MODES),
            help=SL_MODE_HELP,
        ),
        SettingDef(
            f"{scope}.sl_after_tp", group, "Move Stop-loss after TP", INT, 1, min=1, max=MAX_TARGETS,
            help=SL_AFTER_HELP,
        ),
        SettingDef(
            f"{scope}.reversal_mode", group, "Reversal mode", CHOICE, "ONE_WAY", choices=tuple(REVERSAL_MODES),
            help="What a NEW signal does when its own source already has an active trade on the same coin in the "
            "opposite direction. ONE_WAY: close that trade, then take the new signal. IGNORE: skip the new signal. "
            "(Hedge Mode is upcoming.)",
        ),
        SettingDef(
            f"{scope}.tp_split", group, "Default take-profit split", INTLIST, list(DEFAULT_TP_PERCENTS), unit="%",
            help="Percent closed at TP1, TP2, ... separated by commas, e.g. 60, 40. Used by sources that set no "
            "take-profit percentages. Must add up to 100 or less; any remainder goes to the last target.",
        ),
        SettingDef(
            f"{scope}.max_entries", group, "Maximum entries", INT, MAX_ENTRIES, editable=False,
            help="Fixed by the order logic (a trade has at most two entry orders).",
        ),
        SettingDef(
            f"{scope}.duplicate_window_seconds", group, "Duplicate window", INT, 300, unit="seconds",
            env="SIGNAL_DUPLICATE_WINDOW_SECONDS", min=0,
            help="Same source + symbol + direction + entry within this many seconds counts as a duplicate. 0 turns it off.",
        ),
    ]


DEFINITIONS: tuple[SettingDef, ...] = tuple(
    [
        # ---- Real trading -----------------------------------------------------------------
        SettingDef(
            "trading.real_enabled", G_REAL, "Allow live trading", BOOL, False, env="TRADING_ENABLED",
            help="Master switch for opening NEW live trades. Open trades are always managed. The ON/OFF button on the "
            "Real pages is a second, independent switch: both must be on.",
        ),
        *_scoped("real", G_REAL),
        # ---- Forward test -----------------------------------------------------------------
        SettingDef(
            "trading.forward_enabled", G_FORWARD, "Allow forward test", BOOL, True, env="FORWARD_TEST_ENABLED",
            help="Master switch for opening NEW simulated trades. The ON/OFF button on the Forward pages is a second, "
            "independent switch: both must be on.",
        ),
        *_scoped("forward", G_FORWARD),
        SettingDef(
            "forward.fee_percent", G_FORWARD, "Fee per fill", DECIMAL, Decimal("0.05"), unit="%",
            env="FORWARD_TEST_FEE_PERCENT", min=Decimal("0"), max=Decimal("100"),
            help="Charged on every simulated fill. Applies to fills from now on; the PnL of trades already closed does "
            "not change. Real trades pay the exchange's own fees.",
        ),
        # ---- Manual trading ---------------------------------------------------------------
        SettingDef(
            "manual.leverage", G_MANUAL, "Leverage", DECIMAL, Decimal("10"), unit="x",
            min=Decimal("1"), max=Decimal("2000"),
            help="Numbers only; the x is added for you. The exchange may cap it per symbol; the trade is then sized for the cap.",
        ),
        SettingDef(
            "manual.margin", G_MANUAL, "Margin per trade", DECIMAL, Decimal("3"), unit="USDT",
            min=Decimal("0"), min_exclusive=True, help="Margin used for each manual trade.",
        ),
        SettingDef("manual.margin_type", G_MANUAL, "Margin type", CHOICE, "CROSSED", choices=MARGIN_TYPES),
        SettingDef(
            "manual.entry_mode", G_MANUAL, "Entry mode", CHOICE, "NEAREST", choices=tuple(ENTRY_MODES),
            help="NEAREST: long = highest entry, short = lowest. FARTHEST: the opposite. BOTH: lowest + highest, margin split equally.",
        ),
        SettingDef(
            "manual.sl_mode", G_MANUAL, "Stop-loss mode", CHOICE, "TP1_TOUCH", choices=tuple(SL_MODES),
            help=SL_MODE_HELP,
        ),
        SettingDef(
            "manual.sl_after_tp", G_MANUAL, "Move Stop-loss after TP", INT, 1, min=1, max=MAX_TARGETS,
            help=SL_AFTER_HELP,
        ),
        SettingDef(
            "manual.reversal_mode", G_MANUAL, "Reversal mode", CHOICE, "ONE_WAY", choices=tuple(REVERSAL_MODES),
            help="What a NEW manual signal does when a manual trade on the same coin is active in the opposite direction. "
            "ONE_WAY: close that trade, then take the new signal. IGNORE: skip the new signal. (Hedge Mode is upcoming.)",
        ),
        SettingDef(
            "manual.tp_split", G_MANUAL, "Default take-profit split", INTLIST, list(DEFAULT_TP_PERCENTS), unit="%",
            help="Percent closed at TP1, TP2, ... separated by commas, e.g. 60, 40. Must add up to 100 or less; "
            "any remainder goes to the last target.",
        ),
        SettingDef(
            "manual.max_entries", G_MANUAL, "Maximum entries", INT, MAX_ENTRIES, editable=False,
            help="Fixed by the order logic (a trade has at most two entry orders).",
        ),
        # ---- Trading engine ---------------------------------------------------------------
        SettingDef(
            "trading.testnet", G_ENGINE, "Use Binance testnet", BOOL, False, env="BINANCE_TESTNET",
            help="Where NEW live trades are placed. A live trade stays on the environment it was opened on and keeps "
            "being managed there, so switching never strands an open trade. The API keys in .env must belong to the "
            "environment you select.",
        ),
        SettingDef(
            "trading.poll_seconds", G_ENGINE, "Monitor poll interval", INT, 5, unit="seconds", env="TRADING_POLL_SECONDS",
            min=1, help="How often open and pending trades are checked.",
        ),
        SettingDef(
            "trading.min_poll_seconds", G_ENGINE, "Minimum poll interval", INT, 1, unit="seconds", min=1,
            help="The poll interval is never allowed below this.",
        ),
        SettingDef(
            "trading.max_concurrent_trades", G_ENGINE, "Maximum concurrent trades", INT, 8, min=1, max=100,
            help="How many trades are checked at the same time in one poll.",
        ),
        SettingDef(
            "engine.reconnect_wait_seconds", G_ENGINE, "Engine reconnect wait", INT, 30, unit="seconds", min=1,
            help="Wait before retrying when Binance cannot be reached.",
        ),
        SettingDef(
            "engine.call_timeout_seconds", G_ENGINE, "Engine call timeout", INT, 15, unit="seconds", min=1,
            help="Longest a page waits for the trading engine.",
        ),
        SettingDef(
            "engine.ready_timeout_seconds", G_ENGINE, "Engine ready timeout", INT, 5, unit="seconds", min=1,
            help="Longest a page waits for the engine to finish starting; also the shutdown wait.",
        ),
        SettingDef(
            "engine.view_timeout_seconds", G_ENGINE, "Price / position lookup timeout", INT, 10, unit="seconds", min=1,
            help="Longest the trade tables wait for current prices or live positions.",
        ),
        # ---- Binance & paper trading ------------------------------------------------------
        SettingDef(
            "binance.filters_cache_seconds", G_BINANCE, "Exchange rules cache", INT, 600, unit="seconds", min=1,
            help="How long symbol rules (tick size, minimum quantity) are reused before reloading.",
        ),
        SettingDef(
            "binance.leverage_cache_seconds", G_BINANCE, "Max-leverage cache", INT, 3600, unit="seconds", min=1,
            help="How long a symbol's maximum leverage is reused.",
        ),
        SettingDef(
            "binance.rate_limit_retry_seconds", G_BINANCE, "Rate-limit retry", INT, 60, unit="seconds", min=1,
            help="Forward-test price sync pauses this long after Binance asks us to slow down.",
        ),
        SettingDef(
            "binance.candle_interval", G_BINANCE, "Price candle interval", CHOICE, "1m", choices=CANDLE_INTERVALS,
            help="Candle size used to detect that price touched a level while the engine was not looking.",
        ),
        SettingDef(
            "paper.page_size", G_BINANCE, "Paper trades page size", INT, 1000, min=1, max=1000,
            help="Exchange trades fetched per request when replaying the market (Binance allows at most 1000).",
        ),
        SettingDef(
            "paper.pages_per_sync", G_BINANCE, "Pages per sync", INT, 6, min=1,
            help="Requests per poll cycle across all symbols (about 20 request-weight each; Binance allows 2400 per minute).",
        ),
        SettingDef(
            "paper.trade_lookback_minutes", G_BINANCE, "Trade lookback", INT, 2 * 24 * 60 - 5, unit="minutes", min=1,
            max=2 * 24 * 60 - 5,
            help="How far back the paper replay may start. Binance only serves the last 2 days of trades, so 2875 is the maximum.",
        ),
        # ---- Telegram ---------------------------------------------------------------------
        SettingDef(
            "telegram.listener_enabled", G_TELEGRAM, "Telegram listener", BOOL, True, env="TELEGRAM_LISTENER_ENABLED",
            help="Off disconnects the listener (no new messages are read). On reconnects it, within a few seconds.",
        ),
        SettingDef(
            "telegram.reconnect_wait_seconds", G_TELEGRAM, "Listener reconnect wait", INT, 5, unit="seconds", min=1,
            help="Wait before reconnecting after the connection drops.",
        ),
        SettingDef(
            "telegram.entity_lookup_timeout_seconds", G_TELEGRAM, "Entity lookup timeout", INT, 15, unit="seconds",
            min=1, help="Longest 'Add source' waits to confirm the account can access a chat.",
        ),
        SettingDef(
            "telegram.signals_channel", G_TELEGRAM, "Crypto signals channel", TEXT, "", env="CRYPTO_SIGNALS_CHANNEL",
            optional=True, help="Channel id, e.g. -1001234567890. Stored for later use; nothing reads it yet.",
        ),
        SettingDef(
            "telegram.signal_sender_bot", G_TELEGRAM, "Signal sender bot", TEXT, "", env="CRYPTO_SIGNAL_SENDER_BOT",
            optional=True, help="Bot token. Stored for later use; nothing reads it yet.",
        ),
        # ---- Signal parser ----------------------------------------------------------------
        SettingDef(
            "parser.min_message_length", G_PARSER, "Minimum message length", INT, DEFAULT_MIN_MESSAGE_LENGTH,
            unit="characters", min=1, help="Shorter messages are never treated as signals.",
        ),
        SettingDef(
            "parser.playground_max_chars", G_PARSER, "Playground maximum text length", INT, 10_000, unit="characters",
            min=1, help="Longest text the Parser Playground accepts.",
        ),
        SettingDef(
            "parser.skip_words", G_PARSER, "Skip words", WORDS, list(DEFAULT_SKIP_WORDS), optional=True,
            help="Ignored when the symbol is taken from the first line of a message.",
        ),
        SettingDef(
            "parser.entry_keywords", G_PARSER, "Entry keywords", WORDS, list(DEFAULT_ENTRY_WORDS),
            help="Words that introduce the entry price. At least one is required.",
        ),
        SettingDef(
            "parser.target_keywords", G_PARSER, "Target keywords", WORDS, list(DEFAULT_TARGET_WORDS),
            help="Words that introduce take-profit targets. At least one is required.",
        ),
        SettingDef(
            "parser.stoploss_keywords", G_PARSER, "Stop-loss keywords", WORDS, list(DEFAULT_STOPLOSS_WORDS),
            help="Words that introduce the stop-loss. At least one is required.",
        ),
        # ---- Display ----------------------------------------------------------------------
        SettingDef("display.dashboard_recent_signals", G_DISPLAY, "Dashboard recent signals", INT, 30, min=1, max=1000),
        SettingDef("display.messages_page_size", G_DISPLAY, "Messages page size", INT, 50, min=1, max=1000),
        SettingDef("display.history_limit", G_DISPLAY, "History limit", INT, 200, min=1, max=10000,
                   help="Most recent closed / cancelled trades shown."),
        SettingDef("display.amount_decimals", G_DISPLAY, "Price / quantity decimals", INT, 8, min=0, max=15),
        SettingDef("display.percent_decimals", G_DISPLAY, "Percentage & size decimals", INT, 2, min=0, max=15,
                   help="ROI, margin ratio, margin and position size."),
        SettingDef("display.pnl_decimals", G_DISPLAY, "PnL & margin decimals", INT, 4, min=0, max=15),
    ]
)

BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFINITIONS}
assert len(BY_KEY) == len(DEFINITIONS), "duplicate setting key"


def slug_of(group: str) -> str:
    return group.lower().replace(" & ", "-").replace(" ", "-")


def by_group() -> list[tuple[str, list[SettingDef]]]:
    return [(g, [d for d in DEFINITIONS if d.group == g]) for g in GROUPS]
