import contextlib

from django.apps import AppConfig


class WabaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "separator.waba"

    def ready(self):
        with contextlib.suppress(ImportError):
            import separator.waba.signals  # noqa: F401
