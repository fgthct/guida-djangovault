"""Revisione a più persone.

Il proprietario del documento sceglie chi deve rispondere (anche una sola persona) e se la
risposta è **in sequenza** (coercitiva: tocca a uno alla volta, nell'ordine scelto) oppure
**libera** (tutti vengono avvisati subito e rispondono nell'ordine che vogliono).

- Per approvare servono TUTTI i revisori. Se uno rimanda (con un motivo) il giro si chiude, il
  documento torna in Bozza al proprietario e le approvazioni già date decadono: un nuovo invio
  apre un nuovo giro.
- Il proprietario può ritirare il giro in corso (con un motivo).
- Durante la revisione il documento è bloccato (vedere `workflow.assicura_modificabile`), quindi
  le approvazioni valgono per la versione che il revisore ha davanti (`ReviewRound.version`).
- Chi viene scelto e non vede ancora il documento riceve la Lettura.
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from core.models import Notification
from core.services import audit
from core.services.notifications import notify

from .. import permissions
from ..models import Document, DocumentPermission, ReviewRound, ReviewStep
from .workflow import TransizioneNonValida

logger = logging.getLogger(__name__)
S = Document.Status
MAX_REVISORI = 10


def _url(document: Document) -> str:
    return reverse("documents:detail", args=[document.pk])


def _nome(user) -> str:
    return user.get_full_name() or user.get_username()


def _archivia_richieste(document: Document, utenti=None) -> None:
    """Segna come lette le richieste di revisione ormai evase (il bollino non deve restare acceso)."""
    richieste = Notification.objects.filter(kind="review.requested", url=_url(document), read_at__isnull=True)
    if utenti is not None:
        richieste = richieste.filter(recipient__in=utenti)
    richieste.update(read_at=timezone.now())


def _cambia_stato(user, document: Document, nuovo: str, nota: str = "") -> None:
    precedente = document.status
    document.status = nuovo
    document.save(update_fields=["status", "updated_at"])
    audit.record("document.status_changed", user=user, obj=document, da=precedente, a=nuovo, nota=nota)


def giro_in_corso(document: Document) -> ReviewRound | None:
    return document.review_rounds.filter(outcome=ReviewRound.Outcome.PENDING).first()


def start_review(
    *, user, document: Document, reviewers: list[tuple], sequential: bool, message: str = ""
) -> ReviewRound:
    """Avvia un giro di revisione. `reviewers` è una lista di (utente, ruolo) nell'ordine scelto."""
    if not permissions.can_submit_review(user, document):
        raise PermissionDenied
    with transaction.atomic():
        document = Document.objects.select_for_update(of=("self",)).get(pk=document.pk)
        if document.status != S.DRAFT:
            raise TransizioneNonValida(
                f"Il documento è «{document.get_status_display()}»: la revisione si avvia solo da Bozza."
            )
        if document.current_version_id is None:
            raise TransizioneNonValida("Il documento non ha ancora un file da sottoporre.")
        _valida_revisori(document, reviewers)

        numero = (document.review_rounds.aggregate(m=Max("number"))["m"] or 0) + 1
        giro = ReviewRound.objects.create(
            document=document, number=numero, sequential=sequential,
            version_id=document.current_version_id, started_by=user, message=(message or "").strip()[:1000],
        )
        passi = ReviewStep.objects.bulk_create(
            ReviewStep(round=giro, position=i, reviewer=u, label=(ruolo or "").strip()[:80])
            for i, (u, ruolo) in enumerate(reviewers, start=1)
        )
        _cambia_stato(user, document, S.IN_REVIEW)
        audit.record(
            "review.started", user=user, obj=document, giro=numero, sequenziale=sequential, messaggio=(message or "").strip()[:1000],
            revisori=[f"{_nome(p.reviewer)}" + (f" ({p.label})" if p.label else "") for p in passi],
        )
        _concedi_lettura(user, document, [u for u, _ in reviewers])
        _avvisa_di_turno(user, document, giro, passi if not sequential else passi[:1], primo=True)
    logger.info("Documento %s: avviato il giro %s (%s revisori)", document.pk, numero, len(reviewers))
    return giro


def _valida_revisori(document: Document, reviewers: list[tuple]) -> None:
    if not reviewers:
        raise TransizioneNonValida("Scegli almeno una persona.")
    if len(reviewers) > MAX_REVISORI:
        raise TransizioneNonValida(f"Al massimo {MAX_REVISORI} persone.")
    visti = set()
    for u, _ in reviewers:
        if u.pk == document.owner_id:
            raise TransizioneNonValida("Il proprietario non può essere tra i revisori del proprio documento.")
        if not u.is_active:
            raise TransizioneNonValida(f"{_nome(u)} non è un utente attivo.")
        if u.pk in visti:
            raise TransizioneNonValida(f"{_nome(u)} è stato scelto due volte.")
        visti.add(u.pk)


def _concedi_lettura(user, document: Document, utenti) -> None:
    for u in utenti:
        if permissions.livello(u, document) == 0:
            DocumentPermission.objects.create(
                document=document, user=u, level=DocumentPermission.Level.READ, granted_by=user
            )
            audit.record(
                "share.granted", user=user, obj=document, destinatario=str(u),
                livello=DocumentPermission.Level.READ.label, motivo="revisione",
            )
            document.__dict__.pop("_livelli", None)


def _avvisa_di_turno(user, document: Document, giro: ReviewRound, passi, *, primo: bool) -> None:
    if giro.sequential:
        base = f"{_nome(user)} ti chiede di rivedere «{document.title}»"
        testo = base + (": tocca a te." if primo else " (i passi precedenti sono approvati): tocca a te.")
    else:
        testo = f"{_nome(user)} ti chiede di rivedere «{document.title}»."
    if giro.message:
        testo += " Messaggio: " + (giro.message if len(giro.message) <= 160 else giro.message[:159] + "…")
    notify([p.reviewer for p in passi], kind="review.requested", message=testo, url=_url(document), exclude=None)


def decide_review(*, user, document: Document, approve: bool, note: str = "") -> ReviewRound:
    """Risposta di un revisore: approva oppure rimanda (il motivo è obbligatorio)."""
    note = note.strip()
    with transaction.atomic():
        document = Document.objects.select_for_update(of=("self",)).get(pk=document.pk)
        giro = giro_in_corso(document)
        if giro is None:
            raise TransizioneNonValida("Non c'è nessuna revisione in corso: potrebbe essere già stata chiusa.")
        passo = giro.steps.filter(reviewer=user).first()
        if passo is None:
            raise PermissionDenied
        if passo.decision != ReviewStep.Decision.PENDING:
            raise TransizioneNonValida("Hai già risposto in questo giro di revisione.")
        if giro.sequential and giro.prossimo_passo().pk != passo.pk:
            raise TransizioneNonValida("Non è ancora il tuo turno: attendi chi ti precede.")
        if not approve and not note:
            raise TransizioneNonValida("Per rimandare serve una nota con il motivo.")

        passo.decision = ReviewStep.Decision.APPROVED if approve else ReviewStep.Decision.REJECTED
        passo.decided_at = timezone.now()
        passo.note = note
        passo.save(update_fields=["decision", "decided_at", "note"])
        audit.record(
            "review.approved" if approve else "review.rejected", user=user, obj=document,
            giro=giro.number, ruolo=passo.label, nota=note,
        )
        _archivia_richieste(document, [user])
        if approve:
            _dopo_approvazione(user, document, giro, passo)
        else:
            _chiudi(giro, ReviewRound.Outcome.REJECTED, note)
            _cambia_stato(user, document, S.DRAFT, nota=note)
            _avvisa_chiusura(user, document, giro, f"ha rimandato «{document.title}» al proprietario", note)
    logger.info("Documento %s: giro %s, %s da utente %s", document.pk, giro.number, "approvato" if approve else "rimandato", user.pk)
    return giro


def _dopo_approvazione(user, document: Document, giro: ReviewRound, passo: ReviewStep) -> None:
    prossimo = giro.prossimo_passo()
    if prossimo is None:
        _chiudi(giro, ReviewRound.Outcome.APPROVED)
        _cambia_stato(user, document, S.APPROVED)
        _avvisa_chiusura(user, document, giro, f"ha dato l'ultima approvazione: «{document.title}» è approvato", "")
    elif giro.sequential:
        _avvisa_di_turno(giro.started_by or document.owner, document, giro, [prossimo], primo=False)
    else:
        notify(
            [document.owner], kind="review.progress", exclude=user, url=_url(document),
            message=f"{_nome(user)} ha approvato «{document.title}».",
        )


def _chiudi(giro: ReviewRound, esito: str, nota: str = "") -> None:
    giro.outcome = esito
    giro.closed_at = timezone.now()
    giro.closing_note = nota[:500]
    giro.save(update_fields=["outcome", "closed_at", "closing_note"])
    _archivia_richieste(giro.document)  # giro chiuso: le richieste pendenti non servono più


def _avvisa_chiusura(user, document: Document, giro: ReviewRound, azione: str, nota: str) -> None:
    messaggio = f"{_nome(user)} {azione}."
    if nota:
        messaggio += f" Motivo: {nota}"
    partecipanti = [p.reviewer for p in giro.steps.select_related("reviewer")]
    notify([document.owner, *partecipanti], kind="review.closed", message=messaggio, url=_url(document), exclude=user)


def withdraw_review(*, user, document: Document, note: str) -> ReviewRound:
    """Il proprietario ritira il giro in corso: il documento torna in Bozza."""
    note = note.strip()
    if not permissions.can_submit_review(user, document):
        raise PermissionDenied
    with transaction.atomic():
        document = Document.objects.select_for_update(of=("self",)).get(pk=document.pk)
        giro = giro_in_corso(document)
        if giro is None:
            raise TransizioneNonValida("Non c'è nessuna revisione in corso: potrebbe essere già stata chiusa.")
        if not note:
            raise TransizioneNonValida("Per ritirare serve una nota con il motivo.")
        _chiudi(giro, ReviewRound.Outcome.WITHDRAWN, note)
        audit.record("review.withdrawn", user=user, obj=document, giro=giro.number, nota=note)
        _cambia_stato(user, document, S.DRAFT, nota=note)
        _avvisa_chiusura(user, document, giro, f"ha ritirato «{document.title}» dalla revisione", note)
    return giro
