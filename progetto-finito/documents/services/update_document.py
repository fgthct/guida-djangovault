from django.core.exceptions import PermissionDenied
from django.db import transaction

from core.services import audit

from .. import permissions
from ..models import Document
from .workflow import assicura_modificabile


def update_document(*, user, document: Document, title, description="", category=None) -> Document:
    """Modifica i metadati (solo in bozza). Il file non cambia: per quello servono le versioni."""
    if not permissions.can_edit(user, document):
        raise PermissionDenied
    with transaction.atomic():
        document = Document.objects.select_for_update(of=("self",)).select_related("category").get(pk=document.pk)
        assicura_modificabile(document)
        nuovi = {"titolo": title.strip(), "descrizione": description.strip(), "categoria": category}
        vecchi = {"titolo": document.title, "descrizione": document.description, "categoria": document.category}
        modifiche = {
            k: [str(vecchi[k] or "—"), str(nuovi[k] or "—")] for k in nuovi if vecchi[k] != nuovi[k]
        }
        document.title = nuovi["titolo"]
        document.description = nuovi["descrizione"]
        document.category = category
        document.save(update_fields=["title", "description", "category", "updated_at"])
        if modifiche:
            audit.record("document.updated", user=user, obj=document, modifiche=modifiche)
    return document
