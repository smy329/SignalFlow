from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("trading", "0003_trade_strategy_and_entries")]

    operations = [
        migrations.AddField(
            model_name="trade",
            name="testnet",
            field=models.BooleanField(default=False),
        ),
    ]
