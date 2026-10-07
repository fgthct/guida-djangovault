"""Task Celery del documentale. Sottili: la logica sta in `services/processing.py`."""

import logging

from celery import shared_task
from django.conf import settings

from .services import classificazione, indexing, ingestione, processing

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    name="documents.elabora_versione",
    max_retries=settings.PROCESSING_MAX_ATTEMPTS,
)
def elabora_versione_task(self, version_id: int) -> str:
    esito = processing.elabora_versione(version_id)
    if esito == processing.RIPROVA:
        # Attesa crescente: 30 s, 60 s, 90 s…
        raise self.retry(countdown=30 * (self.request.retries + 1))
    return esito


@shared_task(name="documents.riprendi_elaborazioni")
def riprendi_elaborazioni_task() -> int:
    return processing.riprendi_elaborazioni()


@shared_task(name="documents.indicizza_versione")
def indicizza_versione_task(version_id: int) -> str:
    """Passaggi e vettori di una versione. Un errore (per esempio modello non scaricabile) non viene ritentato
    subito: ci pensa il controllo periodico, con una pausa, così non si martella la rete."""
    try:
        return indexing.indicizza_versione(version_id)
    except Exception:
        logger.exception("Indicizzazione della versione %s non riuscita", version_id)
        return "errore"


@shared_task(name="documents.riprendi_indicizzazioni")
def riprendi_indicizzazioni_task() -> int:
    return indexing.riprendi_indicizzazioni()


@shared_task(name="documents.classifica_documento")
def classifica_documento_task(document_id: int) -> str:
    """Propone categoria, riassunto e parole chiave. Un errore non è ritentato subito: ripensa a tutto il controllo periodico."""
    try:
        sug = classificazione.classifica_documento(document_id)
    except Exception:
        logger.exception("Classificazione del documento %s non riuscita", document_id)
        return "errore"
    return sug.status if sug is not None else "saltato"


@shared_task(name="documents.riprendi_classificazioni")
def riprendi_classificazioni_task() -> int:
    return classificazione.riprendi_classificazioni()


@shared_task(name="documents.osserva_cartella")
def osserva_cartella_task() -> int:
    """Importa ciò che è arrivato nella cartella osservata."""
    try:
        r = ingestione.osserva_cartella()
    except ingestione.IngestioneNonConfigurata as e:
        logger.warning("Cartella osservata: %s", e)
        return 0
    return r.get("importati", 0)


@shared_task(name="documents.leggi_posta")
def leggi_posta_task() -> int:
    """Legge la casella email dedicata e importa gli allegati dei mittenti ammessi."""
    try:
        r = ingestione.leggi_posta()
    except ingestione.IngestioneNonConfigurata as e:
        logger.warning("Posta: %s", e)
        return 0
    return r.get("documenti", 0)
