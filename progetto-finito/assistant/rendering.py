"""Testo della risposta → HTML sicuro.

Si fa l'escape di TUTTO prima di aggiungere i pochi tag che conosciamo (paragrafi, elenchi, grassetto, codice,
citazioni): il modello, e i documenti che ha letto, non possono produrre HTML né script.
"""

from __future__ import annotations

import re

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

_GRASSETTO = re.compile(r"\*\*(.+?)\*\*")
_CORSIVO = re.compile(r"(?<![\w*])_([^_\n]+?)_(?![\w*])")
_CODICE = re.compile(r"`([^`\n]+)`")
_CITAZIONE = re.compile(r"\[(\d{1,2})\]")
_ELENCO = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_TITOLO = re.compile(r"^\s*#{1,6}\s+(.*)$")


def _inline(testo: str, msg_id: int, fonti_valide: set[int]) -> str:
    html = escape(testo)
    html = _CODICE.sub(r"<code>\1</code>", html)
    html = _GRASSETTO.sub(r"<strong>\1</strong>", html)
    html = _CORSIVO.sub(r"<em>\1</em>", html)

    def citazione(m: re.Match) -> str:
        n = int(m.group(1))
        if n not in fonti_valide:
            return m.group(0)
        return f'<sup><a href="#fonte-{msg_id}-{n}" class="citazione">[{n}]</a></sup>'

    return _CITAZIONE.sub(citazione, html)


def risposta_html(testo: str, msg_id: int, fonti_valide: set[int]) -> SafeString:
    blocchi: list[str] = []
    elenco: list[str] = []
    paragrafo: list[str] = []

    def chiudi_paragrafo():
        if paragrafo:
            blocchi.append("<p>" + "<br>".join(_inline(r, msg_id, fonti_valide) for r in paragrafo) + "</p>")
            paragrafo.clear()

    def chiudi_elenco():
        if elenco:
            blocchi.append("<ul>" + "".join(f"<li>{_inline(v, msg_id, fonti_valide)}</li>" for v in elenco) + "</ul>")
            elenco.clear()

    for riga in (testo or "").splitlines():
        if not riga.strip():
            chiudi_paragrafo()
            chiudi_elenco()
        elif m := _ELENCO.match(riga):
            chiudi_paragrafo()
            elenco.append(m.group(1))
        elif m := _TITOLO.match(riga):
            chiudi_paragrafo()
            chiudi_elenco()
            blocchi.append(f"<p><strong>{_inline(m.group(1), msg_id, fonti_valide)}</strong></p>")
        else:
            chiudi_elenco()
            paragrafo.append(riga.strip())
    chiudi_paragrafo()
    chiudi_elenco()
    return mark_safe("".join(blocchi))
