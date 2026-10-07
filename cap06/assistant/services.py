"""Domande e risposte: creazione dei messaggi e generazione (con streaming).

Ogni domanda crea due messaggi: quello dell'utente e quello, vuoto, dell'assistente («in attesa»). La generazione
parte quando il browser apre il flusso: il messaggio viene «prenotato» in modo atomico, così una seconda scheda o un
ricaricamento non generano due volte (e non si paga due volte).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.services import audit
from documents import permissions
from documents.models import Document

from . import prompts, retrieval
from .llm import AssistenteNonDisponibile, get_llm
from .models import Conversation, Message

logger = logging.getLogger(__name__)

GENERAZIONE_SCADUTA = timedelta(minutes=3)


class DomandaNonValida(Exception):
    """Messaggio adatto all'utente."""


def configurato() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


def _controlla_limite(utente) -> None:
    ultima_ora = timezone.now() - timedelta(hours=1)
    n = Message.objects.filter(conversation__owner=utente, role=Message.Role.USER, created_at__gte=ultima_ora).count()
    if n >= settings.AI_MAX_QUESTIONS_PER_HOUR:
        raise DomandaNonValida(
            f"Hai raggiunto il limite di {settings.AI_MAX_QUESTIONS_PER_HOUR} domande all'ora: riprova più tardi."
        )


def documento_interrogabile(utente, documento: Document) -> bool:
    return documento.ai_allowed and permissions.can_view(utente, documento)


def nuova_conversazione(*, utente, domanda: str, documento: Document | None = None, categoria=None) -> Conversation:
    if documento is not None and not documento_interrogabile(utente, documento):
        raise DomandaNonValida("Questo documento non è disponibile per l'assistente.")
    domanda = _pulisci(domanda)
    _controlla_limite(utente)
    with transaction.atomic():
        c = Conversation.objects.create(owner=utente, title=domanda[:80], document=documento,
                                         category=categoria if documento is None else None)
        _aggiungi(c, domanda)
    return c


def fai_domanda(*, conversazione: Conversation, domanda: str) -> tuple[Message, Message]:
    """Aggiunge una domanda a una conversazione esistente. Ritorna (messaggio dell'utente, messaggio vuoto dell'assistente)."""
    domanda = _pulisci(domanda)
    _scadute()
    if conversazione.messages.filter(status__in=[Message.Status.WAITING, Message.Status.GENERATING]).exists():
        raise DomandaNonValida("Aspetta la fine della risposta precedente.")
    _controlla_limite(conversazione.owner)
    if conversazione.document is not None and not documento_interrogabile(conversazione.owner, conversazione.document):
        raise DomandaNonValida("Questo documento non è più disponibile per l'assistente.")
    with transaction.atomic():
        coppia = _aggiungi(conversazione, domanda)
        conversazione.save(update_fields=["updated_at"])
    return coppia


def _pulisci(domanda: str) -> str:
    domanda = " ".join((domanda or "").split())
    if not domanda:
        raise DomandaNonValida("Scrivi una domanda.")
    if len(domanda) > settings.AI_MAX_QUESTION_CHARS:
        raise DomandaNonValida(f"La domanda è troppo lunga (massimo {settings.AI_MAX_QUESTION_CHARS} caratteri).")
    return domanda


def _aggiungi(conversazione: Conversation, domanda: str) -> tuple[Message, Message]:
    utente = Message.objects.create(conversation=conversazione, role=Message.Role.USER, content=domanda)
    risposta = Message.objects.create(
        conversation=conversazione, role=Message.Role.ASSISTANT, status=Message.Status.WAITING
    )
    return utente, risposta


def _scadute() -> None:
    """Una risposta «in generazione» (o mai avviata) da troppo tempo è rimasta orfana: diventa un errore."""
    limite = timezone.now() - GENERAZIONE_SCADUTA
    Message.objects.filter(status=Message.Status.GENERATING, started_at__lt=limite).update(
        status=Message.Status.ERROR, error="La risposta si è interrotta. Riprova a fare la domanda."
    )
    Message.objects.filter(status=Message.Status.WAITING, created_at__lt=limite).update(
        status=Message.Status.ERROR, error="La risposta non è partita. Riprova a fare la domanda."
    )


# --------------------------------------------------------------------------
# Generazione
# --------------------------------------------------------------------------

def genera(messaggio_id: int, utente) -> Iterator[tuple[str, object]]:
    """Genera la risposta del messaggio indicato. Eventi: ("stato", testo), ("testo", pezzo), ("fine", None),
    ("errore", testo), ("altrove", None) se la sta già generando qualcun altro o è finita."""
    _scadute()
    prenotato = Message.objects.filter(
        pk=messaggio_id, conversation__owner=utente, role=Message.Role.ASSISTANT, status=Message.Status.WAITING
    ).update(status=Message.Status.GENERATING, started_at=timezone.now())
    if not prenotato:
        yield ("altrove", None)
        return
    msg = Message.objects.select_related("conversation", "conversation__document").get(pk=messaggio_id)
    conv = msg.conversation
    testo = ""
    terminato = False
    try:
        domanda_msg = conv.messages.filter(role=Message.Role.USER, pk__lt=msg.pk).order_by("-pk").first()
        domanda = domanda_msg.content
        documento = conv.document
        if documento is not None and not documento_interrogabile(conv.owner, documento):
            raise AssistenteNonDisponibile("Questo documento non è più disponibile per l'assistente.")
        if not configurato():
            raise AssistenteNonDisponibile("L'assistente non è configurato: manca la chiave API.")

        precedenti = list(
            conv.messages.filter(pk__lt=domanda_msg.pk, status=Message.Status.DONE).order_by("pk")
        )
        storia = prompts.storia(precedenti)
        llm = get_llm()
        yield ("stato", "Cerco nei documenti…")
        piano = llm.piano(domanda, storia)
        trovato = retrieval.trova_passaggi(conv.owner, domanda, piano, documento, conv.category)
        passaggi, occorrenze = trovato.passaggi, trovato.occorrenze

        if not passaggi:
            testo = _niente_da_dire(conv.owner, documento)
            yield ("testo", testo)
            _chiudi(msg, conv, testo, [], {}, ok=True, occorrenze=_occorrenze(occorrenze))
            terminato = True
            yield ("fine", None)
            return

        yield ("stato", "Scrivo la risposta…")
        messaggi = storia + [{"role": "user", "content": prompts.messaggio_utente(domanda, passaggi, occorrenze)}]
        uso: dict = {}
        for tipo, valore in llm.stream(system=prompts.SISTEMA, messages=messaggi):
            if tipo == "testo":
                testo += valore
                yield ("testo", valore)
            else:
                uso = valore
        if uso.get("stop") == "max_tokens":
            extra = "\n\n_(Risposta interrotta perché troppo lunga: chiedi di proseguire o fai una domanda più mirata.)_"
            testo += extra
            yield ("testo", extra)
        if not testo.strip():
            testo = "Non sono riuscito a formulare una risposta a questa domanda."
            yield ("testo", testo)
        fonti = [
            {"n": p.n, "document_id": p.document_id, "title": p.titolo, "version": p.versione, "text": p.testo}
            for p in passaggi
        ]
        _chiudi(msg, conv, testo, fonti, uso, ok=True, occorrenze=_occorrenze(occorrenze))
        terminato = True
        yield ("fine", None)
    except AssistenteNonDisponibile as errore:
        _chiudi(msg, conv, testo, [], {}, ok=False, errore=str(errore))
        terminato = True
        yield ("errore", str(errore))
    except GeneratorExit:
        # Il browser ha chiuso la connessione (pagina chiusa): quello che c'è si salva, senza risposta finta.
        if not terminato:
            _chiudi(msg, conv, testo, [], {}, ok=False, errore="Risposta interrotta.")
        raise
    except Exception:
        logger.exception("Errore imprevisto nella risposta del messaggio %s", messaggio_id)
        _chiudi(msg, conv, testo, [], {}, ok=False, errore="Qualcosa è andato storto. Riprova.")
        terminato = True
        yield ("errore", "Qualcosa è andato storto. Riprova.")


def _niente_da_dire(utente, documento) -> str:
    if documento is not None:
        return "Questo documento non ha ancora testo indicizzato: riprova fra poco."
    if not retrieval.documenti_interrogabili(utente).exists():
        return (
            "Non c'è nessun documento che l'assistente possa leggere. Il consenso si dà per documento, "
            "dalla pagina del documento (voce «Assistente IA»)."
        )
    return (
        "Non ho trovato nei tuoi documenti passaggi pertinenti a questa domanda. "
        "Prova a riformularla con altre parole."
    )


def _occorrenze(occorrenze) -> list[dict]:
    return [
        {"term": o.termine, "total": o.totale, "documents": [{"id": pk, "title": titolo} for pk, titolo in o.documenti]}
        for o in occorrenze
    ]


def _chiudi(
    msg: Message, conv: Conversation, testo: str, fonti: list, uso: dict, *, ok: bool, errore: str = "", occorrenze=None
) -> None:
    with transaction.atomic():
        msg.content = testo
        msg.sources = fonti
        msg.occurrences = occorrenze or []
        msg.status = Message.Status.DONE if ok else Message.Status.ERROR
        msg.error = errore
        msg.model = settings.AI_MODEL if fonti else ""
        msg.input_tokens = uso.get("input")
        msg.output_tokens = uso.get("output")
        msg.save()
        conv.save(update_fields=["updated_at"])
        if ok and fonti:
            titoli = list(dict.fromkeys([f["title"] for f in fonti] + [d["title"] for o in (occorrenze or []) for d in o["documents"]]))
            audit.record(
                "ai.question", user=conv.owner, obj=conv, documenti=titoli, modello=settings.AI_MODEL,
                token_in=uso.get("input"), token_out=uso.get("output"),
            )
