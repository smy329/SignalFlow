"""Test doubles for the strategy tests: a scripted market and DB helpers (forward-test / paper mode)."""
import itertools
from decimal import Decimal
from typing import Optional

from asgiref.sync import async_to_sync
from django.utils import timezone

from telegram_monitor.models import ParsedSignal, TelegramMessage, TelegramSource
from trading.config import TradingSettings
from trading.domain.types import SymbolFilters
from trading.exchange.base import AggTrade
from trading.exchange.paper_gateway import PaperGateway
from trading.models import Trade
from trading.services.execution_service import ExecutionService
from trading.services.monitor_service import MonitorService

D = Decimal
_ids = itertools.count(1)


def settings_for_tests() -> TradingSettings:
    return TradingSettings(
        trading_enabled=False, forward_test_enabled=True, poll_seconds=5, fee_rate=D("0.0005"),
        api_key="", api_secret="", testnet=False,
    )


class ScriptedMarket:
    """Real-market stand-in: the test pushes prices; every push is one aggregated trade."""

    def __init__(self, price: str = "2010") -> None:
        self.price = D(price)
        self.clock = 1_000_000
        self.trades: list[AggTrade] = []
        self._filters = SymbolFilters(D("0.01"), D("0.001"), D("0.001"), D("1000"), D("5"))

    def push(self, *prices) -> None:
        for price in prices:
            self.clock += 1000
            self.price = D(str(price))
            self.trades.append(AggTrade(id=len(self.trades) + 1, price=self.price, time_ms=self.clock))

    # --- the market interface PaperGateway needs ---
    def filters(self, symbol: str) -> Optional[SymbolFilters]:
        return self._filters

    def now_ms(self) -> int:
        self.clock += 1
        return self.clock

    async def refresh_filters_if_stale(self) -> None: ...

    async def max_leverage(self, symbol: str) -> int:
        return 50

    async def last_price(self, symbol: str) -> Decimal:
        return self.price

    async def last_prices(self, symbols):
        return {s: self.price for s in symbols}

    async def agg_trades(self, symbol, *, from_id=None, start_ms=None, limit=1000):
        if from_id is not None:
            return [t for t in self.trades if t.id >= from_id][:limit]
        return [t for t in self.trades if t.time_ms >= (start_ms or 0)][:limit]


def make_source(**fields) -> TelegramSource:
    defaults = {"telegram_id": -next(_ids), "name": "Test source", "mode": TelegramSource.Mode.FORWARD}
    return TelegramSource.objects.create(**{**defaults, **fields})


def make_signal(source, *, side="BUY", entries=("2000", "1990"), targets=("2020", "2040", "2060"), stoploss="1980",
                margin="30", leverage=10) -> ParsedSignal:
    prices = [D(e) for e in entries]
    message = TelegramMessage.objects.create(
        source=source, telegram_message_id=next(_ids), message_text="signal", telegram_date=timezone.now()
    )
    return ParsedSignal.objects.create(
        telegram_message=message, source=source, symbol="ETHUSDT", direction=side, leverage=leverage,
        margin=margin, margin_type="CROSSED", entry=max(prices) if side == "BUY" else min(prices),
        entries=[str(p) for p in prices], targets=[str(t) for t in targets], stoploss=stoploss,
    )


class Harness:
    """Executes a signal on the paper gateway and drives the monitor with scripted prices."""

    def __init__(self, price: str = "2010") -> None:
        self.market = ScriptedMarket(price)
        self.paper = PaperGateway(self.market)
        self.cfg = settings_for_tests()
        self.executor = ExecutionService({"PAPER": self.paper}, self.cfg)
        self.monitor = MonitorService({"PAPER": self.paper}, self.cfg)
        self.executor.attach_monitor(self.monitor)

    def open(self, signal) -> Trade:
        async_to_sync(self.executor.handle_signal)(signal.id)
        return Trade.objects.get(signal=signal, mode="PAPER")

    def run(self, *prices, polls: int = 1) -> None:
        """Push prices (each is one real trade) and let the monitor process them."""
        self.market.push(*prices)
        for _ in range(polls):
            async_to_sync(self.monitor.poll_once)()


def orders(trade: Trade, role: str, *, status: Optional[str] = None):
    qs = trade.orders.filter(role=role).order_by("id")
    return list(qs.filter(status=status) if status else qs)


def working(trade: Trade, role: str):
    return [o for o in orders(trade, role) if o.status in ("NEW", "PARTIALLY_FILLED")]
