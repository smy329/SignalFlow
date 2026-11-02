"""Trading settings as one immutable snapshot.

`TradingSettings.load()` reads the Settings page values (and the API keys from .env); the engine
reloads it every couple of seconds and hands the fresh snapshot to the executor, the monitor and
the gateways, so a change on the Settings page applies without a restart.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings


@dataclass(frozen=True)
class TradingSettings:
    trading_enabled: bool  # may open NEW live trades (open ones are always monitored)
    forward_test_enabled: bool
    poll_seconds: float
    fee_rate: Decimal  # fraction per fill, e.g. 0.0005 = 0.05% (forward test only)
    api_key: str
    api_secret: str
    testnet: bool  # where NEW live trades go; a live trade keeps the environment it was opened on
    # --- engine ---
    max_concurrent_trades: int = 8
    reconnect_seconds: float = 30
    call_timeout: float = 15
    ready_timeout: float = 5
    view_timeout: float = 10
    # --- Binance / paper trading ---
    filters_cache_seconds: float = 600
    leverage_cache_seconds: float = 3600
    rate_limit_retry_seconds: float = 60
    candle_interval: str = "1m"
    paper_page_size: int = 1000
    paper_pages_per_sync: int = 6
    trade_lookback_ms: int = (2 * 24 * 60 - 5) * 60 * 1000

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @classmethod
    def load(cls) -> "TradingSettings":
        from runtime_settings import service as runtime

        get = runtime.get
        return cls(
            trading_enabled=get("trading.real_enabled"),
            forward_test_enabled=get("trading.forward_enabled"),
            poll_seconds=max(get("trading.min_poll_seconds"), get("trading.poll_seconds")),
            fee_rate=Decimal(str(get("forward.fee_percent"))) / 100,
            api_key=settings.BINANCE_API_KEY,
            api_secret=settings.BINANCE_API_SECRET,
            testnet=get("trading.testnet"),
            max_concurrent_trades=get("trading.max_concurrent_trades"),
            reconnect_seconds=get("engine.reconnect_wait_seconds"),
            call_timeout=get("engine.call_timeout_seconds"),
            ready_timeout=get("engine.ready_timeout_seconds"),
            view_timeout=get("engine.view_timeout_seconds"),
            filters_cache_seconds=get("binance.filters_cache_seconds"),
            leverage_cache_seconds=get("binance.leverage_cache_seconds"),
            rate_limit_retry_seconds=get("binance.rate_limit_retry_seconds"),
            candle_interval=get("binance.candle_interval"),
            paper_page_size=get("paper.page_size"),
            paper_pages_per_sync=get("paper.pages_per_sync"),
            trade_lookback_ms=get("paper.trade_lookback_minutes") * 60 * 1000,
        )

    # Kept so older callers keep working.
    from_django_settings = load
