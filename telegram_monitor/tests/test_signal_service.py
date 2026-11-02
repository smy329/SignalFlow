from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from runtime_settings import service as runtime
from telegram_monitor.models import ParsedSignal, TelegramMessage, TelegramSource
from telegram_monitor.services import signal_service

VALID_SIGNAL_TEXT = (
    "BTCUSDT LONG\n\n"
    "This is a strong setup forming with good volume confirmation across the board on the daily chart today.\n\n"
    "Entry: 100000\n\nTP1: 101000\nTP2: 102000\n\nSL: 98000"
)


def make_message(source, telegram_message_id=1, text=VALID_SIGNAL_TEXT):
    return TelegramMessage.objects.create(
        source=source,
        telegram_message_id=telegram_message_id,
        message_text=text,
        telegram_date=timezone.now(),
    )


class SignalServiceSuccessTests(TestCase):
    def setUp(self):
        self.source = TelegramSource.objects.create(name="Signals Channel", telegram_id=-100111)

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_valid_signal_creates_parsed_signal(self, mock_broadcast):
        message = make_message(self.source)
        with self.captureOnCommitCallbacks(execute=True):
            signal = signal_service.process_message(message)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.symbol, "BTCUSDT")
        self.assertEqual(signal.direction, "BUY")
        message.refresh_from_db()
        self.assertEqual(message.parse_status, TelegramMessage.ParseStatus.SUCCESS)
        self.assertEqual(message.parsed_signal, signal)
        mock_broadcast.assert_called_once_with(signal)

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_broadcast_is_wired_through_on_commit_not_called_directly(self, mock_broadcast):
        # Regression guard: broadcast_signal must be registered via
        # transaction.on_commit, not called directly inline. Proof: outside
        # captureOnCommitCallbacks (i.e. without a real commit), it must
        # never fire — even though the signal itself was still persisted.
        message = make_message(self.source)
        signal = signal_service.process_message(message)
        self.assertIsNotNone(signal)
        mock_broadcast.assert_not_called()


class SignalServiceFailureTests(TestCase):
    def setUp(self):
        self.source = TelegramSource.objects.create(name="Chatty Channel", telegram_id=-100222)

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_invalid_message_does_not_create_signal(self, mock_broadcast):
        message = make_message(self.source, text="Just a friendly good morning message to the whole group today!")
        signal = signal_service.process_message(message)

        self.assertIsNone(signal)
        self.assertEqual(ParsedSignal.objects.count(), 0)
        message.refresh_from_db()
        self.assertEqual(message.parse_status, TelegramMessage.ParseStatus.FAILED)
        self.assertTrue(message.error_code)
        mock_broadcast.assert_not_called()

    def test_parser_error_is_isolated_and_recorded(self):
        message = make_message(self.source)
        with patch("telegram_monitor.services.signal_service.parse_signal", side_effect=RuntimeError("boom")):
            signal = signal_service.process_message(message)

        self.assertIsNone(signal)
        message.refresh_from_db()
        self.assertEqual(message.parse_status, TelegramMessage.ParseStatus.ERROR)
        self.assertEqual(message.error_code, "PARSER_ERROR")


class DecimalPrecisionTests(TestCase):
    def setUp(self):
        self.source = TelegramSource.objects.create(name="Micro Cap Channel", telegram_id=-100333)

    def test_small_crypto_prices_keep_full_precision(self):
        from pathlib import Path

        fixture = Path(__file__).parent / "fixtures" / "valid" / "small_decimal_price.txt"
        message = make_message(self.source, text=fixture.read_text())

        with self.captureOnCommitCallbacks(execute=True):
            with patch("telegram_monitor.services.signal_service.realtime.broadcast_signal"):
                signal = signal_service.process_message(message)

        self.assertIsNotNone(signal)
        signal.refresh_from_db()
        self.assertEqual(str(signal.entry).rstrip("0"), "0.0000091")
        self.assertEqual(str(signal.stoploss).rstrip("0"), "0.0000085")


class DuplicateDetectionTests(TestCase):
    def setUp(self):
        self.source_a = TelegramSource.objects.create(name="Channel A", telegram_id=-100111)
        self.source_b = TelegramSource.objects.create(name="Channel B", telegram_id=-100222)

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_identical_signal_within_window_is_rejected(self, mock_broadcast):
        first = make_message(self.source_a, telegram_message_id=1)
        second = make_message(self.source_a, telegram_message_id=2)

        with self.captureOnCommitCallbacks(execute=True):
            signal_service.process_message(first)
            result = signal_service.process_message(second)

        self.assertIsNone(result)
        self.assertEqual(ParsedSignal.objects.count(), 1)
        second.refresh_from_db()
        self.assertEqual(second.error_code, signal_service.DUPLICATE_SIGNAL)
        mock_broadcast.assert_called_once()  # only for the first, real signal

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_different_source_is_not_suppressed(self, mock_broadcast):
        make_message(self.source_a, telegram_message_id=1)
        signal_service.process_message(TelegramMessage.objects.get(source=self.source_a))

        second = make_message(self.source_b, telegram_message_id=1)
        signal = signal_service.process_message(second)

        self.assertIsNotNone(signal)
        self.assertEqual(ParsedSignal.objects.count(), 2)

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_signal_outside_window_is_not_a_duplicate(self, mock_broadcast):
        runtime.save_many({"real.duplicate_window_seconds": 0, "forward.duplicate_window_seconds": 0})
        first = make_message(self.source_a, telegram_message_id=1)
        signal_service.process_message(first)

        second = make_message(self.source_a, telegram_message_id=2)
        signal = signal_service.process_message(second)

        self.assertIsNotNone(signal)
        self.assertEqual(ParsedSignal.objects.count(), 2)

    @patch("telegram_monitor.services.signal_service.realtime.broadcast_signal")
    def test_different_entry_is_not_a_duplicate(self, mock_broadcast):
        first = make_message(self.source_a, telegram_message_id=1)
        signal_service.process_message(first)

        different_entry_text = VALID_SIGNAL_TEXT.replace("100000", "999000")
        second = make_message(self.source_a, telegram_message_id=2, text=different_entry_text)
        signal = signal_service.process_message(second)

        self.assertIsNotNone(signal)
        self.assertEqual(ParsedSignal.objects.count(), 2)
