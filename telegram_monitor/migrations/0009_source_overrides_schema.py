from django.core.validators import MaxValueValidator  # noqa: F401
from django.db import migrations, models


class Migration(migrations.Migration):
    """Every strategy column of a source becomes an optional override (NULL = use the world's global value)."""

    dependencies = [("telegram_monitor", "0008_parsedsignal_strategy_override")]

    operations = [
        migrations.AddField("telegramsource", "leverage", models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, default=None)),
        migrations.AddField("telegramsource", "margin", models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True, default=None)),
        migrations.AddField("telegramsource", "margin_type", models.CharField(max_length=30, null=True, blank=True, default=None)),
        migrations.AlterField(
            "telegramsource", "entry_mode",
            models.CharField(max_length=8, null=True, blank=True, default=None,
                             choices=[("NEAREST", "Nearest entry"), ("FARTHEST", "Farthest entry"), ("BOTH", "Both entries")]),
        ),
        migrations.AlterField(
            "telegramsource", "sl_mode",
            models.CharField(max_length=20, null=True, blank=True, default=None,
                             choices=[("TP1_TOUCH", "When price touches TP1"), ("AFTER_FILLING_TP", "After a chosen TP is filled")]),
        ),
        migrations.AlterField("telegramsource", "sl_after_tp", models.PositiveSmallIntegerField(null=True, blank=True, default=None)),
        migrations.AlterField(
            "telegramsource", "reversal_mode",
            models.CharField(max_length=8, null=True, blank=True, default=None,
                             choices=[("ONE_WAY", "One-Way Mode"), ("IGNORE", "Ignore"), ("HEDGE", "Hedge Mode (upcoming)")]),
        ),
    ]
