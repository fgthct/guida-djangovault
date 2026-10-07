"""Assegna una categoria a documenti già importati, indicandoli per titolo.

    python manage.py categorizza Romanzi zolfo "la festa dei morti"
    python manage.py categorizza Romanzi --senza-categoria --contiene romanzo
    python manage.py categorizza Copertine --estensione png,jpg,jpeg --da "Libri informatica"
    python manage.py categorizza Romanzi zolfo --prova

I titoli si confrontano senza badare alle maiuscole. Di norma si cambiano solo i documenti SENZA categoria
(`--sovrascrivi` per spostare anche gli altri, `--da Categoria` per spostare solo quelli di una categoria). La categoria è creata se manca.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from documents.models import Category, Document


class Command(BaseCommand):
    help = "Assegna una categoria ai documenti indicati per titolo."

    def add_arguments(self, parser):
        parser.add_argument("categoria")
        parser.add_argument("titoli", nargs="*", help="titoli esatti (senza badare alle maiuscole)")
        parser.add_argument("--contiene", action="append", default=[], help="titoli che contengono questo testo (ripetibile)")
        parser.add_argument("--estensione", action="append", default=[], help="file con questa estensione, es. jpg o png,jpg (ripetibile): per le immagini che hanno lo stesso titolo del libro")
        parser.add_argument("--da", help="considera solo i documenti che sono in questa categoria (implica --sovrascrivi)")
        parser.add_argument("--sovrascrivi", action="store_true", help="sposta anche i documenti che hanno già una categoria")
        parser.add_argument("--prova", action="store_true", help="elenca soltanto")

    def handle(self, *args, **o):
        estensioni = [e.strip().lower().lstrip(".") for voce in o["estensione"] for e in voce.split(",") if e.strip()]
        if not o["titoli"] and not o["contiene"] and not estensioni:
            raise CommandError("Indica almeno un titolo, --contiene o --estensione.")
        filtro = Q()
        for t in o["titoli"]:
            filtro |= Q(title__iexact=t)
        for t in o["contiene"]:
            filtro |= Q(title__icontains=t)
        for e in estensioni:
            filtro |= Q(current_version__original_name__iendswith=f".{e}")
        docs = Document.objects.filter(filtro)
        if o["da"]:
            docs = docs.filter(category__name__iexact=o["da"].strip())
        elif not o["sovrascrivi"]:
            docs = docs.filter(category__isnull=True)
        docs = list(docs.order_by("title"))
        if not docs:
            self.stdout.write("Nessun documento corrisponde (o hanno già una categoria: usa --sovrascrivi).")
            return
        if not o["prova"]:
            nome = o["categoria"].strip()[:80]
            cat = Category.objects.filter(name__iexact=nome).first() or Category.objects.create(name=nome)
            for d in docs:
                d.category = cat
                d.save(update_fields=["category", "updated_at"])
        for d in docs:
            self.stdout.write(f"  {d.title}")
        self.stdout.write(f"{len(docs)} document{'o' if len(docs) == 1 else 'i'} {'da assegnare' if o['prova'] else 'assegnati'} a «{o['categoria']}».")
