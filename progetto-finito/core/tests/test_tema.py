"""Guardia del tema scuro.

Scelta di progetto: nessuno sfondo bianco o molto chiaro (affaticano la vista).
Nei template e nei widget si usano i token del tema (bg-page, bg-surface, bg-surface-2,
bg-rail, text-fg, text-muted...) definiti in frontend/css/app.css. Questo test fallisce se
qualcuno scrive una classe Tailwind con sfondo chiaro.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

# bg-white, grigi 50-300 e tinte 50-200 (anche con prefissi come hover: o md: e opacità /90).
SFONDO_CHIARO = re.compile(
    r"(?<![\w-])bg-(?:white"
    r"|(?:slate|gray|zinc|neutral|stone)-(?:50|100|200|300)"
    r"|(?:red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple"
    r"|fuchsia|pink|rose)-(?:50|100|200))(?![\w-])"
)
ESTENSIONI = {".html", ".py", ".js"}
CARTELLE = ["templates", "accounts", "core", "documents", "static/js"]


class TemaScuroTests(SimpleTestCase):
    def test_nessuna_classe_con_sfondo_chiaro(self):
        trovate = []
        for cartella in CARTELLE:
            for file in (Path(settings.BASE_DIR) / cartella).rglob("*"):
                if file.suffix not in ESTENSIONI or "tests" in file.parts or "migrations" in file.parts:
                    continue
                for n, riga in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                    for m in SFONDO_CHIARO.finditer(riga):
                        trovate.append(f"{file.relative_to(settings.BASE_DIR)}:{n}: {m.group(0)}")
        self.assertEqual(trovate, [], "Sfondi chiari non ammessi: usa i token del tema scuro")

    def test_il_rilevatore_riconosce_le_classi_chiare(self):
        for classe in ["bg-white", "bg-white/90", "bg-slate-50", "hover:bg-slate-100", "bg-red-50", "md:bg-gray-200"]:
            self.assertRegex(classe, SFONDO_CHIARO, classe)
        for classe in ["bg-page", "bg-surface-2", "bg-red-500/10", "bg-indigo-600", "bg-slate-900", "text-white"]:
            self.assertNotRegex(classe, SFONDO_CHIARO, classe)

    def test_css_dell_admin_forza_il_tema_scuro(self):
        css = (Path(settings.BASE_DIR) / "static/css/admin_dark.css").read_text(encoding="utf-8")
        self.assertIn("color-scheme: dark", css)
        self.assertIn("html[data-theme]", css)
