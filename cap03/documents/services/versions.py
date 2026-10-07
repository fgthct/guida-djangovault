import logging

from django.core.exceptions import PermissionDenied
from django.core.files import File
from django.db import transaction
from django.db.models import Max

from core.services import audit

from .. import permissions
from ..models import Document, DocumentVersion
from . import processing
from .files import sha256_file, tipo_contenuto
from .workflow import assicura_modificabile

logger = logging.getLogger(__name__)


class VersioneNonValida(ValueError):
    """Operazione sulle versioni rifiutata per una regola di dominio (messaggio mostrabile all'utente)."""


def salva_nuova_versione(
    *, document: Document, user, uploaded, checksum: str, comment: str = "", restored_from=None
) -> DocumentVersion:
    """Scrive il file e crea la versione successiva. Da chiamare dentro `transaction.atomic()`.

    Il numero è max+1 calcolato con il documento già bloccato dal chiamante (select_for_update):
    due caricamenti simultanei non possono ottenere lo stesso numero. Se qualcosa fallisce
    il file appena scritto viene rimosso.
    """
    numero = (document.versions.aggregate(m=Max("version_number"))["m"] or 0) + 1
    version = DocumentVersion(
        document=document,
        version_number=numero,
        original_name=uploaded.name[:255],
        content_type=tipo_contenuto(uploaded),
        size=uploaded.size,
        checksum=checksum,
        created_by=user,
        comment=comment.strip()[:255],
        restored_from=restored_from,
    )
    version.file.save(uploaded.name, uploaded, save=False)
    try:
        version.save()
        document.current_version = version
        document.save(update_fields=["current_version", "updated_at"])
        processing.accoda(version.pk)  # estrazione del testo, OCR e anteprima: in background, dopo il commit
    except Exception:
        version.file.storage.delete(version.file.name)
        raise
    return version


def add_version(*, user, document: Document, uploaded_file, comment: str = "") -> DocumentVersion:
    """Carica una nuova versione del file. Quelle precedenti restano intatte."""
    if not permissions.can_add_version(user, document):
        raise PermissionDenied
    checksum = sha256_file(uploaded_file)
    with transaction.atomic():
        document = Document.objects.select_for_update().get(pk=document.pk)
        assicura_modificabile(document)
        corrente = document.current_version
        if corrente and corrente.checksum == checksum:
            raise VersioneNonValida("Il file è identico alla versione corrente: non è cambiato nulla.")
        version = salva_nuova_versione(
            document=document, user=user, uploaded=uploaded_file, checksum=checksum, comment=comment
        )
        audit.record(
            "version.added", user=user, obj=document, versione=version.version_number,
            file=version.original_name, nota=version.comment,
        )
    logger.info("Documento %s: nuova versione %s da utente %s", document.pk, version.version_number, user.pk)
    return version


def restore_version(*, user, document: Document, version: DocumentVersion, comment: str = "") -> DocumentVersion:
    """Ripristina una versione precedente creandone una NUOVA con lo stesso contenuto.

    La cronologia non si riscrive mai. Il file viene copiato, così ogni versione possiede il suo.
    """
    if not permissions.can_restore_version(user, document):
        raise PermissionDenied
    if version.document_id != document.pk:
        raise PermissionDenied
    with transaction.atomic():
        document = Document.objects.select_for_update().get(pk=document.pk)
        assicura_modificabile(document)
        if document.current_version_id == version.pk:
            raise VersioneNonValida("Questa è già la versione corrente.")
        with version.file.open("rb") as sorgente:
            copia = File(sorgente, name=version.original_name)
            copia.size = version.size
            nuova = salva_nuova_versione(
                document=document,
                user=user,
                uploaded=copia,
                checksum=version.checksum,
                comment=comment,  # l'origine è già in restored_from
                restored_from=version,
            )
        audit.record(
            "version.restored", user=user, obj=document, versione=nuova.version_number,
            origine=version.version_number, file=nuova.original_name, nota=nuova.comment,
        )
    logger.info(
        "Documento %s: ripristinata v%s come v%s da utente %s",
        document.pk, version.version_number, nuova.version_number, user.pk,
    )
    return nuova
