"""Test di sicurezza permanenti.

Lezione dei progetti precedenti: ogni nuova view deve richiedere l'autenticazione
salvo eccezioni esplicite. Il test sotto scandisce TUTTE le rotte dell'applicazione,
così una view dimenticata senza controllo di accesso fa fallire la suite.
"""

import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse


def _rotte(patterns, prefisso=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _rotte(p.url_patterns, prefisso + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefisso + str(p.pattern)


class AccessoAnonimoTests(TestCase):
    # Rotte volutamente pubbliche. Aggiungerne una qui è una decisione da motivare.
    PUBBLICHE = {
        "accounts/login/",
        "healthz/",
        # PWA: il browser le chiede prima e fuori dalla sessione; non contengono nulla di riservato (core/views_pwa.py).
        "manifest.webmanifest",
        "sw.js",
        "offline/",
        f"{settings.ADMIN_URL}login/",
        f"{settings.ADMIN_URL}jsi18n/",
        f"{settings.ADMIN_URL}logout/",  # da anonimo si limita a rimandare all'indice dell'admin
    }

    def test_nessuna_rotta_e_accessibile_da_anonimo_salvo_le_pubbliche(self):
        controllate = 0
        for rotta in _rotte(get_resolver().url_patterns):
            if re.search(r"[<(^$]", rotta):  # rotte con parametri: richiedono argomenti
                continue
            if rotta in self.PUBBLICHE:
                continue
            with self.subTest(rotta=rotta):
                risposta = self.client.get("/" + rotta)
                self.assertIn(risposta.status_code, {301, 302, 401, 403, 405}, f"/{rotta} è pubblica")
                if risposta.status_code == 302:
                    self.assertIn("login", risposta["Location"])
                controllate += 1
        self.assertGreater(controllate, 3)  # la scansione ha davvero trovato qualcosa


class IntestazioniETemplateTests(TestCase):
    def test_csp_presente_e_senza_unsafe(self):
        csp = self.client.get(reverse("accounts:login"))["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)

    def test_niente_script_o_stili_inline(self):
        """La CSP vieta l'inline: i template non devono contenerne."""
        utente = get_user_model().objects.create_user("mario", password="password-lunga-123")
        pagine = [self.client.get(reverse("accounts:login")).content.decode()]
        self.client.force_login(utente)
        pagine.append(self.client.get(reverse("core:dashboard")).content.decode())
        for html in pagine:
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)[^>]*>")
            self.assertNotRegex(html, r"\sstyle=")
            self.assertNotRegex(html, r"\son(click|load|change|submit)=")

    def test_media_non_serviti_pubblicamente(self):
        self.assertEqual(self.client.get("/media/qualsiasi.pdf").status_code, 404)
