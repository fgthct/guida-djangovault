"""Spezzatura del testo in passaggi per la ricerca semantica.

Funzione pura (nessun database). Un passaggio è abbastanza lungo da avere un senso compiuto e abbastanza
corto da stare nella finestra del modello (512 token ≈ 1500-2000 caratteri in italiano). Si spezza dove il
testo lo permette: prima i paragrafi, poi le frasi, solo in ultimo le parole. Fra un passaggio e il successivo
c'è una piccola sovrapposizione, così una frase a cavallo di due passaggi non si perde.
"""

from __future__ import annotations

import re

_PARAGRAFI = re.compile(r"\n\s*\n")
_FRASI = re.compile(r"(?<=[.!?;:])\s+")


def _pezzi(testo: str, massimo: int) -> list[str]:
    """Unità non più lunghe di `massimo`: paragrafi, altrimenti frasi, altrimenti gruppi di parole."""
    unita: list[str] = []
    for paragrafo in _PARAGRAFI.split(testo):
        paragrafo = " ".join(paragrafo.split())
        if not paragrafo:
            continue
        if len(paragrafo) <= massimo:
            unita.append(paragrafo)
            continue
        for frase in _FRASI.split(paragrafo):
            if len(frase) <= massimo:
                unita.append(frase)
                continue
            corrente = ""
            for parola in frase.split():
                while len(parola) > massimo:  # parola lunghissima (es. una stringa codificata): taglio netto
                    if corrente:
                        unita.append(corrente)
                        corrente = ""
                    unita.append(parola[:massimo])
                    parola = parola[massimo:]
                if len(corrente) + len(parola) + 1 > massimo:
                    unita.append(corrente)
                    corrente = parola
                else:
                    corrente = f"{corrente} {parola}".strip()
            if corrente:
                unita.append(corrente)
    return unita


def _coda(testo: str, lunghezza: int) -> str:
    """Gli ultimi ~`lunghezza` caratteri, partendo da un inizio di parola."""
    if lunghezza <= 0 or len(testo) <= lunghezza:
        return "" if lunghezza <= 0 else testo
    coda = testo[-lunghezza:]
    spazio = coda.find(" ")
    return coda[spazio + 1:] if spazio != -1 else coda


def spezza(testo: str, *, dimensione: int = 1000, sovrapposizione: int = 150, massimo_passaggi: int = 1000) -> tuple[list[str], bool]:
    """Spezza `testo` in passaggi. Ritorna (passaggi, troncato). `troncato` è True se si è raggiunto il massimo."""
    passaggi: list[str] = []
    corrente = ""
    for unita in _pezzi(testo, dimensione):
        if corrente and len(corrente) + len(unita) + 1 > dimensione:
            passaggi.append(corrente)
            if len(passaggi) >= massimo_passaggi:
                return passaggi, True
            corrente = _coda(corrente, sovrapposizione)
            if corrente and len(corrente) + len(unita) + 1 > dimensione:
                corrente = ""  # la sovrapposizione non deve far sforare il limite
        corrente = f"{corrente} {unita}".strip()
    if corrente.strip():
        if len(passaggi) >= massimo_passaggi:
            return passaggi, True
        passaggi.append(corrente)
    return passaggi, False
