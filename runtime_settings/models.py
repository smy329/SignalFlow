from django.db import models


class Setting(models.Model):
    """One stored setting value (JSON text). No row = the registry default.
    The catalogue of keys, types, defaults and limits is runtime_settings.registry."""

    key = models.CharField(max_length=100, primary_key=True)
    value = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key} = {self.value}"
