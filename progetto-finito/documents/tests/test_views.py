from django.test import override_settings
from django.urls import reverse

from documents.models import Category, Document

from .base import DocumentTestCase

HTMX = {"HTTP_HX_REQUEST": "true"}


class ElencoTests(DocumentTestCase):
    def setUp(self):
        self.client.force_login(self.mario)

    def test_elenco_mostra_solo_i_propri_documenti(self):
        self.nuovo_documento(titolo="Mio documento")
        self.nuovo_documento(owner=self.luisa, titolo="Segreto di Luisa", uploaded_file=self.file(contenuto=b"altro"))
        r = self.client.get(reverse("documents:list"))
        self.assertContains(r, "Mio documento")
        self.assertNotContains(r, "Segreto di Luisa")

    def test_superutente_vede_tutto(self):
        self.nuovo_documento(owner=self.luisa, titolo="Di Luisa")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("documents:list")), "Di Luisa")

    def test_ricerca_per_titolo_e_descrizione(self):
        self.nuovo_documento(titolo="Fattura marzo", description="energia")
        self.nuovo_documento(titolo="Contratto", description="affitto ufficio", uploaded_file=self.file(contenuto=b"b"))
        r = self.client.get(reverse("documents:list"), {"q": "FATTURA"})
        self.assertContains(r, "Fattura marzo")
        self.assertNotContains(r, "Contratto")
        r = self.client.get(reverse("documents:list"), {"q": "affitto"})
        self.assertContains(r, "Contratto")

    def test_ricerca_non_rivela_documenti_altrui(self):
        self.nuovo_documento(owner=self.luisa, titolo="Bilancio riservato")
        r = self.client.get(reverse("documents:list"), {"q": "bilancio"})
        self.assertNotContains(r, "Bilancio riservato")
        self.assertContains(r, "Nessun documento corrisponde")

    def test_filtro_categoria(self):
        c = Category.objects.create(name="Legale")
        self.nuovo_documento(titolo="Con categoria", category=c)
        self.nuovo_documento(titolo="Senza", uploaded_file=self.file(contenuto=b"c"))
        r = self.client.get(reverse("documents:list"), {"categoria": c.pk})
        self.assertContains(r, "Con categoria")
        self.assertNotContains(r, "Altro documento")

    def test_parametri_strani_non_rompono(self):
        for params in [{"categoria": "abc"}, {"page": "zzz"}, {"page": "999"}, {"q": "%' OR 1=1 --"}]:
            self.assertEqual(self.client.get(reverse("documents:list"), params).status_code, 200, params)

    def test_htmx_riceve_solo_il_frammento(self):
        self.nuovo_documento(titolo="Frammento")
        r = self.client.get(reverse("documents:list"), **HTMX)
        self.assertContains(r, "Frammento")
        self.assertNotContains(r, "<html")
        self.assertIn("HX-Request", r["Vary"])

    def test_senza_htmx_pagina_intera(self):
        self.assertContains(self.client.get(reverse("documents:list")), "<html")

    def test_ripristino_cronologia_htmx_riceve_pagina_intera(self):
        r = self.client.get(reverse("documents:list"), HTTP_HX_REQUEST="true", HTTP_HX_HISTORY_RESTORE_REQUEST="true")
        self.assertContains(r, "<html")

    def test_paginazione(self):
        for i in range(27):
            self.nuovo_documento(titolo=f"Doc {i:02d}", uploaded_file=self.file(contenuto=f"x{i}".encode()))
        r = self.client.get(reverse("documents:list"))  # 50 per pagina di default
        self.assertEqual(len(r.context["pagina"]), 27)
        r = self.client.get(reverse("documents:list"), {"per": 25})
        self.assertContains(r, "Pagina 1 di 2")
        self.assertEqual(len(r.context["pagina"]), 25)
        self.assertContains(self.client.get(reverse("documents:list"), {"per": 25, "page": 2}), "Pagina 2 di 2")
        # valori non previsti: si torna al predefinito
        self.assertEqual(len(self.client.get(reverse("documents:list"), {"per": 9999}).context["pagina"]), 27)

    def test_contatore_in_elaborazione(self):
        self.nuovo_documento()
        r = self.client.get(reverse("documents:list"))
        self.assertEqual(r.context["in_elaborazione"], 1)
        self.assertContains(r, "1 in elaborazione")


class CreazioneTests(DocumentTestCase):
    def setUp(self):
        self.client.force_login(self.mario)
        self.url = reverse("documents:create")

    def test_upload_valido(self):
        r = self.client.post(self.url, {"title": "Nuovo", "description": "d", "file": self.file()})
        doc = Document.objects.get()
        self.assertRedirects(r, doc.get_absolute_url())
        self.assertEqual(doc.owner, self.mario)

    def test_il_proprietario_non_e_impostabile_dal_form(self):
        self.client.post(self.url, {"title": "X", "owner": self.luisa.pk, "status": "approved", "file": self.file()})
        doc = Document.objects.get()
        self.assertEqual(doc.owner, self.mario)
        self.assertEqual(doc.status, Document.Status.DRAFT)

    def test_titolo_obbligatorio(self):
        r = self.client.post(self.url, {"title": "", "file": self.file()})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(Document.objects.count(), 0)

    def test_estensione_non_consentita(self):
        r = self.client.post(self.url, {"title": "X", "file": self.file(nome="virus.exe")})
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Tipo di file non consentito", status_code=422)

    def test_file_vuoto(self):
        r = self.client.post(self.url, {"title": "X", "file": self.file(contenuto=b"")})
        self.assertEqual(r.status_code, 422)

    @override_settings(DOCUMENTS_MAX_UPLOAD_MB=0)
    def test_file_troppo_grande(self):
        r = self.client.post(self.url, {"title": "X", "file": self.file()})
        self.assertContains(r, "supera il limite", status_code=422)

    def test_senza_file(self):
        self.assertEqual(self.client.post(self.url, {"title": "X"}).status_code, 422)

    def test_duplicato_mostra_avviso(self):
        self.nuovo_documento(titolo="Originale")
        r = self.client.post(self.url, {"title": "Copia", "file": self.file()}, follow=True)
        self.assertContains(r, "esiste già")


class DettaglioModificaEliminaTests(DocumentTestCase):
    def setUp(self):
        self.client.force_login(self.mario)
        self.doc = self.nuovo_documento(titolo="Dettaglio")

    def test_dettaglio(self):
        r = self.client.get(self.doc.get_absolute_url())
        self.assertContains(r, "Dettaglio")
        self.assertContains(r, self.doc.current_version.checksum)

    def test_modifica(self):
        r = self.client.post(reverse("documents:edit", args=[self.doc.pk]), {"title": "Cambiato", "description": ""})
        self.assertRedirects(r, self.doc.get_absolute_url())
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.title, "Cambiato")

    def test_modifica_non_valida(self):
        r = self.client.post(reverse("documents:edit", args=[self.doc.pk]), {"title": ""})
        self.assertEqual(r.status_code, 422)

    def test_eliminazione_classica_con_conferma(self):
        url = reverse("documents:delete", args=[self.doc.pk])
        self.assertContains(self.client.get(url), "Eliminare")
        self.assertTrue(Document.objects.exists())  # il GET non cancella
        self.assertRedirects(self.client.post(url), reverse("documents:list"))
        self.assertFalse(Document.objects.exists())

    def test_eliminazione_htmx_risponde_vuoto(self):
        r = self.client.delete(reverse("documents:delete", args=[self.doc.pk]), **HTMX)
        self.assertEqual((r.status_code, r.content), (200, b""))
        self.assertFalse(Document.objects.exists())

    def test_eliminazione_richiede_csrf(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        c.force_login(self.mario)
        self.assertEqual(c.post(reverse("documents:delete", args=[self.doc.pk])).status_code, 403)
        self.assertTrue(Document.objects.exists())


class IsolamentoTests(DocumentTestCase):
    """Luisa non deve poter fare nulla sui documenti di Mario: sempre 404, mai 403 (non rivela l'esistenza)."""

    def setUp(self):
        self.doc = self.nuovo_documento(owner=self.mario, titolo="Di Mario")
        self.client.force_login(self.luisa)

    def test_tutte_le_azioni_danno_404(self):
        for nome in ["detail", "edit", "delete", "download"]:
            with self.subTest(nome):
                self.assertEqual(self.client.get(reverse(f"documents:{nome}", args=[self.doc.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("documents:edit", args=[self.doc.pk]), {"title": "hack"}).status_code, 404)
        self.assertEqual(self.client.post(reverse("documents:delete", args=[self.doc.pk])).status_code, 404)
        self.assertEqual(self.client.delete(reverse("documents:delete", args=[self.doc.pk]), **HTMX).status_code, 404)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.title, "Di Mario")

    def test_anonimo_viene_mandato_al_login(self):
        self.client.logout()
        for nome in ["detail", "edit", "delete", "download"]:
            r = self.client.get(reverse(f"documents:{nome}", args=[self.doc.pk]))
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])


class DownloadTests(DocumentTestCase):
    def test_download_protetto(self):
        doc = self.nuovo_documento(uploaded_file=self.file(nome="Bilancio 2025.pdf", contenuto=b"%PDF-xyz"))
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:download", args=[doc.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"%PDF-xyz")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn("Bilancio 2025.pdf", r["Content-Disposition"])
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", r["Cache-Control"])

    def test_file_mancante_su_disco_da_404(self):
        doc = self.nuovo_documento()
        doc.current_version.file.storage.delete(doc.current_version.file.name)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get(reverse("documents:download", args=[doc.pk])).status_code, 404)

    def test_non_esiste_una_rotta_media(self):
        doc = self.nuovo_documento()
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get("/media/" + doc.current_version.file.name).status_code, 404)


class CategorieTests(DocumentTestCase):
    def test_solo_staff(self):
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get(reverse("documents:categories")).status_code, 403)
        self.assertEqual(self.client.post(reverse("documents:categories"), {"name": "X"}).status_code, 403)
        self.assertEqual(Category.objects.count(), 0)

    def test_staff_crea_con_htmx(self):
        self.client.force_login(self.staff)
        r = self.client.post(reverse("documents:categories"), {"name": "Fatture"}, **HTMX)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "<html")
        self.assertContains(r, "Fatture")
        self.assertEqual(Category.objects.count(), 1)

    def test_nome_duplicato_da_422(self):
        Category.objects.create(name="Fatture")
        self.client.force_login(self.staff)
        r = self.client.post(reverse("documents:categories"), {"name": "  fatture "}, **HTMX)
        self.assertContains(r, "Esiste già", status_code=422)

    def test_elimina_categoria_libera(self):
        c = Category.objects.create(name="Vuota")
        self.client.force_login(self.staff)
        r = self.client.post(reverse("documents:category_delete", args=[c.pk]), **HTMX)
        self.assertEqual((r.status_code, r.content), (200, b""))
        self.assertFalse(Category.objects.exists())

    def test_categoria_in_uso_da_409_con_retarget(self):
        c = Category.objects.create(name="Usata")
        self.nuovo_documento(category=c)
        self.client.force_login(self.staff)
        r = self.client.post(reverse("documents:category_delete", args=[c.pk]), **HTMX)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r["HX-Retarget"], "#avvisi")
        self.assertTrue(Category.objects.exists())

    def test_voce_menu_solo_per_staff(self):
        self.client.force_login(self.mario)
        self.assertNotContains(self.client.get(reverse("core:dashboard")), reverse("documents:categories"))
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("core:dashboard")), reverse("documents:categories"))


class AssetTagTests(DocumentTestCase):
    def test_asset_ha_versione(self):
        r = self.client.get(reverse("accounts:login"))
        self.assertRegex(r.content.decode(), r"/static/css/app\.css\?v=\d+")

    def test_dashboard_mostra_documenti_recenti(self):
        self.nuovo_documento(titolo="Recente")
        self.client.force_login(self.mario)
        r = self.client.get(reverse("core:dashboard"))
        self.assertContains(r, "Recente")
        self.assertContains(r, "1 documento")
