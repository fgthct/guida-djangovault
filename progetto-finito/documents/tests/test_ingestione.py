"""Ingestione automatica: cartella osservata, posta (con una finta casella IMAP) e comando."""

import io
import os
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from documents.models import ClassificationSuggestion, Document
from documents.services import ingestione

from .base import DocumentTestCase


class CartellaBase(DocumentTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.radice = Path(self._tmp.name) / "arrivi"
        self.over = override_settings(INGESTIONE_CARTELLA=str(self.radice), INGESTIONE_ETA_MINIMA=0, INGESTIONE_UTENTE="mario")
        self.over.enable()
        self.addCleanup(self.over.disable)

    def scrivi(self, rel, contenuto=b"%PDF-1.4 nota di prova", eta=3600):
        p = self.radice / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(contenuto)
        t = time.time() - eta
        os.utime(p, (t, t))
        return p


class CartellaOsservataTests(CartellaBase):
    def test_importa_e_toglie_dalla_cartella(self):
        f = self.scrivi("uno.pdf")
        r = ingestione.osserva_cartella()
        self.assertEqual((r["importati"], r["scartati"]), (1, 0))
        d = Document.objects.get(title="uno")
        self.assertEqual(d.owner, self.mario)
        self.assertFalse(f.exists())
        self.assertEqual(ClassificationSuggestion.objects.get(document=d).status, ClassificationSuggestion.Status.REQUESTED)

    def test_il_consenso_ia_di_default_e_spento(self):
        self.scrivi("uno.pdf")
        ingestione.osserva_cartella()
        self.assertFalse(Document.objects.get(title="uno").ai_allowed)
        self.scrivi("due.pdf", b"%PDF-1.4 altro")
        with override_settings(INGESTIONE_CONSENSO_IA=True):
            ingestione.osserva_cartella()
        self.assertTrue(Document.objects.get(title="due").ai_allowed)

    def test_non_prende_i_file_ancora_in_copia(self):
        f = self.scrivi("nuovo.pdf", eta=1)
        with override_settings(INGESTIONE_ETA_MINIMA=60):
            r = ingestione.osserva_cartella()
        self.assertEqual(r["importati"], 0)
        self.assertTrue(f.exists())
        self.assertFalse(Document.objects.exists())

    def test_la_sottocartella_diventa_la_categoria_e_non_classifica(self):
        self.scrivi("Romanzi/uno.pdf")
        ingestione.osserva_cartella()
        d = Document.objects.get(title="uno")
        self.assertEqual(d.category.name, "Romanzi")
        self.assertFalse(ClassificationSuggestion.objects.filter(document=d).exists())

    def test_i_file_non_importabili_vanno_in_scartati_senza_perderli(self):
        self.scrivi("musica.webm", b"x" * 10)
        self.scrivi("Sub/vuoto.pdf", b"")
        r = ingestione.osserva_cartella()
        self.assertEqual((r["importati"], r["scartati"]), (0, 2))
        self.assertTrue((self.radice / "_scartati" / "musica.webm").is_file())
        self.assertTrue((self.radice / "_scartati" / "Sub" / "vuoto.pdf").is_file())
        # e al giro dopo non vengono riesaminati
        self.assertEqual(ingestione.osserva_cartella()["scartati"], 0)

    def test_scartati_con_lo_stesso_nome_non_si_sovrascrivono(self):
        self.scrivi("a.webm", b"1")
        ingestione.osserva_cartella()
        self.scrivi("a.webm", b"2")
        ingestione.osserva_cartella()
        self.assertEqual(sorted(p.name for p in (self.radice / "_scartati").iterdir()), ["a.1.webm", "a.webm"])

    def test_doppione_si_toglie_senza_crearne_un_altro(self):
        self.scrivi("uno.pdf")
        ingestione.osserva_cartella()
        f = self.scrivi("copia.pdf")
        r = ingestione.osserva_cartella()
        self.assertEqual((r["importati"], r["presenti"]), (0, 1))
        self.assertEqual(Document.objects.count(), 1)
        self.assertFalse(f.exists())

    def test_immagini_scartate_di_default(self):
        self.scrivi("foto.png", b"\x89PNG\r\n\x1a\n0000")
        self.assertEqual(ingestione.osserva_cartella()["importati"], 0)
        self.assertTrue((self.radice / "_scartati" / "foto.png").exists())

    def test_prova_non_sposta_nulla(self):
        f = self.scrivi("uno.pdf")
        r = ingestione.osserva_cartella(prova=True)
        self.assertTrue(f.exists())
        self.assertFalse(Document.objects.exists())
        self.assertEqual(r["importati"], 1)  # «da importare»

    def test_crea_la_cartella_se_manca_e_utente_inesistente(self):
        self.assertTrue(ingestione.osserva_cartella()["attiva"])
        self.assertTrue(self.radice.is_dir())
        with override_settings(INGESTIONE_UTENTE="nessuno"):
            with self.assertRaises(ingestione.IngestioneNonConfigurata):
                ingestione.osserva_cartella()

    def test_senza_utente_usa_il_primo_superutente(self):
        with override_settings(INGESTIONE_UTENTE=""):
            self.assertEqual(ingestione.proprietario(), self.admin)


class FintaCasella:
    """Sostituisce imaplib.IMAP4_SSL: una casella con messaggi dati, registra i flag messi."""

    messaggi: list[bytes] = []
    visti: list[bytes] = []
    errore_login = False
    ultima = None

    def __init__(self, host, porta=993):
        FintaCasella.ultima = self
        self.host = host

    def login(self, utente, password):
        if FintaCasella.errore_login:
            import imaplib

            raise imaplib.IMAP4.error("LOGIN failed")

    def select(self, cartella):
        return ("OK", [b"1"])

    def search(self, _charset, criterio):
        assert criterio == "UNSEEN"
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(FintaCasella.messaggi)))])

    def fetch(self, numero, parti):
        assert "PEEK" in parti, "si legge senza segnare come letto"
        return ("OK", [(b"1 (BODY[] {n}", FintaCasella.messaggi[int(numero) - 1]), b")"])

    def store(self, numero, op, flag):
        FintaCasella.visti.append(numero)
        return ("OK", [])

    def logout(self):
        return ("BYE", [])


def mail(da="franky@esempio.it", oggetto="Per l'archivio", corpo="", allegati=(), auth="mx.google.com; dkim=pass header.i=@esempio.it; spf=pass"):
    m = EmailMessage()
    if auth is not None:
        m["Authentication-Results"] = auth
    m["From"] = f"Franky <{da}>"
    m["To"] = "archivio@esempio.it"
    m["Subject"] = oggetto
    m.set_content(corpo or "Ciao")
    for nome, dati in allegati:
        m.add_attachment(dati, maintype="application", subtype="octet-stream", filename=nome)
    return m.as_bytes()


POSTA = dict(
    INGESTIONE_IMAP_HOST="imap.esempio.it", INGESTIONE_IMAP_UTENTE="archivio@esempio.it", INGESTIONE_IMAP_PASSWORD="x",
    INGESTIONE_MITTENTI=["franky@esempio.it", "@studio.it"], INGESTIONE_UTENTE="mario",
)


@override_settings(**POSTA)
class PostaTests(DocumentTestCase):
    def setUp(self):
        FintaCasella.messaggi, FintaCasella.visti, FintaCasella.errore_login = [], [], False
        p = mock.patch("documents.services.ingestione.imaplib.IMAP4_SSL", FintaCasella)
        p.start()
        self.addCleanup(p.stop)

    def test_importa_gli_allegati_e_segna_come_letto(self):
        FintaCasella.messaggi = [mail(allegati=[("contratto.pdf", b"%PDF-1.4 contratto"), ("nota.txt", b"testo dell'allegato")])]
        r = ingestione.leggi_posta()
        self.assertEqual(r["documenti"], 2)
        self.assertEqual(set(Document.objects.values_list("title", flat=True)), {"contratto", "nota"})
        self.assertEqual(FintaCasella.visti, [b"1"])
        d = Document.objects.get(title="contratto")
        self.assertEqual((d.owner, d.ai_allowed), (self.mario, False))
        self.assertEqual(ClassificationSuggestion.objects.get(document=d).status, "richiesta")

    def test_mittente_non_ammesso_e_ignorato(self):
        FintaCasella.messaggi = [mail(da="sconosciuto@altro.it", allegati=[("x.pdf", b"%PDF-1.4 x")])]
        r = ingestione.leggi_posta()
        self.assertEqual(r["documenti"], 0)
        self.assertEqual(r["messaggi"][0]["ignorato"], "mittente non ammesso")
        self.assertFalse(Document.objects.exists())

    def test_dominio_ammesso(self):
        FintaCasella.messaggi = [mail(da="segreteria@studio.it", allegati=[("x.pdf", b"%PDF-1.4 x")])]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 1)
        self.assertFalse(ingestione.mittente_ammesso("segreteria@studio.it.evil.com"))
        self.assertFalse(ingestione.mittente_ammesso("evil@sotto.studio.it"))
        self.assertFalse(ingestione.mittente_ammesso(""))

    def test_mittente_falsificato_non_passa_senza_spf_o_dkim(self):
        for auth in (None, "mx.google.com; dkim=fail; spf=fail", "mx.google.com; spf=softfail"):
            FintaCasella.messaggi = [mail(auth=auth, allegati=[("x.pdf", b"%PDF-1.4 x")])]
            r = ingestione.leggi_posta()
            self.assertEqual(r["messaggi"][0].get("ignorato"), "mittente non verificato (SPF/DKIM)", auth)
        self.assertFalse(Document.objects.exists())

    def test_conta_solo_l_intestazione_piu_in_alto(self):
        """Chi scrive può aggiungere un proprio «Authentication-Results: dkim=pass» sotto quello vero."""
        grezzo = mail(auth="mx.google.com; dkim=fail; spf=fail", allegati=[("x.pdf", b"%PDF-1.4 x")])
        grezzo = grezzo + b""  # intestazione finta aggiunta più in basso
        finto = grezzo.replace(b"From:", b"Authentication-Results: falso; dkim=pass\r\nFrom:", 1)
        FintaCasella.messaggi = [finto]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 0)

    @override_settings(INGESTIONE_RICHIEDI_AUTENTICAZIONE=False)
    def test_verifica_spegnibile(self):
        FintaCasella.messaggi = [mail(auth=None, allegati=[("x.pdf", b"%PDF-1.4 x")])]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 1)

    def test_email_senza_allegati_diventa_una_nota(self):
        FintaCasella.messaggi = [mail(oggetto="Idea per il capitolo 4", corpo="Ricordarsi di rivedere la scena del porto e il dialogo.")]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 1)
        d = Document.objects.get()
        self.assertEqual(d.title, "Idea per il capitolo 4")
        self.assertIn("scena del porto", d.current_version.file.read().decode())

    def test_nota_troppo_corta_non_conta(self):
        FintaCasella.messaggi = [mail(corpo="ok")]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 0)

    @override_settings(INGESTIONE_POSTA_CORPO=False)
    def test_note_spegnibili(self):
        FintaCasella.messaggi = [mail(corpo="Un testo abbastanza lungo da diventare una nota.")]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 0)

    def test_allegato_non_ammesso_e_nome_pericoloso(self):
        FintaCasella.messaggi = [mail(allegati=[("virus.exe", b"MZ00"), ("../../etc/passwd.txt", b"contenuto di prova")])]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 1)
        d = Document.objects.get()
        self.assertEqual(d.current_version.original_name, "passwd.txt")

    def test_immagini_della_firma_ignorate(self):
        FintaCasella.messaggi = [mail(corpo="Ciao a tutti, in allegato la firma e il documento vero.", allegati=[("logo.png", b"\x89PNG\r\n\x1a\n0000"), ("vero.pdf", b"%PDF-1.4 vero")])]
        self.assertEqual(ingestione.leggi_posta()["documenti"], 1)
        self.assertEqual(Document.objects.get().title, "vero")

    def test_doppioni_tra_messaggi(self):
        FintaCasella.messaggi = [mail(allegati=[("a.pdf", b"%PDF-1.4 stesso")]), mail(allegati=[("b.pdf", b"%PDF-1.4 stesso")])]
        r = ingestione.leggi_posta()
        self.assertEqual((r["documenti"], Document.objects.count()), (1, 1))
        self.assertEqual(r["messaggi"][1]["presenti"], 1)

    def test_prova_non_importa_ne_segna_come_letto(self):
        FintaCasella.messaggi = [mail(allegati=[("a.pdf", b"%PDF-1.4 a")])]
        r = ingestione.leggi_posta(prova=True)
        self.assertEqual((len(r["messaggi"]), Document.objects.count(), FintaCasella.visti), (1, 0, []))

    def test_login_rifiutato_non_solleva(self):
        FintaCasella.errore_login = True
        r = ingestione.leggi_posta()
        self.assertIn("errore IMAP", r["errore"])

    def test_server_irraggiungibile_non_solleva(self):
        with mock.patch("documents.services.ingestione.imaplib.IMAP4_SSL", side_effect=OSError("no route")):
            self.assertEqual(ingestione.leggi_posta()["errore"], "server di posta non raggiungibile")

    def test_un_messaggio_rotto_non_blocca_gli_altri(self):
        FintaCasella.messaggi = [b"\xff\xfe non e' una mail", mail(allegati=[("ok.pdf", b"%PDF-1.4 ok")])]
        with mock.patch.object(ingestione, "_elabora_messaggio", side_effect=[RuntimeError("boom"), {"documenti": 1, "presenti": 0, "saltati": 0, "mittente": "a@b.it", "oggetto": ""}]):
            r = ingestione.leggi_posta()
        self.assertEqual(len(r["messaggi"]), 2)
        self.assertEqual(len(FintaCasella.visti), 2)

    @override_settings(INGESTIONE_MITTENTI=[])
    def test_senza_elenco_di_mittenti_la_posta_non_funziona(self):
        self.assertFalse(ingestione.posta_configurata())
        r = ingestione.leggi_posta()
        self.assertFalse(r["attiva"])


class ComandoETaskTests(CartellaBase):
    def esegui(self, *args):
        out = io.StringIO()
        call_command("ingestione", *args, stdout=out)
        return out.getvalue()

    def test_stato_non_mostra_la_password(self):
        with override_settings(**{**POSTA, "INGESTIONE_IMAP_PASSWORD": "segretissima"}):
            out = self.esegui("stato")
        self.assertIn("imap.esempio.it", out)
        self.assertIn("franky@esempio.it", out)
        self.assertNotIn("segretissima", out)

    def test_cartella_prova(self):
        self.scrivi("uno.pdf")
        out = self.esegui("cartella", "--prova")
        self.assertIn("uno.pdf", out)
        self.assertIn("(PROVA)", out)

    def test_posta_spenta_da_errore_chiaro(self):
        from django.core.management.base import CommandError

        with self.assertRaisesMessage(CommandError, "posta non configurata"):
            self.esegui("posta")

    def test_task(self):
        from documents.tasks import leggi_posta_task, osserva_cartella_task

        self.scrivi("uno.pdf")
        self.assertEqual(osserva_cartella_task(), 1)
        self.assertEqual(leggi_posta_task(), 0)  # posta non configurata: niente da fare, nessun errore
        with override_settings(INGESTIONE_UTENTE="nessuno"):
            self.assertEqual(osserva_cartella_task(), 0)

    def test_beat_schedule_segue_le_impostazioni(self):
        from config.celery import app

        self.assertIn("osserva-cartella", app.conf.beat_schedule)
        self.assertNotIn("leggi-posta", app.conf.beat_schedule)  # posta spenta in questo ambiente
