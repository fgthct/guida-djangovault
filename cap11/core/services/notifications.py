from __future__ import annotations

from .. import realtime
from ..models import Notification


def notify(recipients, *, kind: str, message: str, url: str = "", exclude=None) -> int:
    """Crea una notifica per ogni destinatario (senza duplicati, escluso `exclude`). Ritorna quante."""
    visti: set[int] = set()
    da_creare = []
    for utente in recipients:
        if utente.pk in visti or (exclude is not None and utente.pk == exclude.pk) or not utente.is_active:
            continue
        visti.add(utente.pk)
        da_creare.append(Notification(recipient=utente, kind=kind, message=message[:300], url=url[:300]))
    Notification.objects.bulk_create(da_creare)
    realtime.notifiche_cambiate(n.recipient_id for n in da_creare)
    return len(da_creare)
