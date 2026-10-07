"""Recupero dei passaggi da dare al modello.

Si parte SEMPRE da `documenti_interrogabili`: documenti che l'utente può leggere (permessi) E per i quali c'è il
consenso all'assistente IA. Un passaggio di un altro documento non viene nemmeno esaminato, quindi non può finire
nel prompt, e quindi non può essere inviato ad Anthropic.

Due strade che si fondono (Reciprocal Rank Fusion):
- per significato: i passaggi più vicini alla domanda e alle sue riformulazioni;
- per termini: se la domanda nomina qualcosa alla lettera («Marco Ferrante», «Israele»), si cercano i documenti che
  lo contengono e, di ciascuno, il passaggio più pertinente fra quelli che lo citano. Serve alle domande «dove compare
  X», in cui il significato non basta: la risposta sta in molti documenti, uno per volta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings

from documents.embeddings import get_embedder
from documents.models import Document, DocumentChunk
from documents.services import search
from pgvector.django import CosineDistance

from .llm import Piano

CANDIDATI_PER_QUERY = 30
K_FUSIONE = search.K_FUSIONE
MAX_TERMINI = 3

_MAIUSCOLA = r"[A-ZÀ-ÖØ-Þ]"
_PAROLA = rf"{_MAIUSCOLA}[\wà-öø-ÿ'’-]*"
_NOME = re.compile(rf"(?<![\wà-öø-ÿ]){_PAROLA}(?:\s+(?:(?:di|de|del|della|dei|da|van|von|d')\s*)?{_PAROLA})*")


@dataclass
class Passaggio:
    n: int
    document_id: int
    titolo: str
    versione: int
    posizione: int
    testo: str
    punteggio: float


@dataclass
class Occorrenza:
    """Dove compare alla lettera un nome o un termine della domanda."""

    termine: str
    totale: int  # documenti in cui compare (può superare quelli elencati)
    documenti: list[tuple[int, str]] = field(default_factory=list)  # (id, titolo), i più pertinenti per primi


@dataclass
class Recupero:
    passaggi: list[Passaggio] = field(default_factory=list)
    occorrenze: list[Occorrenza] = field(default_factory=list)


def documenti_interrogabili(utente, documento: Document | None = None, categoria=None):
    qs = Document.objects.visible_to(utente).filter(ai_allowed=True, current_version__isnull=False)
    if documento is not None:
        qs = qs.filter(pk=documento.pk)
    elif categoria is not None:
        qs = qs.filter(category=categoria)
    return qs


def termini_dalla_domanda(domanda: str) -> list[str]:
    """Nomi propri nella domanda («Marco Ferrante»), per quando il modello piccolo non li ha indicati.

    Una sola parola con la maiuscola vale solo se non è la prima della frase e non è breve.
    """
    trovati: list[str] = []
    for m in _NOME.finditer(domanda):
        testo = m.group(0).strip()
        parole = testo.split()
        if len(parole) == 1 and (m.start() == 0 or len(testo) < 4):
            continue
        if testo not in trovati:
            trovati.append(testo)
    return trovati


_TRATTINI = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212\u00ad"), "-")  # ‐ ‑ ‒ – — − e il trattino «morbido»


def _pulisci_termine(t: str) -> str:
    """Il modello può scrivere un trattino tipografico o virgolette: si riporta tutto a caratteri semplici."""
    t = search.normalizza(t.translate(_TRATTINI))
    return t.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ").strip("'\u2018\u2019 ")


def _schema_termine(termine: str) -> str | None:
    """Espressione regolare (Postgres) che trova il termine comunque sia scritto: «t-string», «t string», «T‑String»,
    «t-\\nstring»… I segni fra le parole valgono qualunque separatore; in coda si accetta il plurale/le desinenze."""
    parole = re.findall(r"[^\W_]+", termine)
    return r"[^[:alnum:]]+".join(parole) if parole else None


_CAMPI_PASSAGGIO = ("pk", "version__document_id", "version__version_number", "position", "text", "distanza", "version_id")
_MAX_PASSAGGI_ESAMINATI = 5000


def _migliori_per_versione(passaggi, vettore) -> list[tuple]:
    """Per ogni versione, il passaggio più vicino al vettore fra quelli di `passaggi`.

    Volutamente SENZA `order_by` sulla distanza: con un ordinamento per distanza Postgres può scegliere l'indice
    vettoriale (HNSW), che restituisce solo i primi candidati di TUTTA la tabella e applica il filtro dopo. Se i passaggi
    di questi documenti non sono fra i più vicini in assoluto, ne esce nulla. Qui i candidati sono già pochi e filtrati:
    si calcola la distanza su quelli e si sceglie in Python.
    """
    migliori: dict[int, tuple] = {}
    for r in passaggi.order_by().annotate(distanza=CosineDistance("embedding", vettore)).values_list(*_CAMPI_PASSAGGIO)[:_MAX_PASSAGGI_ESAMINATI]:
        attuale = migliori.get(r[6])
        if attuale is None or float(r[5]) < float(attuale[5]):
            migliori[r[6]] = r
    return list(migliori.values())


def _termini(domanda: str, piano: Piano) -> list[str]:
    elenco: list[str] = []
    for t in [*piano.termini, *termini_dalla_domanda(domanda)]:
        t = _pulisci_termine(t)
        if len(t) >= 3 and t.lower() not in {e.lower() for e in elenco}:
            elenco.append(t)
    return elenco[:MAX_TERMINI]


def trova_passaggi(utente, domanda: str, piano: Piano, documento: Document | None = None, categoria=None) -> Recupero:
    domanda = search.normalizza(domanda)
    if not domanda:
        return Recupero()  # una domanda vuota darebbe un vettore nullo (distanze NaN) e la ricerca per parole troverebbe tutto
    docs = documenti_interrogabili(utente, documento, categoria)
    versioni = docs.order_by().values("current_version_id")
    embedder = get_embedder()
    soglia = None if documento is not None else settings.AI_MIN_SCORE  # su un solo documento si risponde comunque

    dettagli: dict[int, tuple] = {}  # id passaggio -> (id, documento, versione, posizione, testo, similarità)
    liste: list[list[int]] = []
    da_termini: set[int] = set()  # passaggi scelti perché citano alla lettera un termine della domanda

    # -- per significato ------------------------------------------------------------------------
    interrogazioni = [domanda]
    for q in piano.query:
        q = search.normalizza(q)
        if q and q not in interrogazioni:
            interrogazioni.append(q)
    for q in interrogazioni[:4]:
        righe = search.chunk_vicini(versioni, embedder.domanda(q), CANDIDATI_PER_QUERY)
        validi = [r for r in righe if soglia is None or r[5] >= soglia]
        for r in validi:
            dettagli.setdefault(r[0], r)
        liste.append([r[0] for r in validi])

    # -- per termini ----------------------------------------------------------------------------
    occorrenze: list[Occorrenza] = []
    vettore_domanda = embedder.domanda(domanda)
    for termine in _termini(domanda, piano):
        schema = _schema_termine(termine)
        if schema is None:
            continue
        trovati_qs = search.cerca_parole(docs, f'"{termine}"')
        elenco = list(trovati_qs.values_list("pk", "title", "current_version_id")[: settings.AI_MAX_OCCURRENCES])
        if not elenco:
            continue
        if documento is None:
            occorrenze.append(
                Occorrenza(termine, trovati_qs.count(), [(pk, titolo) for pk, titolo, _ in elenco])
            )
        ordine = {v: i for i, (_, _, v) in enumerate(elenco)}
        righe = _migliori_per_versione(DocumentChunk.objects.filter(version_id__in=list(ordine), text__unaccent__iregex=schema), vettore_domanda)
        # La ricerca per parole ha trovato questi documenti: nessuno deve restare senza passaggio, nemmeno se il termine
        # è scritto in un modo che il confronto sopra non riconosce. Per i mancanti si prende il passaggio più vicino.
        trovati_v = {r[6] for r in righe}
        mancanti = [v for v in ordine if v not in trovati_v]
        if mancanti:
            righe += _migliori_per_versione(DocumentChunk.objects.filter(version_id__in=mancanti, embedding__isnull=False), vettore_domanda)
        righe.sort(key=lambda r: ordine[r[6]])
        for pk, doc, ver, pos, testo, distanza, _ in righe:
            dettagli.setdefault(pk, (pk, doc, ver, pos, testo, 1 - float(distanza)))
            da_termini.add(pk)
        liste.append([r[0] for r in righe])

    # -- fusione --------------------------------------------------------------------------------
    punti: dict[int, float] = {}
    for elenco in liste:
        for posizione, pid in enumerate(elenco):
            punti[pid] = punti.get(pid, 0.0) + 1 / (K_FUSIONE + posizione + 1)
    ordinati = sorted(punti, key=lambda pid: (-punti[pid], pid))
    if da_termini and documento is None:
        # La domanda nomina qualcosa che è nel testo: i passaggi «vicini» di documenti che non lo contengono sono solo
        # rumore (e il modello finirebbe per citarli). Restano i passaggi dei documenti che lo nominano davvero.
        con_termine = {dettagli[pid][1] for pid in da_termini}
        ordinati = [pid for pid in ordinati if dettagli[pid][1] in con_termine]

    scelti: list[int] = []
    per_documento: dict[int, int] = {}
    for pid in ordinati:
        doc_id = dettagli[pid][1]
        if per_documento.get(doc_id, 0) >= settings.AI_MAX_PER_DOCUMENT:
            continue
        per_documento[doc_id] = per_documento.get(doc_id, 0) + 1
        scelti.append(pid)
        if len(scelti) >= settings.AI_MAX_PASSAGES:
            break

    if soglia is not None:
        # Fra i passaggi trovati per significato, solo quelli vicini al migliore: in un archivio con molti testi sullo
        # stesso tema (libri, per esempio) gli altri darebbero al modello materiale per divagare. I passaggi che
        # citano alla lettera un termine della domanda restano sempre: è una prova più forte della somiglianza.
        semantici = [dettagli[pid][5] for pid in scelti if pid not in da_termini]
        if semantici:
            minimo = max(semantici) - settings.AI_SCORE_MARGIN
            scelti = [pid for pid in scelti if pid in da_termini or dettagli[pid][5] >= minimo]

    titoli = dict(Document.objects.filter(pk__in={dettagli[pid][1] for pid in scelti}).values_list("pk", "title"))
    passaggi = [
        Passaggio(
            n=i, document_id=dettagli[pid][1], titolo=titoli.get(dettagli[pid][1], ""), versione=dettagli[pid][2],
            posizione=dettagli[pid][3], testo=dettagli[pid][4], punteggio=dettagli[pid][5],
        )
        for i, pid in enumerate(scelti, 1)
    ]
    return Recupero(passaggi, occorrenze)
