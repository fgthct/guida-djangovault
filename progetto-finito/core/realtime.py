"""Invio degli avvisi in tempo reale.

Gli avvisi non portano dati: dicono solo «qualcosa è cambiato». Il browser poi chiede il frammento HTML alla view
normale (che controlla i permessi). Così il WebSocket non può far trapelare contenuti e la pagina funziona uguale
(più lenta, col polling) se il WebSocket non c'è.

L'invio parte solo dopo il commit e non solleva mai errori: se Redis non risponde, l'operazione vera è già riuscita.
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

logger = logging.getLogger(__name__)


def gruppo_utente(user_id: int) -> str:
    return f"utente_{int(user_id)}"


def gruppo_documento(document_id: int) -> str:
    return f"documento_{int(document_id)}"


def _invia(gruppi: list[str], messaggio: dict) -> None:
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        for g in gruppi:
            async_to_sync(layer.group_send)(g, {"type": "avviso", **messaggio})
    except Exception:  # noqa: BLE001
        logger.warning("Avviso in tempo reale non inviato", exc_info=True)


def notifiche_cambiate(user_ids) -> None:
    gruppi = [gruppo_utente(i) for i in set(user_ids)]
    if gruppi:
        transaction.on_commit(lambda: _invia(gruppi, {"t": "notifiche"}))


def versione_cambiata(document_id: int) -> None:
    transaction.on_commit(lambda: _invia([gruppo_documento(document_id)], {"t": "versione", "doc": int(document_id)}))
