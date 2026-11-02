"""
Cross-app events published by telegram_monitor.

telegram_monitor never imports trading code: other apps subscribe here instead.
"""
from django.dispatch import Signal

# Sent (via transaction.on_commit) after a ParsedSignal is safely persisted.
# Keyword argument: signal_id (int). Receivers must return quickly.
signal_committed = Signal()
