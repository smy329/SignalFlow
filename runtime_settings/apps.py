from django.apps import AppConfig


class RuntimeSettingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "runtime_settings"
    verbose_name = "Settings"
