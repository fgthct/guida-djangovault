"""Embeddings: trasformano un testo in un vettore, così testi dal significato simile stanno vicini.

Modello predefinito: `intfloat/multilingual-e5-small` (384 dimensioni), lo stesso già usato in LexMind. Gira in locale
su CPU: nessun servizio esterno, nessun costo, i testi non lasciano il server. Il modello (~470 MB) si scarica da
Hugging Face la prima volta e resta nella cartella `HF_HOME` (in Docker, un volume condiviso da sito e worker).

I modelli E5 vogliono un prefisso: «passage: » per i testi dei documenti, «query: » per le domande.

Per cambiare modello basta `EMBEDDING_BACKEND` (percorso di una classe con gli stessi metodi); se le dimensioni
cambiano va cambiata anche la colonna `DocumentChunk.embedding` con una migration.
"""

from __future__ import annotations

import functools
import logging
import threading

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


class ModelloNonDisponibile(RuntimeError):
    """Il modello non si può caricare (rete assente al primo avvio, disco pieno…). Messaggio mostrabile."""


class SentenceTransformerEmbedder:
    def __init__(self) -> None:
        self.nome = settings.EMBEDDING_MODEL
        self._modello = None
        self._blocco = threading.Lock()

    def _carica(self):
        with self._blocco:
            if self._modello is None:
                try:
                    import torch
                    from sentence_transformers import SentenceTransformer

                    torch.set_num_threads(settings.EMBEDDING_THREADS)
                    modello = SentenceTransformer(self.nome, device="cpu")
                except Exception as exc:
                    logger.exception("Modello di embeddings %s non caricabile", self.nome)
                    raise ModelloNonDisponibile(
                        "Il modello per la ricerca per significato non è disponibile: "
                        "al primo uso serve la connessione a Internet per scaricarlo."
                    ) from exc
                dimensioni = modello.get_embedding_dimension()
                if dimensioni != settings.EMBEDDING_DIM:
                    raise ModelloNonDisponibile(
                        f"Il modello {self.nome} produce {dimensioni} dimensioni, la colonna ne ha {settings.EMBEDDING_DIM}."
                    )
                self._modello = modello
        return self._modello

    def _codifica(self, testi: list[str]) -> list[list[float]]:
        vettori = self._carica().encode(
            testi, batch_size=16, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True
        )
        return [[float(x) for x in v] for v in vettori]

    def passaggi(self, testi: list[str]) -> list[list[float]]:
        return self._codifica([f"passage: {t}" for t in testi]) if testi else []

    def domanda(self, testo: str) -> list[float]:
        return self._codifica([f"query: {testo}"])[0]


@functools.lru_cache(maxsize=1)
def _istanza(percorso: str):
    return import_string(percorso)()


def get_embedder():
    """L'embedder configurato (un'unica istanza per processo: il modello si carica una volta sola)."""
    return _istanza(settings.EMBEDDING_BACKEND)
