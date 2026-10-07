from django.test import TestCase
from django.urls import reverse

from accounts.models import User


class LoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.utente = User.objects.create_user("mario", password="password-lunga-123")

    def test_pagina_di_accesso_visibile(self):
        risposta = self.client.get(reverse("accounts:login"))
        self.assertEqual(risposta.status_code, 200)
        self.assertContains(risposta, "Accedi")

    def test_accesso_corretto_porta_alla_dashboard(self):
        risposta = self.client.post(
            reverse("accounts:login"), {"username": "mario", "password": "password-lunga-123"}
        )
        self.assertRedirects(risposta, reverse("core:dashboard"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.utente.pk)

    def test_accesso_rispetta_il_parametro_next(self):
        url = reverse("accounts:login") + "?next=/stato-servizi/"
        risposta = self.client.post(url, {"username": "mario", "password": "password-lunga-123"})
        self.assertRedirects(risposta, "/stato-servizi/", fetch_redirect_response=False)

    def test_password_errata_non_crea_sessione(self):
        risposta = self.client.post(
            reverse("accounts:login"), {"username": "mario", "password": "sbagliata"}
        )
        self.assertEqual(risposta.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(risposta, "text-red-300")  # riquadro d'errore

    def test_utente_gia_autenticato_va_alla_dashboard(self):
        self.client.force_login(self.utente)
        risposta = self.client.get(reverse("accounts:login"))
        self.assertRedirects(risposta, reverse("core:dashboard"))

    def test_logout_solo_con_post(self):
        self.client.force_login(self.utente)
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)

        risposta = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(risposta, reverse("accounts:login"))
        self.assertNotIn("_auth_user_id", self.client.session)
