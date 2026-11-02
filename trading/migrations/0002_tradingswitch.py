from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("trading", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="TradingSwitch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.CharField(max_length=16, unique=True)),
                ("enabled", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
