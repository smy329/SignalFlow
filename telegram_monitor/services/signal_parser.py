"""
Pure crypto signal text parser.

No Django ORM, no Telethon, no HTTP, no WebSocket, no logging side effects
beyond what the caller asks for via the returned result. This module must
remain independently importable and testable without Django installed.

Algorithm is a direct refactor of the original `parse_signal()` — extraction
semantics (symbol/direction/entry/targets/stoploss detection) are preserved
exactly. What changed vs. the original:
  - no `print()` — returns a structured result instead
  - no config-module import — leverage/margin/margin_type are parameters
  - no DB / realtime side effects — this function only parses text
  - explicit error codes instead of `return None`
"""
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence, TypedDict


class ParsedSignalDict(TypedDict):
    symbol: str
    direction: str
    leverage: float
    margin: Optional[float]
    margin_type: str
    entry: float
    entries: list[float]  # every entry price listed (lowest first); `entry` is the max (BUY) / min (SELL) of them
    targets: list[float]
    stoploss: Optional[float]  # None only when the caller passed require_stoploss=False (Manual Trade)
    channel: str


@dataclass(frozen=True)
class ParseResult:
    """Structured parser outcome — always exactly one of signal/error is set."""

    success: bool
    signal: Optional[ParsedSignalDict] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def ok(cls, signal: ParsedSignalDict) -> "ParseResult":
        return cls(success=True, signal=signal)

    @classmethod
    def fail(cls, error_code: str, error_message: str) -> "ParseResult":
        return cls(success=False, error_code=error_code, error_message=error_message)


# Error codes — kept as plain strings so they map directly onto
# TelegramMessage.error_code (a CharField, not a Django-specific enum) and
# stay usable from pure-Python test code with no Django import.
MESSAGE_TOO_SHORT = "MESSAGE_TOO_SHORT"
NO_SYMBOL = "NO_SYMBOL"
NO_DIRECTION = "NO_DIRECTION"
NO_ENTRY = "NO_ENTRY"
NO_TARGET = "NO_TARGET"
NO_STOPLOSS = "NO_STOPLOSS"
PARSER_ERROR = "PARSER_ERROR"

# ---------------------------------------------------------------------------
# Tunable parser rules. These defaults are what the Settings page starts with; the
# live pipeline builds a ParserRules from the stored settings (see
# telegram_monitor.services.parser_rules) and passes it to parse_signal().
# ---------------------------------------------------------------------------
DEFAULT_MIN_MESSAGE_LENGTH = 80

DEFAULT_SKIP_WORDS = (
    "LONG", "SHORT", "BUY", "SELL",
    "LONGING", "SHORTING", "HERE",
    "CONSIDER", "SOME", "FOR", "AND",
    "THE", "IN", "AT", "TO",
)

# Plain words / phrases. A space inside a phrase matches any amount of whitespace
# ("TAKE PROFIT" also matches "TAKEPROFIT"). Order is kept: the first listed word wins.
DEFAULT_ENTRY_WORDS = (
    "ENTRY", "ENTRADA", "PRICE", "ENTRY PRICE", "PRECO",
    "PONTO DE ENTRADA", "PUNTO DE ENTRADA", "ENTRIES",
)
DEFAULT_TARGET_WORDS = (
    "TARGET", "OBJETIVO", "GOAL", "ALVO", "TARGETS", "OBJETIVOS",
    "GOALS", "ALVOS", "TP", "TARG", "TAKE PROFIT", "PROFIT",
    "TAKE PROFITS",
)
DEFAULT_STOPLOSS_WORDS = ("SL", "STOP LOSS", "STOPLOSS", "STOP-LOSS", "STOP", "PERDA", "LOSS")


def build_keyword_pattern(words: Sequence[str]) -> str:
    """Regex matching any of the plain `words` as whole words (case is handled by the caller,
    which upper-cases the text). Blank entries are ignored; at least one word is required."""
    parts = []
    for word in words:
        tokens = str(word).upper().split()
        if tokens:
            parts.append(r"\s*".join(re.escape(token) for token in tokens))
    if not parts:
        raise ValueError("At least one keyword is required.")
    return r"\b(" + "|".join(parts) + r")\b"


@dataclass(frozen=True)
class ParserRules:
    min_length: int = DEFAULT_MIN_MESSAGE_LENGTH
    skip_tokens: frozenset = field(default_factory=lambda: frozenset(DEFAULT_SKIP_WORDS))
    entry_pattern: str = field(default_factory=lambda: build_keyword_pattern(DEFAULT_ENTRY_WORDS))
    target_pattern: str = field(default_factory=lambda: build_keyword_pattern(DEFAULT_TARGET_WORDS))
    stoploss_pattern: str = field(default_factory=lambda: build_keyword_pattern(DEFAULT_STOPLOSS_WORDS))

    @classmethod
    def from_words(
        cls,
        *,
        min_length: int = DEFAULT_MIN_MESSAGE_LENGTH,
        skip_words: Sequence[str] = DEFAULT_SKIP_WORDS,
        entry_words: Sequence[str] = DEFAULT_ENTRY_WORDS,
        target_words: Sequence[str] = DEFAULT_TARGET_WORDS,
        stoploss_words: Sequence[str] = DEFAULT_STOPLOSS_WORDS,
    ) -> "ParserRules":
        return cls(
            min_length=int(min_length),
            skip_tokens=frozenset(str(w).strip().upper() for w in skip_words if str(w).strip()),
            entry_pattern=build_keyword_pattern(entry_words),
            target_pattern=build_keyword_pattern(target_words),
            stoploss_pattern=build_keyword_pattern(stoploss_words),
        )


DEFAULT_RULES = ParserRules()


def _extract_nums(raw: str) -> list[float]:
    return [float(x) for x in re.findall(r'\$?\s*(\d+\.\d+|\d+)', raw)]


def parse_signal(
    text: str,
    channel: str = "",
    *,
    leverage: float,
    margin: Optional[float],
    margin_type: str,
    rules: ParserRules = DEFAULT_RULES,
    require_stoploss: bool = True,
) -> ParseResult:
    """Parse raw Telegram message text into a crypto trading signal.

    Never raises for malformed input — parsing failures are returned as
    `ParseResult.fail(...)`. A genuinely unexpected exception is caught and
    returned as `PARSER_ERROR` so a single bad message can never propagate
    into (and crash) the Telegram listener.
    """
    try:
        return _parse_signal_inner(text, channel, leverage=leverage, margin=margin, margin_type=margin_type, rules=rules,
                                  require_stoploss=require_stoploss)
    except Exception as exc:  # noqa: BLE001 - last-resort safety net, see docstring
        return ParseResult.fail(PARSER_ERROR, f"Unexpected parser error: {exc}")


def _parse_signal_inner(
    text: str,
    channel: str,
    *,
    leverage: float,
    margin: Optional[float],
    margin_type: str,
    rules: ParserRules,
    require_stoploss: bool = True,
) -> ParseResult:
    if not text or len(text) < rules.min_length:
        return ParseResult.fail(
            MESSAGE_TOO_SHORT, f"Message is shorter than the minimum signal length ({rules.min_length} chars)."
        )

    text = text.replace("\u00a0", " ")
    text = re.sub(r'\d+\ufe0f?\u20e3', '', text)
    text = re.sub(r'[^\x00-\x7F]+', ' ', text)
    text_upper = text.upper()
    lines = [l.strip() for l in text_upper.splitlines() if l.strip()]

    symbol_match = re.search(r'#?([A-Z0-9]{1,15})\s*/?\s*(USDT|USD)\b', text_upper)

    if not symbol_match:
        first_line = lines[0] if lines else ""
        tokens = re.findall(r'[A-Z0-9]{2,15}', first_line)
        symbol_token = next((t for t in tokens if t not in rules.skip_tokens), None)
        if not symbol_token:
            return ParseResult.fail(NO_SYMBOL, "No valid trading symbol found in the message.")
        symbol = symbol_token + "USDT"
    else:
        symbol = symbol_match.group(1) + "USDT"

    direction_match = re.search(r'\b(LONG|BUY|SHORT|SELL|LONGING|SHORTING)\b', text_upper)
    if not direction_match:
        return ParseResult.fail(NO_DIRECTION, "No valid trade direction (LONG/SHORT/BUY/SELL) found.")

    direction = "BUY" if direction_match.group(1) in ["LONG", "BUY", "LONGING"] else "SELL"

    entry: Optional[float] = None
    entry_candidates: list[float] = []
    targets: list[float] = []
    stoploss: Optional[float] = None
    in_targets_block = False

    for line in lines:
        if re.search(rules.entry_pattern, line):
            if ':' not in line:
                raw = re.sub(rules.entry_pattern, '', line)
                nums = _extract_nums(raw)
                if nums:
                    entry_candidates.extend(nums)
                    all_entries = ([entry] if entry is not None else []) + nums
                    entry = max(all_entries) if direction == "BUY" else min(all_entries)
            else:
                raw = line.split(":", 1)[-1]
                nums = _extract_nums(raw)
                if nums:
                    entry_candidates.extend(nums)
                    all_entries = ([entry] if entry is not None else []) + nums
                    entry = max(all_entries) if direction == "BUY" else min(all_entries)

        elif re.search(rules.target_pattern, line):
            raw = line.split(":", 1)[-1] if ':' in line else re.sub(rules.target_pattern, '', line)
            raw = re.sub(r'(?<=\d)(TP)', r' \1', raw, flags=re.IGNORECASE)
            raw = re.sub(r'\bTP\s*\d+\b', '', raw, flags=re.IGNORECASE)
            nums = _extract_nums(raw)
            if nums:
                targets.extend(nums)
            in_targets_block = True

        elif re.match(r'^TP\s*\d+', line):
            raw = re.sub(r'^TP\s*\d+\s*[:\u2192\-]?\s*', '', line, flags=re.IGNORECASE)
            nums = _extract_nums(raw)
            if nums:
                targets.extend(nums)
            in_targets_block = True

        elif in_targets_block and re.match(r'^\d+(?:\s*\)|\s*[.\-]\s+)\s*\d', line):
            # list index: "1) 3450", "1. 3450", "1 - 3450". A bare "0.025" is a decimal, not an index.
            raw = re.sub(r'^\d+(?:\s*\)|\s*[.\-]\s+)\s*', '', line)
            nums = _extract_nums(raw)
            if nums:
                targets.extend(nums)

        elif in_targets_block and re.match(r'^[$\d]', line):
            nums = _extract_nums(line)
            if nums:
                targets.extend(nums)
            else:
                in_targets_block = False

        elif re.search(rules.stoploss_pattern, line):
            in_targets_block = False
            raw = line.split(":", 1)[-1]
            nums = _extract_nums(raw)
            if nums:
                stoploss = nums[0]

    if entry is None:
        for line in lines:
            if re.search(r'\b(LONG|SHORT|BUY|SELL|LONGING|SHORTING)\b', line) and re.search(
                r'\b(BELOW|ABOVE|AT|AROUND|NEAR)\b', line
            ):
                nums = _extract_nums(line)
                if nums:
                    entry = nums[0]
                    break

    if entry is None:
        for line in lines:
            if re.search(r'\b(LONG|SHORT|BUY|SELL|LONGING|SHORTING)\b', line):
                raw = re.sub(r'\b(LONG|SHORT|BUY|SELL|LONGING|SHORTING)\b', '', line)
                raw = re.sub(r'#?[A-Z]{2,15}\s*/?\s*(USDT|USD)?', '', raw)
                nums = _extract_nums(raw)
                if nums:
                    entry = nums[0]
                    break

    if entry is None:
        return ParseResult.fail(NO_ENTRY, "No entry price could be extracted.")
    if not targets:
        return ParseResult.fail(NO_TARGET, "No take-profit targets could be extracted.")
    if stoploss is None and require_stoploss:
        return ParseResult.fail(NO_STOPLOSS, "No stop-loss value could be extracted.")

    return ParseResult.ok(
        {
            "symbol": symbol,
            "direction": direction,
            "leverage": leverage,
            "margin": margin,
            "margin_type": margin_type,
            "entry": entry,
            "entries": sorted(set(entry_candidates or [entry])),
            "targets": targets,
            "stoploss": stoploss,
            "channel": channel,
        }
    )
