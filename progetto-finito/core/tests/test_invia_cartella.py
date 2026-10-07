"""Script per il laptop (scripts/invia_cartella.py): le parti pure."""

import importlib.util
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_spec = importlib.util.spec_from_file_location("invia_cartella", Path(settings.BASE_DIR) / "scripts" / "invia_cartella.py")
ic = importlib.util.module_from_spec(_spec)
sys.modules["invia_cartella"] = ic  # serve ai dataclass
_spec.loader.exec_module(ic)


class InviaCartellaTests(SimpleTestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.radice = Path(t.name) / "libri"
        self.radice.mkdir()
        self.registro = ic.Registro(Path(t.name) / "reg.json")

    def scrivi(self, rel, dati=b"x"):
        p = self.radice / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(dati)

    def test_candidati_filtra(self):
        self.scrivi("a.pdf")
        self.scrivi("Sotto/b.PDF")
        self.scrivi("c.exe")
        self.scrivi("d.pdf", b"")
        self.scrivi(".x.pdf")
        self.scrivi("~$y.docx")
        buoni, scartati = ic.candidati(self.radice)
        self.assertEqual([f.rel for f in buoni], ["Sotto/b.PDF", "a.pdf"])
        self.assertEqual({r for r, _ in scartati}, {"c.exe", "d.pdf"})

    def test_immagini_escluse_di_norma(self):
        self.scrivi("copertina.png")
        self.scrivi("libro.pdf")
        senza = [e for e in ic.ESTENSIONI if e not in ic.IMMAGINI]
        buoni, scartati = ic.candidati(self.radice, senza)
        self.assertEqual([f.rel for f in buoni], ["libro.pdf"])
        self.assertIn("--con-immagini", scartati[0][1])
        self.assertEqual(len(ic.candidati(self.radice)[0]), 2)  # con tutte le estensioni (--con-immagini)

    def test_oltre_il_limite(self):
        self.scrivi("grosso.pdf", b"x" * 2048)
        buoni, scartati = ic.candidati(self.radice, max_mb=0)
        self.assertEqual((buoni, [r for r, _ in scartati]), ([], ["grosso.pdf"]))

    def test_nuovi_esclude_inviati_e_doppioni(self):
        self.scrivi("a.pdf", b"1")
        self.scrivi("b.pdf", b"1")  # stesso contenuto
        self.scrivi("c.pdf", b"2")
        buoni, _ = ic.candidati(self.radice)
        n = ic.nuovi(buoni, self.registro)
        self.assertEqual([f.rel for f in n], ["a.pdf", "c.pdf"])
        self.registro.segna(n[0].sha, "a.pdf")
        self.assertEqual([f.rel for f in ic.nuovi(ic.candidati(self.radice)[0], self.registro)], ["c.pdf"])
        self.assertEqual(len(ic.nuovi(ic.candidati(self.radice)[0], self.registro, tutto=True)), 2)

    def test_registro_persiste_e_cambia_con_il_contenuto(self):
        self.scrivi("a.pdf", b"1")
        f = ic.candidati(self.radice)[0][0]
        sha = self.registro.sha(f)
        self.registro.segna(sha, "a.pdf")
        self.registro.salva()
        r2 = ic.Registro(self.registro.percorso)
        self.assertTrue(r2.inviato(sha))
        self.scrivi("a.pdf", b"22")  # file modificato: nuovo sha
        f2 = ic.candidati(self.radice)[0][0]
        self.assertNotEqual(r2.sha(f2), sha)

    def test_registro_illeggibile_riparte_vuoto(self):
        self.registro.percorso.write_text("{rotto")
        self.assertFalse(ic.Registro(self.registro.percorso).inviato("x"))

    def test_comando_remoto_con_virgolette(self):
        c = ic.comando_remoto("progetti/djangovault", ["--categoria", "Libri; rm -rf /"])
        self.assertEqual(c, "cd progetti/djangovault && ./scripts/prod.sh importa --categoria 'Libri; rm -rf /'")

    def test_leggi_esiti(self):
        out = "log\n@@ importato\tLibri/a.pdf\t\n@@ saltato\tb.exe\ttipo\nProprietario: x"
        self.assertEqual(ic.leggi_esiti(out), [("importato", "Libri/a.pdf", ""), ("saltato", "b.exe", "tipo")])

    def test_consenso_ia_spento_di_default(self):
        import argparse
        base = dict(categoria=None, per_sottocartelle=False, classifica=False)
        o = argparse.Namespace(con_consenso_ia=False, **base)
        self.assertIn("--senza-consenso-ia", ic.opzioni_importazione(o))
        o = argparse.Namespace(con_consenso_ia=True, **base)
        self.assertNotIn("--senza-consenso-ia", ic.opzioni_importazione(o))
