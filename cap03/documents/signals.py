from django.db.models.signals import post_save
from django.dispatch import receiver

from core import realtime

from .models import DocumentVersion

# Salvataggi che cambiano ciò che l'utente vede nel blocco «elaborazione» (non il solo battito di «ancora vivo»).
_CAMPI_VISIBILI = {"processing_status", "processing_message", "indexed_at", "indexed_model"}


@receiver(post_save, sender=DocumentVersion)
def versione_salvata(sender, instance, created, update_fields, **kwargs):
    if created or update_fields is None or _CAMPI_VISIBILI & set(update_fields):
        realtime.versione_cambiata(instance.document_id)
