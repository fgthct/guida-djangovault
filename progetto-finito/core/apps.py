from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        from . import signals  # noqa: F401  (collega i segnali di accesso al registro)
