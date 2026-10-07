"""Registro delle operazioni (audit).

`record()` si chiama dai servizi, dentro la stessa transazione dell'operazione: se questa
viene annullata, sparisce anche la riga di registro. L'indirizzo IP arriva dal middleware
tramite una context variable, così i servizi non devono conoscere la richiesta.
"""

from __future__ import annotations

import ipaddress
from contextvars import ContextVar

from ..models import AuditLog

_ip_corrente: ContextVar[str | None] = ContextVar("audit_ip", default=None)

ETICHETTE = {
    "auth.login": "Accesso",
    "auth.logout": "Uscita",
    "auth.login_failed": "Accesso fallito",
    "document.created": "Documento creato",
    "document.updated": "Documento modificato",
    "document.deleted": "Documento eliminato",
    "document.ai_consent_changed": "Consenso IA modificato",
    "ai.question": "Domanda all'assistente IA",
    "document.downloaded": "Download",
    "document.opened": "Apertura",
    "document.classified": "Classificazione",
    "document.classification_rejected": "Proposta di categoria rifiutata",
    "document.classification_undone": "Classificazione annullata",
    "document.status_changed": "Cambio di stato",
    "version.added": "Nuova versione",
    "version.restored": "Versione ripristinata",
    "version.reprocess_requested": "Rielaborazione richiesta",
    "version.processing_failed": "Elaborazione fallita",
    "version.reindex_requested": "Indicizzazione richiesta",
    "review.started": "Revisione avviata",
    "review.approved": "Revisione: approvato",
    "review.rejected": "Revisione: rimandato",
    "review.withdrawn": "Revisione ritirata",
    "share.granted": "Condivisione",
    "share.updated": "Condivisione modificata",
    "share.revoked": "Condivisione revocata",
}

STATI = {"draft": "Bozza", "in_review": "In revisione", "approved": "Approvato", "archived": "Archiviato"}


def imposta_ip(ip: str | None) -> None:
    _ip_corrente.set(ip)


def ip_valido(valore: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((valore or "").strip()))
    except ValueError:
        return None


def record(action: str, *, user=None, obj=None, username: str = "", **metadata) -> AuditLog:
    return AuditLog.objects.create(
        user=user if getattr(user, "pk", None) else None,
        username=(getattr(user, "get_username", lambda: "")() if user else username)[:150],
        action=action,
        object_type=obj._meta.model_name if obj is not None else "",
        object_id=obj.pk if obj is not None else None,
        object_repr=str(obj)[:200] if obj is not None else "",
        ip_address=_ip_corrente.get(),
        metadata=metadata,
    )


def descrivi(voce: AuditLog) -> str:
    """Testo leggibile dei dettagli di una voce."""
    m = voce.metadata or {}
    a = voce.action
    if a == "document.status_changed":
        testo = f"{STATI.get(m.get('da'), m.get('da'))} → {STATI.get(m.get('a'), m.get('a'))}"
        return f"{testo}. {m['nota']}" if m.get("nota") else testo
    if a in ("version.added", "version.restored"):
        testo = f"v{m.get('versione')}"
        if m.get("origine"):
            testo += f" (copia della v{m['origine']})"
        if m.get("file"):
            testo += f" · {m['file']}"
        return f"{testo}. {m['nota']}" if m.get("nota") else testo
    if a in ("document.downloaded", "document.opened"):
        return f"v{m.get('versione')} · {m.get('file', '')}"
    if a == "document.created":
        return f"{m.get('file', '')} ({m.get('byte', 0)} byte)"
    if a == "document.updated":
        return ", ".join(f"{k}: «{v[0]}» → «{v[1]}»" for k, v in (m.get("modifiche") or {}).items())
    if a in ("share.granted", "share.updated"):
        testo = f"{m.get('destinatario')} · {m.get('livello')}"
        return testo
    if a == "review.started":
        modo = "in sequenza" if m.get("sequenziale") else "libera"
        testo = f"Giro {m.get('giro')} ({modo}): {', '.join(m.get('revisori', []))}"
        return f"{testo}. Messaggio: {m['messaggio']}" if m.get("messaggio") else testo
    if a in ("review.approved", "review.rejected", "review.withdrawn"):
        testo = f"Giro {m.get('giro')}" + (f" · {m['ruolo']}" if m.get("ruolo") else "")
        return f"{testo}. {m['nota']}" if m.get("nota") else testo
    if a == "document.classified":
        origine = "in automatico" if m.get("automatica") else "da una persona"
        return f"→ {m.get('categoria')} ({m.get('fonte') or '—'}, fiducia {m.get('fiducia', 0)}%, {origine})"
    if a == "document.ai_consent_changed":
        return "consentito" if m.get("consentito") else "revocato"
    if a == "ai.question":
        titoli = ", ".join(m.get("documenti") or []) or "nessun documento"
        return f"{m.get('modello', '')} · {m.get('token_in', 0)}+{m.get('token_out', 0)} token · fonti: {titoli}"
    if a == "share.revoked":
        return str(m.get("destinatario", ""))
    if a == "auth.login_failed":
        return "Credenziali errate"
    return ""
