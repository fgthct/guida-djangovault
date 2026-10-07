"""Propone la categoria dei documenti che non ne hanno una (e misura quanto ci si può fidare).

    python manage.py classifica --valuta           # quanto indovina sui TUOI documenti già categorizzati (non scrive nulla)
    python manage.py classifica --prova            # mostra le proposte senza salvarle
    python manage.py classifica                    # salva le proposte in «Da smistare»
    python manage.py classifica --applica          # come sopra, e applica quelle sopra soglia (annullabili)
    python manage.py classifica --senza-ia         # solo documenti simili: nessun testo esce dal server
    python manage.py classifica --limite 20 --utente franky

Per i documenti con consenso IA il testo iniziale va anche a Claude Haiku (pochi centesimi in tutto).
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.models import ClassificationSuggestion, Document
from documents.services import classificazione

S = ClassificationSuggestion.Status


class Command(BaseCommand):
    help = "Propone la categoria dei documenti senza categoria, oppure misura l'affidabilità (--valuta)."

    def add_arguments(self, parser):
        parser.add_argument("--utente", help="nome utente proprietario (default: il primo superutente)")
        parser.add_argument("--limite", type=int, default=0, help="classifica al massimo N documenti")
        parser.add_argument("--senza-ia", action="store_true", help="solo documenti simili: nessun testo va a Claude")
        parser.add_argument("--applica", action="store_true", help="applica le proposte sopra la soglia di fiducia (annullabili)")
        parser.add_argument("--prova", action="store_true", help="non salva nulla: mostra soltanto")
        parser.add_argument("--valuta", action="store_true", help="misura l'accuratezza sui documenti già categorizzati")
        parser.add_argument("--rifai", action="store_true", help="ripropone anche dove c'è già una proposta rifiutata o senza esito")

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
        if o["valuta"]:
            return self.valuta(owner)

        escludi = [S.PENDING, S.AUTO, S.ACCEPTED] + ([] if o["rifai"] else [S.REJECTED, S.NONE])
        docs = (
            Document.objects.filter(owner=owner, category__isnull=True, current_version__isnull=False)
            .exclude(suggestion__status__in=escludi)
            .order_by("pk")
        )
        if o["limite"]:
            docs = docs[: o["limite"]]
        docs = list(docs)
        if not docs:
            self.stdout.write("Nessun documento da classificare.")
            return
        archivio = classificazione.Archivio(owner)
        self.stdout.write(f"{len(docs)} documenti senza categoria · {len(archivio)} documenti già ordinati come riferimento\n")
        conteggi = {"proposte": 0, "applicate": 0, "nessuna": 0}
        for d in docs:
            if o["prova"]:
                esito = classificazione.classifica_documento(d.pk, usa_ia=not o["senza_ia"], archivio=archivio, salva=False)
                if esito is None:
                    continue
                dec, voto, _ia = esito
                nome = dec.categoria.name if dec.categoria else (f"nuova «{dec.nuova}»" if dec.nuova else "—")
                fiducia, fonte = round(dec.fiducia * 100), dec.fonte or "—"
                stato = "proposta" if (dec.categoria or dec.nuova) else "nessuna"
            else:
                sug = classificazione.classifica_documento(
                    d.pk, usa_ia=not o["senza_ia"], archivio=archivio, applica=True if o["applica"] else False
                )
                if sug is None:
                    continue
                nome, fiducia, fonte = sug.nome_categoria or "—", sug.fiducia_percento, sug.source or "—"
                stato = {S.AUTO: "applicata", S.PENDING: "proposta"}.get(sug.status, "nessuna")
            conteggi["applicate" if stato == "applicata" else "proposte" if stato == "proposta" else "nessuna"] += 1
            self.stdout.write(f"#{d.pk:<5} {d.title[:48]:<48}  →  {nome[:28]:<28} {fiducia:>3}%  {fonte:<10} {stato}")
        self.stdout.write(
            f"\nProposte {conteggi['proposte']} · applicate {conteggi['applicate']} · senza proposta {conteggi['nessuna']}"
            + (" (PROVA: nulla è stato salvato)" if o["prova"] else "")
            + ("" if o["prova"] else "\nLe proposte sono nella pagina «Da smistare».")
        )

    def valuta(self, owner):
        self.decisioni(owner)
        r = classificazione.valuta(owner)
        if r["precisione"] is None:
            self.stdout.write(f"Servono più documenti già categorizzati (ora {r['n']}) per misurare qualcosa.")
            return
        self.stdout.write(
            f"Documenti categorizzati usati come prova: {r['n']} (copie quasi identiche escluse dai vicini)\n"
            f"Con una proposta: {r['risposti']} ({r['copertura']:.0%}) · di questi, categoria giusta: {r['precisione']:.0%}\n"
        )
        self.stdout.write("Se si accettasse solo da questa fiducia in su:")
        self.stdout.write("  fiducia   documenti  copertura  giuste")
        for b in r["bins"]:
            prec = "—" if b["precisione"] is None else f"{b['precisione']:.0%}"
            self.stdout.write(f"  ≥ {b['soglia']:.0%}      {b['n']:>6}     {b['copertura']:>6.0%}    {prec:>5}")
        self.stdout.write(
            "\nSoglia per l'applicazione automatica (CLASSIFICAZIONE_SOGLIA_AUTO, ora 0,85): scegli la riga in cui «giuste» è "
            "quasi 100%. Misura solo i documenti simili: con l'IA d'accordo la fiducia è più alta."
        )

    def decisioni(self, owner):
        """Le proposte già esaminate da una persona: accettate così com'erano o corrette?"""
        d = classificazione.esito_delle_decisioni(owner)
        if not d["n"]:
            return
        self.stdout.write(f"Le tue decisioni in «Da smistare»: {d['giuste']} proposte su {d['n']} accettate com'erano ({d['giuste'] / d['n']:.0%})")
        for fonte, (giuste, n) in sorted(d["per_fonte"].items()):
            self.stdout.write(f"  origine {fonte:<10} {giuste}/{n}")
        for f in d["per_fascia"]:
            if f["n"]:
                self.stdout.write(f"  fiducia {f['da']:.0%}–{f['a']:.0%}: {f['giuste']}/{f['n']} giuste")
        self.stdout.write("")
