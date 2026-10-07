"""Documenti simili: i più vicini per significato a un documento, fra quelli che l'utente può vedere."""
from __future__ import annotations

import numpy as np
from django.conf import settings

from ..models import Document

SOGLIA = None  # se None si usa CLASSIFICAZIONE_SIMILITUDINE_MIN: stessa nozione di «abbastanza vicino» del classificatore
QUASI_UGUALE = 0.95


def documenti_simili(user, documento: Document, *, massimo: int = 5) -> list[dict]:
    """Fino a `massimo` documenti simili, dal più vicino. Solo documenti visibili a `user`, mai il documento stesso.

    Il confronto è sul vettore del documento (media dei suoi passaggi), centrato sulla media dei documenti visibili:
    i vettori grezzi sono tutti molto vicini fra loro, centrati distinguono meglio un argomento dall'altro.
    """
    v = documento.current_version
    if v is None or v.doc_vector is None:
        return []
    righe = list(
        Document.objects.visible_to(user)
        .filter(current_version__doc_vector__isnull=False)
        .exclude(pk=documento.pk)
        .values_list("pk", "title", "category__name", "current_version__doc_vector")
    )
    if not righe:
        return []
    grezza = np.vstack([np.asarray(r[3], dtype=np.float32) for r in righe])
    proprio = np.asarray(v.doc_vector, dtype=np.float32)
    media = np.vstack([grezza, proprio]).mean(axis=0)

    def norma(m):
        n = np.linalg.norm(m, axis=-1, keepdims=True)
        n[n == 0] = 1.0
        return m / n

    sims = norma(grezza - media) @ norma(proprio - media)
    risultato = []
    for i in np.argsort(-sims)[:massimo]:
        if sims[i] < (SOGLIA if SOGLIA is not None else settings.CLASSIFICAZIONE_SIMILITUDINE_MIN):
            break
        risultato.append(
            {
                "id": righe[i][0],
                "titolo": righe[i][1],
                "categoria": righe[i][2] or "",
                "sim": round(float(sims[i]), 3),
                "quasi_uguale": bool(sims[i] >= QUASI_UGUALE),
            }
        )
    return risultato
