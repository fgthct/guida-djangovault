"""Importazione di una cartella: servizio e comando."""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError

from documents.models import Category, Document
from documents.services import import_folder as imp

from .base import DocumentTestCase


class ImportaCartellaBase(DocumentTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.radice = Path(self.tmp.name)

    def scrivi(self, rel, contenuto=b"%PDF-1.4 uno"):
        p = self.radice / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(contenuto)
        return p

    def importa(self, **kw):
        kw.setdefault("owner", self.admin)
        return imp.importa_cartella(self.radice, **kw)


class ServizioTests(ImportaCartellaBase):
    def test_importa_con_titolo_dal_nome(self):
        self.scrivi("Guida_Django-6.1_finale.pdf")
        r = self.importa()
        self.assertEqual(r.conta(imp.IMPORTATO), 1)
        d = Document.objects.get()
        self.assertEqual(d.title, "Guida Django-6.1 finale")
        self.assertEqual(d.owner, self.admin)
        self.assertTrue(d.ai_allowed)
        self.assertEqual(r.esiti[0].document_id, d.pk)

    def test_categoria_unica_e_riuso_senza_badare_alle_maiuscole(self):
        Category.objects.create(name="Libri informatica")
        self.scrivi("a.pdf", b"%PDF a")
        self.scrivi("b.pdf", b"%PDF b")
        self.importa(categoria="libri INFORMATICA")
        self.assertEqual(Category.objects.count(), 1)
        self.assertEqual(Document.objects.filter(category__name="Libri informatica").count(), 2)

    def test_categoria_dalla_prima_sottocartella(self):
        self.scrivi("Libri/x.pdf", b"%PDF x")
        self.scrivi("Guide/sotto/y.pdf", b"%PDF y")
        self.scrivi("radice.pdf", b"%PDF r")
        self.importa(per_sottocartelle=True)
        cat = {d.title: d.category.name if d.category else None for d in Document.objects.all()}
        self.assertEqual(cat, {"x": "Libri", "y": "Guide", "radice": None})

    def test_stesso_contenuto_non_crea_doppioni(self):
        self.scrivi("uno.pdf", b"%PDF stesso")
        self.scrivi("due.pdf", b"%PDF stesso")
        r = self.importa()
        self.assertEqual((r.conta(imp.IMPORTATO), r.conta(imp.PRESENTE)), (1, 1))
        self.assertEqual(Document.objects.count(), 1)
        r = self.importa()  # e rilanciando
        self.assertEqual(r.conta(imp.PRESENTE), 2)
        self.assertEqual(Document.objects.count(), 1)

    def test_saltati_con_il_motivo(self):
        self.scrivi("programma.exe", b"MZ")
        self.scrivi("vuoto.pdf", b"")
        r = self.importa()
        self.assertEqual(r.conta(imp.SALTATO), 2)
        self.assertTrue(all(e.dettaglio for e in r.esiti))
        self.assertEqual(Document.objects.count(), 0)

    def test_file_nascosti_e_temporanei_ignorati(self):
        self.scrivi(".nascosto.pdf")
        self.scrivi("~$bozza.docx", b"x")
        self.scrivi(".cache/q.pdf")
        self.scrivi("Thumbs.db", b"x")
        self.assertEqual(self.importa().esiti, [])

    def test_senza_immagini(self):
        self.scrivi("copertina.PNG", b"\x89PNG fake")
        self.scrivi("libro.pdf", b"%PDF libro")
        r = self.importa(senza_immagini=True)
        self.assertEqual((r.conta(imp.IMPORTATO), r.conta(imp.SALTATO)), (1, 1))
        self.assertEqual(Document.objects.get().title, "libro")
        self.assertEqual(Document.objects.count(), 1)

    def test_prova_non_scrive_ne_rimuove(self):
        p = self.scrivi("a.pdf")
        r = self.importa(prova=True, rimuovi=True)
        self.assertEqual(r.conta(imp.IMPORTATO), 1)
        self.assertEqual(Document.objects.count(), 0)
        self.assertTrue(p.exists())

    def test_rimuove_solo_importati_e_presenti(self):
        nuovo = self.scrivi("nuovo.pdf", b"%PDF nuovo")
        gia = self.scrivi("gia.pdf", b"%PDF gia")
        brutto = self.scrivi("brutto.exe", b"MZ")
        self.importa()  # importa nuovo e gia
        nuovo.write_bytes(b"%PDF nuovo")  # rimessi in cartella
        gia.write_bytes(b"%PDF gia")
        self.importa(rimuovi=True)
        self.assertFalse(nuovo.exists())
        self.assertFalse(gia.exists())
        self.assertTrue(brutto.exists())

    def test_togli_le_cartelle_rimaste_vuote(self):
        self.scrivi("Libri/sotto/a.pdf")
        self.scrivi("Altri/b.exe", b"MZ")
        self.importa(rimuovi=True)
        self.assertFalse((self.radice / "Libri").exists())
        self.assertTrue((self.radice / "Altri" / "b.exe").exists())
        self.assertTrue(self.radice.exists())

    def test_senza_rimuovi_i_file_restano(self):
        p = self.scrivi("a.pdf")
        self.importa()
        self.assertTrue(p.exists())

    def test_consenso_ia_negato(self):
        self.scrivi("a.pdf")
        self.importa(ai_allowed=False)
        self.assertFalse(Document.objects.get().ai_allowed)

    def test_un_file_rotto_non_ferma_gli_altri(self):
        self.scrivi("a.pdf", b"%PDF a")
        self.scrivi("b.pdf", b"%PDF b")
        originale = imp.create_document
        chiamate = []

        def finto(**kw):
            chiamate.append(kw["title"])
            if kw["title"] == "a":
                raise RuntimeError("boom")
            return originale(**kw)

        imp.create_document = finto
        self.addCleanup(setattr, imp, "create_document", originale)
        r = self.importa()
        self.assertEqual((r.conta(imp.ERRORE), r.conta(imp.IMPORTATO)), (1, 1))
        self.assertIn("boom", r.esiti[0].dettaglio)

    def test_cartella_inesistente(self):
        with self.assertRaises(NotADirectoryError):
            imp.importa_cartella(self.radice / "no", owner=self.admin)

    def test_titolo_da_nome(self):
        self.assertEqual(imp.titolo_da_nome("a_b  c.PDF"), "a b c")


class ComandoTests(ImportaCartellaBase):
    def lancia(self, *args):
        out = StringIO()
        call_command("importa_cartella", str(self.radice), *args, stdout=out)
        return out.getvalue()

    def test_righe_leggibili_e_proprietario_predefinito(self):
        self.scrivi("Libri/a.pdf", b"%PDF a")
        self.scrivi("b.exe", b"MZ")
        out = self.lancia("--per-sottocartelle")
        self.assertIn("@@ importato\tLibri/a.pdf\t", out)
        self.assertIn("@@ saltato\tb.exe\t", out)
        self.assertEqual(Document.objects.get().owner, self.admin)  # il primo superutente
        self.assertIn("importati 1", out)

    def test_utente_indicato_e_sconosciuto(self):
        self.scrivi("a.pdf")
        self.lancia("--utente", "mario", "--categoria", "Guide", "--senza-consenso-ia")
        d = Document.objects.get()
        self.assertEqual((d.owner, d.category.name, d.ai_allowed), (self.mario, "Guide", False))
        with self.assertRaises(CommandError):
            self.lancia("--utente", "nessuno")

    def test_prova_e_rimuovi(self):
        p = self.scrivi("a.pdf")
        self.assertIn("PROVA", self.lancia("--prova", "--rimuovi"))
        self.assertTrue(p.exists())
        self.lancia("--rimuovi")
        self.assertFalse(p.exists())

    def test_cartella_sbagliata(self):
        with self.assertRaises(CommandError):
            call_command("importa_cartella", str(self.radice / "no"), stdout=StringIO())


class CategorizzaTests(DocumentTestCase):
    def setUp(self):
        self.a = self.nuovo_documento(titolo="Zolfo", uploaded_file=self.file(contenuto=b"%PDF a"))
        self.b = self.nuovo_documento(titolo="Altro romanzo", uploaded_file=self.file(contenuto=b"%PDF b"))
        self.c = self.nuovo_documento(titolo="Guida", uploaded_file=self.file(contenuto=b"%PDF c"))
        self.guide = Category.objects.create(name="Guide")
        Document.objects.filter(pk=self.c.pk).update(category=self.guide)

    def lancia(self, *args):
        out = StringIO()
        call_command("categorizza", *args, stdout=out)
        return out.getvalue()

    def test_per_titolo_e_per_testo(self):
        self.lancia("romanzi", "zolfo", "--contiene", "ROMANZO")
        for d in (self.a, self.b):
            d.refresh_from_db()
            self.assertEqual(d.category.name, "romanzi")

    def test_non_sposta_chi_ha_gia_una_categoria_salvo_sovrascrivi(self):
        self.assertIn("Nessun documento", self.lancia("Romanzi", "guida"))
        self.lancia("Romanzi", "guida", "--sovrascrivi")
        self.c.refresh_from_db()
        self.assertEqual(self.c.category.name, "Romanzi")

    def test_da_sposta_solo_quelli_di_quella_categoria(self):
        Document.objects.filter(pk=self.a.pk).update(category=self.guide)
        self.lancia("Romanzi", "zolfo", "guida", "--da", "guide")
        for d in (self.a, self.b, self.c):
            d.refresh_from_db()
        self.assertEqual((self.a.category.name, self.c.category.name), ("Romanzi", "Romanzi"))
        self.assertIsNone(self.b.category)  # non era in «Guide»

    def test_per_estensione_anche_con_lo_stesso_titolo_del_libro(self):
        img = self.nuovo_documento(titolo="Zolfo", uploaded_file=SimpleUploadedFile("zolfo.JPEG", b"img"))
        self.lancia("Copertine", "--estensione", "png,jpg,jpeg")
        img.refresh_from_db(); self.a.refresh_from_db()
        self.assertEqual(img.category.name, "Copertine")
        self.assertIsNone(self.a.category)  # il PDF con lo stesso titolo resta dov'era

    def test_da_ignora_i_documenti_di_altre_categorie(self):
        self.lancia("Romanzi", "guida", "--da", "Altra")
        self.c.refresh_from_db()
        self.assertEqual(self.c.category.name, "Guide")

    def test_prova_non_scrive(self):
        self.assertIn("da assegnare", self.lancia("Romanzi", "zolfo", "--prova"))
        self.assertFalse(Category.objects.filter(name="Romanzi").exists())

    def test_serve_un_criterio(self):
        with self.assertRaises(CommandError):
            call_command("categorizza", "Romanzi", stdout=StringIO())


class DuplicatiTests(DocumentTestCase):
    def nuovo(self, titolo, nome="x.pdf", n=[0]):
        n[0] += 1
        return self.nuovo_documento(titolo=titolo, uploaded_file=SimpleUploadedFile(nome, f"%PDF {n[0]}".encode()))

    def lancia(self, *args):
        out = StringIO()
        call_command("duplicati", *args, stdout=out)
        return out.getvalue()

    def test_raggruppa_titoli_imparentati(self):
        for t in ("ZERO DAY", "ZeroDay", "ZERO DAY (con i commenti)", "Zero Day cover KDP", "Guida Django", "Python"):
            self.nuovo(t)
        out = self.lancia()
        self.assertIn("(4)", out)  # i quattro «Zero Day»
        self.assertNotIn("Guida Django", out)
        self.assertIn("1 gruppi, 4 documenti coinvolti su 6", out)

    def test_titoli_corti_non_si_uniscono_per_prefisso(self):
        self.nuovo("Rust")
        self.nuovo("Rustico e altro")
        self.assertIn("0 gruppi", self.lancia())

    def test_non_modifica_nulla(self):
        self.nuovo("Libro nuovo")
        self.nuovo("libro nuovo")
        prima = Document.objects.count()
        self.lancia()
        self.assertEqual(Document.objects.count(), prima)

    def test_minimo(self):
        for t in ("Il segnale", "IL SEGNALE", "Il segnale scaletta"):
            self.nuovo(t)
        self.assertIn("1 gruppi", self.lancia("--minimo", "3"))
        self.assertIn("0 gruppi", self.lancia("--minimo", "4"))


class EliminaDocumentiTests(DocumentTestCase):
    def setUp(self):
        self.a = self.nuovo_documento(titolo="A", uploaded_file=SimpleUploadedFile("a.pdf", b"%PDF a"))
        self.b = self.nuovo_documento(titolo="B", uploaded_file=SimpleUploadedFile("b.pdf", b"%PDF b"))

    def lancia(self, *args):
        out = StringIO()
        call_command("elimina_documenti", *args, stdout=out)
        return out.getvalue()

    def test_senza_conferma_e_solo_una_prova(self):
        out = self.lancia(str(self.a.pk), str(self.b.pk))
        self.assertIn("PROVA: 2 documenti", out)
        self.assertEqual(Document.objects.count(), 2)

    def test_con_conferma_elimina_solo_gli_indicati_e_lascia_traccia(self):
        from core.models import AuditLog

        out = self.lancia(str(self.a.pk), "99999", "--conferma")
        self.assertIn("NON TROVATO", out)
        self.assertIn("Eliminati 1 documenti su 2", out)
        self.assertEqual(list(Document.objects.values_list("pk", flat=True)), [self.b.pk])
        self.assertTrue(AuditLog.objects.filter(action="document.deleted").exists())

    def test_senza_ids_errore(self):
        with self.assertRaises(CommandError):
            call_command("elimina_documenti", stdout=StringIO())


class ConfrontaTests(DocumentTestCase):
    def doc(self, titolo, testo):
        d = self.nuovo_documento(titolo=titolo, uploaded_file=SimpleUploadedFile(f"{titolo}.txt", testo.encode()))
        v = d.current_version
        v.extracted_text = testo
        v.save(update_fields=["extracted_text"])
        return d

    def lancia(self, *ids):
        out = StringIO()
        call_command("confronta", *map(str, ids), stdout=out)
        return out.getvalue()

    def test_versioni_simili_e_capitolo_in_piu(self):
        base = " ".join(f"parola{i}" for i in range(400))
        a = self.doc("A", base)
        b = self.doc("B", base.replace("parola10 ", "diversa "))  # un ritocco
        c = self.doc("C", base + " " + " ".join(f"extra{i}" for i in range(200)))  # un capitolo in più
        out = self.lancia(a.pk, b.pk, c.pk)
        righe = [r for r in out.splitlines() if r.strip().startswith("#") and "%" in r]
        self.assertEqual(len(righe), 3)
        riga_a = righe[0].split()
        self.assertTrue(int(riga_a[2].rstrip("%")) > 95)  # A si ritrova quasi tutto in B
        self.assertIn("100%", righe[0])  # A è tutta dentro C
        self.assertIn("Nulla è stato modificato", out)

    def test_formato_e_impaginazione_non_contano(self):
        a = self.doc("A", "Il ciclone arrivò da sud-est, nella notte; per le prime due ore nessuno lo temette davvero mai.")
        b = self.doc("B", "il ciclone\narrivò da sud est nella notte\n\nper le prime due ore nessuno lo temette davvero mai")
        self.assertIn("100%", self.lancia(a.pk, b.pk))

    def test_id_inesistente(self):
        with self.assertRaises(CommandError):
            call_command("confronta", "99999", stdout=StringIO())
