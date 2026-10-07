import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import User

MEDIA_TEST = tempfile.mkdtemp(prefix="dv-test-media-")


@override_settings(MEDIA_ROOT=MEDIA_TEST)
class DocumentTestCase(TestCase):
    """Base dei test dei documenti: media in una cartella temporanea."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_TEST, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.mario = User.objects.create_user("mario", password="password-lunga-123")
        cls.luisa = User.objects.create_user("luisa", password="password-lunga-123")
        cls.admin = User.objects.create_superuser("admin", password="password-lunga-123")
        cls.staff = User.objects.create_user("staff", password="password-lunga-123", is_staff=True)

    @staticmethod
    def file(nome="relazione.pdf", contenuto=b"%PDF-1.4 contenuto di prova"):
        return SimpleUploadedFile(nome, contenuto, content_type="application/pdf")

    def nuovo_documento(self, owner=None, titolo="Relazione", **kw):
        from documents.services import create_document

        return create_document(
            owner=owner or self.mario, title=titolo, uploaded_file=kw.pop("uploaded_file", self.file()), **kw
        ).document
