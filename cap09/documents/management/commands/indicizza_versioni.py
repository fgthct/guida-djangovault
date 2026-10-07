"""Indicizzazione per significato (passaggi + vettori) delle versioni.

    python manage.py indicizza_versioni              # mette in coda quelle non ancora indicizzate
    python manage.py indicizza_versioni --sincrono   # le indicizza qui, senza worker
    python manage.py indicizza_versioni --tutte      # rifà tutto (dopo aver cambiato modello o parametri di spezzatura)
"""

from django.core.management.base import BaseCommand

from documents.models import DocumentVersion
from documents.services import indexing


class Command(BaseCommand):
    help = "Indicizza per significato le versioni dei documenti."

    def add_arguments(self, parser):
        parser.add_argument("--tutte", action="store_true", help="rifà anche le versioni già indicizzate")
        parser.add_argument("--sincrono", action="store_true", help="indicizza subito in questo processo")

    def handle(self, *args, **opzioni):
        if opzioni["tutte"]:
            qs = DocumentVersion.objects.filter(processing_status=DocumentVersion.Processing.DONE)
        else:
            qs = indexing.da_indicizzare()
        ids = list(qs.order_by("pk").values_list("pk", flat=True))
        if opzioni["sincrono"]:
            esiti: dict[str, int] = {}
            for n, pk in enumerate(ids, 1):
                esito = indexing.indicizza_versione(pk, forza=opzioni["tutte"])
                esiti[esito] = esiti.get(esito, 0) + 1
                self.stdout.write(f"  {n}/{len(ids)} versione {pk}: {esito}")
            self.stdout.write(self.style.SUCCESS(f"Indicizzate {len(ids)} versioni: {esiti or 'nessuna'}"))
        else:
            inviati = sum(indexing._invia(pk) for pk in ids)
            self.stdout.write(self.style.SUCCESS(f"{len(ids)} versioni in coda ({inviati} inviate al broker)."))
