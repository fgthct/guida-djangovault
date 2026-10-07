"""Pulizie periodiche: sessioni scadute, notifiche vecchie e (se richiesto) voci vecchie del registro."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.sessions.models import Session
from django.utils import timezone

from ..models import AuditLog, Notification

logger = logging.getLogger(__name__)


def pulisci() -> dict[str, int]:
    """Esegue tutte le pulizie. Ritorna quante righe sono state eliminate per categoria."""
    adesso = timezone.now()
    esito = {"sessioni": 0, "notifiche": 0, "registro": 0}

    esito["sessioni"] = Session.objects.filter(expire_date__lt=adesso).delete()[0]

    # Solo le notifiche già lette: una non letta resta finché qualcuno non la vede.
    giorni_notifiche = settings.NOTIFICATION_RETENTION_DAYS
    if giorni_notifiche > 0:
        soglia = adesso - timedelta(days=giorni_notifiche)
        esito["notifiche"] = Notification.objects.filter(read_at__isnull=False, read_at__lt=soglia).delete()[0]

    # Il registro resta per sempre se non si sceglie un periodo (0): è la prova di chi ha fatto cosa.
    giorni_registro = settings.AUDIT_RETENTION_DAYS
    if giorni_registro > 0:
        soglia = adesso - timedelta(days=giorni_registro)
        esito["registro"] = AuditLog.objects.filter(timestamp__lt=soglia).delete()[0]

    logger.info("Pulizia periodica: %s", esito)
    return esito
