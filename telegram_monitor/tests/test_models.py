from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from telegram_monitor.models import TelegramMessage, TelegramSource


class TelegramSourceModelTests(TestCase):
    def test_create_source(self):
        source = TelegramSource.objects.create(name="My Channel", telegram_id=-1001234567890)
        self.assertTrue(source.is_active)
        self.assertEqual(str(source), "My Channel")

    def test_str_falls_back_to_telegram_id_when_no_name(self):
        source = TelegramSource.objects.create(telegram_id=-1009999999999)
        self.assertEqual(str(source), "-1009999999999")

    def test_activation_toggle(self):
        source = TelegramSource.objects.create(telegram_id=-100111, is_active=True)
        source.is_active = False
        source.save()
        source.refresh_from_db()
        self.assertFalse(source.is_active)


class TelegramMessageModelTests(TestCase):
    def setUp(self):
        self.source = TelegramSource.objects.create(name="Group A", telegram_id=-100222)

    def test_create_message(self):
        msg = TelegramMessage.objects.create(
            source=self.source,
            telegram_message_id=1,
            sender_name="Alice",
            message_text="Hello",
            telegram_date=timezone.now(),
        )
        self.assertEqual(msg.source, self.source)

    def test_duplicate_message_raises_integrity_error(self):
        TelegramMessage.objects.create(
            source=self.source, telegram_message_id=42, message_text="first", telegram_date=timezone.now()
        )
        with self.assertRaises(IntegrityError):
            TelegramMessage.objects.create(
                source=self.source, telegram_message_id=42, message_text="dup", telegram_date=timezone.now()
            )
