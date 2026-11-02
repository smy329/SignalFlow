"""One-time import of the values that used to live in .env.

After this runs, the Settings page is the only source of truth: the old variables can be
deleted from .env. Variables that are absent are simply left at their defaults.
Skipped under `manage.py test`, so the test database always starts from the defaults
whatever the developer's .env says.
"""
import os

from django.conf import settings
from django.db import migrations


def seed(apps, schema_editor):
    if not getattr(settings, "RUNTIME_SETTINGS_SEED_FROM_ENV", True):
        return
    from runtime_settings import service

    model = apps.get_model("runtime_settings", "Setting")
    service.seed_from_env(model, os.environ)


class Migration(migrations.Migration):
    dependencies = [("runtime_settings", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
