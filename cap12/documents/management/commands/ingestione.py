"""Esegue a mano un controllo dell'ingestione automatica (di norma lo fa il worker ogni pochi minuti).

    python manage.py ingestione cartella            # importa ciò che c'è nella cartella osservata
    python manage.py ingestione cartella --prova    # mostra cosa farebbe, senza spostare nulla
    python manage.py ingestione posta               # legge la casella email dedicata
    python manage.py ingestione posta --prova       # elenca i messaggi non letti, senza importare né segnare come letti
    python manage.py ingestione stato               # mostra le impostazioni in uso (senza password)
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from documents.services import ingestione


class Command(BaseCommand):
    help = "Controlla subito la cartella osservata o la casella email dedicata."

    def add_arguments(self, parser):
        parser.add_argument("canale", choices=["cartella", "posta", "stato"])
        parser.add_argument("--prova", action="store_true", help="non importa e non sposta nulla")

    def handle(self, *args, **o):
        try:
            if o["canale"] == "stato":
                return self.stato()
            if o["canale"] == "cartella":
                return self.cartella(o["prova"])
            return self.posta(o["prova"])
        except ingestione.IngestioneNonConfigurata as e:
            raise CommandError(str(e))

    def stato(self):
        s = settings
        self.stdout.write(f"Proprietario dei documenti: {ingestione.proprietario().get_username()}")
        self.stdout.write(f"Consenso IA ai documenti in arrivo: {'sì' if s.INGESTIONE_CONSENSO_IA else 'no'}")
        self.stdout.write(f"Cartella osservata: {s.INGESTIONE_CARTELLA} (ogni {s.INGESTIONE_CARTELLA_OGNI} s, file fermi da almeno {s.INGESTIONE_ETA_MINIMA} s)"
                          if s.INGESTIONE_CARTELLA_OGNI else "Cartella osservata: spenta")
        if ingestione.posta_configurata():
            self.stdout.write(f"Posta: {s.INGESTIONE_IMAP_UTENTE} su {s.INGESTIONE_IMAP_HOST}, ogni {s.INGESTIONE_POSTA_OGNI} s; "
                              f"mittenti ammessi: {', '.join(s.INGESTIONE_MITTENTI)}; "
                              f"verifica SPF/DKIM: {'sì' if s.INGESTIONE_RICHIEDI_AUTENTICAZIONE else 'NO'}")
        else:
            self.stdout.write("Posta: spenta (servono host, utente, password e almeno un mittente ammesso)")

    def cartella(self, prova):
        r = ingestione.osserva_cartella(prova=prova)
        if not r["attiva"]:
            raise CommandError(r["motivo"])
        for stato, percorso, dettaglio in r["dettagli"]:
            self.stdout.write(f"  {stato:<9} {percorso}  {dettaglio}")
        self.stdout.write(f"Importati {r['importati']} · già presenti {r['presenti']} · scartati {r['scartati']}" + (" (PROVA)" if prova else ""))

    def posta(self, prova):
        r = ingestione.leggi_posta(prova=prova)
        if not r["attiva"]:
            raise CommandError(r["motivo"])
        if r.get("errore"):
            self.stdout.write(f"ATTENZIONE: {r['errore']}")
        for m in r["messaggi"]:
            dettaglio = m.get("ignorato") or m.get("errore") or ("da importare" if m.get("prova") else f"{m['documenti']} nuovi, {m['presenti']} già presenti, {m['saltati']} saltati")
            self.stdout.write(f"  {m['mittente'] or '?':<32} «{m['oggetto']}»  {dettaglio}")
        self.stdout.write(f"{len(r['messaggi'])} messaggi esaminati" + (" (PROVA)" if prova else f", {r.get('documenti', 0)} documenti creati"))
