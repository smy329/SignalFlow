"""How a stored Telegram message is presented: signal / not a signal, and why.

Pure mapping from the parse outcome saved on TelegramMessage (parse_status + error_code)
to the two labels shown on the Messages pages. No Django imports, so it is trivially testable.
"""
from typing import Tuple

SIGNAL = "Signal"
NOT_SIGNAL = "Not a signal"
PROCESSING = "Processing"

_REASONS = {
    "MESSAGE_TOO_SHORT": "Message too short",
    "NO_SYMBOL": "No symbol found",
    "NO_DIRECTION": "No direction found",
    "NO_ENTRY": "No entry price found",
    "NO_TARGET": "No take-profit found",
    "NO_STOPLOSS": "No stop-loss found",
    "PARSER_ERROR": "Parser error",
}
_DUPLICATE = "DUPLICATE_SIGNAL"


def describe(parse_status: str, error_code: str) -> Tuple[str, str]:
    """(status, reason) for one message."""
    if parse_status == "SUCCESS":
        return SIGNAL, "Valid Signal"
    if parse_status == "PENDING":
        return PROCESSING, "—"
    if error_code == _DUPLICATE:
        # It did look like a signal, but an identical one was just handled: no new trade.
        return SIGNAL, "Duplicate signal (ignored)"
    return NOT_SIGNAL, _REASONS.get(error_code, error_code.replace("_", " ").capitalize() or "Unknown")
