import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TelegramSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(blank=True, max_length=255)),
                ("telegram_id", models.BigIntegerField(unique=True)),
                ("username", models.CharField(blank=True, max_length=255, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="TelegramMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("telegram_message_id", models.BigIntegerField()),
                ("sender_id", models.BigIntegerField(blank=True, null=True)),
                ("sender_name", models.CharField(blank=True, max_length=255)),
                ("sender_username", models.CharField(blank=True, max_length=255)),
                ("message_text", models.TextField(blank=True)),
                ("reply_to_message_id", models.BigIntegerField(blank=True, null=True)),
                ("telegram_date", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="telegram_monitor.telegramsource",
                    ),
                ),
            ],
            options={"ordering": ["-telegram_date"]},
        ),
        migrations.AddIndex(
            model_name="telegrammessage",
            index=models.Index(fields=["source", "telegram_message_id"], name="telegram_mo_source__f3c1e1_idx"),
        ),
        migrations.AddConstraint(
            model_name="telegrammessage",
            constraint=models.UniqueConstraint(
                fields=("source", "telegram_message_id"), name="unique_source_telegram_message"
            ),
        ),
    ]
