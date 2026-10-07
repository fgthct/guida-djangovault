"""Applicazione Celery di DjangoVault.

Il worker si avvia con:  celery -A config worker -B -l info
(`-B` esegue anche il pianificatore dei task periodici; in produzione si può separare.)
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("djangovault")
# Tutte le impostazioni CELERY_* di settings.py (senza il prefisso nel nome interno).
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # Rete di sicurezza: rimette in coda le versioni rimaste "in coda" o "in corso" per troppo tempo.
    "riprendi-elaborazioni": {"task": "documents.riprendi_elaborazioni", "schedule": 300.0},
    "riprendi-indicizzazioni": {"task": "documents.riprendi_indicizzazioni", "schedule": 600.0},
    "riprendi-classificazioni": {"task": "documents.riprendi_classificazioni", "schedule": 600.0},
    # Pulizia giornaliera (sessioni scadute, notifiche lette vecchie, registro oltre il periodo di conservazione).
    "manutenzione": {"task": "core.manutenzione", "schedule": 86400.0},
}


# Ingestione automatica (documents/services/ingestione.py): ciascun canale si spegne mettendo 0 al suo intervallo.
from django.conf import settings as _settings  # noqa: E402

if _settings.INGESTIONE_CARTELLA_OGNI > 0:
    app.conf.beat_schedule["osserva-cartella"] = {
        "task": "documents.osserva_cartella", "schedule": float(_settings.INGESTIONE_CARTELLA_OGNI),
    }
if _settings.INGESTIONE_POSTA_OGNI > 0 and _settings.INGESTIONE_IMAP_HOST:
    app.conf.beat_schedule["leggi-posta"] = {
        "task": "documents.leggi_posta", "schedule": float(_settings.INGESTIONE_POSTA_OGNI),
    }
