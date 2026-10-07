"""Embedder finto per i test: deterministico, senza scaricare modelli.

Ogni parola (senza accenti, ridotta alle prime 5 lettere, con qualche sinonimo) finisce in una casella del
vettore: testi con parole in comune sono vicini, testi senza parole in comune sono ortogonali.
"""

import hashlib
import math
import re
import unicodedata

SINONIMI = {
    "affitto": "locazione",
    "affitti": "locazione",
    "casa": "immobile",
    "appartamento": "immobile",
    "eredi": "successione",
    "eredita": "successione",
}


def _parole(testo: str) -> list[str]:
    testo = unicodedata.normalize("NFD", testo.lower())
    testo = "".join(c for c in testo if unicodedata.category(c) != "Mn")
    risultato = []
    for p in re.findall(r"[a-z0-9]+", testo):
        p = SINONIMI.get(p, p)
        if len(p) > 3:
            risultato.append(p[:5])
    return risultato


def _vettore(testo: str, dim: int = 384) -> list[float]:
    v = [0.0] * dim
    for p in _parole(testo):
        v[int(hashlib.md5(p.encode()).hexdigest(), 16) % dim] += 1.0
    norma = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norma for x in v]


class FakeEmbedder:
    nome = "fake"

    def passaggi(self, testi):
        return [_vettore(t) for t in testi]

    def domanda(self, testo):
        return _vettore(testo)


class EmbedderRotto:
    """Simula il modello non scaricabile."""

    def passaggi(self, testi):
        from documents.embeddings import ModelloNonDisponibile

        raise ModelloNonDisponibile("modello assente")

    domanda = passaggi
