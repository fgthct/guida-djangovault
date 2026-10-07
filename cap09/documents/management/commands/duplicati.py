"""Elenca i documenti che sembrano versioni dello stesso testo (stesso titolo scritto in modo diverso, formati diversi,
varianti «KDP», «con commenti»…). Sola lettura: non modifica e non cancella niente.

    python manage.py duplicati
    python manage.py duplicati --minimo 3        # solo i gruppi di almeno 3 documenti

Due documenti finiscono nello stesso gruppo se i titoli, ridotti a lettere e cifre minuscole (senza il testo fra
parentesi), sono uguali o l'uno comincia con l'altro: «ZERO DAY», «ZeroDay» e «ZERO DAY (con i commenti)» sono un gruppo.
"""

import re

from django.core.management.base import BaseCommand
from django.template.defaultfilters import filesizeformat

from documents.models import Document

MIN_CHIAVE = 6  # titoli più corti non si raggruppano per «inizia con» (troppe false coincidenze)


def chiave(titolo: str) -> str:
    senza_parentesi = re.sub(r"\([^)]*\)", " ", titolo)
    return re.sub(r"[\W_]+", "", senza_parentesi.casefold())


def raggruppa(documenti) -> list[list]:
    """Gruppi (di almeno 2) di documenti con titoli «imparentati»; in ogni gruppo, dal più recente."""
    gruppi: list[dict] = []
    for d in sorted(documenti, key=lambda d: len(chiave(d.title))):
        k = chiave(d.title)
        if not k:
            continue
        for g in gruppi:
            base = g["base"]
            if k == base or (len(base) >= MIN_CHIAVE and k.startswith(base)):
                g["docs"].append(d)
                break
        else:
            gruppi.append({"base": k, "docs": [d]})
    risultato = [sorted(g["docs"], key=lambda d: d.updated_at, reverse=True) for g in gruppi if len(g["docs"]) > 1]
    return sorted(risultato, key=lambda docs: (-len(docs), chiave(docs[0].title)))


class Command(BaseCommand):
    help = "Elenca i documenti che sembrano versioni dello stesso testo (sola lettura)."

    def add_arguments(self, parser):
        parser.add_argument("--minimo", type=int, default=2, help="dimensione minima dei gruppi (default 2)")

    def handle(self, *args, **o):
        documenti = list(Document.objects.select_related("category", "current_version").defer("current_version__extracted_text"))
        gruppi = [g for g in raggruppa(documenti) if len(g) >= o["minimo"]]
        for docs in gruppi:
            self.stdout.write(f"\n== {docs[0].title} ({len(docs)})")
            for d in docs:
                v = d.current_version
                self.stdout.write(
                    f"  #{d.pk:<5} {(v.original_name if v else '—')[:46]:<46} {filesizeformat(v.size) if v else '':>9}  "
                    f"{d.updated_at:%d/%m %H:%M}  {d.category or '—'}"
                )
        coinvolti = sum(len(g) for g in gruppi)
        self.stdout.write(f"\n{len(gruppi)} gruppi, {coinvolti} documenti coinvolti su {len(documenti)}. Nulla è stato modificato.")
