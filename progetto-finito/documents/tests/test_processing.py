"""Elaborazione asincrona delle versioni: stato, idempotenza, errori, riprese, permessi."""

import shutil
import unittest
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog
from documents import extraction, tasks
from documents.models import Document, DocumentPermission, DocumentVersion
from documents.services import add_version, delete_document, processing, restore_version, share_document

from . import fixtures
from .base import DocumentTestCase

P = DocumentVersion.Processing
serve_poppler = unittest.skipUnless(shutil.which("pdftotext"), "poppler-utils non installato")


def carica(nome: str, contenuto: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(nome, contenuto)


class ProcessingBase(DocumentTestCase):
    def documento_con(self, nome="note.txt", contenuto=b"Verbale della riunione di giugno", owner=None) -> Document:
        return self.nuovo_documento(owner=owner, uploaded_file=carica(nome, contenuto))

    @staticmethod
    def versione(doc: Document) -> DocumentVersion:
        return doc.versions.get(version_number=1)

    def processa(self, versione: DocumentVersion, **kw) -> str:
        esito = processing.elabora_versione(versione.pk, **kw)
        versione.refresh_from_db()
        return esito


class AccodamentoTests(ProcessingBase):
    def test_la_versione_nasce_in_coda(self):
        v = self.versione(self.documento_con())
        self.assertEqual(v.processing_status, P.PENDING)
        self.assertEqual(v.extracted_text, "")

    def test_il_task_parte_solo_dopo_il_commit(self):
        with mock.patch.object(tasks.elabora_versione_task, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=False) as richiami:
                doc = self.documento_con()
            delay.assert_not_called()  # niente prima del commit: il worker non vede una riga non ancora salvata
            for richiamo in richiami:  # c'è anche l'avviso in tempo reale, oltre al task
                richiamo()
        delay.assert_called_once_with(self.versione(doc).pk)

    def test_ogni_nuova_versione_e_ripristino_vengono_accodati(self):
        doc = self.documento_con()
        with mock.patch.object(tasks.elabora_versione_task, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                v2 = add_version(user=self.mario, document=doc, uploaded_file=carica("note.txt", b"seconda"))
                v3 = restore_version(user=self.mario, document=doc, version=self.versione(doc))
        self.assertEqual([c.args[0] for c in delay.call_args_list], [v2.pk, v3.pk])

    def test_broker_fermo_non_blocca_il_caricamento(self):
        with mock.patch.object(tasks.elabora_versione_task, "delay", side_effect=ConnectionError("redis spento")):
            with self.assertLogs("documents.services.processing", "WARNING"):
                with self.captureOnCommitCallbacks(execute=True):
                    doc = self.documento_con()
        v = self.versione(doc)
        self.assertEqual(doc.current_version, v)
        self.assertEqual(v.processing_status, P.PENDING)  # la riprenderà riprendi_elaborazioni


class ElaborazioneTests(ProcessingBase):
    def test_testo_semplice_completato(self):
        v = self.versione(self.documento_con())
        self.assertEqual(self.processa(v), processing.OK)
        self.assertEqual(v.processing_status, P.DONE)
        self.assertEqual(v.extracted_text, "Verbale della riunione di giugno")
        self.assertIsNotNone(v.processed_at)
        self.assertEqual(v.processing_attempts, 1)

    def test_nul_nel_testo_non_rompe_postgres(self):
        v = self.versione(self.documento_con(contenuto=b"ciao\x00mondo"))
        self.processa(v)
        self.assertEqual(v.processing_status, P.DONE)
        self.assertEqual(v.extracted_text, "ciaomondo")

    def test_e_idempotente(self):
        v = self.versione(self.documento_con())
        self.processa(v)
        with mock.patch.object(extraction, "estrai") as estrai:
            self.assertEqual(self.processa(v), processing.IGNORATA)  # messaggio duplicato: nessun lavoro
        estrai.assert_not_called()

    def test_forza_rifa_il_lavoro(self):
        v = self.versione(self.documento_con())
        self.processa(v)
        self.assertEqual(self.processa(v, forza=True), processing.OK)
        self.assertEqual(v.processing_attempts, 2)

    def test_formato_senza_testo_e_non_applicabile(self):
        v = self.versione(self.documento_con("dati.xyz", b"\x00\x01"))
        self.assertEqual(self.processa(v), processing.NON_APPLICABILE)
        self.assertEqual(v.processing_status, P.SKIPPED)
        self.assertIn(".xyz", v.processing_message)

    @serve_poppler
    def test_file_danneggiato_va_in_errore_e_nel_registro(self):
        v = self.versione(self.documento_con("rotto.pdf", b"%PDF-1.4 finto"))
        self.assertEqual(self.processa(v), processing.ERRORE)
        self.assertEqual(v.processing_status, P.FAILED)
        self.assertIn("PDF", v.processing_message)
        riga = AuditLog.objects.get(action="version.processing_failed")
        self.assertEqual(riga.username, "sistema")
        self.assertEqual(riga.metadata["versione"], 1)

    def test_errore_imprevisto_riprova_poi_si_arrende(self):
        v = self.versione(self.documento_con())
        with mock.patch.object(extraction, "estrai", side_effect=RuntimeError("boom")):
            with self.assertLogs("documents.services.processing", "ERROR"):
                self.assertEqual(self.processa(v), processing.RIPROVA)
                self.assertEqual(v.processing_status, P.PENDING)
                self.assertEqual(self.processa(v), processing.RIPROVA)
                self.assertEqual(self.processa(v), processing.ERRORE)  # terzo tentativo = ultimo
        self.assertEqual(v.processing_status, P.FAILED)
        self.assertEqual(v.processing_attempts, 3)
        self.assertNotIn("boom", v.processing_message)  # i dettagli stanno nei log, non davanti all'utente

    def test_tempo_scaduto_diventa_errore(self):
        from celery.exceptions import SoftTimeLimitExceeded

        v = self.versione(self.documento_con())
        with mock.patch.object(extraction, "estrai", side_effect=SoftTimeLimitExceeded()):
            self.assertEqual(self.processa(v), processing.ERRORE)
        self.assertIn("Tempo massimo", v.processing_message)

    def test_file_sparito_dallo_storage(self):
        v = self.versione(self.documento_con())
        Path(v.file.path).unlink()
        self.assertEqual(self.processa(v), processing.ERRORE)
        self.assertIn("File non trovato", v.processing_message)

    def test_due_worker_non_lavorano_la_stessa_versione(self):
        v = self.versione(self.documento_con())
        self.assertIsNotNone(processing._prenota(v.pk, forza=False))
        self.assertIsNone(processing._prenota(v.pk, forza=False))  # già in corso (e non scaduta)

    def test_versione_eliminata_durante_il_lavoro_non_lascia_file(self):
        v = self.versione(self.documento_con())

        def elimina_mentre_lavora(*a, **kw):
            DocumentVersion.objects.filter(pk=v.pk).delete()
            return extraction.Estratto(text="x", preview=b"\x89PNG-finto")

        with mock.patch.object(extraction, "estrai", side_effect=elimina_mentre_lavora):
            self.assertEqual(processing.elabora_versione(v.pk), processing.OK)
        from django.conf import settings

        anteprime = Path(settings.MEDIA_ROOT) / "anteprime"
        self.assertFalse(anteprime.exists() and any(anteprime.rglob("*.png")))


@serve_poppler
class AnteprimaTests(ProcessingBase):
    def setUp(self):
        self.doc = self.documento_con("relazione.pdf", fixtures.pdf_con_testo("Bilancio consuntivo 2026"))
        self.v = self.versione(self.doc)
        self.processa(self.v)

    def test_testo_pagine_e_anteprima_salvati(self):
        self.assertEqual(self.v.processing_status, P.DONE)
        self.assertIn("Bilancio consuntivo 2026", self.v.extracted_text)
        self.assertEqual(self.v.page_count, 1)
        self.assertTrue(self.v.preview.name.startswith("anteprime/"))
        self.assertTrue(self.v.preview.name.endswith(".png"))
        self.assertTrue(Path(self.v.preview.path).is_file())

    def test_rielaborare_sostituisce_l_anteprima_senza_lasciare_orfani(self):
        vecchia = self.v.preview.path
        with self.captureOnCommitCallbacks(execute=True):
            self.processa(self.v, forza=True)
        self.assertNotEqual(vecchia, self.v.preview.path)
        self.assertFalse(Path(vecchia).exists())
        self.assertTrue(Path(self.v.preview.path).is_file())

    def test_ripristino_riusa_il_risultato_senza_rifare_il_lavoro(self):
        add_version(user=self.mario, document=self.doc, uploaded_file=carica("relazione.pdf", fixtures.pdf_con_testo("Altro testo")))
        nuova = restore_version(user=self.mario, document=self.doc, version=self.v)
        with mock.patch.object(extraction, "estrai") as estrai:
            self.assertEqual(self.processa(nuova), processing.OK)
        estrai.assert_not_called()
        self.assertIn("Bilancio consuntivo 2026", nuova.extracted_text)
        self.assertTrue(Path(nuova.preview.path).is_file())
        self.assertNotEqual(nuova.preview.path, self.v.preview.path)  # ogni versione possiede il suo file

    def test_eliminare_il_documento_rimuove_anche_le_anteprime(self):
        percorso = Path(self.v.preview.path)
        with self.captureOnCommitCallbacks(execute=True):
            delete_document(user=self.mario, document=self.doc)
        self.assertFalse(percorso.exists())

    def test_anteprima_servita_solo_a_chi_vede_il_documento(self):
        url = reverse("documents:version_preview", args=[self.doc.pk, 1])
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.get(url).status_code, 404)  # non lo vede: come se non esistesse
        self.client.force_login(self.mario)
        risposta = self.client.get(url)
        self.assertEqual(risposta.status_code, 200)
        self.assertEqual(risposta["Content-Type"], "image/png")
        self.assertTrue(b"".join(risposta.streaming_content).startswith(b"\x89PNG"))
        self.assertIn("private", risposta["Cache-Control"])

    def test_anteprima_assente_e_404(self):
        altro = self.versione(self.documento_con("nota.txt", b"solo testo"))
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get(reverse("documents:version_preview", args=[altro.document_id, 1])).status_code, 404)

    def test_pagina_di_dettaglio_mostra_anteprima_e_testo(self):
        self.client.force_login(self.mario)
        risposta = self.client.get(self.doc.get_absolute_url())
        self.assertContains(risposta, reverse("documents:version_preview", args=[self.doc.pk, 1]))
        self.assertContains(risposta, "Testo estratto")
        self.assertContains(risposta, "Bilancio consuntivo 2026")
        self.assertContains(risposta, 'x-data="anteprima"')


class RipresaTests(ProcessingBase):
    def setUp(self):
        self.v = self.versione(self.documento_con())

    def invecchia(self, **campi):
        DocumentVersion.objects.filter(pk=self.v.pk).update(**campi)

    def test_in_coda_da_troppo_viene_reinviata_una_volta_sola(self):
        self.invecchia(created_at=timezone.now() - timedelta(minutes=10))
        with mock.patch.object(processing, "_invia", return_value=True) as invia:
            self.assertEqual(processing.riprendi_elaborazioni(), 1)
            self.assertEqual(processing.riprendi_elaborazioni(), 0)  # appena toccata: si aspetta
        invia.assert_called_once_with(self.v.pk)

    def test_appena_caricata_non_viene_toccata(self):
        with mock.patch.object(processing, "_invia") as invia:
            self.assertEqual(processing.riprendi_elaborazioni(), 0)
        invia.assert_not_called()

    def test_in_corso_da_troppo_torna_in_coda(self):
        self.invecchia(processing_status=P.RUNNING, processing_attempts=1,
                       processing_updated_at=timezone.now() - timedelta(hours=1))
        with mock.patch.object(processing, "_invia", return_value=True) as invia:
            processing.riprendi_elaborazioni()
        self.v.refresh_from_db()
        self.assertEqual(self.v.processing_status, P.PENDING)
        invia.assert_called_once()

    def test_in_corso_di_recente_non_viene_toccata(self):
        self.invecchia(processing_status=P.RUNNING, processing_updated_at=timezone.now())
        with mock.patch.object(processing, "_invia") as invia:
            processing.riprendi_elaborazioni()
        self.v.refresh_from_db()
        self.assertEqual(self.v.processing_status, P.RUNNING)
        invia.assert_not_called()

    def test_tentativi_esauriti_vanno_in_errore(self):
        self.invecchia(processing_status=P.RUNNING, processing_attempts=3,
                       processing_updated_at=timezone.now() - timedelta(hours=1))
        with mock.patch.object(processing, "_invia") as invia:
            processing.riprendi_elaborazioni()
        self.v.refresh_from_db()
        self.assertEqual(self.v.processing_status, P.FAILED)
        self.assertIn("interrotta più volte", self.v.processing_message)
        invia.assert_not_called()

    def test_versione_in_errore_non_viene_ripresa_da_sola(self):
        self.invecchia(processing_status=P.FAILED, created_at=timezone.now() - timedelta(days=1))
        with mock.patch.object(processing, "_invia") as invia:
            processing.riprendi_elaborazioni()
        invia.assert_not_called()

    def test_il_task_periodico_chiama_il_servizio(self):
        with mock.patch.object(processing, "riprendi_elaborazioni", return_value=4) as r:
            self.assertEqual(tasks.riprendi_elaborazioni_task(), 4)
        r.assert_called_once()

    def test_il_task_riprova_con_attesa_crescente(self):
        with mock.patch.object(processing, "elabora_versione", return_value=processing.RIPROVA):
            from celery.exceptions import Retry

            with self.assertRaises(Retry) as ctx:
                tasks.elabora_versione_task.apply(args=[self.v.pk], throw=True).get()
        self.assertEqual(ctx.exception.when, 30)


class RielaborazioneTests(ProcessingBase):
    def setUp(self):
        self.doc = self.documento_con()
        self.v = self.versione(self.doc)

    def test_dopo_un_errore_si_rimette_in_coda_e_si_registra(self):
        DocumentVersion.objects.filter(pk=self.v.pk).update(
            processing_status=P.FAILED, processing_attempts=3, processing_message="boom"
        )
        with mock.patch.object(tasks.elabora_versione_task, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                v = processing.richiedi_rielaborazione(user=self.mario, version=self.v)
        self.assertEqual((v.processing_status, v.processing_attempts, v.processing_message), (P.PENDING, 0, ""))
        delay.assert_called_once_with(self.v.pk)
        self.assertTrue(AuditLog.objects.filter(action="version.reprocess_requested", user=self.mario).exists())

    def test_in_corso_adesso_non_si_puo_rimettere_in_coda(self):
        DocumentVersion.objects.filter(pk=self.v.pk).update(
            processing_status=P.RUNNING, processing_updated_at=timezone.now()
        )
        with self.assertRaises(processing.ElaborazioneInCorso):
            processing.richiedi_rielaborazione(user=self.mario, version=self.v)

    def test_in_corso_ma_scaduta_si_puo(self):
        DocumentVersion.objects.filter(pk=self.v.pk).update(
            processing_status=P.RUNNING, processing_updated_at=timezone.now() - timedelta(hours=2)
        )
        self.assertEqual(processing.richiedi_rielaborazione(user=self.mario, version=self.v).processing_status, P.PENDING)


class ViewElaborazioneTests(ProcessingBase):
    HX = {"HX-Request": "true"}

    def setUp(self):
        self.doc = self.documento_con()
        self.v = self.versione(self.doc)
        self.url_stato = reverse("documents:version_processing", args=[self.doc.pk, 1])
        self.url_rielabora = reverse("documents:version_reprocess", args=[self.doc.pk, 1])

    def test_il_frammento_ricarica_finche_non_e_finita(self):
        self.client.force_login(self.mario)
        r = self.client.get(self.url_stato, headers=self.HX)
        self.assertContains(r, "In coda")
        self.assertContains(r, 'hx-trigger="every 3s, dv:versione from:body"')
        self.assertNotContains(r, "<html")
        self.processa(self.v)
        r = self.client.get(self.url_stato, headers=self.HX)
        self.assertContains(r, "Testo estratto")
        self.assertNotContains(r, 'hx-trigger="every 3s')  # il polling dell'elaborazione si ferma da solo
        self.assertEqual(r["HX-Refresh"], "true")  # a fine lavoro la pagina si ricarica: appaiono anteprima e testo

    def test_finche_e_in_corso_la_pagina_non_si_ricarica(self):
        self.client.force_login(self.mario)
        self.assertNotIn("HX-Refresh", self.client.get(self.url_stato, headers=self.HX))

    def test_nessun_commento_di_template_finisce_nella_pagina(self):
        # {# #} vale solo su una riga: su più righe comparirebbe come testo.
        self.client.force_login(self.mario)
        for url in (self.doc.get_absolute_url(), self.url_stato):
            r = self.client.get(url, headers=self.HX)
            self.assertNotContains(r, "{#")
            self.assertNotContains(r, "#}")

    def test_senza_htmx_si_torna_al_documento(self):
        self.client.force_login(self.mario)
        self.assertRedirects(self.client.get(self.url_stato), self.doc.get_absolute_url())

    def test_chi_non_vede_il_documento_riceve_404(self):
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.get(self.url_stato, headers=self.HX).status_code, 404)
        self.assertEqual(self.client.post(self.url_rielabora).status_code, 404)

    def test_solo_chi_puo_modificare_rielabora(self):
        share_document(user=self.mario, document=self.doc, grantee_user=self.luisa, level=DocumentPermission.Level.READ)
        DocumentVersion.objects.filter(pk=self.v.pk).update(processing_status=P.FAILED)
        self.client.force_login(self.luisa)
        self.assertEqual(self.client.post(self.url_rielabora).status_code, 403)
        self.assertNotContains(self.client.get(self.url_stato, headers=self.HX), "Rielabora")
        self.client.force_login(self.mario)
        self.assertContains(self.client.get(self.url_stato, headers=self.HX), "Rielabora")

    def test_rielabora_via_htmx(self):
        DocumentVersion.objects.filter(pk=self.v.pk).update(processing_status=P.FAILED, processing_message="boom")
        self.client.force_login(self.mario)
        r = self.client.post(self.url_rielabora, headers=self.HX)
        self.assertContains(r, "In coda")
        self.v.refresh_from_db()
        self.assertEqual(self.v.processing_status, P.PENDING)

    def test_rielabora_senza_htmx_torna_al_documento(self):
        self.client.force_login(self.mario)
        self.assertRedirects(self.client.post(self.url_rielabora), self.doc.get_absolute_url())

    def test_rielabora_accetta_solo_post(self):
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get(self.url_rielabora).status_code, 405)

    def test_rielabora_in_corso_risponde_409(self):
        DocumentVersion.objects.filter(pk=self.v.pk).update(
            processing_status=P.RUNNING, processing_updated_at=timezone.now()
        )
        self.client.force_login(self.mario)
        self.assertEqual(self.client.post(self.url_rielabora, headers=self.HX).status_code, 409)

    def test_documento_bloccato_si_puo_comunque_rielaborare(self):
        Document.objects.filter(pk=self.doc.pk).update(status=Document.Status.APPROVED)
        DocumentVersion.objects.filter(pk=self.v.pk).update(processing_status=P.FAILED)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.post(self.url_rielabora, headers=self.HX).status_code, 200)

    def test_il_testo_estratto_si_carica_solo_dove_serve(self):
        # Il testo può pesare megabyte: l'elenco, il download e la cronologia non devono portarselo dietro.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.processa(self.v)
        self.client.force_login(self.mario)

        def query_con_testo(url):
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(url).status_code, 200)
            return [q for q in ctx.captured_queries if "extracted_text" in q["sql"]]

        self.assertEqual(query_con_testo(reverse("documents:list")), [])
        self.assertEqual(query_con_testo(reverse("documents:download", args=[self.doc.pk])), [])
        self.assertEqual(len(query_con_testo(self.doc.get_absolute_url())), 1)  # solo la versione corrente

    def test_il_dettaglio_mostra_lo_stato_di_ogni_versione(self):
        self.client.force_login(self.mario)
        self.assertContains(self.client.get(self.doc.get_absolute_url()), f'id="elab-{self.v.pk}"')


class ComandoTests(ProcessingBase):
    def test_sincrono_elabora_tutto_senza_worker(self):
        doc = self.documento_con()
        out = StringIO()
        call_command("elabora_versioni", "--sincrono", stdout=out)
        v = self.versione(doc)
        self.assertEqual(v.processing_status, P.DONE)
        self.assertIn("Elaborate 1 versioni", out.getvalue())

    def test_tutte_rimette_in_coda_anche_le_completate(self):
        doc = self.documento_con()
        self.processa(self.versione(doc))
        out = StringIO()
        with mock.patch.object(processing, "_invia", return_value=True) as invia:
            call_command("elabora_versioni", "--tutte", stdout=out)
        invia.assert_called_once()
        self.assertEqual(self.versione(doc).processing_status, P.PENDING)


class ImpostazioniTests(DocumentTestCase):
    def test_i_test_non_scrivono_nella_coda_vera(self):
        from django.conf import settings

        self.assertEqual(settings.CELERY_BROKER_URL, "memory://")

    def test_cache_e_coda_usano_database_redis_diversi(self):
        from config.settings import _redis_con_db

        self.assertEqual(_redis_con_db("redis://redis:6379/0", 1), "redis://redis:6379/1")
