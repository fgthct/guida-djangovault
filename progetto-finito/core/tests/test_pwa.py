"""PWA: manifest, service worker, pagina offline, icone, scansione con la fotocamera e creazione del PDF."""

import json
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import User

RADICE = Path(settings.BASE_DIR)


class ManifestTests(TestCase):
    def setUp(self):
        self.m = json.loads(self.client.get(reverse("core:manifest")).content)

    def test_e_pubblico_e_non_si_conserva(self):
        r = self.client.get(reverse("core:manifest"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/manifest+json")
        self.assertEqual(r["Cache-Control"], "no-cache")

    def test_campi_per_l_installazione(self):
        self.assertEqual(self.m["display"], "standalone")
        self.assertEqual(self.m["scope"], "/")
        self.assertEqual(self.m["lang"], "it")
        self.assertTrue(self.m["start_url"].startswith("/"))
        self.assertTrue(self.m["name"] and self.m["short_name"])
        misure = {(i["sizes"], i["purpose"]) for i in self.m["icons"]}
        self.assertIn(("192x192", "any"), misure)
        self.assertIn(("512x512", "any"), misure)
        self.assertIn(("512x512", "maskable"), misure)

    def test_le_icone_esistono_e_hanno_la_misura_dichiarata(self):
        for icona in self.m["icons"]:
            nome = icona["src"].removeprefix("/static/")
            percorso = RADICE / "static" / nome
            self.assertTrue(percorso.is_file(), nome)
            larg, alt = struct.unpack(">II", percorso.read_bytes()[16:24])  # intestazione IHDR del PNG
            self.assertEqual(f"{larg}x{alt}", icona["sizes"], nome)
        self.assertEqual(struct.unpack(">II", (RADICE / "static/img/apple-touch-icon.png").read_bytes()[16:24]), (180, 180))

    def test_scorciatoie_puntano_a_pagine_vere(self):
        for s in self.m["shortcuts"]:
            self.client.force_login(User.objects.create_user(f"u{len(s['url'])}", password="password-lunga-123"))
            self.assertIn(self.client.get(s["url"]).status_code, (200, 302), s["url"])

    def test_condivisione_dal_telefono(self):
        t = self.m["share_target"]
        self.assertEqual(t["method"], "POST")
        self.assertEqual(t["enctype"], "multipart/form-data")
        self.assertEqual(t["action"], reverse("core:condividi"))
        self.assertEqual(t["params"]["files"][0]["name"], "file")
        self.assertIn("application/pdf", t["params"]["files"][0]["accept"])


class ServiceWorkerTests(TestCase):
    def test_sta_alla_radice_e_non_si_conserva(self):
        r = self.client.get(reverse("core:service_worker"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(r["Service-Worker-Allowed"], "/")
        self.assertEqual(r["Cache-Control"], "no-cache")
        self.assertEqual(reverse("core:service_worker"), "/sw.js")

    def test_non_conserva_pagine_ne_dati(self):
        """Regola di riservatezza: nella cache entrano solo i file statici e la pagina offline."""
        js = self.client.get(reverse("core:service_worker")).content.decode()
        self.assertIn('url.pathname.startsWith("/static/")', js)
        self.assertEqual(js.count("cache.put("), 3)  # statici + i due passaggi della condivisione
        self.assertIn("caches.keys()", js)
        self.assertNotIn("HTMLElement", js)

    def test_gestisce_la_condivisione_e_la_pagina_offline(self):
        js = self.client.get(reverse("core:service_worker")).content.decode()
        self.assertIn(f'"{reverse("core:condividi")}"', js)
        self.assertIn(reverse("core:offline"), js)
        self.assertIn(reverse("documents:upload"), js)

    def test_la_versione_cambia_con_il_contenuto(self):
        a = self.client.get(reverse("core:service_worker")).content.decode()
        self.assertRegex(a, r"versione: [0-9a-f]{10}")

    def test_pagina_offline_pubblica(self):
        r = self.client.get(reverse("core:offline"))
        self.assertContains(r, "WireGuard")
        self.assertContains(r, "non è raggiungibile")

    def test_condividi_senza_service_worker_non_perde_l_utente(self):
        """Il POST che arriva al server (service worker non attivo) non dà errore CSRF: rimanda al caricamento."""
        self.client.force_login(User.objects.create_user("mario", password="password-lunga-123"))
        r = self.client.post(reverse("core:condividi"), {"title": "x"})
        self.assertRedirects(r, reverse("documents:upload"), fetch_redirect_response=False)

    def test_condividi_da_anonimo_passa_dall_accesso(self):
        r = self.client.post(reverse("core:condividi"), {"title": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("accounts:login"), r["Location"])

    def test_la_pagina_collega_manifest_icona_e_registrazione(self):
        self.client.force_login(User.objects.create_user("mario", password="password-lunga-123"))
        html = self.client.get(reverse("core:dashboard")).content.decode()
        self.assertIn(f'rel="manifest" href="{reverse("core:manifest")}"', html)
        self.assertIn("apple-touch-icon", html)
        self.assertIn("js/pwa.js", html)
        self.assertNotIn("<script>", html)  # CSP: niente codice inline

    def test_csp_ammette_le_anteprime_della_scansione(self):
        r = self.client.get(reverse("core:offline"))
        self.assertIn("blob:", r["Content-Security-Policy"])
        self.assertIn("script-src 'self'", r["Content-Security-Policy"])


class ScansioneTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.utente = User.objects.create_user("mario", password="password-lunga-123")

    def test_serve_l_accesso(self):
        r = self.client.get(reverse("documents:scan"))
        self.assertEqual(r.status_code, 302)

    def test_pagina(self):
        self.client.force_login(self.utente)
        r = self.client.get(reverse("documents:scan"))
        self.assertContains(r, 'capture="environment"')
        self.assertContains(r, "js/pdf_immagini.js")
        self.assertContains(r, "js/scansiona.js")
        self.assertContains(r, reverse("documents:upload_file"))
        self.assertContains(r, 'id="esito"')

    def test_raggiungibile_da_caricamento_ed_elenco(self):
        self.client.force_login(self.utente)
        self.assertContains(self.client.get(reverse("documents:upload")), reverse("documents:scan"))
        self.assertContains(self.client.get(reverse("documents:list")), reverse("documents:scan"))


class CreazionePdfTests(SimpleTestCase):
    """Il PDF si costruisce nel browser (static/js/pdf_immagini.js): qui lo si verifica con Node e con i programmi di sistema."""

    def test_pdf_valido_da_jpeg(self):
        node = shutil.which("node")
        if not node or not shutil.which("pdfinfo"):
            self.skipTest("servono node e poppler (pdfinfo)")
        from PIL import Image

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for i, (w, h) in enumerate([(300, 400), (400, 200)]):
                Image.new("RGB", (w, h), "white").save(d / f"p{i}.jpg", quality=80)
            script = f"""
              const fs = require('fs');
              const {{ daJpeg }} = require({json.dumps(str(RADICE / 'static/js/pdf_immagini.js'))});
              const pag = [[0,300,400],[1,400,200]].map(([i,w,h]) => ({{dati:new Uint8Array(fs.readFileSync({json.dumps(str(d))}+'/p'+i+'.jpg')),larghezza:w,altezza:h}}));
              fs.writeFileSync({json.dumps(str(d / 'o.pdf'))}, daJpeg(pag, {{titolo: 'Prova (àè)'}}));
            """
            subprocess.run([node, "-e", script], check=True, capture_output=True, timeout=30)
            info = subprocess.run(["pdfinfo", str(d / "o.pdf")], capture_output=True, text=True, check=True).stdout
            self.assertIn("Pages:           2", info)
            self.assertIn("595 x 793", info)  # 300x400 → proporzione 3:4
            sezione = subprocess.run(["pdfinfo", "-f", "2", "-l", "2", str(d / "o.pdf")], capture_output=True, text=True).stdout
            self.assertIn("595 x 298", sezione)  # 400x200 → 2:1
            self.assertEqual(Path(d / "o.pdf").read_bytes()[:5], b"%PDF-")

    def test_senza_pagine_e_un_errore(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("serve node")
        r = subprocess.run(
            [node, "-e", f"require({json.dumps(str(RADICE / 'static/js/pdf_immagini.js'))}).daJpeg([])"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Nessuna pagina", r.stderr)
