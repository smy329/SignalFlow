from django.test import SimpleTestCase

from telegram_monitor.services.message_status import describe


class DescribeTests(SimpleTestCase):
    def test_valid_signal(self):
        self.assertEqual(describe("SUCCESS", ""), ("Signal", "Valid Signal"))

    def test_parser_rejections_are_not_signals_with_a_readable_reason(self):
        self.assertEqual(describe("FAILED", "MESSAGE_TOO_SHORT"), ("Not a signal", "Message too short"))
        self.assertEqual(describe("FAILED", "NO_DIRECTION"), ("Not a signal", "No direction found"))
        self.assertEqual(describe("FAILED", "NO_SYMBOL"), ("Not a signal", "No symbol found"))
        self.assertEqual(describe("FAILED", "NO_ENTRY"), ("Not a signal", "No entry price found"))
        self.assertEqual(describe("FAILED", "NO_TARGET"), ("Not a signal", "No take-profit found"))
        self.assertEqual(describe("FAILED", "NO_STOPLOSS"), ("Not a signal", "No stop-loss found"))
        self.assertEqual(describe("FAILED", "INVALID_LEVELS"), ("Not a signal", "Invalid price levels"))
        self.assertEqual(describe("FAILED", "MULTIPLE_SIGNALS"), ("Not a signal", "Multiple signals in one message"))
        self.assertEqual(describe("ERROR", "PARSER_ERROR"), ("Not a signal", "Parser error"))

    def test_duplicate_is_a_signal_that_was_ignored(self):
        self.assertEqual(describe("FAILED", "DUPLICATE_SIGNAL"), ("Signal", "Duplicate signal (ignored)"))

    def test_unprocessed_and_unknown_codes_never_crash(self):
        self.assertEqual(describe("PENDING", ""), ("Processing", "—"))
        self.assertEqual(describe("FAILED", "SOMETHING_NEW"), ("Not a signal", "Something new"))
        self.assertEqual(describe("FAILED", ""), ("Not a signal", "Unknown"))
