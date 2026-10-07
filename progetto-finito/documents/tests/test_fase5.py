from django.test import override_settings

from documents.models import Document
from documents.services import simili

from .test_classificazione import CUCINA, FAKE, PYTHON, VARIE, ClassificazioneBase

DIRITTO_TESTO = "contratto di locazione tra le parti, canone mensile e durata"


@override_settings(**FAKE)
class DocumentiSimiliTests(ClassificazioneBase):
    def test_i_simili_sono_dello_stesso_argomento(self):
        self.archivio_di_base()
        nuovo = self.doc("Nuovo python", f"{PYTHON} {VARIE[4]}")
        nuovo = Document.objects.select_related("current_version").get(pk=nuovo.pk)
        risultato = simili.documenti_simili(self.mario, nuovo)
        self.assertTrue(risultato)
        self.assertTrue(risultato[0]["titolo"].startswith("Python"))
        self.assertNotIn(nuovo.pk, [r["id"] for r in risultato])

    def test_non_mostra_documenti_non_visibili(self):
        a = self.doc("Mio", PYTHON, owner=self.mario)
        self.doc("Privato di luisa", PYTHON, owner=self.luisa)
        self.assertEqual(simili.documenti_simili(self.mario, a), [])

    def test_documento_senza_vettore_non_ha_simili(self):
        a = self.doc("A", PYTHON)
        a.current_version.__class__.objects.filter(pk=a.current_version_id).update(doc_vector=None)
        a.refresh_from_db()
        self.assertEqual(simili.documenti_simili(self.mario, a), [])

    def test_la_pagina_del_documento_li_mostra(self):
        self.archivio_di_base()
        nuovo = self.doc("Nuovo python", f"{PYTHON} {VARIE[4]}")
        self.client.force_login(self.mario)
        r = self.client.get(f"/documenti/{nuovo.pk}/")
        self.assertContains(r, "data-simili")
        self.assertContains(r, "Documenti simili")


@override_settings(**FAKE)
class RicercaRapidaTests(ClassificazioneBase):
    def test_trova_per_parole_e_per_titolo(self):
        self.doc("Contratto affitto", DIRITTO_TESTO)
        self.client.force_login(self.mario)
        r = self.client.get("/documenti/rapida/?q=contr")
        self.assertEqual([x["titolo"] for x in r.json()["risultati"]], ["Contratto affitto"])

    def test_senza_testo_mostra_gli_ultimi(self):
        self.doc("Uno", PYTHON)
        self.doc("Due", CUCINA)
        self.client.force_login(self.mario)
        titoli = [x["titolo"] for x in self.client.get("/documenti/rapida/").json()["risultati"]]
        self.assertEqual(titoli[0], "Due")

    def test_trova_anche_per_significato(self):
        # il titolo non contiene «affitti», il testo parla di «locazione»: lo trova solo il significato
        self.doc("Carte di Giulia", DIRITTO_TESTO)
        self.client.force_login(self.mario)
        r = self.client.get("/documenti/rapida/?q=affitti+contratto+parti+canone+mensile+durata")
        self.assertEqual([x["titolo"] for x in r.json()["risultati"]], ["Carte di Giulia"])

    def test_significato_non_disponibile_ripiega_sulle_parole(self):
        from unittest import mock

        self.doc("Contratto affitto", DIRITTO_TESTO)
        self.client.force_login(self.mario)
        with mock.patch("documents.services.search.cerca_significato", side_effect=RuntimeError("niente modello")):
            r = self.client.get("/documenti/rapida/?q=contr")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["titolo"] for x in r.json()["risultati"]], ["Contratto affitto"])

    def test_non_supera_otto_risultati(self):
        for i in range(10):
            self.doc(f"Locazione {i}", DIRITTO_TESTO)
        self.client.force_login(self.mario)
        self.assertEqual(len(self.client.get("/documenti/rapida/?q=locazione").json()["risultati"]), 8)

    def test_significato_non_rivela_documenti_altrui(self):
        self.doc("Carte di luisa", DIRITTO_TESTO, owner=self.luisa)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get("/documenti/rapida/?q=affitti+contratto+parti+canone+mensile+durata").json()["risultati"], [])

    def test_non_rivela_documenti_altrui(self):
        self.doc("Segreto di luisa", PYTHON, owner=self.luisa)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get("/documenti/rapida/?q=segreto").json()["risultati"], [])

    def test_richiede_il_login(self):
        self.assertEqual(self.client.get("/documenti/rapida/?q=a").status_code, 302)

    def test_la_palette_c_e_nelle_pagine_dell_app(self):
        self.client.force_login(self.mario)
        self.assertContains(self.client.get("/documenti/"), 'id="ricerca-rapida"')


@override_settings(**FAKE)
class AzioneMultiplaTests(ClassificazioneBase):
    URL = "/documenti/azione/"

    def test_sposta_in_una_categoria(self):
        a, b = self.doc("A", PYTHON), self.doc("B", CUCINA)
        self.client.force_login(self.mario)
        r = self.client.post(self.URL, {"azione": "categoria", "categoria": self.cucina.pk, "scelti": [a.pk, b.pk]})
        self.assertEqual(r.status_code, 302)
        a.refresh_from_db(), b.refresh_from_db()
        self.assertEqual((a.category, b.category), (self.cucina, self.cucina))

    def test_senza_categoria(self):
        a = self.doc("A", PYTHON, categoria=self.informatica)
        self.client.force_login(self.mario)
        self.client.post(self.URL, {"azione": "categoria", "categoria": "nessuna", "scelti": [a.pk]})
        a.refresh_from_db()
        self.assertIsNone(a.category)

    def test_ignora_documenti_non_visibili(self):
        altrui = self.doc("Di luisa", PYTHON, owner=self.luisa)
        self.client.force_login(self.mario)
        self.client.post(self.URL, {"azione": "categoria", "categoria": self.cucina.pk, "scelti": [altrui.pk]})
        altrui.refresh_from_db()
        self.assertIsNone(altrui.category)

    def test_documenti_non_in_bozza_sono_saltati(self):
        a = self.doc("A", PYTHON)
        Document.objects.filter(pk=a.pk).update(status=Document.Status.APPROVED)
        self.client.force_login(self.mario)
        self.client.post(self.URL, {"azione": "categoria", "categoria": self.cucina.pk, "scelti": [a.pk]})
        a.refresh_from_db()
        self.assertIsNone(a.category)

    def test_zip_con_i_file_scelti(self):
        import io
        import zipfile

        a, b = self.doc("A", PYTHON), self.doc("B", CUCINA)
        self.client.force_login(self.mario)
        r = self.client.post(self.URL, {"azione": "zip", "scelti": [a.pk, b.pk]})
        self.assertEqual(r["Content-Type"], "application/zip")
        nomi = zipfile.ZipFile(io.BytesIO(b"".join(r.streaming_content))).namelist()
        self.assertEqual(sorted(nomi), sorted([f"{a.pk}-n.txt", f"{b.pk}-n.txt"]))

    def test_solo_post_e_login(self):
        self.assertEqual(self.client.post(self.URL, {}).status_code, 302)
        self.client.force_login(self.mario)
        self.assertEqual(self.client.get(self.URL).status_code, 405)

    def test_ritorno_esterno_non_viene_seguito(self):
        self.client.force_login(self.mario)
        r = self.client.post(self.URL, {"azione": "zip", "ritorno": "https://evil.example/", "scelti": []})
        self.assertEqual(r["Location"], "/documenti/")

    def test_l_elenco_ha_le_caselle(self):
        self.doc("A", PYTHON)
        self.client.force_login(self.mario)
        self.assertContains(self.client.get("/documenti/"), 'name="scelti"')


@override_settings(**FAKE)
class RicercheSalvateTests(ClassificazioneBase):
    def test_salva_e_riapre(self):
        from documents.models import SavedSearch

        self.client.force_login(self.mario)
        self.client.post("/documenti/ricerche/salva/", {"nome": "Bollette", "filtri": "?q=luce&categoria=3&page=4&x=1"})
        r = SavedSearch.objects.get(owner=self.mario)
        self.assertEqual(r.name, "Bollette")
        self.assertEqual(sorted(r.query.split("&")), ["categoria=3", "q=luce"])  # niente pagina, niente parametri ignoti
        self.assertContains(self.client.get("/documenti/"), "Bollette")

    def test_senza_filtri_non_salva(self):
        from documents.models import SavedSearch

        self.client.force_login(self.mario)
        self.client.post("/documenti/ricerche/salva/", {"nome": "Vuota", "filtri": "?page=2"})
        self.assertFalse(SavedSearch.objects.exists())

    def test_stesso_nome_aggiorna(self):
        from documents.models import SavedSearch

        self.client.force_login(self.mario)
        self.client.post("/documenti/ricerche/salva/", {"nome": "X", "filtri": "?q=a"})
        self.client.post("/documenti/ricerche/salva/", {"nome": "x", "filtri": "?q=b"})
        self.assertEqual(SavedSearch.objects.get().query, "q=b")

    def test_massimo_venti(self):
        from documents.models import SavedSearch

        self.client.force_login(self.mario)
        for i in range(21):
            self.client.post("/documenti/ricerche/salva/", {"nome": f"R{i}", "filtri": f"?q={i}"})
        self.assertEqual(SavedSearch.objects.count(), 20)

    def test_sono_personali(self):
        from documents.models import SavedSearch

        r = SavedSearch.objects.create(owner=self.luisa, name="Sua", query="q=a")
        self.client.force_login(self.mario)
        self.assertNotContains(self.client.get("/documenti/"), "Sua")
        self.assertEqual(self.client.post(f"/documenti/ricerche/{r.pk}/elimina/").status_code, 404)
        self.assertTrue(SavedSearch.objects.filter(pk=r.pk).exists())

    def test_elimina_la_propria(self):
        from documents.models import SavedSearch

        r = SavedSearch.objects.create(owner=self.mario, name="Mia", query="q=a")
        self.client.force_login(self.mario)
        self.client.post(f"/documenti/ricerche/{r.pk}/elimina/")
        self.assertFalse(SavedSearch.objects.exists())
