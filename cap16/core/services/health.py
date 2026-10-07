"""Controlli di stato dei servizi da cui dipende l'applicazione."""

from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache, caches
from django.db import connection
from django.db.models import Count


@dataclass(frozen=True)
class StatoServizio:
    nome: str
    ok: bool
    dettaglio: str


def stato_database() -> StatoServizio:
    try:
        with connection.cursor() as cur:
            cur.execute("SHOW server_version")
            versione = cur.fetchone()[0].split()[0]
        return StatoServizio("PostgreSQL", True, f"versione {versione}")
    except Exception as exc:  # qualunque errore di connessione va mostrato, non nascosto
        return StatoServizio("PostgreSQL", False, str(exc).splitlines()[0])


def stato_pgvector() -> StatoServizio:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            riga = cur.fetchone()
        if riga:
            return StatoServizio("pgvector", True, f"estensione {riga[0]}")
        return StatoServizio("pgvector", False, "estensione non installata")
    except Exception as exc:
        return StatoServizio("pgvector", False, str(exc).splitlines()[0])


def stato_cache() -> StatoServizio:
    try:
        cache.set("dv:health", "ok", 10)
        if cache.get("dv:health") != "ok":
            return StatoServizio("Cache", False, "scrittura non riletta")
        backend = caches["default"].__class__.__name__.replace("Cache", "")  # "cache" è un proxy
        return StatoServizio("Cache", True, f"backend {backend}")
    except Exception as exc:
        return StatoServizio("Cache", False, str(exc).splitlines()[0])


def _broker_reale() -> bool:
    return not settings.CELERY_BROKER_URL.startswith("memory://")


def stato_broker() -> StatoServizio:
    """Redis come coda dei task (database diverso da quello della cache)."""
    if not _broker_reale():
        return StatoServizio("Coda dei task", True, "in memoria (nessun Redis configurato)")
    try:
        import redis

        client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        in_attesa = client.llen("celery")
        return StatoServizio("Coda dei task", True, f"Redis raggiungibile · {in_attesa} task in attesa")
    except Exception as exc:
        return StatoServizio("Coda dei task", False, str(exc).splitlines()[0])


def stato_worker() -> StatoServizio:
    """Almeno un worker Celery deve rispondere; altrimenti i file restano «in coda»."""
    from documents.models import DocumentVersion

    conteggi = dict(DocumentVersion.objects.values_list("processing_status").annotate(n=Count("pk")))
    coda, corso, errori = conteggi.get("pending", 0), conteggi.get("running", 0), conteggi.get("failed", 0)
    riepilogo = f"{coda} in coda · {corso} in corso · {errori} in errore"
    if not _broker_reale():
        return StatoServizio("Elaborazione file", True, f"senza worker · {riepilogo}")
    risposta = cache.get("dv:worker-ping")
    if risposta is None:
        try:
            from config.celery import app

            risposta = len(app.control.inspect(timeout=1.0).ping() or {})
        except Exception:
            risposta = 0
        cache.set("dv:worker-ping", risposta, 10)  # la dashboard interroga ogni 30 s per ogni utente
    if risposta:
        return StatoServizio("Elaborazione file", True, f"{risposta} worker attivo · {riepilogo}")
    return StatoServizio("Elaborazione file", False, f"nessun worker attivo: i file restano in coda · {riepilogo}")


def stato_indice() -> StatoServizio:
    """Ricerca per significato: quante versioni sono indicizzate e quante no. «Non riuscite» = tentate da più di 10 minuti."""
    from django.utils import timezone

    from documents.models import DocumentChunk, DocumentVersion

    try:
        pronte = DocumentVersion.objects.filter(processing_status="done", indexed_model=settings.EMBEDDING_MODEL).count()
        mancanti = DocumentVersion.objects.filter(processing_status="done").exclude(indexed_model=settings.EMBEDDING_MODEL)
        recente = timezone.now() - DocumentVersion.INDICE_TENTATIVO_RECENTE
        fallite = mancanti.filter(index_attempted_at__lt=recente).count()
        in_attesa = mancanti.count() - fallite
        passaggi = DocumentChunk.objects.count()
    except Exception as exc:
        return StatoServizio("Indice per significato", False, str(exc).splitlines()[0])
    dettaglio = f"{pronte} versioni indicizzate · {passaggi} passaggi · {settings.EMBEDDING_MODEL.rsplit('/', 1)[-1]}"
    if in_attesa:
        dettaglio += f" · {in_attesa} in attesa"
    if fallite:
        return StatoServizio("Indice per significato", False, dettaglio + f" · {fallite} non riuscite (riprovo ogni 30 minuti)")
    return StatoServizio("Indice per significato", True, dettaglio)


def controlla_servizi() -> list[StatoServizio]:
    return [stato_database(), stato_pgvector(), stato_cache(), stato_broker(), stato_worker(), stato_indice()]
