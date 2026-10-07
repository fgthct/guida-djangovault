"""Elimina documenti per id (stessa operazione del pulsante «Elimina»: versioni, passaggi e file su disco).

    python manage.py elimina_documenti 26 29 34            # PROVA: elenca cosa verrebbe eliminato, non tocca nulla
    python manage.py elimina_documenti 26 29 34 --conferma # elimina davvero (registrato nel Registro)

Senza `--conferma` è sempre una prova. Gli id inesistenti vengono segnalati e ignorati. Opera come il primo superutente
(`--utente nome` per un altro). Irreversibile: `./scripts/prod.sh elimina … --conferma` fa prima un backup.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.models import Document
from documents.services import delete_document


class Command(BaseCommand):
    help = "Elimina documenti per id (di norma in prova; --conferma per eliminare davvero)."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="+", type=int)
        parser.add_argument("--utente", help="utente che esegue l'operazione (default: il primo superutente)")
        parser.add_argument("--conferma", action="store_true", help="elimina davvero (senza, è solo una prova)")

    def handle(self, *args, **o):
        User = get_user_model()
        qs = User.objects.filter(is_active=True)
        utente = qs.filter(username=o["utente"]).first() if o["utente"] else qs.filter(is_superuser=True).order_by("pk").first()
        if utente is None:
            raise CommandError("Utente non trovato (serve un superutente o --utente).")
        ids = list(dict.fromkeys(o["ids"]))
        trovati = {d.pk: d for d in Document.objects.filter(pk__in=ids).select_related("category", "current_version")}
        eliminati = 0
        for pk in ids:
            d = trovati.get(pk)
            if d is None:
                self.stdout.write(f"  #{pk:<5} NON TROVATO (già eliminato?)")
                continue
            v = d.current_version
            self.stdout.write(f"  #{pk:<5} {(v.original_name if v else d.title)[:50]:<50} {d.category or '—'}")
            if o["conferma"]:
                delete_document(user=utente, document=d)
                eliminati += 1
        if o["conferma"]:
            self.stdout.write(f"Eliminati {eliminati} documenti su {len(ids)} indicati.")
        else:
            self.stdout.write(
                f"PROVA: {len(trovati)} documenti verrebbero eliminati. Per eliminarli davvero aggiungi --conferma. Nulla è stato modificato."
            )
