"""The Real and Manual Positions pages must never make the engine loop read the database.

A sync view waits on the engine loop with `engine.run(...)`. If the engine coroutine itself calls
`sync_to_async` (a database read), that call inherits the request's thread, which is the one blocked
waiting: a deadlock that ends in the 10 s TimeoutError / 502 "Could not load positions".
So the coroutine handed to the engine may only talk to Binance; the rows are built in the view."""
import asyncio
import os
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from types import SimpleNamespace

from telegram_monitor.models import MANUAL_CHANNEL, MANUAL_TELEGRAM_ID, TelegramSource
from trading import views, views_manual
from trading.models import Trade
from trading.services import position_service
from trading.strategy_tests.support import make_signal, make_source


def snap(symbol, amount="1"):
    return dict(symbol=symbol, amount=D(amount), mark_price=D("2000"), notional=D("2000"), unrealized_pnl=D("5"), leverage=10,
                initial_margin=D("200"), maint_margin=D("10"), entry_price=D("1990"), liq_price=D("1800"))


class FakeGateway:
    async def position_snapshots(self):
        return [snap("ETHUSDT"), snap("BTCUSDT", "-1"), snap("SOLUSDT")]  # SOL: opened by hand on Binance


def engine_stub():
    def run(coro, timeout=None):  # like the real engine: the coroutine runs elsewhere, the caller waits
        return asyncio.run(coro)

    return SimpleNamespace(is_running=True, has_live=True, live_gateway=FakeGateway(), run=run, view_timeout=2,
                           gateways={"LIVE": object(), "PAPER": object()}, settings=SimpleNamespace(fee_rate=D("0")),
                           market_gateway=None)


async def forbidden(*args, **kwargs):  # what a database read on the engine loop would be
    raise AssertionError("the engine loop must not read the database")


def no_db_on_engine_loop(func):
    return mock.patch("trading.services.position_service.sync_to_async", lambda f: forbidden)(func)


def live_trade(source, symbol):
    signal = make_signal(source)
    signal.symbol = symbol
    signal.channel = MANUAL_CHANNEL if source.telegram_id == MANUAL_TELEGRAM_ID else source.name
    signal.save()
    return Trade.objects.create(
        signal=signal, mode="LIVE", status="OPEN", symbol=symbol, side="BUY", leverage=10, margin_type="CROSSED",
        quantity=D("1"), entry_price=D("1990"), entry_prices=["1990"], stoploss=D("1980"), targets=["2020"], filled_quantity=D("1"),
    )


class PositionsApiTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("t", password="pw"))
        os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

    @no_db_on_engine_loop
    def test_real_positions_show_our_trade_and_hand_opened_positions_without_the_engine_touching_the_db(self):
        mine = live_trade(make_source(name="Real src", mode=TelegramSource.Mode.REAL), "ETHUSDT")
        manual = TelegramSource.objects.create(telegram_id=MANUAL_TELEGRAM_ID, mode="REAL", name="Manual", is_active=False)
        live_trade(manual, "BTCUSDT")
        with mock.patch.object(views, "engine", engine_stub()):
            data = self.client.get(reverse("real-api-positions")).json()
        self.assertEqual(
            {(r["symbol"], r["trade_id"], r["channel"]) for r in data["rows"]},
            {("ETHUSDT", mine.id, "Real src"), ("SOLUSDT", None, "—")},  # the manual BTC trade lives on the Manual page
        )

    @no_db_on_engine_loop
    def test_manual_positions_show_the_live_manual_trade_without_the_engine_touching_the_db(self):
        manual = TelegramSource.objects.create(telegram_id=MANUAL_TELEGRAM_ID, mode="REAL", name="Manual", is_active=False)
        trade = live_trade(manual, "BTCUSDT")
        live_trade(make_source(name="Real src", mode=TelegramSource.Mode.REAL), "ETHUSDT")
        with mock.patch.object(views_manual, "engine", engine_stub()), mock.patch.object(views, "engine", engine_stub()):
            data = self.client.get(reverse("manual-api-positions")).json()
        self.assertEqual(data["message"], "")
        self.assertEqual([(r["symbol"], r["trade_id"], r["mode"]) for r in data["rows"]], [("BTCUSDT", trade.id, "Real")])

    def test_rows_from_matches_build_positions(self):
        from asgiref.sync import async_to_sync

        live_trade(make_source(name="Real src", mode=TelegramSource.Mode.REAL), "ETHUSDT")
        from trading.services import trade_repository as repo

        snapshots = async_to_sync(FakeGateway().position_snapshots)()
        built = async_to_sync(position_service.build_positions)(FakeGateway())
        self.assertEqual(position_service.rows_from(snapshots, repo.active_live_trades_by_symbol()), built)
