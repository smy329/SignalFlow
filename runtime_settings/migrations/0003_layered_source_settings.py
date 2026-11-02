"""v2.12: Real trading and Forward test hold the full strategy option set.

* The old "Default source strategy" values are copied into BOTH Real trading and Forward test
  (only where the target has no stored value yet), then removed: that page no longer exists.
* The stop-loss mode value AFTER_TP is renamed AFTER_FILLING_TP in every stored setting.
"""
import json

from django.db import migrations

NAMES = ("entry_mode", "sl_mode", "sl_after_tp", "reversal_mode", "tp_split")


def forwards(apps, schema_editor):
    Setting = apps.get_model("runtime_settings", "Setting")
    for name in NAMES:
        old = Setting.objects.filter(key=f"strategy.{name}").first()
        if old is None:
            continue
        for world in ("real", "forward"):
            Setting.objects.get_or_create(key=f"{world}.{name}", defaults={"value": old.value})
    Setting.objects.filter(key__startswith="strategy.").delete()
    for row in Setting.objects.filter(key__endswith=".sl_mode"):
        if json.loads(row.value) == "AFTER_TP":
            row.value = json.dumps("AFTER_FILLING_TP")
            row.save(update_fields=["value"])


class Migration(migrations.Migration):
    dependencies = [("runtime_settings", "0002_seed_from_env")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
