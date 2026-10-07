from pathlib import Path

from django.core.exceptions import PermissionDenied
from django.urls import reverse

from documents.models import Document, DocumentVersion
from documents.services import VersioneNonValida, add_version, delete_document, restore_version

from .base import DocumentTestCase


class AddVersionTests(DocumentTestCase):
    def setUp(self):
        self.doc = self.nuovo_documento(uploaded_file=self.file(nome="v1.pdf", contenuto=b"%PDF uno"))

    def test_nuova_versione_diventa_corrente_e_la_precedente_resta(self):
        v2 = add_version(
            user=self.mario, document=self.doc, uploaded_file=self.file(nome="v2.pdf", contenuto=b"%PDF due"), comment=" Rivista "
        )
        self.doc.refresh_from_db()
        self.assertEqual(v2.version_number, 2)
        self.assertEqual(self.doc.current_version, v2)
        self.assertEqual(v2.comment, "Rivista")
        self.assertEqual(self.doc.versions.count(), 2)
        v1 = self.doc.versions.get(version_number=1)
        self.assertEqual(v1.file.read(), b"%PDF uno")
        self.assertTrue(Path(v1.file.path).is_file() and Path(v2.file.path).is_file())
        self.assertNotEqual(v1.file.name, v2.file.name)

    def test_numeri_progressivi(self):
        for i in range(3):
            add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=f"c{i}".encode()))
        self.assertEqual(list(self.doc.versions.values_list("version_number", flat=True)), [4, 3, 2, 1])

    def test_file_identico_alla_corrente_rifiutato(self):
        with self.assertRaises(VersioneNonValida):
            add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"%PDF uno"))
        self.assertEqual(self.doc.versions.count(), 1)

    def test_file_identico_a_una_vecchia_versione_e_ammesso(self):
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"due"))
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"%PDF uno"))
        self.assertEqual(self.doc.versions.count(), 3)

    def test_aggiorna_updated_at_del_documento(self):
        prima = Document.objects.get(pk=self.doc.pk).updated_at
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"nuovo"))
        self.assertGreater(Document.objects.get(pk=self.doc.pk).updated_at, prima)

    def test_permessi(self):
        with self.assertRaises(PermissionDenied):
            add_version(user=self.luisa, document=self.doc, uploaded_file=self.file(contenuto=b"x"))
        add_version(user=self.admin, document=self.doc, uploaded_file=self.file(contenuto=b"x"))

    def test_errore_non_lascia_file_orfani(self):
        from unittest import mock

        from django.conf import settings

        def su_disco():
            return {p for p in (Path(settings.MEDIA_ROOT) / "documenti").rglob("*") if p.is_file()}

        prima = su_disco()
        with mock.patch("documents.models.DocumentVersion.save", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"boom"))
        self.assertEqual(su_disco(), prima)
        self.assertEqual(self.doc.versions.count(), 1)


class RestoreVersionTests(DocumentTestCase):
    def setUp(self):
        self.doc = self.nuovo_documento(uploaded_file=self.file(nome="a.pdf", contenuto=b"contenuto-1"))
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(nome="b.pdf", contenuto=b"contenuto-2"))
        self.v1 = self.doc.versions.get(version_number=1)

    def test_ripristino_crea_nuova_versione_con_file_proprio(self):
        nuova = restore_version(user=self.mario, document=self.doc, version=self.v1)
        self.doc.refresh_from_db()
        self.assertEqual(nuova.version_number, 3)
        self.assertEqual(self.doc.current_version, nuova)
        self.assertEqual(nuova.restored_from, self.v1)
        self.assertEqual(nuova.checksum, self.v1.checksum)
        self.assertEqual(nuova.original_name, "a.pdf")
        self.assertEqual(nuova.file.read(), b"contenuto-1")
        self.assertNotEqual(nuova.file.name, self.v1.file.name)
        self.assertEqual(nuova.comment, "")
        self.assertEqual(self.doc.versions.count(), 3)  # niente cancellato

    def test_ripristinare_la_corrente_e_rifiutato(self):
        self.doc.refresh_from_db()
        with self.assertRaises(VersioneNonValida):
            restore_version(user=self.mario, document=self.doc, version=self.doc.current_version)

    def test_permessi_e_versione_di_altro_documento(self):
        with self.assertRaises(PermissionDenied):
            restore_version(user=self.luisa, document=self.doc, version=self.v1)
        altro = self.nuovo_documento(titolo="Altro", uploaded_file=self.file(contenuto=b"zzz"))
        with self.assertRaises(PermissionDenied):
            restore_version(user=self.mario, document=self.doc, version=altro.current_version)

    def test_eliminare_il_documento_rimuove_tutti_i_file(self):
        percorsi = [Path(v.file.path) for v in self.doc.versions.all()]
        with self.captureOnCommitCallbacks(execute=True):
            delete_document(user=self.mario, document=self.doc)
        self.assertEqual(DocumentVersion.objects.count(), 0)
        self.assertFalse(any(p.exists() for p in percorsi))


class VersionViewsTests(DocumentTestCase):
    def setUp(self):
        self.doc = self.nuovo_documento(titolo="Contratto", uploaded_file=self.file(nome="c1.pdf", contenuto=b"uno"))
        self.client.force_login(self.mario)

    def test_dettaglio_mostra_la_cronologia(self):
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(nome="c2.pdf", contenuto=b"due"), comment="Firmata")
        r = self.client.get(self.doc.get_absolute_url())
        self.assertContains(r, "Cronologia versioni")
        self.assertContains(r, "c1.pdf")
        self.assertContains(r, "c2.pdf")
        self.assertContains(r, "Firmata")
        self.assertContains(r, "Corrente", count=1)
        self.assertContains(r, reverse("documents:version_restore", args=[self.doc.pk, 1]))
        self.assertNotContains(r, reverse("documents:version_restore", args=[self.doc.pk, 2]))

    def test_upload_nuova_versione(self):
        url = reverse("documents:version_create", args=[self.doc.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {"file": self.file(nome="c2.pdf", contenuto=b"due"), "comment": "v due"}, follow=True)
        self.assertContains(r, "Caricata la versione 2")
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.current_version.version_number, 2)

    def test_upload_identico_da_422(self):
        r = self.client.post(reverse("documents:version_create", args=[self.doc.pk]), {"file": self.file(contenuto=b"uno")})
        self.assertContains(r, "identico alla versione corrente", status_code=422)

    def test_upload_non_valido(self):
        url = reverse("documents:version_create", args=[self.doc.pk])
        self.assertEqual(self.client.post(url, {"file": self.file(nome="x.exe")}).status_code, 422)
        self.assertEqual(self.client.post(url, {}).status_code, 422)
        self.assertEqual(self.doc.versions.count(), 1)

    def test_download_di_versione_precedente(self):
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(nome="c2.pdf", contenuto=b"due"))
        r = self.client.get(reverse("documents:version_download", args=[self.doc.pk, 1]))
        self.assertEqual(b"".join(r.streaming_content), b"uno")
        self.assertIn("c1.pdf", r["Content-Disposition"])
        self.assertEqual(self.client.get(reverse("documents:version_download", args=[self.doc.pk, 9])).status_code, 404)
        # il download "normale" resta la versione corrente
        r = self.client.get(reverse("documents:download", args=[self.doc.pk]))
        self.assertEqual(b"".join(r.streaming_content), b"due")

    def test_ripristino_con_conferma(self):
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"due"))
        url = reverse("documents:version_restore", args=[self.doc.pk, 1])
        self.assertContains(self.client.get(url), "Ripristinare la versione 1")
        self.assertEqual(self.doc.versions.count(), 2)  # il GET non cambia nulla
        r = self.client.post(url, {"comment": ""}, follow=True)
        self.assertContains(r, "ripristinata come nuova versione 3")
        self.assertEqual(self.doc.versions.count(), 3)

    def test_ripristino_della_corrente_avvisa(self):
        r = self.client.post(reverse("documents:version_restore", args=[self.doc.pk, 1]), {}, follow=True)
        self.assertContains(r, "già la versione corrente")
        self.assertEqual(self.doc.versions.count(), 1)

    def test_elenco_mostra_il_numero_di_versione(self):
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"due"))
        self.assertContains(self.client.get(reverse("documents:list")), "v2 · ")


class VersionIsolationTests(DocumentTestCase):
    def setUp(self):
        self.doc = self.nuovo_documento(owner=self.mario)
        add_version(user=self.mario, document=self.doc, uploaded_file=self.file(contenuto=b"due"))

    def test_estranei_ricevono_404_ovunque(self):
        self.client.force_login(self.luisa)
        for nome, args in [
            ("version_create", [self.doc.pk]),
            ("version_download", [self.doc.pk, 1]),
            ("version_restore", [self.doc.pk, 1]),
        ]:
            with self.subTest(nome):
                self.assertEqual(self.client.get(reverse(f"documents:{nome}", args=args)).status_code, 404)
        self.assertEqual(self.client.post(reverse("documents:version_restore", args=[self.doc.pk, 1])).status_code, 404)
        self.assertEqual(
            self.client.post(reverse("documents:version_create", args=[self.doc.pk]), {"file": self.file(contenuto=b"hack")}).status_code,
            404,
        )
        self.assertEqual(self.doc.versions.count(), 2)

    def test_anonimo_va_al_login(self):
        for nome, args in [("version_create", [self.doc.pk]), ("version_download", [self.doc.pk, 1]), ("version_restore", [self.doc.pk, 1])]:
            r = self.client.get(reverse(f"documents:{nome}", args=args))
            self.assertEqual(r.status_code, 302)
            self.assertIn("login", r["Location"])
