from django.db import migrations


def rename(apps, schema_editor):
    apps.get_model("telegram_monitor", "TelegramSource").objects.filter(sl_mode="AFTER_TP").update(sl_mode="AFTER_FILLING_TP")


def restore(apps, schema_editor):
    apps.get_model("telegram_monitor", "TelegramSource").objects.filter(sl_mode="AFTER_FILLING_TP").update(sl_mode="AFTER_TP")


class Migration(migrations.Migration):
    """Stop-loss mode AFTER_TP is now called AFTER_FILLING_TP. Existing sources keep every stored value (as overrides)."""

    dependencies = [("telegram_monitor", "0009_source_overrides_schema")]
    operations = [migrations.RunPython(rename, restore)]
