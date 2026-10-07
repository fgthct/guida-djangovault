"""Elaborazione manuale delle versioni (testo, OCR, anteprima).

Esempi:
    python manage.py elabora_versioni                 # rimette in coda ciò che è fermo (come il pianificatore)
    python manage.py elabora_versioni --falliti       # rimette in coda anche quelle in errore
    python manage.py elabora_versioni --tutte         # rielabora tutto (per esempio dopo un aggiornamento dell'OCR)
    python manage.py elabora_versioni --sincrono      # elabora qui, senza worker né Redis
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from documents.models import DocumentVersion
from documents.services import processing

P = DocumentVersion.Processing


class Command(BaseCommand):
    help = "Elabora (o rimette in coda) le versioni dei documenti."

    def add_arguments(self, parser):
        parser.add_argument("--falliti", action="store_true", help="include le versioni in errore")
        parser.add_argument("--tutte", action="store_true", help="rielabora ogni versione, anche le completate")
        parser.add_argument("--sincrono", action="store_true", help="elabora subito in questo processo")

    def handle(self, *args, **opzioni):
        stati = [P.PENDING]
        if opzioni["falliti"] or opzioni["tutte"]:
            stati += [P.FAILED]
        if opzioni["tutte"]:
            stati += [P.DONE, P.SKIPPED]

        if not (opzioni["falliti"] or opzioni["tutte"] or opzioni["sincrono"]):
            n = processing.riprendi_elaborazioni()
            self.stdout.write(self.style.SUCCESS(f"{n} versioni rimesse in coda."))
            return

        ids = list(
            DocumentVersion.objects.filter(processing_status__in=stati).order_by("pk").values_list("pk", flat=True)
        )
        if opzioni["tutte"] or opzioni["falliti"]:
            DocumentVersion.objects.filter(pk__in=ids).update(
                processing_status=P.PENDING, processing_attempts=0, processing_message="",
                processing_updated_at=timezone.now(),
            )
        if opzioni["sincrono"]:
            esiti: dict[str, int] = {}
            for pk in ids:
                esito = processing.elabora_versione(pk, forza=True)
                esiti[esito] = esiti.get(esito, 0) + 1
            self.stdout.write(self.style.SUCCESS(f"Elaborate {len(ids)} versioni: {esiti or 'nessuna'}"))
        else:
            inviati = sum(processing._invia(pk) for pk in ids)
            self.stdout.write(self.style.SUCCESS(f"{len(ids)} versioni in coda ({inviati} inviate al broker)."))
