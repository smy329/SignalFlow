from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from telegram_monitor.models import TelegramMessage, TelegramSource


class AuthenticationRequiredTests(TestCase):
    def test_sources_endpoint_requires_login(self):
        response = self.client.get("/api/sources/")
        self.assertIn(response.status_code, (401, 403))

    def test_messages_endpoint_requires_login(self):
        response = self.client.get("/api/messages/")
        self.assertIn(response.status_code, (401, 403))

    def test_dashboard_redirects_to_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)


class TelegramSourceApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dev", password="pass12345")
        self.client.login(username="dev", password="pass12345")

    @patch("telegram_monitor.api_views.listener_manager.resolve_entity")
    def test_create_source_resolves_entity(self, mock_resolve):
        mock_resolve.return_value = {"name": "Trading Group", "username": "tradinggroup"}
        response = self.client.post("/api/sources/", {"telegram_id": "-1001234567890"})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(TelegramSource.objects.filter(telegram_id=-1001234567890).exists())

    @patch("telegram_monitor.api_views.listener_manager.resolve_entity")
    def test_create_source_rejects_inaccessible_chat(self, mock_resolve):
        mock_resolve.side_effect = Exception("A wait of 5 seconds is required")
        response = self.client.post("/api/sources/", {"telegram_id": "-1009999999999"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TelegramSource.objects.filter(telegram_id=-1009999999999).exists())

    def test_create_source_rejects_non_integer_id(self):
        response = self.client.post("/api/sources/", {"telegram_id": "not-a-number"})
        self.assertEqual(response.status_code, 400)

    def test_list_sources(self):
        TelegramSource.objects.create(name="A", telegram_id=-100111)
        response = self.client.get("/api/sources/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"] if "results" in response.json() else response.json()), 1)

    def test_update_source_deactivate(self):
        source = TelegramSource.objects.create(name="A", telegram_id=-100111)
        response = self.client.patch(
            f"/api/sources/{source.id}/", {"is_active": False}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        source.refresh_from_db()
        self.assertFalse(source.is_active)

    def test_delete_source(self):
        source = TelegramSource.objects.create(name="A", telegram_id=-100111)
        response = self.client.delete(f"/api/sources/{source.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(TelegramSource.objects.filter(id=source.id).exists())


class TelegramMessageApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dev", password="pass12345")
        self.client.login(username="dev", password="pass12345")
        self.source_a = TelegramSource.objects.create(name="A", telegram_id=-100111)
        self.source_b = TelegramSource.objects.create(name="B", telegram_id=-100222)
        for i in range(1, 4):
            TelegramMessage.objects.create(
                source=self.source_a, telegram_message_id=i, message_text=f"a{i}", telegram_date=timezone.now()
            )
        TelegramMessage.objects.create(
            source=self.source_b, telegram_message_id=1, message_text="b1", telegram_date=timezone.now()
        )

    def _results(self, response):
        data = response.json()
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def test_list_all_messages(self):
        response = self.client.get("/api/messages/")
        self.assertEqual(len(self._results(response)), 4)

    def test_filter_by_source(self):
        response = self.client.get(f"/api/messages/?source_id={self.source_a.id}")
        results = self._results(response)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["source"] == self.source_a.id for r in results))

    def test_after_id_returns_only_newer_messages(self):
        first_id = TelegramMessage.objects.filter(source=self.source_a).order_by("id").first().id
        response = self.client.get(f"/api/messages/?source_id={self.source_a.id}&after_id={first_id}")
        results = self._results(response)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["id"] > first_id for r in results))
