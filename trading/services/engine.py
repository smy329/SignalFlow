"""
The trading engine: one background thread with its own asyncio loop (same
pattern as the Telegram listener) that owns the exchange gateways, the signal
executor and the 5-second monitor.

Started once per process from trading.apps.TradingConfig.ready() (guarded like
the listener). Django views reach it through `engine.run(coroutine)`.

Settings are live: every few seconds the engine reloads TradingSettings from the Settings page and
reconciles itself (connects / adds gateways, hands the new values to the executor, the monitor and the
gateways). Nothing is ever torn down while a trade needs it: a live trade keeps being managed on the
Binance environment (real or testnet) it was opened on, whatever the testnet setting says now.
"""
import asyncio
import atexit
import logging
import threading
import time
from dataclasses import replace
from decimal import Decimal
from typing import Awaitable, Optional, TypeVar

from asgiref.sync import sync_to_async

from telegram_monitor.apps import wait_for_apps_ready
from trading.config import TradingSettings
from trading.domain.types import ALL_ROLE_VALUES, ENTRY_ROLES
from trading.exchange.base import ExchangeGateway, GatewayError
from trading.exchange.binance_gateway import BinanceGateway
from trading.exchange.paper_gateway import PaperGateway
from trading.services import trade_repository as repo
from trading.services.execution_service import ExecutionService
from trading.services.gateways import LIVE, live_key
from trading.services.monitor_service import MonitorService

logger = logging.getLogger(__name__)
T = TypeVar("T")

CONFIG_REFRESH_SECONDS = 2  # how often the Settings page values are re-read


class EngineNotReady(RuntimeError):
    """The engine has not finished starting (or is disabled)."""


class TradingEngine:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._ready = threading.Event()
        self._tasks: set[asyncio.Task] = set()
        self._market: Optional[BinanceGateway] = None  # real-market prices/rules (public client), never the testnet
        self._live_by_env: dict[bool, BinanceGateway] = {}  # testnet? -> verified, authenticated live gateway
        self._paper: Optional[PaperGateway] = None
        self._retry_at: dict[object, float] = {}  # connection attempts are throttled by "Engine reconnect wait"
        self._last_error: dict[bool, str] = {}
        # mode -> gateway. "LIVE" is the environment NEW live trades use; "LIVE@MAINNET" / "LIVE@TESTNET"
        # are every connected live environment (open trades are managed on their own).
        self.gateways: dict[str, ExchangeGateway] = {}
        self.settings: Optional[TradingSettings] = None
        self._executor: Optional[ExecutionService] = None
        self._monitor: Optional[MonitorService] = None

    # ---- public API (thread-safe, callable from Django) -----------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    @property
    def has_live(self) -> bool:
        return repo.MODE_LIVE in self.gateways

    @property
    def live_gateway(self) -> BinanceGateway:
        return self.gateways[repo.MODE_LIVE]  # type: ignore[return-value]

    @property
    def market_gateway(self) -> BinanceGateway:
        """Source of every "current price" shown in the UI: always the real market."""
        if self._market is None:
            raise EngineNotReady("The trading engine is not ready yet.")
        return self._market

    @property
    def call_timeout(self) -> float:
        return self.settings.call_timeout if self.settings is not None else 15.0

    @property
    def close_timeout(self) -> float:
        """A close is several exchange calls in a row (cancel orders, read the position, market close)."""
        return self.call_timeout * 3

    @property
    def view_timeout(self) -> float:
        return self.settings.view_timeout if self.settings is not None else 10.0

    @property
    def ready_timeout(self) -> float:
        return self.settings.ready_timeout if self.settings is not None else 5.0

    def start(self) -> None:
        """Idempotent: only the first call starts the thread."""
        with self._lock:
            if self._thread is not None:
                return
            self._loop = asyncio.new_event_loop()
            self._queue = asyncio.Queue()
            self._thread = threading.Thread(target=self._run_thread, name="trading-engine", daemon=True)
            self._thread.start()
        atexit.register(self.stop)

    def submit_signal(self, signal_id: int) -> None:
        """Non-blocking hand-off from the signal_committed receiver. Signals that
        arrive before the engine is ready are queued and handled once it is."""
        if self._loop is None or self._queue is None:
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, signal_id)

    def run(self, coro: Awaitable[T], timeout: Optional[float] = None) -> T:
        """Run a coroutine on the engine loop from sync code (views)."""
        timeout = timeout or self.call_timeout
        if not self._ready.wait(timeout=min(timeout, self.ready_timeout)) or self._loop is None:
            if hasattr(coro, "close"):
                coro.close()
            raise EngineNotReady("The trading engine is not ready yet.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    async def close_trade(self, trade_id: int) -> str:
        """Close an open trade at market now (the Close button). Returns a monitor_service outcome.

        Awaited from an async view on purpose: a sync view would block the one thread that the engine's own
        database calls (sync_to_async) need, and the close would wait for itself forever."""
        if self._monitor is None or self._loop is None or not self._ready.is_set():
            raise EngineNotReady("The trading engine is not ready yet.")
        future = asyncio.run_coroutine_threadsafe(self._monitor.close_now(trade_id), self._loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=self.close_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("closing took too long") from exc

    async def trade_now(self, trade_id: int) -> tuple[str, str]:
        """Trade a pending trade at the current market price now (the Trade Now button).
        Returns (monitor_service outcome, detail). Awaited from an async view for the same reason as close_trade."""
        if self._monitor is None or self._loop is None or not self._ready.is_set():
            raise EngineNotReady("The trading engine is not ready yet.")
        future = asyncio.run_coroutine_threadsafe(self._monitor.trade_now(trade_id), self._loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=self.close_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("trading now took too long") from exc

    def stop(self) -> None:
        if self._loop is None:
            return
        gateways = [self._market, *self._live_by_env.values()]
        for gateway in {id(g): g for g in gateways if g is not None and g.client is not None}.values():
            try:
                asyncio.run_coroutine_threadsafe(gateway.close(), self._loop).result(timeout=self.ready_timeout)
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logger.exception("Error while closing the Binance client.")

    # ---- thread / loop bootstrap -------------------------------------------------

    def _run_thread(self) -> None:
        wait_for_apps_ready()  # no DB access while Django is still initializing apps
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            logger.exception("Trading engine crashed.")

    async def _load_settings(self) -> TradingSettings:
        return await sync_to_async(TradingSettings.load)()

    async def _main(self) -> None:
        cfg = self.settings = await self._load_settings()
        self._executor = ExecutionService(self.gateways, cfg)
        self._monitor = MonitorService(self.gateways, cfg)
        self._executor.attach_monitor(self._monitor)

        # Wait for the network (like before), but never for credentials or a switch: those can change later.
        while True:
            await self._reconcile(cfg)
            if self._market is not None:
                break
            await asyncio.sleep(cfg.reconnect_seconds)
            cfg = self.settings = await self._load_settings()
            self._executor.update_settings(cfg)
            self._monitor.update_settings(cfg)

        await self._executor.recover_interrupted_trades()
        self._ready.set()
        logger.info(
            "Trading engine ready: live=%s (new trades %s), forward test=%s",
            self.has_live, "ENABLED" if cfg.trading_enabled else "disabled", repo.MODE_PAPER in self.gateways,
        )
        await asyncio.gather(self._monitor.run_forever(), self._signal_worker(), self._config_loop())

    async def _config_loop(self) -> None:
        """Re-read the Settings page and apply it, so no restart is ever needed."""
        while True:
            await asyncio.sleep(CONFIG_REFRESH_SECONDS)
            try:
                cfg = await self._load_settings()
                self.settings = cfg
                self._executor.update_settings(cfg)
                self._monitor.update_settings(cfg)
                await self._reconcile(cfg)
            except Exception:  # noqa: BLE001 - keep the previous settings and try again
                logger.exception("Could not apply the Settings page values")

    # ---- connections ----------------------------------------------------------------

    async def _connect(self, api_key: str, api_secret: str, testnet: bool) -> Optional[BinanceGateway]:
        """One attempt. None when Binance cannot be reached (network down, geo-block, clock, ...)."""
        gateway = BinanceGateway(api_key, api_secret, testnet)
        try:
            await gateway.start()
            return gateway
        except Exception:  # noqa: BLE001
            logger.exception("Could not connect to Binance, retrying in %ss", self.settings.reconnect_seconds)
            await gateway.close()
            return None

    def _throttled(self, name: object) -> bool:
        """True while a failed connection attempt should not be repeated yet."""
        return time.monotonic() < self._retry_at.get(name, 0.0)

    def _back_off(self, name: object, cfg: TradingSettings) -> None:
        """A connection attempt failed: wait "Engine reconnect wait" before the next one."""
        self._retry_at[name] = time.monotonic() + cfg.reconnect_seconds

    async def _reconcile(self, cfg: TradingSettings) -> None:
        """Bring the connections and gateways in line with the current settings. Nothing that an open trade
        still needs is ever removed; switching things OFF only stops NEW trades (see ExecutionService)."""
        # 1. Public market client: prices and rules for the UI and the forward test.
        if self._market is None and not self._throttled("market"):
            self._market = await self._connect("", "", False)
            if self._market is None:
                self._back_off("market", cfg)
        if self._market is not None:
            self._market.apply_settings(cfg)

        # 2. Live clients: the environment new trades use, plus every environment with a live trade in progress.
        if cfg.has_credentials:
            needed = {cfg.testnet} | await sync_to_async(repo.active_live_environments)()
            for testnet in needed:
                await self._ensure_live(testnet, cfg)
        for testnet, gateway in self._live_by_env.items():
            gateway.apply_settings(cfg)
            self.gateways[live_key(testnet)] = gateway
        current = self._live_by_env.get(cfg.testnet)
        if current is not None:
            self.gateways[LIVE] = current
        else:
            self.gateways.pop(LIVE, None)  # no connection for the selected environment: no NEW live trades

        # 3. Paper gateway: created when the forward test is on (or paper trades are still running); never removed.
        if self._paper is None and self._market is not None:
            if cfg.forward_test_enabled or await sync_to_async(repo.has_active_paper_trades)():
                paper = PaperGateway(self._market)
                await self._restore_paper_state(paper)
                self._paper = paper
                self.gateways[repo.MODE_PAPER] = paper
        if self._paper is not None:
            self._paper.apply_settings(cfg)

    async def _ensure_live(self, testnet: bool, cfg: TradingSettings) -> None:
        key = ("live", testnet)
        if testnet in self._live_by_env or self._throttled(key):
            return
        gateway = await self._connect(cfg.api_key, cfg.api_secret, testnet)
        if gateway is None:
            self._back_off(key, cfg)
            return
        try:
            await gateway.verify_account()
        except GatewayError as exc:
            await gateway.close()
            self._back_off(key, cfg)
            message = str(exc)
            if self._last_error.get(testnet) != message:  # log a persistent problem once, not every retry
                self._last_error[testnet] = message
                logger.error("Live trading on Binance %s is unavailable: %s", "testnet" if testnet else "mainnet", exc)
            return
        self._last_error.pop(testnet, None)
        self._live_by_env[testnet] = gateway
        logger.info("Live gateway ready on Binance %s", "testnet" if testnet else "mainnet")

    async def _restore_paper_state(self, paper: PaperGateway) -> None:
        """Rebuild simulated orders/positions of unfinished paper trades after a restart,
        and resume the trade replay from the last processed exchange trade id."""
        cursors: dict[str, list[int]] = {}
        for trade in await sync_to_async(repo.snapshot_active_trades)():
            if trade.mode != repo.MODE_PAPER:
                continue
            for order in trade.rows(*ALL_ROLE_VALUES):
                spec = order.spec()
                if spec.role in ENTRY_ROLES:  # the "entry becomes void" levels are not stored per order
                    spec = replace(spec, guard_tp=trade.targets[0] if trade.targets else None, guard_sl=trade.stoploss)
                paper.restore_order(
                    exchange_id=order.exchange_id, trade_ref=trade.id, symbol=trade.symbol, spec=spec,
                    placed_ms=order.created_ms, status=order.status, executed_qty=order.executed_quantity,
                    avg_price=order.avg_fill_price,
                )
            if trade.paper_cursor is not None:
                cursors.setdefault(trade.symbol, []).append(trade.paper_cursor)
        for symbol, values in cursors.items():
            paper.set_cursor(symbol, min(values))

    async def _signal_worker(self) -> None:
        while True:
            signal_id = await self._queue.get()
            task = asyncio.create_task(self._handle(signal_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle(self, signal_id: int) -> None:
        try:
            await self._executor.handle_signal(signal_id)
        except Exception:  # noqa: BLE001
            logger.exception("Trading engine failed handling signal %s", signal_id)


engine = TradingEngine()
