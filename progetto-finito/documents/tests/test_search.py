"""Ricerca per parole: pertinenza, accenti, sintassi, permessi, sicurezza degli estratti."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from documents.models import Document, DocumentPermission
from documents.services import add_version, processing, search, share_document

from .base import DocumentTestCase


class RicercaBase(DocumentTestCase):
    def doc(self, titolo, testo="", owner=None, nome="nota.txt", descrizione=""):
        d = self.nuovo_documento(
            owner=owner, titolo=titolo, description=descrizione,
            uploaded_file=SimpleUploadedFile(nome, testo.encode()),
        )
        processing.elabora_versione(d.current_version_id)
        return d

    def trova(self, utente, testo, **kw):
        qs = Document.objects.visible_to(utente)
        return list(search.cerca_parole(qs, testo))

    def titoli(self, utente, testo):
        return [d.title for d in self.trova(utente, testo)]


class PertinenzaTests(RicercaBase):
    def test_il_titolo_vale_piu_del_testo(self):
        self.doc("Verbale assemblea", "si discute del bilancio dell'anno")
        self.doc("Bilancio 2026", "documento contabile")
        self.assertEqual(self.titoli(self.mario, "bilancio"), ["Bilancio 2026", "Verbale assemblea"])

    def test_trova_il_contenuto_dei_file(self):
        self.doc("Scansione 41", "Il contratto di locazione scade il 30 giugno")
        self.assertEqual(self.titoli(self.mario, "locazione"), ["Scansione 41"])

    def test_senza_accenti(self):
        self.doc("Atto", "La società ha firmato")
        self.assertEqual(self.titoli(self.mario, "societa"), ["Atto"])
        self.assertEqual(self.titoli(self.mario, "SOCIETÀ"), ["Atto"])

    def test_forme_diverse_della_stessa_parola(self):
        self.doc("Raccolta", "Elenco dei contratti firmati nel mese")
        self.assertEqual(self.titoli(self.mario, "contratto"), ["Raccolta"])  # stemming italiano

    def test_descrizione_e_nome_del_file(self):
        self.doc("Uno", descrizione="Preventivo per gli arredi")
        self.doc("Due", nome="planimetria_piano_terra.txt")
        self.assertEqual(self.titoli(self.mario, "arredi"), ["Uno"])
        self.assertEqual(self.titoli(self.mario, "planimetria"), ["Due"])

    def test_corrispondenza_parziale_mentre_si_digita(self):
        self.doc("Contratto di fornitura")
        self.assertEqual(self.titoli(self.mario, "contr"), ["Contratto di fornitura"])

    def test_solo_la_versione_corrente(self):
        d = self.doc("Relazione", "testo originale con la parola zafferano")
        add_version(user=self.mario, document=d, uploaded_file=SimpleUploadedFile("nota.txt", b"testo riscritto"))
        processing.elabora_versione(Document.objects.get(pk=d.pk).current_version_id)
        self.assertEqual(self.titoli(self.mario, "zafferano"), [])  # è nella v1, non più corrente
        self.assertEqual(self.titoli(self.mario, "riscritto"), ["Relazione"])


class SintassiTests(RicercaBase):
    def setUp(self):
        self.doc("A", "penale per ritardo nella consegna")
        self.doc("B", "consegna puntuale senza ritardo")

    def test_frase_esatta(self):
        self.assertEqual(self.titoli(self.mario, '"ritardo nella consegna"'), ["A"])

    def test_esclusione(self):
        self.assertEqual(self.titoli(self.mario, "consegna -penale"), ["B"])

    def test_alternative(self):
        self.assertEqual(sorted(self.titoli(self.mario, "penale OR puntuale")), ["A", "B"])

    def test_input_strani_non_causano_errori(self):
        for testo in ["'", '"', "&|!(", ":*", "a" * 5000, "%_\\", "il", "   ", "\x00ciao", "-", "OR OR OR"]:
            with self.subTest(testo=testo):
                self.trova(self.mario, testo)  # nessuna eccezione


class PermessiTests(RicercaBase):
    def test_non_si_trova_cio_che_non_si_vede(self):
        self.doc("Segreto", "codice riservato ametista", owner=self.mario)
        self.assertEqual(self.titoli(self.luisa, "ametista"), [])
        self.assertEqual(self.titoli(self.luisa, "Segreto"), [])
        self.assertEqual(self.titoli(self.mario, "ametista"), ["Segreto"])

    def test_dopo_la_condivisione_compare_dopo_la_revoca_no(self):
        d = self.doc("Condiviso", "parola ametista", owner=self.mario)
        permesso, _ = share_document(
            user=self.mario, document=d, grantee_user=self.luisa, level=DocumentPermission.Level.READ
        )
        self.assertEqual(self.titoli(self.luisa, "ametista"), ["Condiviso"])
        permesso.delete()
        self.assertEqual(self.titoli(self.luisa, "ametista"), [])

    def test_gli_estratti_non_rivelano_documenti_non_visibili(self):
        self.doc("Pubblico", "ametista nel documento visibile", owner=self.luisa)
        self.doc("Privato", "ametista nel documento riservato di mario", owner=self.mario)
        self.client.force_login(self.luisa)
        r = self.client.get(reverse("documents:list"), {"q": "ametista"})
        self.assertContains(r, "documento visibile")
        self.assertNotContains(r, "riservato")
        self.assertNotContains(r, "Privato")


class EstrattiTests(RicercaBase):
    def estratti(self, utente, testo):
        docs = self.trova(utente, testo)
        search.aggiungi_estratti(docs, testo)
        return {d.title: d.estratto for d in docs}

    def test_estratto_con_parola_evidenziata(self):
        self.doc("Lettera", "Gentile cliente, la informiamo che il contratto scade a fine mese.")
        e = self.estratti(self.mario, "contratto")["Lettera"]
        self.assertIn("<mark>contratto</mark>", e)
        self.assertIn("scade a fine mese", e)

    def test_html_nel_documento_non_arriva_alla_pagina(self):
        self.doc("Insidia", "<script>alert(1)</script> <b>contratto</b> & altro")
        e = str(self.estratti(self.mario, "contratto")["Insidia"])
        self.assertNotIn("<script>", e)
        self.assertNotIn("<b>", e)
        self.assertIn("<mark>contratto</mark>", e)
        self.assertIn("&amp; altro", e)

    def test_escape_prima_dell_evidenziazione(self):
        # Anche se PostgreSQL lasciasse passare del markup, viene neutralizzato prima di inserire i <mark>.
        grezzo = f'x <img src=x onerror=alert(1)> {search.INIZIO}contratto{search.FINE} "&'
        html = str(search._html_estratto(grezzo))
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertIn("<mark>contratto</mark>", html)
        self.assertIn("&quot;&amp;", html)

    def test_i_segnaposto_nella_query_non_possono_essere_usati(self):
        self.assertEqual(search.normalizza(f"a{search.INIZIO}b{search.FINE}c\x00d"), "a b c d")

    def test_nessun_estratto_se_trovato_solo_per_titolo(self):
        self.doc("Contratto", "testo senza la parola cercata")
        self.assertIsNone(self.estratti(self.mario, "contratto")["Contratto"])

    def test_estratto_breve_anche_per_testi_enormi(self):
        self.doc("Lungo", ("riempitivo " * 20000) + "ametista " + ("coda " * 100))
        self.assertLess(len(self.estratti(self.mario, "riempitivo")["Lungo"]), 2500)

    def test_oltre_il_limite_si_trova_ma_senza_estratto(self):
        self.doc("Enorme", ("riempitivo " * 20000) + "ametista")  # la parola sta oltre i primi 100.000 caratteri
        self.assertEqual(self.titoli(self.mario, "ametista"), ["Enorme"])
        self.assertIsNone(self.estratti(self.mario, "ametista")["Enorme"])


class ElencoTests(RicercaBase):
    def test_ricerca_dal_vivo_restituisce_il_frammento_con_l_estratto(self):
        self.doc("Fattura 12", "Importo dovuto entro il termine di pagamento")
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:list"), {"q": "pagamento"}, headers={"HX-Request": "true"})
        self.assertContains(r, "Fattura 12")
        self.assertContains(r, "<mark>pagamento</mark>")
        self.assertNotContains(r, "<html")

    def test_senza_ricerca_nessun_estratto(self):
        self.doc("Fattura 12", "Importo dovuto")
        self.client.force_login(self.mario)
        self.assertNotContains(self.client.get(reverse("documents:list")), "<mark>")

    def test_i_filtri_si_combinano_con_la_ricerca(self):
        self.doc("Alfa", "ametista")
        d = self.doc("Beta", "ametista")
        Document.objects.filter(pk=d.pk).update(status=Document.Status.ARCHIVED)
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:list"), {"q": "ametista", "stato": "archived"})
        self.assertContains(r, "Beta")
        self.assertNotContains(r, "Alfa")

    def test_paginazione_con_ricerca(self):
        for i in range(27):
            self.doc(f"Doc {i:02d}", "ametista")
        self.client.force_login(self.mario)
        r = self.client.get(reverse("documents:list"), {"q": "ametista", "page": 2, "per": 25})
        self.assertEqual(len(r.context["pagina"].object_list), 2)
        self.assertContains(r, "q=ametista")

    def test_aiuto_sulla_sintassi_e_visibile(self):
        self.client.force_login(self.mario)
        self.assertContains(self.client.get(reverse("documents:list")), "Frase esatta")
