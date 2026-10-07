from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase


class PagineDiErroreTests(TestCase):
    """Le pagine di errore (in produzione, cioè con DEBUG spento) sono nostre e in tema scuro."""

    def assertPaginaScura(self, risposta, stato, testo):
        self.assertEqual(risposta.status_code, stato)
        contenuto = risposta.content.decode()
        self.assertIn("bg-page", contenuto)
        self.assertIn(testo, contenuto)

    def test_404(self):
        self.assertPaginaScura(self.client.get("/questa-pagina-non-esiste/"), 404, "Pagina non trovata")

    def test_400_host_non_ammesso(self):
        risposta = self.client.get("/", headers={"Host": "host-non-ammesso.example"})
        self.assertPaginaScura(risposta, 400, "Richiesta non valida")

    def test_403_csrf(self):
        client = Client(enforce_csrf_checks=True)
        risposta = client.post("/accounts/login/", {"username": "x", "password": "y"})
        self.assertPaginaScura(risposta, 403, "sessione del modulo")


class Pagina500Tests(SimpleTestCase):
    def test_500_si_rende_senza_contesto(self):
        html = render_to_string("500.html")
        self.assertIn("bg-page", html)
        self.assertIn("Errore del server", html)
