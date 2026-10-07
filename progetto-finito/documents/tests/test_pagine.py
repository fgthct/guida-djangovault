"""Apertura di un documento al passaggio citato: pagina del PDF, vista «apri», filtri e schede dell'elenco."""

import shutil
from unittest import skipUnless

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core.models import AuditLog
from documents.models import Category, Document
from documents.services import pagine

from .base import DocumentTestCase
from .fixtures import pdf_con_testo

PAGINE = (
    "Introduzione generale al volume e ai suoi scopi principali",
    "Capitolo secondo: le migrazioni del database in Django sei",
    "Capitolo terzo: i template e le stringhe t-string di Python",
)


class RicercaPaginaTests(DocumentTestCase):
    def test_sonde_ignorano_punteggiatura_e_maiuscole(self):
        self.assertEqual(pagine.sonde("Ciao,  Mondo!"), ["ciaomondo"])
        self.assertEqual(pagine.sonde(""), [])
        lunga = pagine.sonde("parola " * 100)
        self.assertTrue(1 <= len(lunga) <= pagine.MAX_SONDE)
        self.assertTrue(all(len(s) == pagine.LUNGHEZZA_SONDA for s in lunga))

    def test_trova_pagina_in_testo_a_capo_e_trattini(self):
        elenco = ["Prima pagina con altro", "le migra-\nzioni del  database sono\nautomatiche in Django", "ultima"]
        self.assertEqual(pagine.trova_pagina_in(elenco, "Le migrazioni del database sono automatiche"), 2)
        self.assertIsNone(pagine.trova_pagina_in(elenco, "un testo che non c'è da nessuna parte"))
        self.assertIsNone(pagine.trova_pagina_in([], "qualcosa"))

    @skipUnless(shutil.which("pdftotext"), "pdftotext non installato")
    def test_pagina_del_passaggio_in_un_pdf_vero(self):
        d = self.nuovo_documento(
            titolo="Libro", uploaded_file=SimpleUploadedFile("libro.pdf", pdf_con_testo(*PAGINE), content_type="application/pdf")
        )
        v = d.current_version
        self.assertEqual(pagine.pagina_del_passaggio(v, "i template e le stringhe t-string di Python"), 3)
        self.assertEqual(pagine.pagina_del_passaggio(v, "le migrazioni del database in Django sei"), 2)
        self.assertIsNone(pagine.pagina_del_passaggio(v, "passaggio inesistente nel libro"))

    def test_non_pdf_non_ha_pagina(self):
        d = self.nuovo_documento(uploaded_file=SimpleUploadedFile("nota.txt", b"testo"))
        self.assertIsNone(pagine.pagina_del_passaggio(d.current_version, "testo"))


class ApriTests(DocumentTestCase):
    def setUp(self):
        self.pdf = self.nuovo_documento(
            titolo="Libro", uploaded_file=SimpleUploadedFile("libro.pdf", pdf_con_testo(*PAGINE), content_type="application/pdf")
        )
        self.txt = self.nuovo_documento(titolo="Nota", uploaded_file=SimpleUploadedFile("nota.txt", b"ciao", content_type="text/plain"))
        self.client.force_login(self.mario)

    def url(self, d):
        return reverse("documents:version_open", args=[d.pk, 1])

    def test_pdf_si_apre_nel_browser_con_csp_per_il_lettore(self):
        r = self.client.get(self.url(self.pdf))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Disposition"].startswith("inline"))
        self.assertIn("object-src 'self'", r["Content-Security-Policy"])
        self.assertTrue(AuditLog.objects.filter(action="document.opened").exists())

    def test_gli_altri_tipi_si_scaricano(self):
        r = self.client.get(self.url(self.txt))
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))
        self.assertTrue(AuditLog.objects.filter(action="document.downloaded").exists())

    def test_chi_non_vede_il_documento_non_lo_apre(self):
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.get(self.url(self.pdf)).status_code, 404)

    def test_login_richiesto(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url(self.pdf)).status_code, 302)


class ElencoTests(DocumentTestCase):
    def setUp(self):
        self.libri = Category.objects.create(name="Libri")
        self.testo = self.nuovo_documento(titolo="Guida", uploaded_file=SimpleUploadedFile("guida.pdf", b"%PDF a"), category=self.libri)
        self.img = self.nuovo_documento(titolo="Copertina", uploaded_file=SimpleUploadedFile("copertina.JPG", b"x"))
        self.client.force_login(self.mario)

    def titoli(self, **q):
        return {d.title for d in self.client.get(reverse("documents:list"), q).context["pagina"]}

    def test_filtro_tipo(self):
        self.assertEqual(self.titoli(), {"Guida", "Copertina"})
        self.assertEqual(self.titoli(tipo="documenti"), {"Guida"})
        self.assertEqual(self.titoli(tipo="immagini"), {"Copertina"})

    def test_filtro_senza_categoria(self):
        self.assertEqual(self.titoli(categoria="nessuna"), {"Copertina"})
        self.assertEqual(self.titoli(categoria=str(self.libri.pk)), {"Guida"})

    def test_schede_con_conteggi(self):
        schede = {s["nome"]: s["n"] for s in self.client.get(reverse("documents:list")).context["schede"]}
        self.assertEqual(schede, {"Tutte": 2, "Libri": 1, "Senza categoria": 1})

    def test_schede_non_contano_documenti_invisibili(self):
        self.nuovo_documento(owner=self.luisa, titolo="Di Luisa", uploaded_file=SimpleUploadedFile("l.pdf", b"%PDF l"))
        self.assertEqual(self.client.get(reverse("documents:list")).context["schede"][0]["n"], 2)

    def test_badge_bozza_nascosto_ma_non_gli_altri_stati(self):
        # «Bozza» compare solo nell'opzione del filtro «Ogni stato», non come badge nelle righe.
        self.assertContains(self.client.get(reverse("documents:list")), "Bozza", count=1)
        Document.objects.filter(pk=self.testo.pk).update(status=Document.Status.APPROVED)
        self.assertContains(self.client.get(reverse("documents:list")), "Approvato", count=2)  # opzione + badge
