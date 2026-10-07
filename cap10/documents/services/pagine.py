"""In quale pagina di un PDF si trova un passaggio.

I passaggi non memorizzano la pagina (il testo estratto non ha i salti pagina), quindi la si ricava quando serve:
`pdftotext` dà il testo pagina per pagina (separate da «\\f») e si cerca l'inizio del passaggio, confrontando solo
lettere e cifre (così a capo, trattini e spazi diversi non contano). Per le scansioni senza testo nel PDF non si trova
nulla e il chiamante apre il documento alla prima pagina.
"""

from __future__ import annotations

import re
import subprocess

_NON_ALNUM = re.compile(r"[\W_]+")
LUNGHEZZA_SONDA = 40
MAX_SONDE = 4


def _chiave(testo: str) -> str:
    return _NON_ALNUM.sub("", testo.lower())


def sonde(testo_passaggio: str) -> list[str]:
    """Pezzi del passaggio da cercare: inizio, due punti centrali e fine (se il primo cade su un'intestazione, gli altri aiutano)."""
    k = _chiave(testo_passaggio)
    if len(k) < LUNGHEZZA_SONDA:
        return [k] if k else []
    n = LUNGHEZZA_SONDA
    posizioni = [0, len(k) // 3, 2 * len(k) // 3, len(k) - n]
    return list(dict.fromkeys(k[p : p + n] for p in posizioni))[:MAX_SONDE]


def testo_per_pagina(percorso: str, timeout: int = 60) -> list[str]:
    proc = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", "-q", str(percorso), "-"], capture_output=True, timeout=timeout, check=False
    )
    if proc.returncode != 0:
        return []
    pagine = proc.stdout.decode("utf-8", "replace").split("\f")
    if pagine and not pagine[-1].strip():
        pagine.pop()  # pdftotext chiude l'ultima pagina con un salto pagina
    return pagine


def trova_pagina_in(pagine: list[str], testo_passaggio: str) -> int | None:
    """Numero di pagina (da 1) o None. Il voto va alla pagina che contiene più sonde (la prima in caso di parità)."""
    chiavi = [_chiave(p) for p in pagine]
    migliore, voti_migliore = None, 0
    for i, k in enumerate(chiavi, 1):
        voti = sum(1 for s in sonde(testo_passaggio) if s in k)
        if voti > voti_migliore:
            migliore, voti_migliore = i, voti
    return migliore


def pagina_del_passaggio(versione, testo_passaggio: str) -> int | None:
    """Pagina di un passaggio di una versione PDF, oppure None (non è un PDF, nessun testo nel PDF, passaggio non trovato)."""
    if not versione.original_name.lower().endswith(".pdf") or not testo_passaggio.strip():
        return None
    try:
        percorso = versione.file.path
    except (ValueError, NotImplementedError):
        return None
    try:
        return trova_pagina_in(testo_per_pagina(percorso), testo_passaggio)
    except (OSError, subprocess.TimeoutExpired):
        return None
