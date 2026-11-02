"""
Parser tests — no Django, no DB, no Telegram. Pure function tests.
Every previously-supported extraction format gets a regression test here.
"""
import os
from pathlib import Path
from unittest import TestCase

from telegram_monitor.services.signal_parser import (
    NO_DIRECTION,
    NO_ENTRY,
    NO_STOPLOSS,
    NO_SYMBOL,
    NO_TARGET,
    MESSAGE_TOO_SHORT,
    parse_signal,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

DEFAULT_KWARGS = {"leverage": 20, "margin": None, "margin_type": "ISOLATED"}


def _parse(text, channel=""):
    return parse_signal(text, channel, **DEFAULT_KWARGS)


def _load_fixture(category, name):
    return (FIXTURES_DIR / category / name).read_text()


class ValidSignalTests(TestCase):
    def test_standard_long_with_colon_entry_and_labeled_targets(self):
        text = _load_fixture("valid", "standard_long_colon.txt")
        result = _parse(text, channel="Example Channel")
        self.assertTrue(result.success, result.error_message)
        signal = result.signal
        self.assertEqual(signal["symbol"], "BTCUSDT")
        self.assertEqual(signal["direction"], "BUY")
        self.assertEqual(signal["entry"], 100000.0)
        self.assertEqual(signal["targets"], [101000.0, 102000.0, 103000.0])
        self.assertEqual(signal["stoploss"], 98000.0)
        self.assertEqual(signal["channel"], "Example Channel")
        self.assertEqual(signal["leverage"], 20)
        self.assertEqual(signal["margin_type"], "ISOLATED")

    def test_short_direction_with_numbered_targets(self):
        text = _load_fixture("valid", "standard_short_numbered_targets.txt")
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        signal = result.signal
        self.assertEqual(signal["symbol"], "ETHUSDT")
        self.assertEqual(signal["direction"], "SELL")
        self.assertEqual(signal["entry"], 3500.0)
        self.assertEqual(signal["targets"], [3450.0, 3400.0, 3350.0])
        self.assertEqual(signal["stoploss"], 3600.0)

    def test_decimal_targets_on_own_lines_are_not_treated_as_list_indexes(self):
        text = (
            "$SQD Long\nEntry: 0.02030-0.02130\nTargets:\n0.025\n0.03\n0.035\n0.04\nSL: 0.01760\n"
            "Extra padding text to satisfy the minimum signal length requirement."
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["symbol"], "SQDUSDT")
        self.assertEqual(result.signal["entry"], 0.0213)
        self.assertEqual(result.signal["targets"], [0.025, 0.03, 0.035, 0.04])
        self.assertEqual(result.signal["stoploss"], 0.0176)

    def test_symbol_with_slash_usdt(self):
        text = (
            "#SOL/USDT LONG setup building nicely on the daily timeframe with strong buyer interest "
            "showing up across multiple exchanges right now.\n"
            "Entry: 150\nTP1: 155\nTP2: 160\nSL: 145"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["symbol"], "SOLUSDT")

    def test_symbol_with_bare_usd(self):
        text = (
            "BTC/USD LONG looking strong here as buyers continue to defend this key level of support "
            "on the four hour chart with volume.\n"
            "Entry: 60000\nTP1: 61000\nTP2: 62000\nSL: 58000"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        # Original parser always normalizes the pair suffix to USDT, even when
        # the message said USD — preserved exactly (see module docstring).
        self.assertEqual(result.signal["symbol"], "BTCUSDT")

    def test_fallback_symbol_extraction_from_first_line(self):
        text = (
            "DOGE LONG here we go again with another strong move expected on this coin over the "
            "next few trading sessions based on chart patterns.\n"
            "Entry: 0.15\nTP1: 0.16\nTP2: 0.17\nSL: 0.14"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["symbol"], "DOGEUSDT")

    def test_entry_without_colon(self):
        text = (
            "XRPUSDT LONG strong support forming here with a great risk reward setup for swing "
            "traders looking at the daily chart pattern.\n"
            "Entry 0.55\nTP1 0.60\nTP2 0.65\nSL 0.50"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["entry"], 0.55)

    def test_multiple_entry_prices_picks_max_for_buy(self):
        text = (
            "ADAUSDT LONG scaling in across a small range here for a safer average entry price on "
            "this setup over the next few candles.\n"
            "Entry: 0.40 0.42\nTP1: 0.45\nTP2: 0.48\nSL: 0.38"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["entry"], 0.42)  # BUY -> max of entry candidates

    def test_multiple_entry_prices_picks_min_for_sell(self):
        text = (
            "ADAUSDT SHORT scaling in across a small range here for a safer average entry price on "
            "this setup over the next few candles.\n"
            "Entry: 0.40 0.42\nTP1: 0.36\nTP2: 0.33\nSL: 0.45"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["entry"], 0.40)  # SELL -> min of entry candidates

    def test_target_block_with_take_profit_keyword(self):
        text = (
            "LINKUSDT LONG breaking out of a multi week accumulation range with strong volume "
            "backing this move on the daily timeframe today.\n"
            "Entry: 14\nTake Profit: 15 16 17\nSL: 13"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["targets"], [15.0, 16.0, 17.0])

    def test_tp_labeled_lines(self):
        text = (
            "AVAXUSDT LONG this level has held multiple times as strong support and we expect a "
            "bounce here over the coming sessions ahead.\n"
            "Entry: 30\nTP1: 32\nTP2: 34\nTP3: 36\nSL: 28"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["targets"], [32.0, 34.0, 36.0])

    def test_fallback_entry_from_above_below_at_around_near(self):
        text = (
            "MATICUSDT LONG below 0.90 this is a great area to start scaling into a position over "
            "the next few hours as price consolidates.\n"
            "TP1: 0.95\nTP2: 1.00\nSL: 0.85"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["entry"], 0.90)

    def test_direction_line_entry_fallback(self):
        text = (
            "DOTUSDT LONG 6.50 this is shaping up to be one of the better setups this week with "
            "clean structure on multiple timeframes for us.\n"
            "TP1: 6.80\nTP2: 7.00\nSL: 6.20"
        )
        result = _parse(text)
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.signal["entry"], 6.50)


class InvalidSignalTests(TestCase):
    def test_message_too_short(self):
        result = _parse("BTC LONG entry 100")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, MESSAGE_TOO_SHORT)

    def test_no_symbol(self):
        # First line contains only skip-listed tokens (LONG/HERE/CONSIDER/...),
        # and no digit+USDT/USD pattern appears anywhere else in the message.
        text = (
            "LONG SHORT HERE CONSIDER SOME FOR AND THE IN AT TO\n"
            "This padded filler sentence intentionally avoids any ticker symbol "
            "or trading pair anywhere in its text."
        )
        result = _parse(text)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, NO_SYMBOL)

    def test_no_direction(self):
        text = "BTCUSDT here is a padded message describing market conditions without stating a trade direction at all."
        result = _parse(text)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, NO_DIRECTION)

    def test_no_entry(self):
        text = (
            "BTCUSDT LONG this is a padded message with a direction and symbol but absolutely no "
            "entry price mentioned anywhere within it."
        )
        result = _parse(text)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, NO_ENTRY)

    def test_no_target(self):
        text = (
            "BTCUSDT LONG entry: 100000 this padded message has an entry price but deliberately no "
            "take profit targets listed anywhere within it."
        )
        result = _parse(text)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, NO_TARGET)

    def test_no_stoploss(self):
        text = (
            "BTCUSDT LONG this padded message has an entry and targets but no stop loss value "
            "anywhere within it at all today.\n"
            "Entry: 100000\nTP1: 101000\nTP2: 102000"
        )
        result = _parse(text)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, NO_STOPLOSS)

    def test_unrelated_telegram_message(self):
        text = _load_fixture("invalid", "unrelated_chatter.txt")
        result = _parse(text)
        self.assertFalse(result.success)

    def test_real_world_style_message_missing_stoploss(self):
        text = _load_fixture("invalid", "no_stoploss_real_world.txt")
        result = _parse(text)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, NO_STOPLOSS)

    def test_malformed_signal_missing_multiple_fields(self):
        text = "Some random padded text that is long enough to pass the length check but contains " * 2
        result = _parse(text)
        self.assertFalse(result.success)

    def test_empty_text(self):
        result = _parse("")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, MESSAGE_TOO_SHORT)
