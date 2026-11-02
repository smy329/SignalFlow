"""Dry-run of the signal parser for the Parser Playground page.

Uses the exact same `parse_signal` call and parser rules as the real pipeline
(signal_service), with the Real signal values, but is pure: no database writes, no broadcast, no trading hook.
It therefore cannot detect duplicates or trade-level rejections, only whether the
parser understands the text.
"""
from runtime_settings import service as runtime
from telegram_monitor.services import message_status
from telegram_monitor.services.signal_parser import parse_signal


def max_text_length() -> int:
    """Longest text the playground accepts (a setting)."""
    return runtime.get("parser.playground_max_chars")


def preview(text: str) -> dict:
    """What the live pipeline would decide about `text`: status, reason and parsed fields."""
    values = runtime.signal_values("real")
    result = parse_signal(
        text,
        channel="Parser Playground",
        leverage=values.leverage,
        margin=values.margin,
        margin_type=values.margin_type,
        rules=runtime.parser_rules(),
    )
    status, reason = message_status.describe("SUCCESS" if result.success else "FAILED", result.error_code or "")
    return {
        "is_signal": result.success,
        "status": status,
        "reason": reason,
        "signal": _jsonable(result.signal) if result.success else None,
    }


def _jsonable(signal: dict) -> dict:
    """Decimals become floats so the dict can be sent as JSON."""
    out = {}
    for key, value in signal.items():
        if isinstance(value, (list, tuple)):
            out[key] = [float(v) for v in value]
        elif hasattr(value, "as_tuple"):  # Decimal
            out[key] = float(value)
        else:
            out[key] = value
    return out
