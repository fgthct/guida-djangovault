"""Consenso all'assistente IA per documento.

Con il consenso, i passaggi pertinenti del documento possono essere inviati all'API di Anthropic per rispondere
alle domande di chi può vedere il documento. Senza, il documento non viene mai inviato. Lo decide chi lo amministra,
in qualunque stato sia il documento (non è un contenuto: è una scelta di privacy).
"""

from django.db import transaction

from core.services import audit

from ..models import Document


def set_ai_consent(*, user, document: Document, allowed: bool) -> bool:
    """Imposta il consenso. Ritorna True se è cambiato qualcosa (e solo allora lo registra)."""
    with transaction.atomic():
        d = Document.objects.select_for_update().get(pk=document.pk)
        if d.ai_allowed == allowed:
            return False
        d.ai_allowed = allowed
        d.save(update_fields=["ai_allowed"])  # niente updated_at: il contenuto non è cambiato
        audit.record("document.ai_consent_changed", user=user, obj=d, consentito=allowed)
    document.ai_allowed = allowed
    return True
