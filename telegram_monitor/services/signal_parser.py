"""
Pure crypto signal text parser.

No Django ORM, no Telethon, no HTTP, no WebSocket, no logging side effects
beyond what the caller asks for via the returned result. This module must
remain independently importable and testable without Django installed.

Extraction is line- and label-based:
  - the text is Unicode-normalised (diacritics stripped) and emoji are removed
  - the symbol comes from an explicit pair (BTC/USDT, BTCUSDT, BTC USDT) or,
    when no pair is written, from the ticker nearest the direction word
  - every line is split into the labels it contains, so several labels can
    share one line ("Entry: 100 | TP: 110 | SL: 90")
  - entry / target / stop-loss blocks are collected even when the values are
    on the following lines
  - prices support thousands separators, leading-dot decimals and scientific
    notation; percentages are resolved against the entry price (SL: 2% below,
    TP: 20%)
  - the resulting levels are validated (SL and every target must be on the
    correct side of the entry) before a signal is returned.

The public API (`parse_signal`, `ParserRules`, `ParseResult`, error codes) is
unchanged. Messages with more than one explicit trading pair are rejected
with MULTIPLE_SIGNALS, because one Telegram message produces at most one
signal by design.
"""
import math
import re
import unicodedata
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
INVALID_LEVELS = "INVALID_LEVELS"
MULTIPLE_SIGNALS = "MULTIPLE_SIGNALS"
PARSER_ERROR = "PARSER_ERROR"

# ---------------------------------------------------------------------------
# Tunable parser rules. These defaults are what the Settings page starts with; the
# live pipeline builds a ParserRules from the stored settings (see
# runtime_settings.service.parser_rules) and passes it to parse_signal().
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

_DIRECTION_RE = re.compile(r'\b(LONG|BUY|SHORT|SELL|LONGING|SHORTING)\b')
_TP_LABEL_RE = re.compile(r'\bTP\s*\d+\b')

# A price. Supported: 100,000 / 1,234.56 / 0.5 / .5 / 100 / 9.1e-6.
# The look-behind keeps numbers embedded in a ticker (TP1, 1INCH, 1000PEPE) out.
_NUMBER_RE = re.compile(
    r'(?<![A-Za-z0-9.])'
    r'(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d*\.\d+|\d+)'
    r'(?:[eE]([+-]?\d+))?'
)
_PERCENT_RE = re.compile(r'(?<![\d.,])([+-]?)\s*(\d+(?:\.\d+)?)\s*%')
# Parentheses holding commentary ("(2% risk, 10x)", "(24/7 market)") are not
# prices. Bare numeric parentheses such as "(100)" and percentage-only ones
# such as "(20%)" are kept.
_PAREN_COMMENTARY_RE = re.compile(r'\([^)]*[A-Za-z/][^)]*\)')
_PERCENT_SUFFIX_RE = re.compile(r'\s*%')
_UNIT_SUFFIX_RE = re.compile(r'[A-Za-z]')  # 10x, 4h, 100k
_SPACED_UNIT_RE = re.compile(r'\s+[xX]\b')
# "1) 3450", "1. 3450", "1 - 3450" list indexes (not decimals like 0.025).
_LIST_INDEX_RE = re.compile(r'(?:^|(?<=\s))\d+\s*[).\-]\s+')
# "Targets (4):" / "Targets: (4)" — a count of targets, not a price.
_COUNT_PREFIX_RE = re.compile(r'^\(\s*\d{1,2}\s*\)\s*:\s*')
_COUNT_ONLY_RE = re.compile(r'^\(\s*\d{1,2}\s*\)$')
# "Target 1 (TP1):" / "Target 2: 0.09" / "Entry 1 0.09222" — the number right
# after a label is an ordinal or count, never a price. A decimal ("Entry 12.5")
# and a bare number at end of line ("Target 24") are left alone.
_ORDINAL_AFTER_LABEL_RE = re.compile(r'\s*#?\d{1,2}(?=\s*[\(\):\-=]|\s+\d*\.\d)')


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
    """Every positive price in `raw`, ignoring percentages, leverage/timeframe suffixes,
    ticker digits and parenthetical commentary."""
    raw = _PAREN_COMMENTARY_RE.sub(" ", raw)
    nums: list[float] = []
    for match in _NUMBER_RE.finditer(raw):
        tail = raw[match.end():]
        if _PERCENT_SUFFIX_RE.match(tail) or _UNIT_SUFFIX_RE.match(tail) or _SPACED_UNIT_RE.match(tail):
            continue
        try:
            value = float(match.group(0).replace(",", ""))
        except ValueError:
            continue
        if math.isfinite(value) and value > 0:
            nums.append(value)
    return nums


def _percent_specs(raw: str) -> list[tuple[float, Optional[str]]]:
    """(percentage, hint) pairs. `hint` is "above"/"below" when the text or the sign says so."""
    raw = _PAREN_COMMENTARY_RE.sub(" ", raw)
    upper = raw.upper()
    word_hint = "above" if re.search(r'\bABOVE\b', upper) else ("below" if re.search(r'\bBELOW\b', upper) else None)
    specs: list[tuple[float, Optional[str]]] = []
    for match in _PERCENT_RE.finditer(raw):
        sign = match.group(1)
        hint = "above" if sign == "+" else ("below" if sign == "-" else word_hint)
        value = float(match.group(2))
        if math.isfinite(value) and value > 0:
            specs.append((value, hint))
    return specs


def _strip_list_indexes(text: str) -> str:
    return _LIST_INDEX_RE.sub(" ", text)


def _clean_value(value: str) -> str:
    """Drop the colon after a label and an optional leading "(N):" target count."""
    value = value.strip()
    if value.startswith(":"):
        value = value[1:].strip()
    if value.endswith(":"):
        value = value[:-1].strip()
    return _COUNT_PREFIX_RE.sub("", value).strip()


# ---------------------------------------------------------------------------
# Label detection
# ---------------------------------------------------------------------------

def _classify_label(text: str, rules: ParserRules) -> Optional[str]:
    """target > stop-loss > entry, so "TARGET PRICE" is a target, not an entry."""
    if re.search(rules.target_pattern, text) or _TP_LABEL_RE.search(text):
        return "target"
    if re.search(rules.stoploss_pattern, text):
        return "stoploss"
    if re.search(rules.entry_pattern, text):
        return "entry"
    return None


def _label_spans(line: str, rules: ParserRules) -> list[tuple[int, int, Optional[str]]]:
    """(start, end, kind) for every label in `line`, left to right.

    Overlapping matches are collapsed (the longest wins) and adjacent words such
    as TARGET + PRICE are merged so the whole phrase is classified together.
    """
    found: list[list] = []
    for kind, pattern in (
        ("target", rules.target_pattern),
        ("stoploss", rules.stoploss_pattern),
        ("entry", rules.entry_pattern),
    ):
        for match in re.finditer(pattern, line):
            found.append([match.start(), match.end(), kind])
    for match in _TP_LABEL_RE.finditer(line):
        found.append([match.start(), match.end(), "target"])

    found.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    kept: list[list] = []
    for span in found:
        if any(span[0] < other[1] and span[1] > other[0] for other in kept):
            continue
        kept.append(span)

    merged: list[list] = []
    for span in kept:
        if merged and re.fullmatch(r'[\s\-_]*', line[merged[-1][1]:span[0]]):
            start = merged[-1][0]
            end = span[1]
            merged[-1] = [start, end, _classify_label(line[start:end], rules)]
        else:
            merged.append(span)
    return [(start, end, kind) for start, end, kind in merged]


def _line_segments(line: str, rules: ParserRules) -> list[tuple[str, str]]:
    """The labelled segments of one line as (kind, value), in order.

    A keyword only counts as a label when it is followed by a colon, by a value,
    or is the whole line (a block header such as "Targets"). This keeps the word
    "entry" in ordinary commentary from being treated as a label.
    """
    spans = _label_spans(line, rules)
    if not spans:
        return []

    kept: list[tuple[int, int, str]] = []
    for index, (start, end, kind) in enumerate(spans):
        next_start = spans[index + 1][0] if index + 1 < len(spans) else len(line)
        ordinal = _ORDINAL_AFTER_LABEL_RE.match(line, end) if kind is not None else None
        if ordinal and ordinal.end() <= next_start:
            end = ordinal.end()
        between = line[end:next_start]
        has_colon = between.lstrip().startswith(":")
        has_value = bool(_extract_nums(between)) or bool(_percent_specs(between))
        label_only = not line[:start].strip() and not line[end:].strip()
        if kind is not None and (has_colon or has_value or label_only or ordinal is not None):
            kept.append((start, end, kind))

    segments: list[tuple[str, str]] = []
    for index, (start, end, kind) in enumerate(kept):
        next_start = kept[index + 1][0] if index + 1 < len(kept) else len(line)
        segments.append((kind, _clean_value(line[end:next_start])))
    return segments


# ---------------------------------------------------------------------------
# Symbol detection
# ---------------------------------------------------------------------------

def _pair_matches(text: str) -> list[tuple[int, str, str]]:
    """(position, base, quote) of every explicit pair in `text`, left to right."""
    patterns = (
        re.compile(r'#?([A-Z0-9]{2,15})[ \t]*/[ \t]*(USDT|USD)\b'),
        re.compile(r'#?([A-Z0-9]{2,15})(USDT|USD)\b'),
        re.compile(r'#?([A-Z]{2,15})[ \t]+(USDT|USD)\b'),
    )
    matches: list[tuple[int, str, str]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            base = match.group(1)
            if base.isdigit() or base in {"USDT", "USD"}:
                continue
            matches.append((match.start(), base, match.group(2)))
    matches.sort(key=lambda match: match[0])
    return matches


def _fallback_symbol(lines: Sequence[str], rules: ParserRules) -> Optional[str]:
    """The ticker nearest the direction word (or the first useful token of the
    first line), used only when the message has no explicit trading pair."""
    if not lines:
        return None

    direction_index = next((index for index, line in enumerate(lines) if _DIRECTION_RE.search(line)), None)
    candidates = [direction_index] if direction_index is not None else []
    if 0 not in candidates:
        candidates.append(0)

    for index in candidates:
        line = lines[index]
        tokens = [
            token for token in re.finditer(r'[A-Z0-9]{2,15}', line)
            if token.group() not in rules.skip_tokens
            and not token.group().isdigit()
            and token.group() not in {"USDT", "USD"}
            and not (token.group().endswith(("USDT", "USD")) and len(token.group()) > 4)
        ]
        if not tokens:
            continue
        direction = _DIRECTION_RE.search(line)
        if direction:
            before = [token for token in tokens if token.end() <= direction.start()]
            if before:
                return max(before, key=lambda token: token.end()).group()
            after = [token for token in tokens if token.start() >= direction.end()]
            if after:
                return min(after, key=lambda token: token.start()).group()
        return tokens[0].group()
    return None


# ---------------------------------------------------------------------------
# Level helpers
# ---------------------------------------------------------------------------

def _record_entries(
    nums: Sequence[float], entry: Optional[float], entry_candidates: list[float], direction: str
) -> float:
    entry_candidates.extend(nums)
    all_entries = ([entry] if entry is not None else []) + list(nums)
    return max(all_entries) if direction == "BUY" else min(all_entries)


def _percent_price(entry: float, percent: float, direction: str, hint: Optional[str], *, is_stoploss: bool) -> float:
    factor = percent / 100.0
    if is_stoploss:
        if hint == "above":
            return entry * (1 + factor)
        if hint == "below":
            return entry * (1 - factor)
        return entry * (1 - factor) if direction == "BUY" else entry * (1 + factor)
    if hint == "above":
        return entry * (1 + factor)
    if hint == "below":
        return entry * (1 - factor)
    return entry * (1 + factor) if direction == "BUY" else entry * (1 - factor)


def _fmt(value: float) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if value else "0"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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
    # Strip accents instead of deleting them: PREÇO -> PRECO, AÇÃO -> ACAO.
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r'[\u0300-\u036f]', '', text)
    text = re.sub(r'[^\x00-\x7F]+', ' ', text)
    text_upper = text.upper()
    lines = [line.strip() for line in text_upper.splitlines() if line.strip()]

    pairs = _pair_matches(text_upper)
    bases = {base for _, base, _ in pairs}
    if len(bases) > 1:
        return ParseResult.fail(
            MULTIPLE_SIGNALS,
            "The message contains more than one trading pair; one Telegram message must describe exactly one signal.",
        )
    if pairs:
        symbol = pairs[0][1] + "USDT"
    else:
        symbol_token = _fallback_symbol(lines, rules)
        if not symbol_token:
            return ParseResult.fail(NO_SYMBOL, "No valid trading symbol found in the message.")
        symbol = symbol_token + "USDT"

    direction_match = _DIRECTION_RE.search(text_upper)
    if not direction_match:
        return ParseResult.fail(NO_DIRECTION, "No valid trade direction (LONG/SHORT/BUY/SELL) found.")

    direction = "BUY" if direction_match.group(1) in ["LONG", "BUY", "LONGING"] else "SELL"

    entry: Optional[float] = None
    entry_candidates: list[float] = []
    target_specs: list[tuple[str, float, Optional[str]]] = []  # ("price"|"pct", value, hint)
    stoploss: Optional[float] = None
    stoploss_spec: Optional[tuple[float, Optional[str]]] = None
    in_entry_block = False
    in_targets_block = False

    for line in lines:
        segments = _line_segments(line, rules)
        if segments:
            for kind, value in segments:
                cleaned = _strip_list_indexes(value)
                if kind == "target" and _COUNT_ONLY_RE.match(cleaned.strip()):
                    cleaned = ""
                nums = _extract_nums(cleaned)
                if kind == "entry":
                    if nums:
                        entry = _record_entries(nums, entry, entry_candidates, direction)
                        in_entry_block = False
                    else:
                        in_entry_block = True
                    in_targets_block = False
                elif kind == "target":
                    if nums:
                        target_specs.extend(("price", number, None) for number in nums)
                    else:
                        target_specs.extend(("pct", percent, hint) for percent, hint in _percent_specs(cleaned))
                    in_entry_block = False
                    in_targets_block = True
                else:  # stoploss
                    if nums:
                        stoploss = nums[0]
                        stoploss_spec = None
                    else:
                        percents = _percent_specs(cleaned)
                        if percents:
                            stoploss_spec = percents[0]
                            stoploss = None
                    in_entry_block = False
                    in_targets_block = False
        else:
            cleaned = _strip_list_indexes(line)
            if in_targets_block and re.match(r'^[$\d]', line):
                nums = _extract_nums(cleaned)
                if nums:
                    target_specs.extend(("price", number, None) for number in nums)
                else:
                    percents = _percent_specs(cleaned)
                    if percents:
                        target_specs.extend(("pct", percent, hint) for percent, hint in percents)
                    else:
                        in_targets_block = False
            elif in_entry_block:
                nums = _extract_nums(cleaned)
                if nums:
                    entry = _record_entries(nums, entry, entry_candidates, direction)
                else:
                    in_entry_block = False

    if entry is None:
        for line in lines:
            if _DIRECTION_RE.search(line) and re.search(r'\b(BELOW|ABOVE|AT|AROUND|NEAR)\b', line):
                nums = _extract_nums(line)
                if nums:
                    entry = nums[0]
                    break

    if entry is None:
        for line in lines:
            if _DIRECTION_RE.search(line):
                raw = _DIRECTION_RE.sub('', line)
                raw = re.sub(r'#?[A-Z]{2,15}\s*/?\s*(USDT|USD)?', '', raw)
                nums = _extract_nums(raw)
                if nums:
                    entry = nums[0]
                    break

    if entry is None:
        return ParseResult.fail(NO_ENTRY, "No entry price could be extracted.")

    if stoploss is None and stoploss_spec is not None:
        percent, hint = stoploss_spec
        stoploss = _percent_price(entry, percent, direction, hint, is_stoploss=True)

    targets: list[float] = []
    for kind, value, hint in target_specs:
        if kind == "pct":
            targets.append(_percent_price(entry, value, direction, hint, is_stoploss=False))
        else:
            targets.append(value)

    if not targets:
        return ParseResult.fail(NO_TARGET, "No take-profit targets could be extracted.")
    if stoploss is None and require_stoploss:
        return ParseResult.fail(NO_STOPLOSS, "No stop-loss value could be extracted.")

    levels = [entry, *targets] + ([stoploss] if stoploss is not None else [])
    if any(value <= 0 or not math.isfinite(value) for value in levels):
        return ParseResult.fail(INVALID_LEVELS, "Entry, stop-loss and targets must be positive numbers.")

    # The first two targets are what the trade planner uses; they must move away
    # from the entry in the signal's direction. Later targets are truncated by
    # the planner when they do not, so they are not rejected here.
    if direction == "BUY":
        invalid = (stoploss is not None and stoploss >= entry) or any(target <= entry for target in targets)
    else:
        invalid = (stoploss is not None and stoploss <= entry) or any(target >= entry for target in targets)
    if not invalid and len(targets) > 1:
        invalid = targets[1] <= targets[0] if direction == "BUY" else targets[1] >= targets[0]
    if invalid:
        expected = "SL < entry < TP1 < TP2" if direction == "BUY" else "SL > entry > TP1 > TP2"
        stoploss_text = _fmt(stoploss) if stoploss is not None else "none"
        return ParseResult.fail(
            INVALID_LEVELS,
            f"{direction} requires {expected}; got SL={stoploss_text}, entry={_fmt(entry)}, "
            f"targets=[{', '.join(_fmt(target) for target in targets)}].",
        )

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
