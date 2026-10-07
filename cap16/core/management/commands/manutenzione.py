from django.core.management.base import BaseCommand

from core.services import maintenance


class Command(BaseCommand):
    help = "Elimina sessioni scadute, notifiche lette vecchie e (se AUDIT_RETENTION_DAYS > 0) voci vecchie del registro."

    def handle(self, *args, **options):
        esito = maintenance.pulisci()
        for categoria, n in esito.items():
            self.stdout.write(f"{categoria}: {n} eliminate")
