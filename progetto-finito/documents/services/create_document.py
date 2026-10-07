import logging
from dataclasses import dataclass, field

from django.db import transaction

from core.services import audit

from ..models import Document
from .files import sha256_file
from .versions import salva_nuova_versione

logger = logging.getLogger(__name__)


@dataclass
class CreateResult:
    document: Document
    # Documenti già esistenti (visibili all'utente) con lo stesso contenuto: solo un avviso.
    duplicates: list[Document] = field(default_factory=list)


def create_document(*, owner, title, description="", category=None, uploaded_file, ai_allowed=False) -> CreateResult:
    """Crea il documento e la sua versione 1 in un'unica transazione.

    Se qualcosa fallisce il file appena scritto viene rimosso: niente file orfani.
    """
    checksum = sha256_file(uploaded_file)
    duplicates = list(
        Document.objects.visible_to(owner)
        .filter(versions__checksum=checksum)
        .distinct()
        .order_by("-updated_at")[:5]
    )
    with transaction.atomic():
        document = Document.objects.create(
            title=title.strip(), description=description.strip(), category=category, owner=owner,
            ai_allowed=ai_allowed,
        )
        version = salva_nuova_versione(
            document=document, user=owner, uploaded=uploaded_file, checksum=checksum
        )
        audit.record(
            "document.created", user=owner, obj=document, file=version.original_name,
            byte=version.size, checksum=checksum, ai=ai_allowed,
        )
    logger.info("Documento %s creato da %s (v1, %s byte)", document.pk, owner.pk, version.size)
    return CreateResult(document=document, duplicates=duplicates)
