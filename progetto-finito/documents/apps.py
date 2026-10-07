from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name = "documents"
    verbose_name = "Documenti"

    def ready(self):
        from . import signals  # noqa: F401  (avvisa in tempo reale quando cambia lo stato di una versione)
