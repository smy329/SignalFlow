"""One-time interactive Telegram login (creates the session used by the listener)."""
import asyncio

from django.core.management.base import BaseCommand

from telegram_monitor.services.telegram_client import build_client


async def _login():
    client = build_client()
    try:
        await client.start()  # prompts for phone, code and 2FA password in this terminal
        return await client.get_me()
    finally:
        await client.disconnect()


class Command(BaseCommand):
    help = "Log in to Telegram once and save the session file. Run with the server stopped."

    def handle(self, *args, **options):
        me = asyncio.run(_login())
        name = getattr(me, "first_name", "") or getattr(me, "username", "") or "account"
        self.stdout.write(self.style.SUCCESS(f"Logged in as {name} (id={me.id}). Session saved."))
