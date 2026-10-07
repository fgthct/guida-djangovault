from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from documents.services import indexing, processing
from documents.tests.base import DocumentTestCase

from .fakes import FakeLLM


@override_settings(
    AI_BACKEND="assistant.tests.fakes.FakeLLM",
    EMBEDDING_BACKEND="documents.tests.fakes.FakeEmbedder",
    ANTHROPIC_API_KEY="chiave-di-prova",
    AI_MIN_SCORE=0.3,
)
class AssistenteBase(DocumentTestCase):
    def setUp(self):
        FakeLLM.reset()

    def doc(self, titolo, testo, owner=None, consenso=True):
        d = self.nuovo_documento(
            owner=owner, titolo=titolo, uploaded_file=SimpleUploadedFile("nota.txt", testo.encode()), ai_allowed=consenso
        )
        processing.elabora_versione(d.current_version_id)
        indexing.indicizza_versione(d.current_version_id)
        return d

    def chiamate(self, tipo):
        return [c for c in FakeLLM.chiamate if c["tipo"] == tipo]
