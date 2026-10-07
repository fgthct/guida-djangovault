import logging

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.db import transaction

from core.services import audit
from core.services.notifications import notify

from .. import permissions
from ..models import Document, DocumentPermission

logger = logging.getLogger(__name__)


class CondivisioneNonValida(ValueError):
    """Regola di dominio violata (messaggio mostrabile all'utente)."""


def share_document(
    *, user, document: Document, level: int, grantee_user=None, grantee_group: Group | None = None
) -> tuple[DocumentPermission, bool]:
    """Condivide il documento con un utente o un gruppo. Se esiste già, aggiorna il livello.

    Registro e notifica partono solo se l'accesso è nuovo o cambia davvero: riconfermare lo stesso
    accesso non fa nulla. Ritorna (condivisione, creata).
    """
    if not permissions.can_share(user, document):
        raise PermissionDenied
    if (grantee_user is None) == (grantee_group is None):
        raise CondivisioneNonValida("Indica un utente oppure un gruppo.")
    if level not in DocumentPermission.Level.values:
        raise CondivisioneNonValida("Livello non valido.")
    if grantee_user is not None:
        if grantee_user.pk == document.owner_id:
            raise CondivisioneNonValida("Il proprietario ha già accesso completo.")
        if not grantee_user.is_active:
            raise CondivisioneNonValida("L'utente non è attivo.")
        chiave = {"user": grantee_user}
    else:
        chiave = {"group": grantee_group}
    with transaction.atomic():
        esistente = DocumentPermission.objects.filter(document=document, **chiave).first()
        modificata = esistente is not None and esistente.level != level
        permesso, creata = DocumentPermission.objects.update_or_create(
            document=document, **chiave, defaults={"level": level, "granted_by": user}
        )
        if creata or modificata:
            audit.record(
                "share.granted" if creata else "share.updated", user=user, obj=document,
                destinatario=str(permesso.grantee), livello=permesso.get_level_display(),
            )
            from django.contrib.auth import get_user_model

            chi = user.get_full_name() or user.get_username()
            accesso = permesso.get_level_display().lower()
            if creata:
                testo = f"{chi} ha condiviso con te «{document.title}» ({accesso})."
            else:
                testo = f"{chi} ha modificato il tuo accesso a «{document.title}»: {accesso}."
            riceventi = [grantee_user] if grantee_user else get_user_model().objects.filter(groups=grantee_group)
            notify(
                riceventi,
                kind="document.shared",
                message=testo,
                url=document.get_absolute_url(),
                exclude=user,
            )
    logger.info(
        "Documento %s: %s %s → livello %s (da utente %s)",
        document.pk, "concesso a" if creata else "aggiornato per", permesso.grantee, level, user.pk,
    )
    return permesso, creata


def revoke_share(*, user, document: Document, permission: DocumentPermission) -> None:
    """Revoca una condivisione."""
    if not permissions.can_share(user, document):
        raise PermissionDenied
    if permission.document_id != document.pk:
        raise PermissionDenied
    destinatario = permission.grantee
    with transaction.atomic():
        audit.record("share.revoked", user=user, obj=document, destinatario=str(destinatario))
        permission.delete()
    logger.info("Documento %s: revocato l'accesso a %s (da utente %s)", document.pk, destinatario, user.pk)
