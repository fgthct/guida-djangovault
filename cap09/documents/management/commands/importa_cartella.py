"""Importa in blocco i file di una cartella come documenti.

    python manage.py importa_cartella /app/inbox --per-sottocartelle
    python manage.py importa_cartella ~/libri --categoria "Libri informatica" --utente franky
    python manage.py importa_cartella /app/inbox --rimuovi        # toglie dalla cartella ciò che è stato importato
    python manage.py importa_cartella /app/inbox --prova          # mostra cosa farebbe, senza importare

Per ogni file stampa una riga `@@ stato<TAB>percorso<TAB>dettaglio` (la legge lo script `invia_cartella.py`).
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.services import import_folder as imp


class Command(BaseCommand):
    help = "Importa in blocco i file di una cartella come documenti."

    def add_arguments(self, parser):
        parser.add_argument("cartella")
        parser.add_argument("--utente", help="nome utente proprietario dei documenti (default: il primo superutente)")
        parser.add_argument("--categoria", help="categoria unica per tutti i file (creata se manca)")
        parser.add_argument(
            "--per-sottocartelle", action="store_true", help="categoria = nome della prima sottocartella di ogni file"
        )
        parser.add_argument("--senza-consenso-ia", action="store_true", help="non consente la lettura all'assistente «Chiedi»")
        parser.add_argument("--rimuovi", action="store_true", help="toglie dalla cartella i file importati o già presenti")
        parser.add_argument("--senza-immagini", action="store_true", help="salta PNG, JPG, GIF e WebP (non hanno testo da cercare)")
        parser.add_argument("--classifica", action="store_true", help="i file senza categoria decisa qui vengono classificati dal sistema (vedi «Da smistare»)")
        parser.add_argument("--prova", action="store_true", help="non importa nulla: elenca soltanto")

    def handle(self, *args, **o):
        User = get_user_model()
        if o["utente"]:
            owner = User.objects.filter(username=o["utente"], is_active=True).first()
            if owner is None:
                raise CommandError(f"Utente «{o['utente']}» non trovato o non attivo.")
        else:
            owner = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
            if owner is None:
                raise CommandError("Non c'è nessun superutente: indica --utente.")
        try:
            riepilogo = imp.importa_cartella(
                o["cartella"], owner=owner, categoria=o["categoria"], per_sottocartelle=o["per_sottocartelle"],
                ai_allowed=not o["senza_consenso_ia"], rimuovi=o["rimuovi"], prova=o["prova"], senza_immagini=o["senza_immagini"],
                classifica=o["classifica"],
            )
        except NotADirectoryError:
            raise CommandError(f"«{o['cartella']}» non è una cartella.")
        for e in riepilogo.esiti:
            self.stdout.write(f"@@ {e.stato}\t{e.percorso}\t{e.dettaglio}")
        self.stdout.write(
            f"Proprietario: {owner.get_username()} · importati {riepilogo.conta(imp.IMPORTATO)}, "
            f"già presenti {riepilogo.conta(imp.PRESENTE)}, saltati {riepilogo.conta(imp.SALTATO)}, "
            f"errori {riepilogo.conta(imp.ERRORE)}" + (" (PROVA: nulla è stato scritto)" if o["prova"] else "")
        )
