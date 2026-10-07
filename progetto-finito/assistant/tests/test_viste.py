"""Pagine dell'assistente: privacy delle conversazioni, flusso SSE, fonti, consenso per documento."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from assistant import services
from assistant.models import Conversation, Message
from core.models import AuditLog
from documents.models import Document, DocumentPermission
from documents.services import share_document

from .base import AssistenteBase
from .fakes import FakeLLM


class VisteBase(AssistenteBase):
    HX = {"HX-Request": "true"}

    def setUp(self):
        super().setUp()
        self.d = self.doc("Contratto", "Il contratto di locazione scade il 30 giugno.")
        self.client.force_login(self.mario)

    def conversazione(self, domanda="Quando scade la locazione?", utente=None, documento=None):
        return services.nuova_conversazione(utente=utente or self.mario, domanda=domanda, documento=documento)

    def leggi_flusso(self, msg):
        r = self.client.get(reverse("assistant:stream", args=[msg.pk]))
        return r, self._corpo(r)

    @staticmethod
    def _corpo(risposta):
        """Il flusso è asincrono (per partire subito sotto ASGI): lo si consuma con un ciclo di eventi."""
        from asgiref.sync import async_to_sync

        async def leggi():
            return b"".join([parte async for parte in risposta.streaming_content])

        return async_to_sync(leggi)().decode()


class PaginaTests(VisteBase):
    def test_pagine_richiedono_il_login(self):
        self.client.logout()
        c = self.conversazione()
        for nome, args in [("index", []), ("new", []), ("detail", [c.pk])]:
            r = self.client.get(reverse(f"assistant:{nome}", args=args))
            self.assertEqual(r.status_code, 302, nome)

    def test_indice_e_menu(self):
        r = self.client.get(reverse("assistant:index"))
        self.assertContains(r, "Chiedi ai tuoi documenti")
        self.assertContains(r, "può leggere <strong")
        self.assertContains(r, reverse("assistant:index"))  # voce del menu

    @override_settings(ANTHROPIC_API_KEY="")
    def test_senza_chiave_lo_dice(self):
        self.assertContains(self.client.get(reverse("assistant:index")), "manca ANTHROPIC_API_KEY")

    def test_crea_conversazione_e_porta_alla_pagina(self):
        r = self.client.post(reverse("assistant:new"), {"domanda": "Quando scade la locazione?"})
        c = Conversation.objects.get()
        self.assertRedirects(r, c.get_absolute_url())
        pagina = self.client.get(c.get_absolute_url())
        self.assertContains(pagina, "data-stream=")  # la risposta parte da sola
        self.assertContains(pagina, "Quando scade la locazione?")

    def test_domanda_non_valida_resta_sulla_pagina(self):
        r = self.client.post(reverse("assistant:new"), {"domanda": "   "})
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Scrivi una domanda", status_code=422)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_conversazione_su_un_documento(self):
        r = self.client.get(reverse("assistant:new") + f"?documento={self.d.pk}")
        self.assertContains(r, "Chiedi a «Contratto»")
        self.client.post(reverse("assistant:new"), {"domanda": "Riassumi", "documento": self.d.pk})
        self.assertEqual(Conversation.objects.get().document, self.d)

    def test_documento_senza_consenso_rimanda_alla_sua_pagina(self):
        self.d.ai_allowed = False
        self.d.save()
        r = self.client.get(reverse("assistant:new") + f"?documento={self.d.pk}")
        self.assertRedirects(r, self.d.get_absolute_url())

    def test_documento_che_non_vedo_e_404(self):
        altro = self.doc("Altrui", "testo", owner=self.luisa)
        self.assertEqual(self.client.get(reverse("assistant:new") + f"?documento={altro.pk}").status_code, 404)
        self.assertEqual(self.client.post(reverse("assistant:new"), {"domanda": "x", "documento": altro.pk}).status_code, 404)

    def test_elimina_conversazione(self):
        c = self.conversazione()
        r = self.client.post(reverse("assistant:delete", args=[c.pk]))
        self.assertRedirects(r, reverse("assistant:index"))
        self.assertFalse(Conversation.objects.exists())


class PrivacyTests(VisteBase):
    """Le conversazioni sono private: nemmeno il superutente le vede."""

    def test_conversazione_altrui_e_404_per_tutto(self):
        c = self.conversazione(utente=self.luisa)
        msg = c.messages.get(role="assistant")
        self.client.force_login(self.mario)
        for url, metodo in [
            (reverse("assistant:detail", args=[c.pk]), "get"),
            (reverse("assistant:ask", args=[c.pk]), "post"),
            (reverse("assistant:delete", args=[c.pk]), "post"),
            (reverse("assistant:message", args=[msg.pk]), "get"),
            (reverse("assistant:stream", args=[msg.pk]), "get"),
        ]:
            r = getattr(self.client, metodo)(url, {"domanda": "x"})
            self.assertEqual(r.status_code, 404, url)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("assistant:detail", args=[c.pk])).status_code, 404)

    def test_elenco_mostra_solo_le_mie(self):
        self.conversazione("domanda mia")
        self.conversazione("domanda di luisa", utente=self.luisa)
        r = self.client.get(reverse("assistant:index"))
        self.assertContains(r, "domanda mia")
        self.assertNotContains(r, "domanda di luisa")

    def test_fonti_nascoste_dopo_la_revoca_dell_accesso(self):
        altro = self.doc("Riservato di Luisa", "Il contratto di locazione scade il 30 giugno.", owner=self.luisa)
        permesso, _ = share_document(user=self.luisa, document=altro, grantee_user=self.mario, level=DocumentPermission.Level.READ)
        self.d.delete()
        c = self.conversazione()
        msg = c.messages.get(role="assistant")
        self.leggi_flusso(msg)
        self.assertContains(self.client.get(c.get_absolute_url()), "Riservato di Luisa")
        permesso.delete()
        pagina = self.client.get(c.get_absolute_url())
        self.assertNotContains(pagina, "Riservato di Luisa")
        self.assertNotContains(pagina, "locazione scade il 30 giugno.</p>")  # nemmeno l'estratto
        self.assertContains(pagina, "Documento non più accessibile")


class FlussoTests(VisteBase):
    def test_domanda_in_htmx_restituisce_le_due_bolle(self):
        c = self.conversazione()
        self.leggi_flusso(c.messages.get(role="assistant"))
        r = self.client.post(reverse("assistant:ask", args=[c.pk]), {"domanda": "E il canone?"}, headers=self.HX)
        self.assertContains(r, "E il canone?")
        self.assertContains(r, "data-stream=")

    def test_domanda_non_valida_in_htmx_va_nel_riquadro_errori(self):
        c = self.conversazione()
        self.leggi_flusso(c.messages.get(role="assistant"))
        r = self.client.post(reverse("assistant:ask", args=[c.pk]), {"domanda": ""}, headers=self.HX)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r["HX-Retarget"], "#errore-domanda")

    def test_non_si_puo_fare_una_domanda_mentre_si_risponde(self):
        c = self.conversazione()
        r = self.client.post(reverse("assistant:ask", args=[c.pk]), {"domanda": "altra"}, headers=self.HX)
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Aspetta la fine", status_code=422)

    def test_flusso_sse(self):
        c = self.conversazione()
        msg = c.messages.get(role="assistant")
        r, corpo = self.leggi_flusso(msg)
        self.assertEqual(r["Content-Type"], "text/event-stream")
        self.assertEqual(r["Cache-Control"], "no-cache")
        self.assertIn("event: stato", corpo)
        self.assertIn("event: testo", corpo)
        self.assertTrue(corpo.rstrip().endswith("data: null"))
        self.assertIn("event: fine", corpo)

    def test_il_flusso_e_asincrono_per_partire_subito_sotto_asgi(self):
        # Con un iteratore sincrono Daphne aspetterebbe la fine della risposta prima di inviare il primo pezzo.
        c = self.conversazione()
        msg = c.messages.get(role="assistant")
        self.client.force_login(self.mario)
        r = self.client.get(reverse("assistant:stream", args=[msg.pk]))
        self.assertTrue(r.is_async)

    def test_secondo_flusso_non_rigenera(self):
        c = self.conversazione()
        msg = c.messages.get(role="assistant")
        self.leggi_flusso(msg)
        _, corpo = self.leggi_flusso(msg)
        self.assertIn("event: altrove", corpo)
        self.assertEqual(len(self.chiamate("risposta")), 1)

    def test_pagina_finale_con_citazioni_e_fonti(self):
        c = self.conversazione()
        msg = c.messages.get(role="assistant")
        self.leggi_flusso(msg)
        pagina = self.client.get(c.get_absolute_url())
        self.assertContains(pagina, f'href="#fonte-{msg.pk}-1"')
        self.assertContains(pagina, f'id="fonte-{msg.pk}-1"')
        self.assertContains(pagina, reverse("documents:detail", args=[self.d.pk]))
        self.assertContains(pagina, "Fonti citate (1)")
        self.assertContains(pagina, "<strong>Attenzione</strong>")

    def test_le_fonti_non_citate_stanno_a_parte(self):
        self.doc("Altro libro", "Il contratto di locazione è un tema trattato anche in questo libro, a tratti.")
        FakeLLM.risposta = "La scadenza è il 30 giugno [1]."
        c = self.conversazione("contratto di locazione")
        msg = c.messages.get(role="assistant")
        self.leggi_flusso(msg)
        msg.refresh_from_db()
        self.assertGreaterEqual(len(msg.sources), 2)  # al modello sono andati più passaggi…
        pagina = self.client.get(c.get_absolute_url())
        self.assertContains(pagina, "Fonti citate (1)")  # …ma in primo piano c'è solo ciò che la risposta cita
        self.assertContains(pagina, "Altri passaggi consultati, non citati (")

    def test_risposta_senza_citazioni_mostra_i_passaggi_consultati(self):
        FakeLLM.risposta = "Non trovo la risposta negli estratti."
        c = self.conversazione()
        self.leggi_flusso(c.messages.get(role="assistant"))
        pagina = self.client.get(c.get_absolute_url())
        self.assertContains(pagina, "Passaggi consultati (")
        self.assertNotContains(pagina, "Fonti citate")

    def test_html_nella_risposta_non_passa(self):
        FakeLLM.risposta = "Ecco <script>alert(1)</script> e <img src=x onerror=alert(1)> [1]"
        c = self.conversazione()
        self.leggi_flusso(c.messages.get(role="assistant"))
        pagina = self.client.get(c.get_absolute_url()).content.decode()
        self.assertNotIn("<script>alert", pagina)
        self.assertNotIn("<img src=x", pagina)

    def test_html_nel_documento_non_passa_dalle_fonti(self):
        self.d.delete()
        self.doc("Cattivo", "locazione <script>alert('x')</script> scade")
        c = self.conversazione("locazione")
        self.leggi_flusso(c.messages.get(role="assistant"))
        self.assertNotIn("<script>alert", self.client.get(c.get_absolute_url()).content.decode())

    def test_errore_si_mostra_nella_bolla(self):
        FakeLLM.errore = "Troppe richieste in questo momento: riprova fra poco."
        c = self.conversazione()
        msg = c.messages.get(role="assistant")
        _, corpo = self.leggi_flusso(msg)
        self.assertIn("event: errore", corpo)
        r = self.client.get(reverse("assistant:message", args=[msg.pk]))
        self.assertContains(r, "Troppe richieste")

    def test_messaggio_in_generazione_fa_polling(self):
        c = self.conversazione()
        msg = c.messages.get(role="assistant")
        Message.objects.filter(pk=msg.pk).update(status="generando", started_at=msg.created_at)
        r = self.client.get(reverse("assistant:message", args=[msg.pk]))
        self.assertContains(r, 'hx-trigger="every 2s"')

    def test_il_messaggio_dell_utente_non_e_una_risposta(self):
        c = self.conversazione()
        domanda = c.messages.get(role="user")
        self.assertEqual(self.client.get(reverse("assistant:message", args=[domanda.pk])).status_code, 404)

    @override_settings(AI_MAX_QUESTIONS_PER_HOUR=1)
    def test_limite_orario_dalla_pagina(self):
        self.client.post(reverse("assistant:new"), {"domanda": "uno"})
        r = self.client.post(reverse("assistant:new"), {"domanda": "due"})
        self.assertContains(r, "limite", status_code=422)


class ConsensoTests(VisteBase):
    def test_pulsante_chiedi_solo_con_consenso(self):
        self.assertContains(self.client.get(self.d.get_absolute_url()), "Chiedi</a>")
        self.d.ai_allowed = False
        self.d.save()
        self.assertNotContains(self.client.get(self.d.get_absolute_url()), "Chiedi</a>")

    def test_il_proprietario_cambia_il_consenso_e_resta_nel_registro(self):
        url = reverse("documents:ai_consent", args=[self.d.pk])
        self.client.post(url, {"consenti": "0"})
        self.d.refresh_from_db()
        self.assertFalse(self.d.ai_allowed)
        self.assertEqual(AuditLog.objects.filter(action="document.ai_consent_changed").count(), 1)
        self.client.post(url, {"consenti": "0"})  # nessun cambiamento: nessuna riga in più
        self.assertEqual(AuditLog.objects.filter(action="document.ai_consent_changed").count(), 1)
        self.client.post(url, {"consenti": "1"})
        self.d.refresh_from_db()
        self.assertTrue(self.d.ai_allowed)

    def test_funziona_anche_se_il_documento_non_e_in_bozza(self):
        Document.objects.filter(pk=self.d.pk).update(status="approved")
        self.client.post(reverse("documents:ai_consent", args=[self.d.pk]), {"consenti": "0"})
        self.d.refresh_from_db()
        self.assertFalse(self.d.ai_allowed)

    def test_chi_puo_solo_modificare_non_decide(self):
        share_document(user=self.mario, document=self.d, grantee_user=self.luisa, level=DocumentPermission.Level.WRITE)
        self.client.force_login(self.luisa)
        r = self.client.post(reverse("documents:ai_consent", args=[self.d.pk]), {"consenti": "0"})
        self.assertEqual(r.status_code, 403)
        self.d.refresh_from_db()
        self.assertTrue(self.d.ai_allowed)

    def test_estraneo_riceve_404(self):
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.post(reverse("documents:ai_consent", args=[self.d.pk]), {"consenti": "0"}).status_code, 404)

    def test_il_caricamento_ha_la_casella_precompilata(self):
        r = self.client.get(reverse("documents:create"))
        self.assertContains(r, 'name="ai_allowed"')
        self.assertContains(r, "checked")

    def test_caricamento_con_e_senza_consenso(self):
        for dati, atteso in [({"ai_allowed": "on"}, True), ({}, False)]:
            self.client.post(
                reverse("documents:create"),
                {"title": f"T{atteso}", "file": SimpleUploadedFile(f"{atteso}.txt", f"testo {atteso}".encode()), **dati},
            )
            self.assertEqual(Document.objects.get(title=f"T{atteso}").ai_allowed, atteso)

    def test_comando_ai_consenti(self):
        Document.objects.update(ai_allowed=False)
        call_command("ai_consenti", "--tutti", stdout=__import__("io").StringIO())
        self.assertTrue(Document.objects.get(pk=self.d.pk).ai_allowed)
        call_command("ai_consenti", "--tutti", "--revoca", stdout=__import__("io").StringIO())
        self.assertFalse(Document.objects.get(pk=self.d.pk).ai_allowed)


class OccorrenzeVistaTests(AssistenteBase):
    def test_elenco_occorrenze_mostrato_e_filtrato_per_visibilita(self):
        from assistant import services

        a = self.doc("Libro Uno", "Marco Ferrante accettò il lavoro.")
        self.doc("Libro Due", "Marco Ferrante incontra un amico.", owner=self.luisa)
        c = services.nuova_conversazione(utente=self.mario, domanda="Dove compare Marco Ferrante?")
        msg = c.messages.get(role="assistant")
        msg.status = "ok"
        msg.content = "Risposta."
        msg.occurrences = [{"term": "Marco Ferrante", "total": 2, "documents": [
            {"id": a.pk, "title": "Libro Uno"}, {"id": 999999, "title": "Segreto Altrui"}]}]
        msg.save()
        self.client.force_login(self.mario)
        html = self.client.get(c.get_absolute_url()).content.decode()
        self.assertIn("Marco Ferrante", html)
        self.assertIn("Libro Uno", html)
        self.assertNotIn("Segreto Altrui", html)


class CategoriaTests(AssistenteBase):
    def test_la_conversazione_si_limita_alla_categoria(self):
        from documents.models import Category

        romanzi, contratti = Category.objects.create(name="Romanzi"), Category.objects.create(name="Contratti")
        a = self.doc("Libro", "Marco Ferrante accettò il lavoro.")
        b = self.doc("Contratto", "Marco Ferrante firma la locazione.")
        a.category, b.category = romanzi, contratti
        a.save()
        b.save()
        self.client.force_login(self.mario)
        r = self.client.post(reverse("assistant:new"), {"domanda": "dove compare Marco Ferrante?", "categoria": romanzi.pk})
        c = Conversation.objects.get()
        self.assertEqual(c.category, romanzi)
        self.assertRedirects(r, c.get_absolute_url())
        from assistant import retrieval
        from assistant.llm import Piano

        trovato = retrieval.trova_passaggi(self.mario, "dove compare Marco Ferrante?", Piano(), None, romanzi)
        self.assertEqual({p.titolo for p in trovato.passaggi}, {"Libro"})
        self.assertEqual([t for _, t in trovato.occorrenze[0].documenti], ["Libro"])

    def test_il_menu_compare_nella_pagina_nuova_ma_non_nella_conversazione(self):
        from documents.models import Category

        Category.objects.create(name="Romanzi")
        self.doc("Libro", "Testo qualunque.")
        self.client.force_login(self.mario)
        self.assertContains(self.client.get(reverse("assistant:new")), "Tutte le categorie")
        c = services.nuova_conversazione(utente=self.mario, domanda="ciao")
        self.assertNotContains(self.client.get(c.get_absolute_url()), "Tutte le categorie")


class FonteApriTests(AssistenteBase):
    def setUp(self):
        super().setUp()
        from django.core.files.uploadedfile import SimpleUploadedFile
        from documents.tests.fixtures import pdf_con_testo

        self.libro = self.nuovo_documento(
            titolo="Libro", ai_allowed=True,
            uploaded_file=SimpleUploadedFile("libro.pdf", pdf_con_testo("Prima pagina di prova qui", "Seconda pagina: le migrazioni di Django sei"), content_type="application/pdf"),
        )
        self.nota = self.doc("Nota", "testo semplice")
        c = Conversation.objects.create(owner=self.mario, title="x")
        self.msg = Message.objects.create(
            conversation=c, role=Message.Role.ASSISTANT, status=Message.Status.DONE, content="ok [1] [2]",
            sources=[
                {"n": 1, "document_id": self.libro.pk, "title": "Libro", "version": 1, "text": "le migrazioni di Django sei"},
                {"n": 2, "document_id": self.nota.pk, "title": "Nota", "version": 1, "text": "testo semplice"},
            ],
        )
        self.client.force_login(self.mario)

    def apri(self, n):
        return self.client.get(reverse("assistant:fonte", args=[self.msg.pk, n]))

    def test_pdf_si_apre_alla_pagina_del_passaggio(self):
        import shutil

        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext non installato")
        r = self.apri(1)
        self.assertRedirects(r, reverse("documents:version_open", args=[self.libro.pk, 1]) + "#page=2", fetch_redirect_response=False)

    def test_altri_tipi_vanno_alla_scheda_del_documento(self):
        self.assertRedirects(self.apri(2), self.nota.get_absolute_url(), fetch_redirect_response=False)

    def test_fonte_inesistente_e_messaggio_altrui(self):
        self.assertEqual(self.apri(9).status_code, 404)
        self.client.force_login(self.luisa)
        self.assertEqual(self.apri(1).status_code, 404)

    def test_il_link_compare_nelle_fonti(self):
        self.assertContains(self.client.get(reverse("assistant:message", args=[self.msg.pk])), "Apri al passaggio")
