"""Stati del documento: Bozza → In revisione → Approvato → Archiviato.

Il passaggio Bozza → In revisione → Approvato/Bozza è gestito da `review.py` (revisione a più
persone scelte dal proprietario). Qui restano le transizioni «amministrative» tra gli stati
Approvato, Archiviato e Bozza, riservate al livello Amministrazione.

Ogni cambio di stato: controlla il permesso nel servizio, blocca il documento
(`select_for_update`) e rivalida lo stato (se qualcuno l'ha già cambiato, rifiuta invece di
sovrascrivere), scrive registro e notifiche nella stessa transazione.

Fuori dallo stato Bozza il documento è bloccato: niente modifiche, nuove versioni o ripristini
finché non viene riaperto (`assicura_modificabile`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse

from core.services import audit
from core.services.notifications import notify

from .. import permissions
from ..models import Document

logger = logging.getLogger(__name__)
S = Document.Status


class TransizioneNonValida(ValueError):
    """Transizione non ammessa o stato cambiato nel frattempo (messaggio mostrabile)."""


class DocumentoBloccato(ValueError):
    """Il documento non è in bozza, quindi non si può modificare."""


@dataclass(frozen=True)
class Transizione:
    da: str
    a: str
    etichetta: str
    permesso: str  # oggi solo "admin"
    nota_obbligatoria: bool = False
    pericolosa: bool = False  # solo per lo stile del pulsante

    @property
    def a_label(self) -> str:
        return S(self.a).label


TRANSIZIONI = [
    Transizione(S.APPROVED, S.ARCHIVED, "Archivia", "admin"),
    Transizione(S.APPROVED, S.DRAFT, "Riapri come bozza", "admin", nota_obbligatoria=True),
    Transizione(S.DRAFT, S.ARCHIVED, "Archivia", "admin"),
    Transizione(S.ARCHIVED, S.DRAFT, "Ripristina dall'archivio", "admin"),
]


def consentita(t: Transizione, user, document: Document) -> bool:
    return permissions.can_delete(user, document)  # livello Amministrazione


def transizione(da: str, a: str) -> Transizione | None:
    return next((t for t in TRANSIZIONI if t.da == da and t.a == a), None)


def transizioni_disponibili(user, document: Document) -> list[Transizione]:
    """Transizioni che `user` può eseguire ora (per mostrare i pulsanti giusti)."""
    return [t for t in TRANSIZIONI if t.da == document.status and consentita(t, user, document)]


def assicura_modificabile(document: Document) -> None:
    if document.status != S.DRAFT:
        raise DocumentoBloccato(
            f"Il documento è «{document.get_status_display()}» e non si può modificare: "
            "riportalo in bozza per cambiarlo."
        )


def change_status(*, user, document: Document, to_status: str, note: str = "") -> Document:
    note = note.strip()
    with transaction.atomic():
        document = Document.objects.select_for_update(of=("self",)).select_related("owner").get(pk=document.pk)
        t = transizione(document.status, to_status)
        if t is None:
            raise TransizioneNonValida(
                f"Non si può passare da «{document.get_status_display()}» a «{S(to_status).label if to_status in S.values else to_status}». "
                "Lo stato potrebbe essere cambiato nel frattempo: ricarica la pagina."
            )
        if not consentita(t, user, document):
            raise PermissionDenied
        if t.nota_obbligatoria and not note:
            raise TransizioneNonValida("Per questa operazione serve una nota con il motivo.")
        precedente = document.status
        document.status = to_status
        document.save(update_fields=["status", "updated_at"])
        audit.record(
            "document.status_changed", user=user, obj=document, da=precedente, a=to_status, nota=note
        )
        _notifica(user, document, precedente, to_status, note)
    logger.info("Documento %s: %s → %s (utente %s)", document.pk, precedente, to_status, user.pk)
    return document


def _notifica(user, document: Document, da: str, a: str, nota: str) -> None:
    chi = user.get_full_name() or user.get_username()
    azione = "ha archiviato" if a == S.ARCHIVED else ("ha riaperto come bozza" if da == S.APPROVED else "ha ripristinato")
    messaggio = f"{chi} {azione} «{document.title}»."
    if nota:
        messaggio += f" Nota: {nota}"
    notify(
        permissions.utenti_con_livello(document, permissions.Level.WRITE),
        kind="document.status_changed",
        message=messaggio,
        url=reverse("documents:detail", args=[document.pk]),
        exclude=user,
    )
