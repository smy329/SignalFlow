from django.db import migrations, models


def split_sources_by_mode(apps, schema_editor):
    """auto_trade sources stay as the REAL source; every source also gets a FORWARD
    source (the original row, or a copy for auto_trade ones), because the forward
    test used to cover all sources."""
    Source = apps.get_model("telegram_monitor", "TelegramSource")
    for source in Source.objects.all():
        if source.auto_trade:
            source.mode = "REAL"
            source.save(update_fields=["mode"])
            Source.objects.create(
                name=source.name, telegram_id=source.telegram_id, username=source.username,
                is_active=source.is_active, mode="FORWARD",
            )
        else:
            source.mode = "FORWARD"
            source.save(update_fields=["mode"])


class Migration(migrations.Migration):
    dependencies = [("telegram_monitor", "0003_telegramsource_auto_trade")]

    operations = [
        migrations.AddField(
            model_name="telegramsource",
            name="mode",
            field=models.CharField(
                choices=[("REAL", "Real"), ("FORWARD", "Forward")], db_index=True, default="FORWARD", max_length=8
            ),
        ),
        migrations.AlterField(
            model_name="telegramsource",
            name="telegram_id",
            field=models.BigIntegerField(db_index=True),
        ),
        migrations.RunPython(split_sources_by_mode, migrations.RunPython.noop),
    ]
