"""Indicizzazione per significato: testo estratto → passaggi → vettori (pgvector).

Come l'elaborazione dei file (processing.py): il database è la fonte di verità. Una versione è indicizzata quando
`indexed_model` è il modello in uso; se il modello cambia, o l'indicizzazione non è mai riuscita, il pianificatore
la rimette in coda (con una pausa fra un tentativo e l'altro). Il lavoro pesante (CPU) avviene fuori dalle transazioni.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .. import chunking
from ..embeddings import get_embedder
from ..models import DocumentChunk, DocumentVersion
from . import classificazione

logger = logging.getLogger(__name__)
P = DocumentVersion.Processing

OK, SALTATA, VUOTA, CAMBIATA = "ok", "saltata", "vuota", "cambiata"
PAUSA_TENTATIVI = timedelta(minutes=30)


def _media(vettori):
    """Vettore del documento: media dei vettori dei passaggi (None se non ce ne sono)."""
    import numpy as np

    return np.mean(np.asarray(vettori, dtype=np.float32), axis=0) if len(vettori) else None


def modello_corrente() -> str:
    return settings.EMBEDDING_MODEL


def accoda_indicizzazione(version_id: int) -> None:
    """Chiede l'indicizzazione dopo il commit (da chiamare dentro la transazione che ha completato l'elaborazione)."""
    transaction.on_commit(lambda: _invia(version_id))


def _invia(version_id: int) -> bool:
    from ..tasks import indicizza_versione_task

    try:
        indicizza_versione_task.delay(version_id)
        return True
    except Exception:
        logger.warning("Broker non raggiungibile: la versione %s sarà indicizzata dal controllo periodico.", version_id, exc_info=True)
        return False


def indicizza_versione(version_id: int, *, forza: bool = False) -> str:
    """Indicizza una versione. Ritorna OK, SALTATA (già fatta o non pronta), VUOTA (nessun testo), CAMBIATA (riprovare)."""
    v = DocumentVersion.objects.filter(pk=version_id).first()
    if v is None or v.processing_status != P.DONE:
        return SALTATA  # eliminata, oppure il testo non è ancora pronto: l'elaborazione richiamerà l'indicizzazione
    modello = modello_corrente()
    if v.indexed_model == modello and not forza:
        return SALTATA
    DocumentVersion.objects.filter(pk=v.pk).update(index_attempted_at=timezone.now())

    testo = v.extracted_text
    passaggi, troncato = chunking.spezza(
        testo,
        dimensione=settings.SEARCH_CHUNK_CHARS,
        sovrapposizione=settings.SEARCH_CHUNK_OVERLAP,
        massimo_passaggi=settings.SEARCH_MAX_CHUNKS,
    )
    vettori = get_embedder().passaggi(passaggi)  # la parte lenta: nessuna transazione aperta

    with transaction.atomic():
        corrente = DocumentVersion.objects.select_for_update().filter(pk=v.pk).first()
        if corrente is None:
            return SALTATA
        if corrente.processed_at != v.processed_at:
            return CAMBIATA  # rielaborata nel frattempo: il testo è cambiato, lo rifarà l'indicizzazione successiva
        DocumentChunk.objects.filter(version=corrente).delete()
        DocumentChunk.objects.bulk_create(
            [DocumentChunk(version=corrente, position=i, text=t, embedding=e) for i, (t, e) in enumerate(zip(passaggi, vettori))],
            batch_size=200,
        )
        corrente.indexed_model = modello
        corrente.indexed_at = timezone.now()
        corrente.doc_vector = _media(vettori)
        corrente.save(update_fields=["indexed_model", "indexed_at", "doc_vector"])
        if corrente.document.current_version_id == corrente.pk:
            classificazione.accoda_se_richiesta(corrente.document_id)
    if troncato:
        logger.warning("Versione %s: testo molto lungo, indicizzati solo i primi %s passaggi.", v.pk, len(passaggi))
    logger.info("Versione %s indicizzata: %s passaggi (%s)", v.pk, len(passaggi), modello)
    return OK if passaggi else VUOTA


def da_indicizzare():
    """Versioni pronte (testo estratto) non ancora indicizzate con il modello in uso."""
    return DocumentVersion.objects.filter(processing_status=P.DONE).exclude(indexed_model=modello_corrente())


def riprendi_indicizzazioni(*, massimo: int = 50) -> int:
    """Rete di sicurezza periodica: rimette in coda le versioni non indicizzate, con una pausa fra i tentativi."""
    limite = timezone.now() - PAUSA_TENTATIVI
    ids = list(
        da_indicizzare()
        .filter(_non_tentata_di_recente(limite))
        .order_by("pk")
        .values_list("pk", flat=True)[:massimo]
    )
    if ids:
        DocumentVersion.objects.filter(pk__in=ids).update(index_attempted_at=timezone.now())
    inviati = sum(_invia(pk) for pk in ids)
    if ids:
        logger.info("Riprese %s indicizzazioni (%s inviate al broker)", len(ids), inviati)
    return len(ids)


def _non_tentata_di_recente(limite):
    from django.db.models import Q

    return Q(index_attempted_at__isnull=True) | Q(index_attempted_at__lt=limite)


class IndicizzazioneNonPossibile(Exception):
    pass


def richiedi_indicizzazione(*, user, version: DocumentVersion) -> DocumentVersion:
    """Rimette in coda l'indicizzazione di una versione (pulsante «Riprova»). Il testo deve essere pronto."""
    from core.services import audit

    with transaction.atomic():
        v = DocumentVersion.objects.select_for_update().select_related("document").get(pk=version.pk)
        if v.processing_status != P.DONE:
            raise IndicizzazioneNonPossibile("Il testo di questa versione non è ancora pronto.")
        if v.stato_indice == DocumentVersion.INDICE_CORSO:
            raise IndicizzazioneNonPossibile("L'indicizzazione è già in corso.")
        v.index_attempted_at = timezone.now()
        v.save(update_fields=["index_attempted_at"])
        audit.record("version.reindex_requested", user=user, obj=v.document, versione=v.version_number)
        accoda_indicizzazione(v.pk)
    return v
