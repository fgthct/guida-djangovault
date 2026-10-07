"""Confronta il testo di più documenti e dice quanto si assomigliano (sola lettura).

    python manage.py confronta 3 17 11 55

Il testo si riduce a parole minuscole senza punteggiatura e si confrontano le sequenze di 8 parole: vale per qualsiasi
formato (un PDF a righe spezzate e un ODT a paragrafi risultano confrontabili) e non dipende dall'impaginazione.
La tabella dice: «quanta parte del testo della RIGA si ritrova nella COLONNA». Due versioni quasi uguali hanno
valori vicini al 100% in entrambe le direzioni; se una ha un capitolo in più, il valore scende solo da un lato.
"""

import re

from django.core.management.base import BaseCommand, CommandError

from documents.models import Document

FINESTRA = 8


def parole(testo: str) -> list[str]:
    return re.sub(r"[\W_]+", " ", testo.casefold()).split()


def impronte(testo: str) -> set[int]:
    p = parole(testo)
    return {hash(tuple(p[i : i + FINESTRA])) for i in range(max(len(p) - FINESTRA + 1, 0))}


def contenimento(a: set[int], b: set[int]) -> float:
    """Quota delle sequenze di `a` presenti anche in `b` (0–1)."""
    return len(a & b) / len(a) if a else 0.0


class Command(BaseCommand):
    help = "Confronta il testo di più documenti (sola lettura)."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="+", type=int)

    def handle(self, *args, **o):
        ids = list(dict.fromkeys(o["ids"]))
        docs = {d.pk: d for d in Document.objects.filter(pk__in=ids).select_related("current_version")}
        mancanti = [i for i in ids if i not in docs]
        if mancanti:
            raise CommandError(f"Documenti non trovati: {', '.join(map(str, mancanti))}")
        dati = {}
        for i in ids:
            v = docs[i].current_version
            testo = v.extracted_text if v else ""
            dati[i] = (v.original_name if v else docs[i].title, len(parole(testo)), impronte(testo))
        self.stdout.write("Documenti:")
        for i in ids:
            nome, n, _ = dati[i]
            self.stdout.write(f"  #{i:<5} {nome[:48]:<48} {n:>7} parole")
        self.stdout.write("\nQuanta parte del testo della RIGA si ritrova nella COLONNA (%):")
        self.stdout.write("        " + "".join(f"{'#' + str(i):>7}" for i in ids))
        for a in ids:
            riga = "".join("      –" if a == b else f"{100 * contenimento(dati[a][2], dati[b][2]):>6.0f}%" for b in ids)
            self.stdout.write(f"  #{a:<5}{riga}")
        self.stdout.write("\nNulla è stato modificato.")
