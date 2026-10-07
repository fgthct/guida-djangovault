"""Estrazione di testo e anteprima con file veri (poppler e Tesseract devono essere installati)."""

import io
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from django.test import SimpleTestCase, override_settings
from PIL import Image

from .. import extraction
from . import fixtures

serve_poppler = unittest.skipUnless(shutil.which("pdftotext"), "poppler-utils non installato")
serve_tesseract = unittest.skipUnless(shutil.which("tesseract"), "tesseract non installato")


class EstrazioneBase(SimpleTestCase):
    def estrai(self, nome: str, contenuto: bytes) -> extraction.Estratto:
        with tempfile.TemporaryDirectory() as tmp:
            percorso = Path(tmp) / "input"
            percorso.write_bytes(contenuto)
            return extraction.estrai(percorso, nome)

    def assertPng(self, dati):
        self.assertIsNotNone(dati)
        img = Image.open(io.BytesIO(dati))
        self.assertEqual(img.format, "PNG")
        self.assertLessEqual(img.width, 720)


class PulizieTests(SimpleTestCase):
    def test_nul_e_controlli_rimossi(self):
        self.assertEqual(extraction.pulisci_testo("a\x00b\x07c"), "abc")  # PostgreSQL rifiuta NUL

    def test_righe_vuote_e_a_capo_compattati(self):
        self.assertEqual(extraction.pulisci_testo("a\r\n\r\n\r\n\r\nb  \n"), "a\n\nb")

    def test_estensione(self):
        self.assertEqual(extraction.estensione("Relazione.FINALE.PDF"), "pdf")
        self.assertEqual(extraction.estensione("senza"), "")


class TestoSempliceTests(EstrazioneBase):
    def test_utf8(self):
        self.assertEqual(self.estrai("a.txt", "Perché è così".encode()).text, "Perché è così")

    def test_utf8_con_bom(self):
        self.assertEqual(self.estrai("a.txt", b"\xef\xbb\xbfciao").text, "ciao")

    def test_windows_1252(self):
        self.assertEqual(self.estrai("a.csv", "caffè".encode("cp1252")).text, "caffè")

    def test_troncato_al_limite(self):
        with override_settings(PROCESSING_TEXT_MAX_CHARS=10):
            r = self.estrai("a.md", b"x" * 100)
        self.assertEqual(len(r.text), 10)
        self.assertTrue(r.truncated)


@serve_poppler
class PdfTests(EstrazioneBase):
    def test_testo_pagine_e_anteprima(self):
        r = self.estrai("a.pdf", fixtures.pdf_con_testo("Contratto di fornitura numero uno", "Seconda pagina con altro testo"))
        self.assertIn("Contratto di fornitura numero uno", r.text)
        self.assertIn("Seconda pagina", r.text)
        self.assertEqual(r.pages, 2)
        self.assertFalse(r.ocr)
        self.assertPng(r.preview)

    def test_file_non_pdf_con_estensione_pdf(self):
        with self.assertRaisesMessage(extraction.ErroreEstrazione, "PDF non valido"):
            self.estrai("a.pdf", b"questo non e' un pdf")

    @serve_tesseract
    def test_scansione_usa_locr(self):
        r = self.estrai("scan.pdf", fixtures.pdf_scansione("CONTRATTO DI LOCAZIONE"))
        self.assertTrue(r.ocr)
        self.assertIn("CONTRATTO", r.text.upper())
        self.assertPng(r.preview)

    @serve_tesseract
    def test_ocr_limitato_alle_prime_pagine_con_avviso(self):
        pagine = fixtures.pdf_scansione("PAGINA DI PROVA")
        with override_settings(PROCESSING_OCR_MAX_PAGES=1):
            r = self.estrai("scan.pdf", pagine)
        self.assertTrue(r.ocr)  # una sola pagina: nessun avviso
        self.assertEqual(r.note, "")


@serve_tesseract
class ImmaginiTests(EstrazioneBase):
    def test_ocr_e_anteprima(self):
        r = self.estrai("fattura.png", fixtures.immagine_con_testo("FATTURA NUMERO 2026"))
        self.assertIn("FATTURA", r.text.upper())
        self.assertTrue(r.ocr)
        self.assertEqual(r.pages, 1)
        self.assertPng(r.preview)

    def test_jpeg(self):
        r = self.estrai("foto.jpg", fixtures.immagine_con_testo("RICEVUTA", "JPEG"))
        self.assertIn("RICEVUTA", r.text.upper())

    def test_immagine_danneggiata(self):
        with self.assertRaisesMessage(extraction.ErroreEstrazione, "Immagine non valida"):
            self.estrai("rotta.png", b"\x89PNG finto")

    def test_bomba_di_decompressione(self):
        with override_settings(PROCESSING_MAX_IMAGE_PIXELS=100):
            with self.assertRaisesMessage(extraction.ErroreEstrazione, "troppo grande"):
                self.estrai("grande.png", fixtures.immagine_con_testo())


class OfficeTests(EstrazioneBase):
    def setUp(self):
        # Le anteprime via LibreOffice sono provate a parte: qui si prova solo il testo.
        patcher = unittest.mock.patch.object(extraction, "libreoffice", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_docx(self):
        r = self.estrai("a.docx", fixtures.docx("Primo paragrafo", "Secondo paragrafo"))
        self.assertEqual(r.text, "Primo paragrafo\nSecondo paragrafo")
        self.assertIsNone(r.preview)

    def test_xlsx_con_testo_condiviso_e_in_cella(self):
        r = self.estrai("a.xlsx", fixtures.xlsx("Totale fattura", "Cliente Rossi"))
        self.assertIn("Cliente Rossi", r.text)
        self.assertIn("testo in cella", r.text)

    def test_pptx_ordina_le_diapositive(self):
        r = self.estrai("a.pptx", fixtures.pptx("Titolo", "Conclusioni"))
        self.assertEqual(r.text, "[Diapositiva 1]\nTitolo\n\n[Diapositiva 2]\nConclusioni")

    def test_odt(self):
        r = self.estrai("a.odt", fixtures.odt("Verbale di riunione"))
        self.assertEqual(r.text, "Verbale di riunione")

    def test_zip_non_valido(self):
        with self.assertRaisesMessage(extraction.ErroreEstrazione, "non valido"):
            self.estrai("a.docx", b"non e' uno zip")

    def test_entita_xml_ricorsive_rifiutate(self):
        with self.assertRaises(extraction.ErroreEstrazione):
            self.estrai("a.docx", fixtures.bomba_xml())

    def test_archivio_troppo_grande_da_decomprimere(self):
        with unittest.mock.patch.object(extraction, "MAX_DECOMPRESSO", 10):
            with self.assertRaisesMessage(extraction.ErroreEstrazione, "troppo grande"):
                self.estrai("a.docx", fixtures.docx("ciao"))


class FormatiTests(EstrazioneBase):
    def test_formato_sconosciuto_e_non_applicabile(self):
        with self.assertRaises(extraction.FormatoNonSupportato):
            self.estrai("a.xyz", b"dati")

    def test_formato_vecchio_senza_libreoffice(self):
        with unittest.mock.patch.object(extraction, "libreoffice", return_value=None):
            with self.assertRaisesMessage(extraction.FormatoNonSupportato, "LibreOffice"):
                self.estrai("a.doc", b"dati")
