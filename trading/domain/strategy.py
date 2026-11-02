"""
Per-source trade strategy: which entries to take, how the position is closed
at the targets, and when the stop-loss moves to entry.

Pure domain module (no Django, no network), shared by the planner, the monitor
and the settings form so the rules live in exactly one place.

Entry strategy (a signal may list several entries; only the lowest and the
highest are ever considered):
  NEAREST   long -> highest entry, short -> lowest entry (the original behaviour)
  FARTHEST  long -> lowest entry,  short -> highest entry
  BOTH      both of them, margin split equally over the two limit orders

Take-profit strategy: whole-number percent of the position closed at each of
TP1..TP8; blank = 0 %. Nothing configured = the default split (60 / 40 on TP1 / TP2 unless the
Settings page says otherwise); the default in force is stored with every trade.
  - the last target the signal has closes whatever is still open
  - percentages that total less than 100 % are completed by the next target
    after the last configured one (or by the last configured one if there is none)

Stop-loss strategy (always moves to the average entry, for what is left):
  TP1_TOUCH  as soon as price touches the signal's TP1 level (even if TP1 closes nothing)
  AFTER_FILLING_TP  once the order of target number `sl_after_tp` has been filled
                    (stored as "AFTER_TP" before v2.12; that text is still understood everywhere)

Reversal mode (a signal in the opposite direction of this source's own active trade on the same coin):
  ONE_WAY    close that trade fully, then take the new signal (close and reverse)
  IGNORE     leave the active trade alone and skip the new signal
  HEDGE      upcoming: open an independent opposite position and keep both. Not selectable yet.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from trading.domain.quantize import floor_to_step
from trading.domain.types import BUY, MAX_TARGETS

NEAREST = "NEAREST"
FARTHEST = "FARTHEST"
BOTH = "BOTH"
ENTRY_MODES = (NEAREST, FARTHEST, BOTH)

SL_TP1_TOUCH = "TP1_TOUCH"
SL_AFTER_FILLING_TP = "AFTER_FILLING_TP"
SL_MODES = (SL_TP1_TOUCH, SL_AFTER_FILLING_TP)
# Names stored by older versions (trades and signals saved before the rename keep working).
LEGACY_SL_MODES = {"AFTER_TP": SL_AFTER_FILLING_TP}


def normalize_sl_mode(value):
    """The current name of a stop-loss mode (old names are translated, anything else is returned as is)."""
    return LEGACY_SL_MODES.get(value, value) if isinstance(value, str) else value


REVERSAL_ONE_WAY = "ONE_WAY"
REVERSAL_IGNORE = "IGNORE"
REVERSAL_HEDGE = "HEDGE"  # upcoming: shown in the UI but not selectable and not implemented
REVERSAL_MODES = (REVERSAL_ONE_WAY, REVERSAL_IGNORE)  # the selectable ones

MAX_ENTRIES = 2
DEFAULT_TP_PERCENTS = (60, 40)

TpPercents = tuple[Optional[int], ...]
_BLANK: TpPercents = (None,) * MAX_TARGETS


def _normalize(percents: Sequence[Optional[int]]) -> TpPercents:
    """Always exactly MAX_TARGETS entries; 0 and blank both mean "nothing closes here"."""
    values = [(int(p) if p else None) for p in list(percents)[:MAX_TARGETS]]
    return tuple(values + [None] * (MAX_TARGETS - len(values)))


@dataclass(frozen=True)
class SourceStrategy:
    entry_mode: str = NEAREST
    tp_percents: TpPercents = field(default=_BLANK)
    sl_mode: str = SL_TP1_TOUCH
    sl_after_tp: int = 1  # only used by SL_AFTER_FILLING_TP
    reversal: str = REVERSAL_ONE_WAY  # what an opposite signal on a coin this source already trades does
    # The split used when no tp_percents are configured. Snapshotted with the trade, so changing the
    # global default never changes the exits of a trade that is already open.
    default_tp: tuple = DEFAULT_TP_PERCENTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "sl_mode", normalize_sl_mode(self.sl_mode))
        object.__setattr__(self, "tp_percents", _normalize(self.tp_percents))
        object.__setattr__(self, "default_tp", tuple(int(p) for p in self.default_tp))

    @property
    def uses_default_tp(self) -> bool:
        return not any(self.tp_percents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_mode": self.entry_mode,
            "tp_percents": list(self.tp_percents),
            "sl_mode": self.sl_mode,
            "sl_after_tp": self.sl_after_tp,
            "default_tp": list(self.default_tp),
            "reversal": self.reversal,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "SourceStrategy":
        """Tolerant on purpose: trades saved before this feature have no snapshot."""
        if not data:
            return cls()
        return cls(
            entry_mode=data.get("entry_mode") or NEAREST,
            tp_percents=_normalize(data.get("tp_percents") or ()),
            sl_mode=normalize_sl_mode(data.get("sl_mode")) or SL_TP1_TOUCH,
            sl_after_tp=int(data.get("sl_after_tp") or 1),
            default_tp=tuple(data.get("default_tp") or DEFAULT_TP_PERCENTS),
            reversal=data.get("reversal") or REVERSAL_ONE_WAY,
        )


DEFAULT_STRATEGY = SourceStrategy()


def validate(strategy: SourceStrategy) -> list[str]:
    """Human-readable problems with a strategy; empty when it is valid."""
    errors: list[str] = []
    if strategy.entry_mode not in ENTRY_MODES:
        errors.append("Choose a valid entry strategy.")
    if any(p is not None and not 0 <= p <= 100 for p in strategy.tp_percents):
        errors.append("Each take-profit percentage must be between 0 and 100.")
    total = sum(p or 0 for p in strategy.tp_percents)
    if total > 100:
        errors.append(f"Take-profit percentages total {total}%, which is more than 100%.")
    if strategy.sl_mode not in SL_MODES:
        errors.append("Choose a valid stop-loss strategy.")
    elif strategy.sl_mode == SL_AFTER_FILLING_TP:
        if not 1 <= strategy.sl_after_tp <= MAX_TARGETS:
            errors.append(f"The stop-loss target number must be between 1 and {MAX_TARGETS}.")
        elif not strategy.uses_default_tp and not strategy.tp_percents[strategy.sl_after_tp - 1]:
            errors.append(f"TP{strategy.sl_after_tp} closes nothing, so the stop-loss can never move after it.")
    if strategy.reversal == REVERSAL_HEDGE:
        errors.append("Hedge Mode is not available yet. Choose One-Way Mode or Ignore.")
    elif strategy.reversal not in REVERSAL_MODES:
        errors.append("Choose a valid reversal mode.")
    return errors


def select_entries(side: str, entries: Sequence[Decimal], mode: str) -> list[Decimal]:
    """The entry prices to trade, nearest-to-market first. At most MAX_ENTRIES."""
    unique = sorted(set(entries))
    if not unique:
        return []
    lowest, highest = unique[0], unique[-1]
    nearest, farthest = (highest, lowest) if side == BUY else (lowest, highest)
    if mode == FARTHEST:
        return [farthest]
    if mode == BOTH and nearest != farthest:
        return [nearest, farthest]
    return [nearest]


def effective_tp_percents(
    target_count: int, tp_percents: Sequence[Optional[int]], default_tp: Sequence[int] = DEFAULT_TP_PERCENTS
) -> list[int]:
    """Percent of the position to close at each of the signal's `target_count` targets (sums to 100)."""
    count = max(0, min(target_count, MAX_TARGETS))
    if count == 0:
        return []
    configured = _normalize(tp_percents)
    if not any(configured):  # nothing configured: the default split (60 / 40 unless changed in Settings)
        if count == 1:
            return [100]
        split = [int(p) for p in default_tp][:count]
        if sum(split) < 100:  # a default that does not add up to 100 % (or has fewer slots than targets): the rest goes on the last slot
            split[-1] += 100 - sum(split)
        return [*split, *([0] * (count - len(split)))]

    percents = [p or 0 for p in configured[:count]]  # settings beyond the signal's targets are dropped
    if not any(percents):
        return [*([0] * (count - 1)), 100]  # only TPs the signal lacks were configured
    remainder = 100 - sum(percents)
    if remainder > 0:
        last = max(i for i, p in enumerate(percents) if p)
        percents[last + 1 if last + 1 < count else last] += remainder
    return percents


def allocate_tp_quantities(
    quantity: Decimal, prices: Sequence[Decimal], percents: Sequence[int], step_size: Decimal, min_qty: Decimal
) -> list[tuple[int, Decimal, Decimal]]:
    """[(target number, price, quantity)] for every target that closes something.

    Quantities are floored to stepSize and the last slice takes the remainder, so the
    slices always add up to `quantity`. A slice below minQty is merged into the next one
    (or, for the last slice, into the previous one) instead of being rejected."""
    open_slots = [i for i, p in enumerate(percents) if p > 0]
    if not open_slots:
        return []
    slices: dict[int, Decimal] = {i: floor_to_step(quantity * percents[i] / 100, step_size) for i in open_slots[:-1]}
    slices[open_slots[-1]] = quantity - sum(slices.values())

    kept: list[list] = []
    carry = Decimal("0")
    for position, i in enumerate(open_slots):
        size = slices[i] + carry
        carry = Decimal("0")
        if size < min_qty and position < len(open_slots) - 1:
            carry = size
            continue
        if size < min_qty and kept:
            kept[-1][1] += size
            continue
        kept.append([i, size])
    return [(i + 1, prices[i], size) for i, size in kept]


def sl_trigger_target(strategy: SourceStrategy, present_targets: Sequence[int]) -> Optional[int]:
    """For AFTER_FILLING_TP: the target number whose fill moves the stop-loss. When that target
    does not exist (the signal has fewer, or its slice was merged away) the highest
    existing one below it is used, so the stop-loss can always move. None for other modes."""
    if strategy.sl_mode != SL_AFTER_FILLING_TP or not present_targets:
        return None
    eligible = [n for n in present_targets if n >= strategy.sl_after_tp]
    return min(eligible) if eligible else max(present_targets)
