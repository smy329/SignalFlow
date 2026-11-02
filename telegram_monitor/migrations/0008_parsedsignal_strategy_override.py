from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("telegram_monitor", "0007_source_reversal_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="parsedsignal",
            name="strategy_override",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
