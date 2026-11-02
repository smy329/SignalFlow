from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from telegram_monitor.models import ParsedSignal, TelegramMessage, TelegramSource
from telegram_monitor.services import parser_playground

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(category, name):
    return (FIXTURES / category / name).read_text()


class PreviewTests(TestCase):  # the preview reads the Real signal values from the Settings table
    def test_valid_signal_reports_signal_with_parsed_values(self):
        out = parser_playground.preview(fixture("valid", "standard_long_colon.txt"))
        self.assertEqual((out["is_signal"], out["status"], out["reason"]), (True, "Signal", "Valid Signal"))
        self.assertEqual(out["signal"]["symbol"], "BTCUSDT")
        self.assertEqual(out["signal"]["direction"], "BUY")
        self.assertEqual(out["signal"]["entry"], 100000.0)
        self.assertEqual(out["signal"]["targets"], [101000.0, 102000.0, 103000.0])
        self.assertEqual(out["signal"]["stoploss"], 98000.0)

    def test_short_message_is_not_a_signal(self):
        out = parser_playground.preview("gm everyone")
        self.assertEqual((out["is_signal"], out["status"], out["reason"], out["signal"]), (False, "Not a signal", "Message too short", None))

    def test_every_invalid_fixture_is_not_a_signal_with_a_reason(self):
        files = sorted((FIXTURES / "invalid").glob("*.txt"))
        self.assertTrue(files)
        for f in files:
            out = parser_playground.preview(f.read_text())
            self.assertFalse(out["is_signal"], f.name)
            self.assertEqual(out["status"], "Not a signal", f.name)
            self.assertTrue(out["reason"], f.name)

    def test_result_is_json_serializable(self):
        import json

        json.dumps(parser_playground.preview(fixture("valid", "standard_long_colon.txt")))


class PlaygroundViewTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user("yasir", password="pw")

    def login(self):
        self.client.login(username="yasir", password="pw")

    def post(self, payload, raw=False):
        import json

        body = payload if raw else json.dumps(payload)
        return self.client.post(reverse("parser-playground-parse"), body, content_type="application/json")

    def test_requires_login(self):
        self.assertEqual(self.client.get(reverse("parser-playground")).status_code, 302)
        response = self.post({"text": "x"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_page_renders_with_input_and_parse_button(self):
        self.login()
        html = self.client.get(reverse("parser-playground")).content.decode()
        for needle in ('id="pg-input"', 'id="pg-parse"', "Parse", "nothing is saved"):
            self.assertIn(needle, html)

    def test_parse_is_post_only(self):
        self.login()
        self.assertEqual(self.client.get(reverse("parser-playground-parse")).status_code, 405)

    def test_parse_returns_status_and_reason(self):
        self.login()
        data = self.post({"text": fixture("valid", "standard_long_colon.txt")}).json()
        self.assertEqual((data["status"], data["reason"], data["is_signal"]), ("Signal", "Valid Signal", True))
        data = self.post({"text": "hello"}).json()
        self.assertEqual((data["status"], data["reason"]), ("Not a signal", "Message too short"))

    def test_bad_input_is_a_400_not_a_crash(self):
        self.login()
        self.assertEqual(self.post("not json", raw=True).status_code, 400)
        self.assertEqual(self.post({"text": 123}).status_code, 400)
        too_long = "x" * (parser_playground.max_text_length() + 1)
        self.assertEqual(self.post({"text": too_long}).status_code, 400)

    def test_empty_text_is_handled(self):
        self.login()
        self.assertEqual(self.post({}).json()["status"], "Not a signal")

    def test_nothing_is_stored_even_for_a_valid_signal(self):
        self.login()
        TelegramSource.objects.create(name="S", telegram_id=-1, mode="REAL")
        self.post({"text": fixture("valid", "standard_long_colon.txt")})
        self.assertEqual((TelegramMessage.objects.count(), ParsedSignal.objects.count()), (0, 0))
        from trading.models import Trade

        self.assertEqual(Trade.objects.count(), 0)
