import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction

from core.services import audit

from .. import permissions
from ..models import Document

logger = logging.getLogger(__name__)


def delete_document(*, user, document: Document) -> None:
    """Elimina il documento, le versioni e (dopo il commit) i file su disco."""
    if not permissions.can_delete(user, document):
        raise PermissionDenied
    with transaction.atomic():
        pk = document.pk
        files = [(v.file.storage, v.file.name) for v in document.versions.all() if v.file.name]
        files += [(v.preview.storage, v.preview.name) for v in document.versions.all() if v.preview]
        audit.record("document.deleted", user=user, obj=document, versioni=len(files))
        # current_version è SET_NULL, le versioni sono in CASCADE.
        document.delete()

        def _rimuovi_file():
            for storage, nome in files:
                try:
                    storage.delete(nome)
                except Exception:
                    logger.exception("File non rimosso: %s", nome)

        # Se la transazione fallisce i file restano: mai un record senza file.
        transaction.on_commit(_rimuovi_file)
    logger.info("Documento %s eliminato da %s", pk, user.pk)
