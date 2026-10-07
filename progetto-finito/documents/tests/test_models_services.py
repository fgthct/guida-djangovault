import hashlib
from pathlib import Path
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from documents.models import Category, Document
from documents.services import create_document, delete_document, update_document

from .base import DocumentTestCase


class CategoryTests(DocumentTestCase):
    def test_nome_unico_senza_distinguere_maiuscole(self):
        Category.objects.create(name="Fatture")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Category.objects.create(name="fatture")

    def test_categoria_in_uso_non_si_cancella(self):
        c = Category.objects.create(name="Contratti")
        self.nuovo_documento(category=c)
        with self.assertRaises(ProtectedError):
            c.delete()


class CreateDocumentTests(DocumentTestCase):
    def test_crea_documento_e_versione_1(self):
        contenuto = b"%PDF-1.4 ciao"
        doc = self.nuovo_documento(uploaded_file=self.file(contenuto=contenuto))
        v = doc.current_version
        self.assertEqual(doc.versions.count(), 1)
        self.assertEqual(v.version_number, 1)
        self.assertEqual(v.checksum, hashlib.sha256(contenuto).hexdigest())
        self.assertEqual(v.size, len(contenuto))
        self.assertEqual(v.original_name, "relazione.pdf")
        self.assertEqual(v.created_by, self.mario)
        self.assertEqual(doc.status, Document.Status.DRAFT)

    def test_il_file_e_salvato_con_nome_casuale(self):
        v = self.nuovo_documento().current_version
        self.assertNotIn("relazione", v.file.name)
        self.assertTrue(v.file.name.endswith(".pdf"))
        self.assertTrue(Path(v.file.path).is_file())

    def test_nome_file_pericoloso_non_esce_dalla_cartella(self):
        v = self.nuovo_documento(uploaded_file=self.file(nome="../../etc/passwd.pdf")).current_version
        self.assertTrue(v.file.name.startswith("documenti/"))
        self.assertNotIn("..", v.file.name)

    def test_duplicato_segnalato_ma_non_bloccato(self):
        primo = self.nuovo_documento(titolo="Uno")
        risultato = create_document(owner=self.mario, title="Due", uploaded_file=self.file())
        self.assertEqual(risultato.duplicates, [primo])
        self.assertEqual(Document.objects.count(), 2)

    def test_duplicato_di_altri_utenti_non_viene_rivelato(self):
        self.nuovo_documento(owner=self.luisa, titolo="Di Luisa")
        risultato = create_document(owner=self.mario, title="Mio", uploaded_file=self.file())
        self.assertEqual(risultato.duplicates, [])

    def test_errore_non_lascia_ne_record_ne_file(self):
        from django.conf import settings

        def file_su_disco():
            return {p for p in (Path(settings.MEDIA_ROOT) / "documenti").rglob("*") if p.is_file()}

        prima = file_su_disco()
        with mock.patch("documents.models.DocumentVersion.save", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                create_document(owner=self.mario, title="X", uploaded_file=self.file(contenuto=b"unico-errore"))
        self.assertEqual(Document.objects.count(), 0)
        self.assertEqual(file_su_disco(), prima)


class UpdateDeleteTests(DocumentTestCase):
    def test_modifica_metadati(self):
        doc = self.nuovo_documento()
        c = Category.objects.create(name="Legale")
        update_document(user=self.mario, document=doc, title="  Nuovo titolo ", description="d", category=c)
        doc.refresh_from_db()
        self.assertEqual((doc.title, doc.description, doc.category), ("Nuovo titolo", "d", c))

    def test_modifica_negata_a_estranei(self):
        doc = self.nuovo_documento()
        with self.assertRaises(PermissionDenied):
            update_document(user=self.luisa, document=doc, title="x")

    def test_superutente_puo_modificare(self):
        doc = self.nuovo_documento()
        update_document(user=self.admin, document=doc, title="dall'admin")

    def test_eliminazione_rimuove_record_e_file(self):
        doc = self.nuovo_documento()
        percorso = Path(doc.current_version.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            delete_document(user=self.mario, document=doc)
        self.assertFalse(Document.objects.filter(pk=doc.pk).exists())
        self.assertFalse(percorso.exists())

    def test_eliminazione_negata_a_estranei(self):
        doc = self.nuovo_documento()
        with self.assertRaises(PermissionDenied):
            delete_document(user=self.luisa, document=doc)
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())


class VisibilitaTests(DocumentTestCase):
    def test_visible_to(self):
        mio = self.nuovo_documento(owner=self.mario)
        altrui = self.nuovo_documento(owner=self.luisa, titolo="Altro")
        self.assertEqual(list(Document.objects.visible_to(self.mario)), [mio])
        self.assertEqual(set(Document.objects.visible_to(self.admin)), {mio, altrui})

    def test_anonimo_non_vede_nulla(self):
        from django.contrib.auth.models import AnonymousUser

        self.nuovo_documento()
        self.assertEqual(Document.objects.visible_to(AnonymousUser()).count(), 0)
