"""Chi può fare cosa sui documenti: un unico punto di controllo.

Livelli (DocumentPermission.Level), ciascuno include i precedenti:

- LETTURA (READ): vedere il documento, la cronologia, scaricare ogni versione;
- MODIFICA (WRITE): + modificare i metadati, caricare nuove versioni, ripristinare;
- AMMINISTRAZIONE (ADMIN): + eliminare il documento e gestire la condivisione.

La revisione non dipende dai livelli: chi deve rispondere lo sceglie il proprietario al momento
dell'invio (vedere `documents/services/review.py`).

Il proprietario e il superutente sono sempre ADMIN. Un utente può ottenere un livello
direttamente e/o tramite i suoi gruppi: vale il più alto.

La sicurezza è sempre server-side: nascondere un pulsante è solo UX.
"""

from __future__ import annotations

from .models import Document, DocumentPermission

Level = DocumentPermission.Level


def livello(user, document: Document) -> int:
    """Livello massimo di `user` su `document` (0 = nessun accesso). Memorizzato sull'istanza."""
    if not user or not user.is_authenticated:
        return 0
    if user.is_superuser or document.owner_id == user.id:
        return Level.ADMIN
    cache = document.__dict__.setdefault("_livelli", {})
    if user.id not in cache:
        from django.db.models import Max, Q

        cache[user.id] = (
            DocumentPermission.objects.filter(document=document)
            .filter(Q(user=user) | Q(group__user=user))
            .aggregate(m=Max("level"))["m"]
            or 0
        )
    return cache[user.id]


def can_view(user, document) -> bool:
    return livello(user, document) >= Level.READ


def can_download(user, document) -> bool:
    return can_view(user, document)


def can_edit(user, document) -> bool:
    return livello(user, document) >= Level.WRITE


def can_add_version(user, document) -> bool:
    return can_edit(user, document)


def can_restore_version(user, document) -> bool:
    return can_edit(user, document)


def can_reprocess(user, document) -> bool:
    """Rimettere in coda l'elaborazione (testo/OCR/anteprima) di una versione.

    Non modifica il documento, solo dati derivati: vale anche quando il documento non è in bozza.
    """
    return can_edit(user, document)


def can_delete(user, document) -> bool:
    return livello(user, document) >= Level.ADMIN


def can_share(user, document) -> bool:
    return livello(user, document) >= Level.ADMIN


def can_submit_review(user, document) -> bool:
    """Solo il proprietario sceglie i revisori e avvia (o ritira) la revisione."""
    return bool(user and user.is_authenticated and document.owner_id == user.id)


def utenti_con_livello(document: Document, minimo: int):
    """Utenti attivi con almeno `minimo` sul documento (proprietario incluso, superutenti esclusi)."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    concessi = DocumentPermission.objects.filter(document=document, level__gte=minimo)
    return get_user_model().objects.filter(is_active=True).filter(
        Q(pk=document.owner_id)
        | Q(pk__in=concessi.filter(user__isnull=False).values("user_id"))
        | Q(groups__in=concessi.filter(group__isnull=False).values("group_id"))
    ).distinct()


def annota_livelli(user, documents) -> None:
    """Imposta `livello_utente` e `puo_eliminare` su ogni documento, con UNA sola query (per gli elenchi)."""
    documents = list(documents)
    if not documents:
        return
    from django.db.models import Max, Q

    massimi = dict(
        DocumentPermission.objects.filter(document__in=documents)
        .filter(Q(user=user) | Q(group__user=user))
        .values_list("document_id")
        .annotate(m=Max("level"))
    )
    for d in documents:
        if user.is_superuser or d.owner_id == user.id:
            d.livello_utente = Level.ADMIN
        else:
            d.livello_utente = massimi.get(d.pk, 0)
        d.puo_eliminare = d.livello_utente >= Level.ADMIN
