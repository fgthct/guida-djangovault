from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User


class DashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.utente = User.objects.create_user("mario", password="password-lunga-123")

    def test_anonimo_viene_mandato_al_login(self):
        risposta = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(
            risposta, f"{reverse('accounts:login')}?next={reverse('core:dashboard')}"
        )

    def test_dashboard_per_utente_autenticato(self):
        self.client.force_login(self.utente)
        risposta = self.client.get(reverse("core:dashboard"))
        self.assertEqual(risposta.status_code, 200)
        self.assertContains(risposta, "Stato dei servizi")
        self.assertContains(risposta, reverse("core:stato_servizi"))  # attributo hx-get

    def test_dashboard_accetta_solo_get(self):
        self.client.force_login(self.utente)
        self.assertEqual(self.client.post(reverse("core:dashboard")).status_code, 405)

    def test_pagina_invia_il_token_csrf_a_htmx(self):
        self.client.force_login(self.utente)
        risposta = self.client.get(reverse("core:dashboard"))
        self.assertContains(risposta, "X-CSRFToken")


class StatoServiziTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.utente = User.objects.create_user("mario", password="password-lunga-123")

    def test_htmx_riceve_solo_il_frammento(self):
        self.client.force_login(self.utente)
        risposta = self.client.get(reverse("core:stato_servizi"), headers={"HX-Request": "true"})
        self.assertEqual(risposta.status_code, 200)
        contenuto = risposta.content.decode()
        self.assertNotIn("<html", contenuto)
        self.assertIn("PostgreSQL", contenuto)
        self.assertIn("pgvector", contenuto)
        self.assertNotIn("Problema", contenuto)  # tutti i servizi operativi
        self.assertNotIn("Proxy", contenuto)  # il nome del backend è quello vero

    def test_senza_htmx_si_torna_alla_dashboard(self):
        self.client.force_login(self.utente)
        risposta = self.client.get(reverse("core:stato_servizi"))
        self.assertRedirects(risposta, reverse("core:dashboard"))

    def test_anonimo_non_vede_lo_stato(self):
        risposta = self.client.get(reverse("core:stato_servizi"), headers={"HX-Request": "true"})
        self.assertEqual(risposta.status_code, 302)
        self.assertIn(reverse("accounts:login"), risposta["Location"])


class HealthzTests(TestCase):
    def test_healthz_pubblico_e_minimale(self):
        risposta = self.client.get(reverse("core:healthz"))
        self.assertEqual(risposta.status_code, 200)
        self.assertEqual(risposta.content, b"ok")


class PgvectorTests(TestCase):
    def test_estensione_vector_installata_dalla_migration(self):
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            self.assertIsNotNone(cur.fetchone())


class StatoElaborazioneTests(TestCase):
    """Controlli della coda e del worker (Fase 6) con Redis e Celery simulati."""

    @classmethod
    def setUpTestData(cls):
        cls.utente = User.objects.create_user("mario", password="password-lunga-123")

    def setUp(self):
        from django.core.cache import cache

        cache.delete("dv:worker-ping")

    def test_senza_broker_reale_e_comunque_operativo(self):
        from core.services import health

        self.assertTrue(health.stato_broker().ok)
        w = health.stato_worker()
        self.assertTrue(w.ok)
        self.assertIn("0 in coda", w.dettaglio)

    def test_nessun_worker_attivo_e_un_problema(self):
        from unittest import mock

        from core.services import health

        with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/1"):
            with mock.patch("config.celery.app.control.inspect") as inspect:
                inspect.return_value.ping.return_value = None
                stato = health.stato_worker()
        self.assertFalse(stato.ok)
        self.assertIn("nessun worker", stato.dettaglio)

    def test_worker_attivo(self):
        from unittest import mock

        from core.services import health

        with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/1"):
            with mock.patch("config.celery.app.control.inspect") as inspect:
                inspect.return_value.ping.return_value = {"worker@x": {"ok": "pong"}}
                stato = health.stato_worker()
        self.assertTrue(stato.ok)
        self.assertIn("1 worker attivo", stato.dettaglio)

    def test_redis_irraggiungibile_e_un_problema(self):
        from core.services import health

        with override_settings(CELERY_BROKER_URL="redis://127.0.0.1:1/1"):
            self.assertFalse(health.stato_broker().ok)

    def test_il_frammento_mostra_i_nuovi_servizi(self):
        self.client.force_login(self.utente)
        r = self.client.get(reverse("core:stato_servizi"), headers={"HX-Request": "true"})
        self.assertContains(r, "Coda dei task")
        self.assertContains(r, "Elaborazione file")
