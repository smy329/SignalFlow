"""Decimal helpers for exchange step/tick rules. Pure, no I/O."""
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal


def to_decimal(value) -> Decimal:
    """Convert floats/ints/strings/Decimals to Decimal without float artifacts."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of `step` that is <= value. Never rounds up, so a
    quantity can never exceed the margin the trade was sized for."""
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    """Nearest multiple of `tick` (half up)."""
    if tick <= 0:
        raise ValueError("tick must be positive")
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def to_plain_str(value: Decimal) -> str:
    """Exchange API parameter form: no exponent, no trailing zeros
    (extra decimals beyond the symbol's precision are rejected)."""
    return format(value.normalize(), "f")
