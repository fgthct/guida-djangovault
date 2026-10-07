"""Classificazione automatica: documenti simili, accordo con l'IA, applicazione, annullamento, coda, valutazione."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from assistant.llm import Classificazione, leggi_classificazione
from assistant.tests.fakes import FakeLLM
from core.models import AuditLog
from documents.models import Category, ClassificationSuggestion, Document
from documents.services import classificazione, indexing, processing

from .base import DocumentTestCase

FAKE = {"EMBEDDING_BACKEND": "documents.tests.fakes.FakeEmbedder", "CLASSIFICAZIONE_SOGLIA_AUTO": 0.85}
S = ClassificationSuggestion.Status

PYTHON = "python django funzione modulo classe libreria programmazione sviluppo codice"
CUCINA = "ricetta cucina pasta farina forno cottura ingredienti sugo cena"
VARIE = ["alfa beta gamma delta", "epsilon zeta omega sigma", "lambda kappa theta iota", "rosso verde giallo azzurro", "tavolo sedia armadio libreria", "monte fiume lago mare"]
DIRITTO = "contratto locazione tribunale sentenza avvocato clausola giudice causa"


@override_settings(**FAKE)
class ClassificazioneBase(DocumentTestCase):
    def setUp(self):
        FakeLLM.reset()
        self.informatica = Category.objects.create(name="Informatica")
        self.cucina = Category.objects.create(name="Cucina")

    def doc(self, titolo, testo, *, categoria=None, ai=False, owner=None):
        d = self.nuovo_documento(
            owner=owner, titolo=titolo, uploaded_file=SimpleUploadedFile("n.txt", testo.encode()), category=categoria, ai_allowed=ai
        )
        processing.elabora_versione(d.current_version_id)
        indexing.indicizza_versione(d.current_version_id)
        return d

    def archivio_di_base(self):
        for i in range(3):
            self.doc(f"Python {i}", f"{PYTHON} {VARIE[i]}", categoria=self.informatica)
            self.doc(f"Cucina {i}", f"{CUCINA} {VARIE[i + 3]}", categoria=self.cucina)


class VettoreDocumentoTests(ClassificazioneBase):
    def test_indicizzazione_salva_il_vettore_del_documento(self):
        d = self.doc("A", PYTHON)
        d.current_version.refresh_from_db()
        self.assertIsNotNone(d.current_version.doc_vector)
        self.assertEqual(len(d.current_version.doc_vector), 384)

    def test_versione_senza_vettore_lo_ricalcola_dai_passaggi(self):
        d = self.doc("A", PYTHON)
        v = d.current_version
        v.__class__.objects.filter(pk=v.pk).update(doc_vector=None)
        v.refresh_from_db()
        self.assertIsNone(v.doc_vector)
        self.assertIsNotNone(classificazione.vettore_della_versione(v))
        v.refresh_from_db()
        self.assertIsNotNone(v.doc_vector)


class ProposteDaiViciniTests(ClassificazioneBase):
    def test_propone_la_categoria_dei_documenti_simili(self):
        self.archivio_di_base()
        nuovo = self.doc("Nuovo", f"{PYTHON} altro")
        sug = classificazione.classifica_documento(nuovo.pk, applica=False)
        self.assertEqual(sug.status, S.PENDING)
        self.assertEqual(sug.category, self.informatica)
        self.assertEqual(sug.source, "vicini")
        self.assertTrue(sug.neighbors)
        self.assertTrue(all(n["categoria"] == "Informatica" for n in sug.neighbors))
        nuovo.refresh_from_db()
        self.assertIsNone(nuovo.category)  # solo una proposta: il documento non è cambiato

    def test_documento_di_argomento_nuovo_non_ha_proposta(self):
        self.archivio_di_base()
        nuovo = self.doc("Legge", DIRITTO)
        sug = classificazione.classifica_documento(nuovo.pk, applica=False)
        self.assertEqual(sug.status, S.NONE)

    def test_non_tocca_documenti_gia_categorizzati(self):
        self.archivio_di_base()
        d = self.doc("Già a posto", PYTHON, categoria=self.cucina)
        self.assertIsNone(classificazione.classifica_documento(d.pk))
        self.assertFalse(ClassificationSuggestion.objects.filter(document=d).exists())

    def test_archivio_vuoto_senza_ia_nessuna_proposta(self):
        d = self.doc("Primo", PYTHON)
        self.assertEqual(classificazione.classifica_documento(d.pk).status, S.NONE)

    def test_i_vicini_sono_solo_documenti_visibili(self):
        self.archivio_di_base()
        # documenti di Luisa, non condivisi: Mario non li "vede" e non devono influenzare la sua classificazione
        for i in range(3):
            self.doc(f"Diritto {i}", f"{DIRITTO} caso{i}", categoria=Category.objects.get_or_create(name="Legale")[0], owner=self.luisa)
        mio = self.doc("Contratto", DIRITTO, owner=self.mario)
        self.assertEqual(classificazione.classifica_documento(mio.pk, applica=False).status, S.NONE)


class ApplicazioneAutomaticaTests(ClassificazioneBase):
    def test_sopra_soglia_applica_da_solo_e_lascia_traccia(self):
        self.archivio_di_base()
        FakeLLM.classificazione_da_dare = Classificazione(
            categoria="Informatica", riassunto="Una guida a Django.", parole_chiave=["django", "python"]
        )
        nuovo = self.doc("Guida", f"{PYTHON} " * 20, ai=True)
        sug = classificazione.classifica_documento(nuovo.pk)
        nuovo.refresh_from_db()
        self.assertEqual(sug.source, "vicini+ia")
        self.assertEqual(sug.status, S.AUTO)
        self.assertEqual(nuovo.category, self.informatica)
        self.assertIn("Una guida a Django.", nuovo.description)
        self.assertIn("Parole chiave: django, python", nuovo.description)
        voce = AuditLog.objects.filter(action="document.classified").latest("pk")
        self.assertTrue(voce.metadata["automatica"])

    def test_non_cambia_updated_at(self):
        self.archivio_di_base()
        FakeLLM.classificazione_da_dare = Classificazione(categoria="Informatica", riassunto="x")
        nuovo = self.doc("Guida", f"{PYTHON} " * 20, ai=True)
        prima = Document.objects.get(pk=nuovo.pk).updated_at
        classificazione.classifica_documento(nuovo.pk)
        self.assertEqual(Document.objects.get(pk=nuovo.pk).updated_at, prima)

    def test_solo_vicini_senza_consenso_resta_proposta_sotto_soglia(self):
        self.archivio_di_base()
        nuovo = self.doc("Guida", f"{PYTHON} altro")  # senza consenso IA
        sug = classificazione.classifica_documento(nuovo.pk)
        self.assertEqual(sug.source, "vicini")
        if sug.confidence < 0.85:
            self.assertEqual(sug.status, S.PENDING)
        self.assertEqual(FakeLLM.chiamate, [])  # senza consenso nessun testo parte verso il modello

    @override_settings(CLASSIFICAZIONE_AUTO=False)
    def test_auto_spento_mai_applicare(self):
        self.archivio_di_base()
        FakeLLM.classificazione_da_dare = Classificazione(categoria="Informatica")
        nuovo = self.doc("Guida", f"{PYTHON} " * 20, ai=True)
        sug = classificazione.classifica_documento(nuovo.pk)
        self.assertEqual(sug.status, S.PENDING)
        nuovo.refresh_from_db()
        self.assertIsNone(nuovo.category)

    def test_categoria_nuova_dell_ia_mai_creata_da_sola(self):
        FakeLLM.classificazione_da_dare = Classificazione(nuova_categoria="Diritto", riassunto="Un contratto.")
        d = self.doc("Contratto", f"{DIRITTO} " * 20, ai=True)
        sug = classificazione.classifica_documento(d.pk, applica=True)
        self.assertEqual(sug.status, S.PENDING)
        self.assertEqual(sug.new_category_name, "Diritto")
        self.assertFalse(Category.objects.filter(name="Diritto").exists())


class IaEPrivacyTests(ClassificazioneBase):
    def test_senza_consenso_il_testo_non_va_al_modello(self):
        self.archivio_di_base()
        d = self.doc("Riservato", f"{PYTHON} " * 20, ai=False)
        classificazione.classifica_documento(d.pk)
        self.assertEqual([c for c in FakeLLM.chiamate if c["tipo"] == "classifica"], [])

    def test_con_consenso_parte_solo_l_estratto_e_le_categorie(self):
        self.archivio_di_base()
        d = self.doc("Aperto", "x " * 4000, ai=True)
        classificazione.classifica_documento(d.pk)
        [chiamata] = [c for c in FakeLLM.chiamate if c["tipo"] == "classifica"]
        self.assertLessEqual(len(chiamata["estratto"]), 3000)
        self.assertEqual({c["nome"] for c in chiamata["categorie"]}, {"Informatica", "Cucina"})
        self.assertTrue(all(len(c["esempi"]) <= 3 for c in chiamata["categorie"]))

    @override_settings(ANTHROPIC_API_KEY="")
    def test_senza_chiave_niente_ia(self):
        self.archivio_di_base()
        d = self.doc("Aperto", f"{PYTHON} " * 20, ai=True)
        classificazione.classifica_documento(d.pk)
        self.assertEqual(FakeLLM.chiamate, [])

    def test_disaccordo_vince_chi_ha_un_idea_forte(self):
        self.archivio_di_base()
        FakeLLM.classificazione_da_dare = Classificazione(categoria="Cucina")
        d = self.doc("Guida", f"{PYTHON} " * 20, ai=True)
        sug = classificazione.classifica_documento(d.pk, applica=False)
        self.assertEqual(sug.category, self.informatica)
        self.assertIn("Cucina", sug.reason)
        self.assertLess(sug.confidence, 0.85)

    def test_leggi_classificazione_tollera_testo_e_campi_sbagliati(self):
        c = leggi_classificazione('ecco: {"categoria": "A", "nuova_categoria": 5, "riassunto": "ok", "parole_chiave": ["x", 3, "x", "y"]}')
        self.assertEqual((c.categoria, c.nuova_categoria, c.riassunto, c.parole_chiave), ("A", "", "ok", ["x", "y"]))
        self.assertEqual(leggi_classificazione("niente json"), Classificazione())
        self.assertEqual(leggi_classificazione("[1,2]"), Classificazione())


class DecisioniTests(ClassificazioneBase):
    def proposta(self, **kw):
        d = self.doc("Doc", PYTHON)
        base = dict(document=d, status=S.PENDING, category=self.informatica, confidence=0.6, summary="Riassunto.", keywords=["a", "b"])
        base.update(kw)
        return ClassificationSuggestion.objects.create(**base)

    def test_accetta_assegna_categoria_e_descrizione(self):
        sug = self.proposta()
        classificazione.applica_proposta(sug, user=self.mario)
        sug.refresh_from_db()
        d = sug.document
        d.refresh_from_db()
        self.assertEqual((sug.status, sug.decided_by, d.category), (S.ACCEPTED, self.mario, self.informatica))
        self.assertEqual(d.description, "Riassunto.\n\nParole chiave: a, b")

    def test_non_sovrascrive_una_descrizione_scritta_a_mano(self):
        sug = self.proposta()
        Document.objects.filter(pk=sug.document_id).update(description="La mia nota")
        classificazione.applica_proposta(sug, user=self.mario)
        self.assertEqual(Document.objects.get(pk=sug.document_id).description, "La mia nota")

    def test_accetta_con_un_altra_categoria(self):
        sug = self.proposta()
        classificazione.applica_proposta(sug, user=self.mario, categoria=self.cucina)
        self.assertEqual(Document.objects.get(pk=sug.document_id).category, self.cucina)

    def test_accetta_crea_la_categoria_nuova(self):
        sug = self.proposta(category=None, new_category_name="Diritto")
        classificazione.applica_proposta(sug, user=self.mario)
        self.assertEqual(Document.objects.get(pk=sug.document_id).category.name, "Diritto")

    def test_accetta_senza_categoria_errore(self):
        sug = self.proposta(category=None)
        with self.assertRaises(ValueError):
            classificazione.applica_proposta(sug, user=self.mario)

    def test_rifiuta(self):
        sug = self.proposta()
        classificazione.rifiuta_proposta(sug, user=self.mario)
        sug.refresh_from_db()
        self.assertEqual(sug.status, S.REJECTED)
        self.assertIsNone(Document.objects.get(pk=sug.document_id).category)

    def test_annulla_ripristina_categoria_e_descrizione(self):
        sug = self.proposta()
        classificazione.applica_proposta(sug, user=None, automatica=True)
        sug.refresh_from_db()
        self.assertTrue(classificazione.annulla_applicazione(sug, user=self.mario))
        d = Document.objects.get(pk=sug.document_id)
        self.assertEqual((d.category, d.description), (None, ""))
        sug.refresh_from_db()
        self.assertEqual(sug.status, S.UNDONE)

    def test_annulla_non_tocca_cio_che_una_persona_ha_cambiato(self):
        sug = self.proposta()
        classificazione.applica_proposta(sug, user=None, automatica=True)
        Document.objects.filter(pk=sug.document_id).update(category=self.cucina)
        sug.refresh_from_db()
        self.assertFalse(classificazione.annulla_applicazione(sug, user=self.mario))
        self.assertEqual(Document.objects.get(pk=sug.document_id).category, self.cucina)


@override_settings(CLASSIFICAZIONE_AUTO=False)
class CodaERichiestaTests(ClassificazioneBase):
    def test_richiedi_e_idempotente_e_riapre_solo_le_chiuse(self):
        d = self.doc("A", PYTHON)
        s1 = classificazione.richiedi(d)
        s1.status = S.ACCEPTED
        s1.save()
        self.assertEqual(classificazione.richiedi(d).status, S.ACCEPTED)  # decisione presa: non si tocca
        s1.status = S.REJECTED
        s1.save()
        self.assertEqual(classificazione.richiedi(d).status, S.REQUESTED)

    def test_dopo_l_indicizzazione_un_documento_richiesto_viene_classificato(self):
        self.archivio_di_base()
        d = self.nuovo_documento(titolo="Nuovo", uploaded_file=SimpleUploadedFile("n.txt", f"{PYTHON} x".encode()))
        classificazione.richiedi(d)
        processing.elabora_versione(d.current_version_id)
        from unittest import mock

        from documents.tasks import classifica_documento_task

        with mock.patch.object(classificazione, "_invia", side_effect=lambda pk: classifica_documento_task(pk)):
            with self.captureOnCommitCallbacks(execute=True):
                indexing.indicizza_versione(d.current_version_id)
        self.assertEqual(ClassificationSuggestion.objects.get(document=d).status, S.PENDING)

    def test_rete_di_sicurezza_classifica_i_rimasti_e_chiude_quelli_senza_testo(self):
        self.archivio_di_base()
        pronto = self.doc("Pronto", f"{PYTHON} z")
        classificazione.richiedi(pronto)
        immagine = self.nuovo_documento(titolo="Foto", uploaded_file=SimpleUploadedFile("f.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20))
        processing.elabora_versione(immagine.current_version_id)
        classificazione.richiedi(immagine)
        self.assertEqual(classificazione.riprendi_classificazioni(), 2)
        self.assertEqual(ClassificationSuggestion.objects.get(document=pronto).status, S.PENDING)
        self.assertEqual(ClassificationSuggestion.objects.get(document=immagine).status, S.PENDING)

    def test_immagine_senza_testo_propone_immagini_e_foto(self):
        immagine = self.nuovo_documento(titolo="Foto", uploaded_file=SimpleUploadedFile("f.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20))
        processing.elabora_versione(immagine.current_version_id)
        classificazione.richiedi(immagine)
        classificazione.riprendi_classificazioni()
        sug = ClassificationSuggestion.objects.get(document=immagine)
        self.assertEqual(sug.status, S.PENDING)
        self.assertEqual(sug.new_category_name, "Immagini e foto")
        immagine.refresh_from_db()
        self.assertIsNone(immagine.category_id)  # mai applicata da sola

    def test_immagine_usa_la_categoria_esistente(self):
        cat = Category.objects.create(name="immagini e foto")
        immagine = self.nuovo_documento(titolo="Foto", uploaded_file=SimpleUploadedFile("f.jpg", b"\xff\xd8\xff" + b"0" * 20))
        processing.elabora_versione(immagine.current_version_id)
        classificazione.richiedi(immagine)
        classificazione.riprendi_classificazioni()
        self.assertEqual(ClassificationSuggestion.objects.get(document=immagine).category, cat)

    def test_documento_senza_testo_spiega_perche_non_ha_proposta(self):
        immagine = self.nuovo_documento(titolo="Dati", uploaded_file=SimpleUploadedFile("f.bin", b"0" * 20))
        processing.elabora_versione(immagine.current_version_id)
        classificazione.richiedi(immagine)
        classificazione.riprendi_classificazioni()
        self.client.force_login(immagine.owner)
        r = self.client.get(f"/documenti/{immagine.pk}/")
        self.assertContains(r, "data-nessuna-proposta")
        self.assertContains(r, "Nessun testo da cui classificare")

    def test_da_smistare_solo_proposte_proprie(self):
        a = self.doc("Mia", PYTHON, owner=self.mario)
        b = self.doc("Sua", PYTHON, owner=self.luisa)
        for d in (a, b):
            ClassificationSuggestion.objects.create(document=d, status=S.PENDING, category=self.informatica)
        self.assertEqual([s.document for s in classificazione.da_smistare(self.mario)], [a])


class ValutazioneTests(ClassificazioneBase):
    def test_valuta_misura_la_precisione_leave_one_out(self):
        self.archivio_di_base()
        r = classificazione.valuta(self.mario)
        self.assertEqual(r["n"], 6)
        self.assertEqual(r["precisione"], 1.0)
        self.assertEqual(r["bins"][-1]["soglia"], 0.0)

    def test_valuta_esclude_le_copie_quasi_identiche(self):
        # due copie identiche in categorie diverse: senza esclusione ciascuna "indovinerebbe" l'altra
        self.doc("Uno", PYTHON, categoria=self.informatica)
        self.doc("Due", PYTHON, categoria=self.cucina)
        r = classificazione.valuta(self.mario)
        self.assertEqual(r["risposti"], 0)

    def test_valuta_con_pochi_documenti(self):
        self.doc("Uno", PYTHON, categoria=self.informatica)
        self.assertIsNone(classificazione.valuta(self.mario)["precisione"])



class VisteSmistamentoTests(ClassificazioneBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.mario)

    def proposta(self, owner=None, **kw):
        d = self.doc("Doc", PYTHON, owner=owner or self.mario)
        base = dict(document=d, status=S.PENDING, category=self.informatica, confidence=0.7, summary="Riassunto.", keywords=["k"], reason="Perché sì.")
        base.update(kw)
        return ClassificationSuggestion.objects.create(**base)

    def test_pagina_mostra_solo_le_proprie_proposte(self):
        mia, altrui = self.proposta(), self.proposta(owner=self.luisa)
        r = self.client.get(reverse("documents:smistamento"))
        self.assertContains(r, "Da rivedere")
        self.assertContains(r, reverse("documents:proposta_accetta", args=[mia.pk]))
        self.assertNotContains(r, reverse("documents:proposta_accetta", args=[altrui.pk]))

    def test_accetta_con_htmx_toglie_la_riga(self):
        sug = self.proposta()
        r = self.client.post(reverse("documents:proposta_accetta", args=[sug.pk]), {"categoria": self.cucina.pk}, headers={"HX-Request": "true"})
        self.assertEqual((r.status_code, r.content), (200, b""))
        self.assertEqual(Document.objects.get(pk=sug.document_id).category, self.cucina)

    def test_accetta_senza_htmx_torna_alla_pagina(self):
        sug = self.proposta()
        r = self.client.post(reverse("documents:proposta_accetta", args=[sug.pk]))
        self.assertRedirects(r, reverse("documents:smistamento"))
        self.assertEqual(Document.objects.get(pk=sug.document_id).category, self.informatica)

    def test_rifiuta(self):
        sug = self.proposta()
        self.client.post(reverse("documents:proposta_rifiuta", args=[sug.pk]))
        sug.refresh_from_db()
        self.assertEqual(sug.status, S.REJECTED)

    def test_annulla_una_applicazione_automatica(self):
        sug = self.proposta()
        classificazione.applica_proposta(sug, user=None, automatica=True)
        r = self.client.get(reverse("documents:smistamento"))
        self.assertContains(r, "Assegnati in automatico")
        self.client.post(reverse("documents:proposta_annulla", args=[sug.pk]))
        self.assertIsNone(Document.objects.get(pk=sug.document_id).category)

    def test_non_si_agisce_sulle_proposte_degli_altri(self):
        altrui = self.proposta(owner=self.luisa)
        for nome in ("accetta", "rifiuta", "annulla"):
            r = self.client.post(reverse(f"documents:proposta_{nome}", args=[altrui.pk]))
            self.assertEqual(r.status_code, 403, nome)
        altrui.refresh_from_db()
        self.assertEqual(altrui.status, S.PENDING)

    def test_solo_post(self):
        sug = self.proposta()
        self.assertEqual(self.client.get(reverse("documents:proposta_accetta", args=[sug.pk])).status_code, 405)

    def test_accetta_tutte_rispetta_la_fiducia_minima(self):
        alta, bassa = self.proposta(confidence=0.9), self.proposta(confidence=0.3)
        self.client.post(reverse("documents:proposte_accetta_tutte"), {"minimo": "0.6"})
        alta.refresh_from_db()
        bassa.refresh_from_db()
        self.assertEqual((alta.status, bassa.status), (S.ACCEPTED, S.PENDING))

    def test_richiede_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("documents:smistamento")).status_code, 302)

    def test_pulsante_suggerisci_categoria(self):
        d = self.doc("Doc", PYTHON)
        r = self.client.get(d.get_absolute_url())
        self.assertContains(r, "Suggerisci categoria")
        self.client.post(reverse("documents:classifica", args=[d.pk]))
        self.assertEqual(ClassificationSuggestion.objects.get(document=d).status, S.REQUESTED)
        d.category = self.cucina
        d.save()
        self.assertNotContains(self.client.get(d.get_absolute_url()), "Suggerisci categoria")

    def test_elenco_mostra_il_contatore(self):
        self.proposta()
        self.assertContains(self.client.get(reverse("documents:list")), reverse("documents:smistamento"))

    def test_elenco_mostra_la_categoria_anche_sul_telefono(self):
        """Sotto 768 px la colonna «Categoria» è nascosta: la categoria deve leggersi sotto il titolo."""
        con = self.doc("Con categoria", PYTHON)
        con.category = self.cucina
        con.save()
        self.doc("Senza categoria", CUCINA)
        html = self.client.get(reverse("documents:list")).content.decode()
        self.assertEqual(html.count("data-categoria-mobile"), 2)
        self.assertIn(f'data-categoria-mobile>{self.cucina.name}</p>', html)
        self.assertIn("data-categoria-mobile>Senza categoria</p>", html)


class CaricamentoAutomaticoTests(ClassificazioneBase):
    def test_upload_web_automatica_crea_la_richiesta(self):
        self.client.force_login(self.mario)
        r = self.client.post(
            reverse("documents:upload_file"),
            {"file": SimpleUploadedFile("guida.txt", b"contenuto di prova unico"), "categoria": "automatica", "ai_allowed": "1"},
        )
        d = Document.objects.get(pk=r.json()["id"])
        self.assertIsNone(d.category)
        self.assertEqual(ClassificationSuggestion.objects.get(document=d).status, S.REQUESTED)

    def test_upload_web_con_categoria_scelta_non_classifica(self):
        self.client.force_login(self.mario)
        r = self.client.post(
            reverse("documents:upload_file"),
            {"file": SimpleUploadedFile("guida.txt", b"altro contenuto"), "categoria": str(self.cucina.pk)},
        )
        self.assertFalse(ClassificationSuggestion.objects.filter(document_id=r.json()["id"]).exists())

    def test_importa_cartella_con_classifica(self):
        import tempfile
        from pathlib import Path

        from documents.services.import_folder import importa_cartella

        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "Romanzi").mkdir()
            (Path(t) / "Romanzi" / "uno.txt").write_text("testo uno unico")
            (Path(t) / "due.txt").write_text("testo due unico")
            importa_cartella(Path(t), owner=self.mario, per_sottocartelle=True, classifica=True)
        con_categoria = Document.objects.get(title="uno")
        senza = Document.objects.get(title="due")
        self.assertIsNotNone(con_categoria.category)
        self.assertFalse(ClassificationSuggestion.objects.filter(document=con_categoria).exists())
        self.assertTrue(ClassificationSuggestion.objects.filter(document=senza).exists())


class ComandoClassificaTests(ClassificazioneBase):
    def esegui(self, *args):
        import io

        from django.core.management import call_command

        out = io.StringIO()
        call_command("classifica", *args, "--utente", "mario", stdout=out)
        return out.getvalue()

    def test_prova_non_salva_nulla(self):
        self.archivio_di_base()
        d = self.doc("Nuovo", f"{PYTHON} extra")
        out = self.esegui("--prova")
        self.assertIn("Informatica", out)
        self.assertIn("PROVA", out)
        self.assertFalse(ClassificationSuggestion.objects.filter(document=d).exists())

    def test_salva_le_proposte_senza_applicare(self):
        self.archivio_di_base()
        d = self.doc("Nuovo", f"{PYTHON} extra")
        self.esegui()
        self.assertEqual(ClassificationSuggestion.objects.get(document=d).status, S.PENDING)
        d.refresh_from_db()
        self.assertIsNone(d.category)

    def test_applica_sopra_soglia(self):
        self.archivio_di_base()
        FakeLLM.classificazione_da_dare = Classificazione(categoria="Informatica")
        d = self.doc("Nuovo", f"{PYTHON} " * 20, ai=True)
        self.assertIn("applicata", self.esegui("--applica"))
        d.refresh_from_db()
        self.assertEqual(d.category, self.informatica)

    def test_senza_ia(self):
        self.archivio_di_base()
        self.doc("Nuovo", f"{PYTHON} " * 20, ai=True)
        self.esegui("--senza-ia")
        self.assertEqual(FakeLLM.chiamate, [])

    def test_non_ripropone_il_gia_deciso(self):
        self.archivio_di_base()
        d = self.doc("Nuovo", f"{PYTHON} extra")
        ClassificationSuggestion.objects.create(document=d, status=S.REJECTED)
        self.assertIn("Nessun documento da classificare", self.esegui())
        self.assertNotIn("Nessun documento da classificare", self.esegui("--rifai"))

    def test_valuta(self):
        self.archivio_di_base()
        out = self.esegui("--valuta")
        self.assertIn("categoria giusta", out)
        self.assertIn("fiducia", out)


class DescrizioneEProposteOriginaliTests(ClassificazioneBase):
    def test_la_descrizione_della_categoria_va_al_modello(self):
        self.informatica.description = "Libri e guide di programmazione"
        self.informatica.save()
        self.archivio_di_base()
        d = self.doc("Guida", "x " * 4000, ai=True)
        classificazione.classifica_documento(d.pk)
        [chiamata] = [c for c in FakeLLM.chiamate if c["tipo"] == "classifica"]
        per_nome = {c["nome"]: c for c in chiamata["categorie"]}
        self.assertEqual(per_nome["Informatica"]["descrizione"], "Libri e guide di programmazione")
        self.assertEqual(per_nome["Cucina"]["descrizione"], "")

    def test_prompt_include_la_descrizione_nell_elenco(self):
        from unittest import mock

        from assistant.llm import AnthropicLLM

        llm = AnthropicLLM.__new__(AnthropicLLM)
        llm._client = mock.Mock()
        llm._client.messages.create.return_value = mock.Mock(content=[mock.Mock(type="text", text='{"categoria": "A"}')])
        r = llm.classifica(titolo="t", nome_file="f", estratto="e", categorie=[{"nome": "A", "n": 3, "descrizione": "Guide dei miei prodotti", "esempi": ["x", "y"]}])
        inviato = llm._client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn("A (3 documenti) — Guide dei miei prodotti. Titoli di esempio: x; y", inviato)
        self.assertEqual(r.categoria, "A")

    def test_la_proposta_originale_resta_anche_se_scegli_un_altra_categoria(self):
        self.archivio_di_base()
        d = self.doc("Nuovo", f"{PYTHON} extra")
        sug = classificazione.classifica_documento(d.pk, applica=False)
        self.assertEqual(sug.proposed_category, self.informatica)
        classificazione.applica_proposta(sug, user=self.mario, categoria=self.cucina)
        sug.refresh_from_db()
        self.assertEqual((sug.category, sug.proposed_category), (self.cucina, self.informatica))
        voce = AuditLog.objects.filter(action="document.classified").latest("pk")
        self.assertEqual(voce.metadata["proposta"], "Informatica")

    def test_esito_delle_decisioni(self):
        self.archivio_di_base()
        a, b, c = (self.doc(f"N{i}", f"{PYTHON} z{i}") for i in range(3))
        sa, sb, sc = (classificazione.classifica_documento(d.pk, applica=False) for d in (a, b, c))
        classificazione.applica_proposta(sa, user=self.mario)  # accettata com'era
        classificazione.applica_proposta(sb, user=self.mario, categoria=self.cucina)  # corretta
        classificazione.rifiuta_proposta(sc, user=self.mario)  # rifiutata
        e = classificazione.esito_delle_decisioni(self.mario)
        self.assertEqual((e["n"], e["giuste"]), (3, 1))
        self.assertEqual(e["per_fonte"]["vicini"], (1, 3))

    def test_valuta_stampa_le_decisioni_reali(self):
        import io

        from django.core.management import call_command

        self.archivio_di_base()
        d = self.doc("N", f"{PYTHON} z")
        classificazione.applica_proposta(classificazione.classifica_documento(d.pk, applica=False), user=self.mario)
        out = io.StringIO()
        call_command("classifica", "--valuta", "--utente", "mario", stdout=out)
        self.assertIn("Le tue decisioni in «Da smistare»: 1 proposte su 1", out.getvalue())

    def test_pagina_categorie_salva_la_descrizione_solo_allo_staff(self):
        url = reverse("documents:category_describe", args=[self.cucina.pk])
        self.client.force_login(self.mario)
        self.assertEqual(self.client.post(url, {"descrizione": "x"}).status_code, 403)
        self.client.force_login(self.staff)
        self.client.post(url, {"descrizione": "  Ricette   e   menu  " + "x" * 400})
        self.cucina.refresh_from_db()
        self.assertTrue(self.cucina.description.startswith("Ricette e menu x"))
        self.assertEqual(len(self.cucina.description), 300)
        self.assertContains(self.client.get(reverse("documents:categories")), 'name="descrizione"')
