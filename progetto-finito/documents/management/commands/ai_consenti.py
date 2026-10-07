"""Consenso all'assistente IA in blocco (i documenti già presenti partono senza consenso).

    python manage.py ai_consenti --tutti                 # consente a tutti i documenti
    python manage.py ai_consenti --categoria "Contratti" # solo una categoria
    python manage.py ai_consenti --tutti --revoca        # toglie il consenso a tutti
"""

from django.core.management.base import BaseCommand, CommandError

from documents.models import Document


class Command(BaseCommand):
    help = "Consente (o revoca) la lettura dell'assistente IA su più documenti."

    def add_arguments(self, parser):
        parser.add_argument("--tutti", action="store_true")
        parser.add_argument("--categoria", help="nome esatto della categoria")
        parser.add_argument("--revoca", action="store_true", help="toglie il consenso invece di darlo")

    def handle(self, *args, **opzioni):
        if not opzioni["tutti"] and not opzioni["categoria"]:
            raise CommandError("Indica --tutti oppure --categoria NOME.")
        qs = Document.objects.all()
        if opzioni["categoria"]:
            qs = qs.filter(category__name=opzioni["categoria"])
        consenti = not opzioni["revoca"]
        n = qs.exclude(ai_allowed=consenti).update(ai_allowed=consenti)
        self.stdout.write(self.style.SUCCESS(f"{n} documenti aggiornati ({'consentiti' if consenti else 'revocati'})."))
