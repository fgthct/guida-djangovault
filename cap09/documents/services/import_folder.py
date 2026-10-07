"""Importazione in blocco dei file di una cartella (per portare dentro libri, guide, capitoli…).

Ogni file diventa un documento (stesso percorso del caricamento da browser: `create_document`), quindi estrazione del
testo, OCR e indicizzazione partono da sole in background. Regole:
- stesso contenuto già presente (SHA-256) → non si crea un doppione;
- estensione non ammessa, file vuoto o oltre il limite → saltato, con il motivo;
- la categoria può essere una sola per tutti oppure il nome della prima sottocartella;
- i file nascosti e i temporanei di Office (`~$…`) si ignorano.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from django import forms
from django.core.files import File

from ..forms import valida_file
from ..models import Category, Document
from .create_document import create_document
from .files import sha256_file

logger = logging.getLogger(__name__)

IMPORTATO, PRESENTE, SALTATO, ERRORE = "importato", "presente", "saltato", "errore"
IMMAGINI = {"png", "jpg", "jpeg", "gif", "webp"}  # niente testo da cercare: si possono escludere con `senza_immagini`


@dataclass
class Esito:
    percorso: str  # relativo alla cartella importata
    stato: str
    dettaglio: str = ""
    document_id: int | None = None


@dataclass
class Riepilogo:
    esiti: list[Esito] = field(default_factory=list)

    def conta(self, stato: str) -> int:
        return sum(1 for e in self.esiti if e.stato == stato)


def titolo_da_nome(nome: str) -> str:
    """«Guida_Django-6.1_finale.pdf» → «Guida Django-6.1 finale»."""
    base = Path(nome).stem
    base = re.sub(r"[_\s]+", " ", base).strip()
    return (base or nome)[:200]


def _da_ignorare(p: Path, radice: Path) -> bool:
    parti = p.relative_to(radice).parts
    return any(x.startswith(".") or x.startswith("~$") for x in parti) or p.name.lower() in {"thumbs.db", "desktop.ini"}


def trova_file(radice: Path) -> list[Path]:
    return sorted(p for p in radice.rglob("*") if p.is_file() and not _da_ignorare(p, radice))


def _categoria(nome: str, cache: dict[str, Category]) -> Category:
    chiave = nome.strip().lower()
    if chiave not in cache:
        cat = Category.objects.filter(name__iexact=nome.strip()).first() or Category.objects.create(name=nome.strip()[:80])
        cache[chiave] = cat
    return cache[chiave]


def importa_cartella(
    radice: Path,
    *,
    owner,
    categoria: str | None = None,
    per_sottocartelle: bool = False,
    ai_allowed: bool = True,
    rimuovi: bool = False,
    prova: bool = False,
    senza_immagini: bool = False,
    classifica: bool = False,
    esclusi: tuple[str, ...] = (),
    piu_vecchi_di: float = 0,
) -> Riepilogo:
    radice = Path(radice)
    if not radice.is_dir():
        raise NotADirectoryError(str(radice))
    cache: dict[str, Category] = {}
    riepilogo = Riepilogo()
    adesso = time.time()
    for p in trova_file(radice):
        rel = p.relative_to(radice)
        if len(rel.parts) > 1 and rel.parts[0] in esclusi:
            continue  # sottocartelle di servizio (es. «_scartati»): non si toccano
        if piu_vecchi_di and adesso - p.stat().st_mtime < piu_vecchi_di:
            continue  # ancora in copia (o appena salvato): lo si riprende al prossimo giro
        if senza_immagini and p.suffix.lower().lstrip(".") in IMMAGINI:
            riepilogo.esiti.append(Esito(str(rel), SALTATO, "immagine (esclusa)"))
            continue
        esito = _importa_file(
            p, str(rel), owner=owner, ai_allowed=ai_allowed, prova=prova, cache=cache, classifica=classifica,
            categoria=categoria if categoria else (rel.parts[0] if per_sottocartelle and len(rel.parts) > 1 else None),
        )
        # Si toglie dalla cartella solo ciò che è davvero dentro (importato o già presente): mai un file saltato o fallito.
        if rimuovi and not prova and esito.stato in (IMPORTATO, PRESENTE):
            try:
                p.unlink()
            except OSError:
                logger.warning("Impossibile rimuovere %s", p, exc_info=True)
        riepilogo.esiti.append(esito)
    if rimuovi and not prova:
        _togli_cartelle_vuote(radice)
    return riepilogo


def _togli_cartelle_vuote(radice: Path) -> None:
    for d in sorted((p for p in radice.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # riesce solo se è vuota
        except OSError:
            pass


def _importa_file(p: Path, rel: str, *, owner, categoria, ai_allowed, prova, cache, classifica=False) -> Esito:
    try:
        with open(p, "rb") as fh:
            f = File(fh, name=p.name)
            try:
                valida_file(f)
            except forms.ValidationError as e:
                return Esito(rel, SALTATO, " ".join(e.messages))
            checksum = sha256_file(f)
            gia = Document.objects.visible_to(owner).filter(versions__checksum=checksum).order_by("pk").first()
            if gia is not None:
                return Esito(rel, PRESENTE, f"uguale a «{gia.title}» (#{gia.pk})", gia.pk)
            if prova:
                return Esito(rel, IMPORTATO, "da importare (prova)")
            cat = _categoria(categoria, cache) if categoria else None
            risultato = create_document(
                owner=owner, title=titolo_da_nome(p.name), category=cat, uploaded_file=f, ai_allowed=ai_allowed
            )
            if classifica and cat is None:
                from . import classificazione

                classificazione.richiedi(risultato.document)  # senza categoria decisa a mano: la propone il sistema
            return Esito(rel, IMPORTATO, "", risultato.document.pk)
    except Exception as e:  # noqa: BLE001 - un file rotto non deve fermare gli altri
        logger.exception("Importazione di %s fallita", p)
        return Esito(rel, ERRORE, f"{type(e).__name__}: {e}")
