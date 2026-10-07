from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from ..models import Category, Document
from ..services import upload_web
from .base import DocumentTestCase


class CaricaFileServizioTests(DocumentTestCase):
    def test_percorso_relativo_prima_cartella(self):
        f = upload_web._cartella_categoria
        self.assertEqual(f("Libri/Romanzi/zolfo.pdf"), "Romanzi")  # sottocartella della cartella scelta
        self.assertEqual(f("Libri/Romanzi/Altro/zolfo.pdf"), "Romanzi")  # solo il primo livello
        self.assertEqual(f("Libri/zolfo.pdf"), "Libri")  # direttamente nella cartella scelta
        self.assertIsNone(f("zolfo.pdf"))
        self.assertEqual(f("../x/y/z.pdf"), "y")
        self.assertEqual(f("a\\b.pdf"), "a")

    def test_categoria_dalla_cartella_riusa_quella_esistente(self):
        esistente = Category.objects.create(name="Libri")
        self.assertEqual(upload_web.risolvi_categoria("cartella", "radice/libri/a.pdf"), esistente)
        self.assertEqual(Category.objects.count(), 1)
        self.assertEqual(upload_web.risolvi_categoria("cartella", "Nuova/a.pdf").name, "Nuova")

    def test_scelte_senza_categoria(self):
        for scelta in ("", "nessuna", "automatica", "boh", "999999"):
            self.assertIsNone(upload_web.risolvi_categoria(scelta, "x/a.pdf"), scelta)


class CaricaFileVistaTests(DocumentTestCase):
    def setUp(self):
        self.url = reverse("documents:upload_file")
        self.client.force_login(self.mario)

    def invia(self, nome="a.pdf", contenuto=b"%PDF-1.4 uno", **extra):
        dati = {"file": SimpleUploadedFile(nome, contenuto), **extra}
        return self.client.post(self.url, dati)

    def test_pagina_richiede_login_e_non_ha_script_inline(self):
        self.client.logout()
        r = self.client.get(reverse("documents:upload"))
        self.assertEqual(r.status_code, 302)
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:upload"))
        self.assertContains(r, "Trascina qui file o cartelle")
        self.assertContains(r, "js/carica.js")
        self.assertNotContains(r, "onclick=")
        # nessun <script> senza src (la CSP li bloccherebbe)
        import re

        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", r.content.decode()), [])

    def test_endpoint_solo_post_e_con_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.invia().status_code, 302)
        self.assertEqual(Document.objects.count(), 0)

    def test_crea_documento_con_titolo_dal_nome_e_consenso(self):
        r = self.invia("Guida_Django-6.1_finale.pdf", ai_allowed="1")
        dati = r.json()
        self.assertEqual(dati["stato"], "importato")
        d = Document.objects.get(pk=dati["id"])
        self.assertEqual(d.title, "Guida Django-6.1 finale")
        self.assertEqual(d.owner, self.mario)
        self.assertTrue(d.ai_allowed)
        self.assertEqual(dati["url"], d.get_absolute_url())

    def test_senza_consenso_ia_di_default(self):
        d = Document.objects.get(pk=self.invia(ai_allowed="0").json()["id"])
        self.assertFalse(d.ai_allowed)
        d2 = Document.objects.get(pk=self.invia("b.pdf", b"%PDF-1.4 due").json()["id"])
        self.assertFalse(d2.ai_allowed)  # campo assente = nessun consenso

    def test_categoria_scelta_e_da_cartella(self):
        cat = Category.objects.create(name="Romanzi")
        d = Document.objects.get(pk=self.invia(categoria=str(cat.pk)).json()["id"])
        self.assertEqual(d.category, cat)
        r = self.invia("b.pdf", b"%PDF-1.4 due", categoria="cartella", percorso="miei_libri/Guide/b.pdf")
        self.assertEqual(Document.objects.get(pk=r.json()["id"]).category.name, "Guide")

    def test_duplicato_non_crea_un_secondo_documento(self):
        primo = self.invia().json()
        secondo = self.invia("copia.pdf").json()
        self.assertEqual(secondo["stato"], "presente")
        self.assertEqual(secondo["id"], primo["id"])
        self.assertEqual(Document.objects.count(), 1)

    def test_il_duplicato_di_un_documento_non_visibile_non_conta(self):
        self.nuovo_documento(owner=self.luisa, uploaded_file=self.file(contenuto=b"%PDF-1.4 uno"))
        r = self.invia(contenuto=b"%PDF-1.4 uno").json()
        self.assertEqual(r["stato"], "importato")
        self.assertEqual(Document.objects.filter(owner=self.mario).count(), 1)

    def test_tipo_non_ammesso_vuoto_e_troppo_grande(self):
        self.assertEqual(self.invia("virus.exe").json()["stato"], "saltato")
        self.assertEqual(self.invia("vuoto.pdf", b"").json()["stato"], "saltato")
        with self.settings(DOCUMENTS_MAX_UPLOAD_MB=0):
            r = self.invia("grande.pdf", b"x" * 10).json()
        self.assertEqual(r["stato"], "saltato")
        self.assertEqual(Document.objects.count(), 0)

    def test_senza_file_errore_400(self):
        r = self.client.post(self.url, {})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["stato"], "errore")

    def test_collegamenti_dalla_lista(self):
        r = self.client.get(reverse("documents:list"))
        self.assertContains(r, reverse("documents:upload"))
