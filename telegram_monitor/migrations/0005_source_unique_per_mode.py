from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("telegram_monitor", "0004_telegramsource_mode")]

    operations = [
        migrations.AddConstraint(
            model_name="telegramsource",
            constraint=models.UniqueConstraint(fields=("telegram_id", "mode"), name="unique_source_telegram_id_per_mode"),
        ),
        migrations.RemoveField(model_name="telegramsource", name="auto_trade"),
    ]
