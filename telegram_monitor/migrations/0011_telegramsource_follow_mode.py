from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("telegram_monitor", "0010_rename_after_tp"),
    ]

    operations = [
        migrations.AddField(
            model_name="telegramsource",
            name="follow_mode",
            field=models.CharField(
                blank=True, default=None, max_length=20, null=True,
                choices=[("SIGNAL_DIRECTION", "According to signal's direction"), ("OPPOSITE_DIRECTION", "Opposite to signal's direction")],
            ),
        ),
    ]
