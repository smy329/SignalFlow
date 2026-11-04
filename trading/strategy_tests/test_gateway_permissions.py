"""A trading action Binance refuses (-2015) names the real cause; only -4046 is swallowed silently."""
from unittest.mock import AsyncMock, MagicMock

from asgiref.sync import async_to_sync
from binance.exceptions import BinanceAPIException
from django.test import SimpleTestCase

from trading.exchange.base import GatewayError
from trading.exchange.binance_gateway import BinanceGateway


def api_error(code: int, message: str) -> BinanceAPIException:
    response = MagicMock()
    response.status = 400
    response.text = f'{{"code": {code}, "msg": "{message}"}}'
    return BinanceAPIException(response, response.status, response.text)


class PrepareSymbolTests(SimpleTestCase):
    def setUp(self):
        self.gateway = BinanceGateway("k", "s", testnet=False)
        self.gateway.client = MagicMock()
        self.gateway.client.futures_change_margin_type = AsyncMock()
        self.gateway.client.futures_change_leverage = AsyncMock(return_value={"leverage": 10})

    def prepare(self, symbol="XPLUSDT", leverage=10, margin_type="cross"):
        return async_to_sync(self.gateway.prepare_symbol)(symbol, leverage, margin_type)

    def test_a_permission_refusal_on_leverage_explains_what_to_check(self):
        self.gateway.client.futures_change_leverage.side_effect = api_error(
            -2015, "Invalid API-key, IP, or permissions for action"
        )
        with self.assertRaisesMessage(GatewayError, "Enable Futures trading on the API key"):
            self.prepare()

    def test_a_permission_refusal_on_the_margin_type_stops_before_leverage(self):
        self.gateway.client.futures_change_margin_type.side_effect = api_error(
            -2015, "Invalid API-key, IP, or permissions for action"
        )
        with self.assertRaisesMessage(GatewayError, "change the margin type"):
            self.prepare()
        self.gateway.client.futures_change_leverage.assert_not_called()

    def test_no_need_to_change_the_margin_type_is_silent(self):
        self.gateway.client.futures_change_margin_type.side_effect = api_error(-4046, "No need to change margin type.")
        with self.assertNoLogs("trading.exchange.binance_gateway", level="WARNING"):
            self.assertEqual(self.prepare(), 10)

    def test_any_other_margin_type_error_stays_a_warning(self):
        self.gateway.client.futures_change_margin_type.side_effect = api_error(-1000, "An unknown error occured.")
        with self.assertLogs("trading.exchange.binance_gateway", level="WARNING") as logs:
            self.assertEqual(self.prepare(), 10)
        self.assertIn("Margin type change on XPLUSDT not applied", logs.output[0])

    def test_the_leverage_the_exchange_applied_is_returned(self):
        self.gateway.client.futures_change_leverage.return_value = {"leverage": 5}
        self.assertEqual(self.prepare(), 5)
