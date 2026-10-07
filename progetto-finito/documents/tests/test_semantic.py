"""Ricerca per significato: spezzatura, indicizzazione, ricerca, fusione, permessi, rete di sicurezza."""

import io
import os
from datetime import timedelta
from unittest import skipUnless

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents import chunking
from documents.models import Document, DocumentChunk, DocumentPermission, DocumentVersion
from documents.services import add_version, indexing, processing, search, share_document

from .base import DocumentTestCase

FAKE = {"EMBEDDING_BACKEND": "documents.tests.fakes.FakeEmbedder", "SEARCH_SEMANTIC_MIN_SCORE": 0.3}


class SpezzaturaTests(SimpleTestCase):
    def test_testo_corto_un_solo_passaggio(self):
        self.assertEqual(chunking.spezza("Ciao mondo."), (["Ciao mondo."], False))

    def test_testo_vuoto(self):
        self.assertEqual(chunking.spezza(" \n\n "), ([], False))

    def test_nessun_passaggio_supera_la_dimensione(self):
        testo = ("Una frase di prova abbastanza lunga. " * 200) + "x" * 5000
        passaggi, _ = chunking.spezza(testo, dimensione=300, sovrapposizione=50)
        self.assertTrue(passaggi)
        self.assertTrue(all(len(p) <= 300 + 50 + 1 for p in passaggi))

    def test_sovrapposizione_e_nessuna_perdita(self):
        parole = [f"parola{i}" for i in range(400)]
        passaggi, _ = chunking.spezza(" ".join(parole), dimensione=200, sovrapposizione=40)
        trovate = set(" ".join(passaggi).split())
        self.assertEqual(trovate, set(parole))

    def test_massimo_di_passaggi(self):
        passaggi, troncato = chunking.spezza("Frase uno. " * 500, dimensione=50, sovrapposizione=0, massimo_passaggi=5)
        self.assertEqual(len(passaggi), 5)
        self.assertTrue(troncato)


@override_settings(**FAKE)
class SemanticaBase(DocumentTestCase):
    def doc(self, titolo, testo, owner=None):
        d = self.nuovo_documento(
            owner=owner, titolo=titolo, uploaded_file=SimpleUploadedFile("nota.txt", testo.encode())
        )
        processing.elabora_versione(d.current_version_id)
        indexing.indicizza_versione(d.current_version_id)
        return d

    def significato(self, utente, testo):
        return search.cerca_significato(Document.objects.visible_to(utente), testo)


class IndicizzazioneTests(SemanticaBase):
    def test_crea_passaggi_e_segna_il_modello(self):
        d = self.doc("Contratto", "Il contratto di locazione scade a giugno.")
        v = DocumentVersion.objects.get(pk=d.current_version_id)
        self.assertEqual(v.chunks.count(), 1)
        self.assertEqual(v.indexed_model, indexing.modello_corrente())
        self.assertIsNotNone(v.indexed_at)

    def test_seconda_chiamata_non_rifa_il_lavoro(self):
        d = self.doc("Contratto", "Il contratto di locazione scade a giugno.")
        self.assertEqual(indexing.indicizza_versione(d.current_version_id), indexing.SALTATA)
        self.assertEqual(indexing.indicizza_versione(d.current_version_id, forza=True), indexing.OK)
        self.assertEqual(DocumentChunk.objects.filter(version_id=d.current_version_id).count(), 1)

    def test_senza_testo_nessun_passaggio_ma_risulta_indicizzata(self):
        d = self.doc("Vuoto", "")
        v = DocumentVersion.objects.get(pk=d.current_version_id)
        self.assertEqual(v.chunks.count(), 0)
        self.assertEqual(v.indexed_model, indexing.modello_corrente())

    def test_versione_non_pronta_viene_saltata(self):
        d = self.nuovo_documento()
        self.assertEqual(indexing.indicizza_versione(d.current_version_id), indexing.SALTATA)

    def test_rielaborazione_azzera_e_rifa_l_indice(self):
        d = self.doc("Nota", "testo originale sulla locazione")
        v = DocumentVersion.objects.get(pk=d.current_version_id)
        DocumentVersion.objects.filter(pk=v.pk).update(processing_status=DocumentVersion.Processing.PENDING)
        with self.captureOnCommitCallbacks(execute=False) as cb:
            processing.elabora_versione(v.pk)
        v.refresh_from_db()
        self.assertEqual(v.indexed_model, "")
        self.assertGreaterEqual(len(cb), 1)  # indicizzazione accodata dopo il commit (più l'avviso in tempo reale)

    def test_modello_diverso_e_da_reindicizzare(self):
        d = self.doc("Nota", "testo sulla locazione")
        self.assertEqual(indexing.da_indicizzare().count(), 0)
        with override_settings(EMBEDDING_MODEL="altro/modello"):
            self.assertEqual(indexing.da_indicizzare().count(), 1)

    def test_testo_cambiato_durante_il_calcolo(self):
        d = self.doc("Nota", "testo sulla locazione")
        v = DocumentVersion.objects.get(pk=d.current_version_id)
        originale = indexing.get_embedder

        class Cambia:
            def passaggi(self, testi):
                DocumentVersion.objects.filter(pk=v.pk).update(processed_at=timezone.now() + timedelta(seconds=5))
                return originale().passaggi(testi)

        indexing.get_embedder = lambda: Cambia()
        try:
            self.assertEqual(indexing.indicizza_versione(v.pk, forza=True), indexing.CAMBIATA)
        finally:
            indexing.get_embedder = originale

    def test_sweeper_rimette_in_coda_con_pausa(self):
        d = self.doc("Nota", "testo sulla locazione")
        DocumentVersion.objects.filter(pk=d.current_version_id).update(indexed_model="", index_attempted_at=None)
        sent = []
        original = indexing._invia
        indexing._invia = lambda pk: sent.append(pk) or True
        try:
            self.assertEqual(indexing.riprendi_indicizzazioni(), 1)
            self.assertEqual(indexing.riprendi_indicizzazioni(), 0)  # appena tentata: pausa
        finally:
            indexing._invia = original
        self.assertEqual(sent, [d.current_version_id])


class RicercaSignificatoTests(SemanticaBase):
    def test_trova_per_sinonimo(self):
        self.doc("Scansione 41", "Il contratto di locazione scade il 30 giugno.")
        self.doc("Altro", "Menù della cena aziendale con antipasti.")
        r = self.significato(self.mario, "affitto")
        self.assertEqual([x.document_id for x in r][:1], [Document.objects.get(title="Scansione 41").pk])

    def test_nessun_risultato_sotto_soglia(self):
        self.doc("Altro", "Menù della cena aziendale con antipasti.")
        self.assertEqual(self.significato(self.mario, "affitto"), [])

    def test_un_risultato_per_documento_con_il_passaggio_migliore(self):
        paragrafi = [f"Bilancio generico numero {n} dell'esercizio." for n in range(8)]
        paragrafi.insert(4, "Sul punto della locazione: affitto annuale.")
        testo = "\n\n".join(paragrafi)
        self.doc("Lungo", testo)
        with override_settings(SEARCH_CHUNK_CHARS=60, SEARCH_CHUNK_OVERLAP=0):
            indexing.indicizza_versione(Document.objects.get(title="Lungo").current_version_id, forza=True)
        r = self.significato(self.mario, "affitto")
        self.assertEqual(len(r), 1)
        self.assertIn("locazione", r[0].testo)

    def test_documento_non_visibile_non_compare(self):
        self.doc("Riservato", "Contratto di locazione segreto.", owner=self.luisa)
        self.assertEqual(self.significato(self.mario, "affitto"), [])
        self.assertEqual(len(self.significato(self.luisa, "affitto")), 1)

    def test_documento_condiviso_compare(self):
        d = self.doc("Condiviso", "Contratto di locazione.", owner=self.luisa)
        share_document(user=self.luisa, document=d, grantee_user=self.mario, level=DocumentPermission.Level.READ)
        self.assertEqual(len(self.significato(self.mario, "affitto")), 1)

    def test_solo_la_versione_corrente(self):
        d = self.doc("Nota", "Contratto di locazione.")
        add_version(user=self.mario, document=d, uploaded_file=SimpleUploadedFile("nota.txt", b"Menu della cena aziendale"))
        d.refresh_from_db()
        processing.elabora_versione(d.current_version_id)
        indexing.indicizza_versione(d.current_version_id)
        self.assertEqual(self.significato(self.mario, "affitto"), [])

    def test_query_vuota_o_strana(self):
        self.assertEqual(self.significato(self.mario, "   "), [])
        self.assertEqual(self.significato(self.mario, "\x00"), [])

    def test_fusione_premia_chi_e_in_entrambe(self):
        s = [search.RisultatoSemantico(2, 0.9, ""), search.RisultatoSemantico(3, 0.8, "")]
        self.assertEqual(search.fondi([1, 2], s), [2, 1, 3])
        self.assertEqual(search.fondi([], []), [])

    def test_estratto_semantico_neutralizza_html(self):
        out = str(search.estratto_semantico("<script>alert(1)</script> testo " * 50))
        self.assertNotIn("<script>", out)
        self.assertLessEqual(len(out), 600)


class ElencoSignificatoTests(SemanticaBase):
    def setUp(self):
        self.client.force_login(self.mario)
        self.a = self.doc("Scansione 41", "Il contratto di locazione scade il 30 giugno.")
        self.b = self.doc("Menù", "Cena aziendale con antipasti.")

    def cerca(self, **p):
        return self.client.get(reverse("documents:list"), p, HTTP_HX_REQUEST="true")

    def test_entrambi_trova_per_significato(self):
        r = self.cerca(q="affitto")
        self.assertContains(r, "Scansione 41")
        self.assertNotContains(r, "Menù")
        self.assertContains(r, "locazione")  # estratto semantico

    def test_solo_parole_non_trova_il_sinonimo(self):
        self.assertNotContains(self.cerca(q="affitto", modo="parole"), "Scansione 41")

    def test_solo_significato(self):
        r = self.cerca(q="affitto", modo="significato")
        self.assertContains(r, "Scansione 41")

    def test_parole_dei_risultati_sono_evidenziate_nel_modo_combinato(self):
        r = self.cerca(q="locazione")
        self.assertContains(r, "<mark>")

    def test_modo_sconosciuto_usa_quello_predefinito(self):
        self.assertEqual(self.cerca(q="affitto", modo="<x>").status_code, 200)

    @override_settings(EMBEDDING_BACKEND="documents.tests.fakes.EmbedderRotto")
    def test_modello_non_disponibile_ricade_sulle_parole(self):
        with self.assertLogs("documents.views", "ERROR"):
            r = self.cerca(q="locazione")
        self.assertContains(r, "Scansione 41")
        self.assertContains(r, "non è al momento disponibile")

    def test_avviso_documenti_non_indicizzati(self):
        DocumentVersion.objects.filter(pk=self.b.current_version_id).update(indexed_model="")
        self.assertContains(self.cerca(q="affitto"), "non ancora ricercabil")

    def test_documenti_altrui_non_compaiono_ne_nel_conteggio(self):
        c = self.doc("Segreto", "Contratto di locazione.", owner=self.luisa)
        DocumentVersion.objects.filter(pk=c.current_version_id).update(indexed_model="")
        r = self.cerca(q="affitto")
        self.assertNotContains(r, "Segreto")
        self.assertNotContains(r, "non ancora ricercabil")

    def test_filtri_si_applicano_prima_della_ricerca(self):
        self.assertNotContains(self.cerca(q="affitto", scope="condivisi"), "Scansione 41")

    def test_senza_ricerca_niente_modello(self):
        self.assertEqual(self.cerca().status_code, 200)

    def test_selettore_nella_pagina(self):
        r = self.client.get(reverse("documents:list"))
        self.assertContains(r, 'name="modo"')


class ComandiTests(SemanticaBase):
    def test_indicizza_versioni_sincrono(self):
        d = self.doc("Nota", "Contratto di locazione.")
        DocumentVersion.objects.filter(pk=d.current_version_id).update(indexed_model="")
        out = io.StringIO()
        call_command("indicizza_versioni", "--sincrono", stdout=out)
        self.assertEqual(indexing.da_indicizzare().count(), 0)
        self.assertIn("Indicizzate 1", out.getvalue())

    def test_scarica_modello(self):
        out = io.StringIO()
        call_command("scarica_modello", stdout=out)
        self.assertIn("384", out.getvalue())


@skipUnless(os.environ.get("DV_TEST_MODELLO_VERO"), "imposta DV_TEST_MODELLO_VERO=1 per provare il modello vero")
class ModelloVeroTests(DocumentTestCase):
    @override_settings(EMBEDDING_BACKEND="documents.embeddings.SentenceTransformerEmbedder")
    def test_sinonimi_con_il_modello_vero(self):
        for titolo, testo in [("A", "Il contratto di locazione dell'appartamento scade a giugno."),
                              ("B", "Il menù della cena aziendale prevede antipasti e dolci.")]:
            d = self.nuovo_documento(titolo=titolo, uploaded_file=SimpleUploadedFile("n.txt", testo.encode()))
            processing.elabora_versione(d.current_version_id)
            indexing.indicizza_versione(d.current_version_id)
        r = search.cerca_significato(Document.objects.visible_to(self.mario), "contratto d'affitto della casa")
        self.assertEqual(Document.objects.get(pk=r[0].document_id).title, "A")


class StatoIndiceTests(SemanticaBase):
    def versione(self, d):
        return DocumentVersion.objects.get(pk=d.current_version_id)

    def senza_indice(self, testo="Contratto di locazione."):
        return self.nuovo_documento(uploaded_file=SimpleUploadedFile("n.txt", testo.encode()))

    def test_stati(self):
        d = self.senza_indice()
        v = self.versione(d)
        self.assertIsNone(v.stato_indice)  # testo non ancora pronto
        processing.elabora_versione(v.pk)
        self.assertEqual(self.versione(d).stato_indice, DocumentVersion.INDICE_ATTESA)
        DocumentVersion.objects.filter(pk=v.pk).update(index_attempted_at=timezone.now())
        self.assertEqual(self.versione(d).stato_indice, DocumentVersion.INDICE_CORSO)
        DocumentVersion.objects.filter(pk=v.pk).update(index_attempted_at=timezone.now() - timedelta(minutes=11))
        self.assertEqual(self.versione(d).stato_indice, DocumentVersion.INDICE_FALLITO)

    def test_pronto_e_vuoto(self):
        a = self.doc("Con testo", "Contratto di locazione.")
        b = self.doc("Senza testo", "")
        self.assertEqual(self.versione(a).stato_indice, DocumentVersion.INDICE_PRONTO)
        self.assertEqual(self.versione(b).stato_indice, DocumentVersion.INDICE_VUOTO)

    def test_pagina_documento_mostra_lo_stato(self):
        d = self.doc("Con testo", "Contratto di locazione.")
        self.client.force_login(self.mario)
        r = self.client.get(d.get_absolute_url())
        self.assertContains(r, "Ricerca per significato:")
        self.assertContains(r, "indicizzato")
        self.assertContains(r, "1 passaggio")

    def test_riprova_dopo_un_fallimento(self):
        d = self.doc("Nota", "Contratto di locazione.")
        v = self.versione(d)
        DocumentVersion.objects.filter(pk=v.pk).update(indexed_model="", index_attempted_at=timezone.now() - timedelta(hours=1))
        self.client.force_login(self.mario)
        url = reverse("documents:version_reindex", args=[d.pk, 1])
        self.assertContains(self.client.get(d.get_absolute_url()), "Riprova")
        with self.captureOnCommitCallbacks(execute=False) as cb:
            r = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(cb), 1)
        self.assertEqual(self.versione(d).stato_indice, DocumentVersion.INDICE_CORSO)
        self.assertContains(r, "in corso")
        # già in corso: rifiutato
        self.assertEqual(self.client.post(url, HTTP_HX_REQUEST="true").status_code, 409)

    def test_chi_non_puo_modificare_non_puo_riprovare(self):
        d = self.doc("Nota", "Contratto di locazione.", owner=self.luisa)
        share_document(user=self.luisa, document=d, grantee_user=self.mario, level=DocumentPermission.Level.READ)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.post(reverse("documents:version_reindex", args=[d.pk, 1])).status_code, 403)

    def test_estraneo_non_vede_nemmeno_che_esiste(self):
        d = self.doc("Nota", "Contratto di locazione.", owner=self.luisa)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.post(reverse("documents:version_reindex", args=[d.pk, 1])).status_code, 404)

    def test_polling_dell_indice_non_ricarica_la_pagina(self):
        d = self.senza_indice()
        processing.elabora_versione(d.current_version_id)
        self.client.force_login(self.mario)
        url = reverse("documents:version_processing", args=[d.pk, 1])
        r = self.client.get(url + "?fase=indice", HTTP_HX_REQUEST="true")
        self.assertNotIn("HX-Refresh", r.headers)
        self.assertContains(r, "fase=indice")  # continua a interrogare finché non è indicizzata
        self.assertContains(r, "in attesa")

    def test_numero_di_query_della_cronologia_non_cresce_con_le_versioni(self):
        d = self.doc("Nota", "Contratto di locazione.")
        self.client.force_login(self.mario)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as poche:
            self.client.get(d.get_absolute_url())
        for i in range(4):
            add_version(user=self.mario, document=d, uploaded_file=SimpleUploadedFile("n.txt", f"testo {i}".encode()))
        with CaptureQueriesContext(connection) as molte:
            self.client.get(d.get_absolute_url())
        self.assertLessEqual(len(molte), len(poche) + 2)

    def test_dashboard_voce_indice(self):
        from core.services.health import stato_indice

        self.doc("Nota", "Contratto di locazione.")
        s = stato_indice()
        self.assertTrue(s.ok)
        self.assertIn("1 versioni indicizzate", s.dettaglio)
        v = DocumentVersion.objects.get()
        DocumentVersion.objects.filter(pk=v.pk).update(indexed_model="", index_attempted_at=timezone.now() - timedelta(hours=1))
        s = stato_indice()
        self.assertFalse(s.ok)
        self.assertIn("1 non riuscite", s.dettaglio)
