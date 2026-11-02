"""Which gateway manages which trade.

A paper trade always uses the paper gateway. A live trade must be managed on the Binance
environment (real or testnet) it was opened on, whatever the "Use Binance testnet" setting says now:
switching the setting only changes where NEW trades go. The engine registers one live gateway per
environment under "LIVE@MAINNET" / "LIVE@TESTNET" (plus "LIVE" for the environment new trades use).
"""
from typing import Mapping, Optional

LIVE = "LIVE"
LIVE_MAINNET = "LIVE@MAINNET"
LIVE_TESTNET = "LIVE@TESTNET"


def live_key(testnet: bool) -> str:
    return LIVE_TESTNET if testnet else LIVE_MAINNET


def gateway_for(gateways: Mapping[str, object], mode: str, testnet: bool = False) -> Optional[object]:
    if mode != LIVE:
        return gateways.get(mode)
    if any(key.startswith("LIVE@") for key in gateways):
        return gateways.get(live_key(testnet))  # strict: never manage a trade on the other environment
    return gateways.get(LIVE)  # a single, unlabelled live gateway (tests / simple setups)
