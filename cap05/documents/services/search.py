"""Ricerca nei documenti.

Ricerca per parole (full-text di PostgreSQL, italiano senza accenti): titolo, descrizione, nome del file
e testo estratto della versione CORRENTE. Si parte sempre da un queryset già filtrato da
`Document.objects.visible_to(user)`: la ricerca non allarga mai i permessi, e nemmeno gli estratti
mostrati possono contenere testo di documenti che l'utente non vede.

Sintassi dell'utente (websearch di PostgreSQL, non può mai dare errore di sintassi):
«frase esatta», -esclusa, a OR b.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.db.models import Case, F, FloatField, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Left
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from ..models import Document

MAX_QUERY = 200  # caratteri: oltre non ha senso e costa
# ts_headline legge tutto il testo che riceve: si limita alle prime pagine, il resto resta ricercabile ma senza estratto.
MAX_TESTO_ESTRATTO = 100_000
# Caratteri d'uso privato come segnaposto dell'evidenziazione: poi il testo viene ESCAPATO e solo dopo
# i segnaposto diventano <mark>, così nessun contenuto del documento può iniettare HTML.
INIZIO, FINE = "", ""


_CONTROLLO = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ue000\ue001]")


def normalizza(testo: str) -> str:
    """Toglie i caratteri di controllo (PostgreSQL rifiuta NUL: sarebbe un errore 500 da `?q=%00`), compatta gli spazi, limita."""
    return " ".join(_CONTROLLO.sub(" ", testo or "").split())[:MAX_QUERY]


def _query(testo: str) -> SearchQuery:
    return SearchQuery(testo, config=settings.SEARCH_CONFIG, search_type="websearch")


def cerca_parole(documenti: QuerySet[Document], testo: str) -> QuerySet[Document]:
    """Filtra e ordina per pertinenza. `documenti` deve già essere limitato ai documenti visibili."""
    testo = normalizza(testo)
    if not testo:
        return documenti
    query = _query(testo)
    return (
        documenti.annotate(
            rank_doc=SearchRank(F("search_vector"), query),
            rank_testo=SearchRank(F("current_version__search_vector"), query),
            bonus_titolo=Case(
                When(title__icontains=testo, then=Value(0.4)), default=Value(0.0), output_field=FloatField()
            ),
        )
        .filter(
            Q(search_vector=query)
            | Q(current_version__search_vector=query)
            # Anche la corrispondenza parziale ("contr" trova "contratto"): è ciò che serve digitando.
            | Q(title__icontains=testo)
            | Q(description__icontains=testo)
            | Q(current_version__original_name__icontains=testo)
        )
        .annotate(punteggio=F("rank_doc") * 2 + F("rank_testo") + F("bonus_titolo"))
        .order_by("-punteggio", "-updated_at", "-id")
    )


def _html_estratto(grezzo: str) -> SafeString | None:
    if INIZIO not in grezzo:
        return None  # nessuna corrispondenza nel testo (es. trovato per titolo): niente estratto
    pulito = " ".join(grezzo.split())
    html = escape(pulito).replace(INIZIO, "<mark>").replace(FINE, "</mark>")
    return mark_safe(html)


def aggiungi_estratti(documenti: list[Document], testo: str) -> None:
    """Imposta `d.estratto` (HTML sicuro con <mark>) sui documenti della pagina. Una sola query, solo per loro."""
    testo = normalizza(testo)
    if not testo or not documenti:
        return
    intestazione = SearchHeadline(
        Left("current_version__extracted_text", MAX_TESTO_ESTRATTO),
        _query(testo),
        config=settings.SEARCH_CONFIG,
        start_sel=INIZIO,
        stop_sel=FINE,
        min_words=12,
        max_words=34,
        max_fragments=2,
        fragment_delimiter=" … ",
    )
    righe = (
        Document.objects.filter(pk__in=[d.pk for d in documenti])
        .annotate(estratto_grezzo=intestazione)
        .values_list("pk", "estratto_grezzo")
    )
    per_pk = {pk: _html_estratto(grezzo or "") for pk, grezzo in righe}
    for d in documenti:
        d.estratto = per_pk.get(d.pk)


# --------------------------------------------------------------------------
# Ricerca per significato (pgvector) e ricerca combinata
# --------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402
import functools  # noqa: E402

from django.db import connection  # noqa: E402
from pgvector.django import CosineDistance  # noqa: E402

from ..embeddings import get_embedder  # noqa: E402
from ..models import DocumentChunk  # noqa: E402

MAX_PASSAGGI = 80  # passaggi più vicini presi in esame per ogni domanda
CANDIDATI_PAROLE = 100  # nella ricerca combinata: quanti risultati per parole entrano nella fusione
K_FUSIONE = 60  # costante della Reciprocal Rank Fusion (valore standard)
LUNGHEZZA_ESTRATTO = 260

MODI = {
    "entrambi": "Parole e significato",
    "parole": "Solo parole",
    "significato": "Solo significato",
}
MODO_PREDEFINITO = "entrambi"


@dataclass
class RisultatoSemantico:
    document_id: int
    punteggio: float  # similarità del coseno del passaggio migliore
    testo: str  # quel passaggio


@functools.lru_cache(maxsize=1)
def _scansione_iterativa_disponibile() -> bool:
    """pgvector ≥ 0.8 ha le scansioni iterative dell'indice HNSW."""
    with connection.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        riga = cur.fetchone()
    if not riga:
        return False
    versione = tuple(int(x) for x in riga[0].split(".")[:2])
    return versione >= (0, 8)


def cerca_significato(documenti: QuerySet[Document], testo: str) -> list[RisultatoSemantico]:
    """Documenti il cui passaggio migliore è vicino per significato alla domanda, dal più al meno pertinente.

    `documenti` DEVE essere già limitato ai documenti visibili: sono gli unici passaggi presi in esame, quindi un
    documento non visibile non può comparire né far comparire il proprio testo. Si cerca solo nella versione corrente.
    """
    testo = normalizza(testo)
    if not testo:
        return []
    vettore = get_embedder().domanda(testo)
    versioni = documenti.exclude(current_version__isnull=True).order_by().values("current_version_id")
    from django.db import transaction

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL hnsw.ef_search = 100")
            if _scansione_iterativa_disponibile():
                # Il filtro sui permessi viene applicato dopo la scansione dell'indice: senza questa opzione un
                # utente che vede pochi documenti potrebbe ricevere pochi risultati o nessuno.
                cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        righe = list(
            DocumentChunk.objects.filter(version_id__in=versioni)
            .annotate(distanza=CosineDistance("embedding", vettore))
            .order_by("distanza")
            .values_list("version__document_id", "distanza", "text")[:MAX_PASSAGGI]
        )
    migliori: dict[int, RisultatoSemantico] = {}
    for doc_id, distanza, passaggio in righe:
        punteggio = 1 - float(distanza)
        if punteggio < settings.SEARCH_SEMANTIC_MIN_SCORE:
            break  # sono ordinati: i successivi sono ancora meno pertinenti
        migliori.setdefault(doc_id, RisultatoSemantico(doc_id, punteggio, passaggio))
    return list(migliori.values())


def chunk_vicini(versioni, vettore, limite: int) -> list[tuple[int, int, int, int, str, float]]:
    """I `limite` passaggi più vicini a `vettore` fra quelli delle versioni indicate (queryset di id di versione).

    Ritorna (id passaggio, id documento, numero versione, posizione, testo, similarità). Chi chiama decide quali
    versioni sono lecite: qui non si applica alcun permesso.
    """
    from django.db import transaction

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL hnsw.ef_search = 100")
            if _scansione_iterativa_disponibile():
                cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        righe = list(
            DocumentChunk.objects.filter(version_id__in=versioni)
            .annotate(distanza=CosineDistance("embedding", vettore))
            .order_by("distanza")
            .values_list("pk", "version__document_id", "version__version_number", "position", "text", "distanza")[:limite]
        )
    return [(pk, doc, ver, pos, testo, 1 - float(d)) for pk, doc, ver, pos, testo, d in righe]


def fondi(per_parole: list[int], semantici: list[RisultatoSemantico]) -> list[int]:
    """Reciprocal Rank Fusion: un documento in alto in entrambe le liste sale; basta una lista per comparire."""
    punti: dict[int, float] = {}
    for posizione, pk in enumerate(per_parole):
        punti[pk] = punti.get(pk, 0.0) + 1 / (K_FUSIONE + posizione + 1)
    for posizione, r in enumerate(semantici):
        punti[r.document_id] = punti.get(r.document_id, 0.0) + 1 / (K_FUSIONE + posizione + 1)
    return sorted(punti, key=lambda pk: (-punti[pk], pk))


def in_ordine(documenti: QuerySet[Document], ordine: list[int]) -> QuerySet[Document]:
    """Restringe ai documenti indicati e li ordina come nell'elenco (che è già il risultato della fusione)."""
    if not ordine:
        return documenti.none()
    posizione = Case(
        *[When(pk=pk, then=Value(i)) for i, pk in enumerate(ordine)],
        output_field=IntegerField(),
    )
    return documenti.filter(pk__in=ordine).annotate(posizione=posizione).order_by("posizione")


def candidati_parole(documenti: QuerySet[Document], testo: str) -> list[int]:
    return list(cerca_parole(documenti, testo).values_list("pk", flat=True)[:CANDIDATI_PAROLE])


def estratto_semantico(passaggio: str) -> SafeString:
    """Passaggio di testo semplice (senza evidenziazione), accorciato e con l'HTML neutralizzato."""
    pulito = " ".join(passaggio.split())
    if len(pulito) > LUNGHEZZA_ESTRATTO:
        pulito = pulito[: LUNGHEZZA_ESTRATTO].rsplit(" ", 1)[0] + " …"
    return mark_safe(escape(pulito))


def non_indicizzati(documenti: QuerySet[Document]) -> int:
    """Quanti documenti (fra quelli indicati) non sono ancora ricercabili per significato, ma lo diventeranno."""
    from ..models import DocumentVersion as V

    modello = settings.EMBEDDING_MODEL
    in_lavorazione = Q(current_version__processing_status__in=[V.Processing.PENDING, V.Processing.RUNNING]) | Q(
        current_version__processing_status=V.Processing.DONE
    )
    return (
        documenti.filter(in_lavorazione, current_version__isnull=False)
        .exclude(current_version__indexed_model=modello)
        .count()
    )
