from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0005_order_kind_market"),
    ]

    operations = [
        migrations.AddField(
            model_name="trade",
            name="exit_price",
            field=models.DecimalField(blank=True, decimal_places=15, max_digits=30, null=True),
        ),
    ]
