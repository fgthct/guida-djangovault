"""Caricamento di un file alla volta dalla pagina «Carica più file».

Il browser manda i file uno per volta (così si vede l'avanzamento e un file rotto non ferma gli altri); ogni file passa
dagli stessi controlli e dallo stesso percorso (`create_document`) del caricamento singolo e dell'importazione da cartella.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from django import forms

from ..forms import valida_file
from ..models import Category, Document
from . import classificazione
from .create_document import create_document
from .files import sha256_file
from .import_folder import ERRORE, IMPORTATO, PRESENTE, SALTATO, titolo_da_nome

logger = logging.getLogger(__name__)

# Come scegliere la categoria dei file caricati.
CAT_NESSUNA = "nessuna"
CAT_CARTELLA = "cartella"  # sottocartella del percorso relativo (vedi _cartella_categoria)
CAT_AUTOMATICA = "automatica"  # la sceglie il classificatore (fase 2)


def _cartella_categoria(percorso_relativo: str) -> str | None:
    """Nome della cartella da usare come categoria.

    Trascinando «miei_libri» il percorso è «miei_libri/Romanzi/zolfo.pdf»: la categoria è la sottocartella («Romanzi»);
    un file direttamente nella cartella scelta («miei_libri/zolfo.pdf») prende il nome della cartella scelta; un file
    senza cartella non ne ha.
    """
    parti = [p for p in PurePosixPath(percorso_relativo.replace("\\", "/")).parts if p not in ("", ".", "..")]
    if len(parti) >= 3:
        return parti[1]
    return parti[0] if len(parti) == 2 else None


def _categoria_per_nome(nome: str) -> Category:
    nome = " ".join(nome.split())[:80]
    return Category.objects.filter(name__iexact=nome).first() or Category.objects.create(name=nome)


def risolvi_categoria(scelta: str, percorso_relativo: str) -> Category | None:
    """`scelta` è «nessuna», «cartella», «automatica» oppure l'id di una categoria esistente."""
    if scelta in ("", CAT_NESSUNA, CAT_AUTOMATICA):
        return None
    if scelta == CAT_CARTELLA:
        nome = _cartella_categoria(percorso_relativo)
        return _categoria_per_nome(nome) if nome else None
    if scelta.isdigit():
        return Category.objects.filter(pk=int(scelta)).first()
    return None


def carica_file(*, uploaded, owner, scelta_categoria: str = CAT_NESSUNA, percorso_relativo: str = "", ai_allowed: bool = True) -> dict:
    """Crea un documento da un file caricato e dice com'è andata, in una forma pronta per il JSON."""
    nome = uploaded.name
    try:
        try:
            valida_file(uploaded)
        except forms.ValidationError as e:
            return {"stato": SALTATO, "dettaglio": " ".join(e.messages)}
        gia = Document.objects.visible_to(owner).filter(versions__checksum=sha256_file(uploaded)).order_by("pk").first()
        if gia is not None:
            return {
                "stato": PRESENTE,
                "dettaglio": f"identico a «{gia.title}»",
                "id": gia.pk,
                "url": gia.get_absolute_url(),
            }
        categoria = risolvi_categoria(scelta_categoria, percorso_relativo)
        risultato = create_document(
            owner=owner, title=titolo_da_nome(nome), category=categoria, uploaded_file=uploaded, ai_allowed=ai_allowed
        )
        documento = risultato.document
        automatica = scelta_categoria == CAT_AUTOMATICA
        if automatica:
            classificazione.richiedi(documento)  # parte da sola appena il testo è indicizzato
        return {
            "stato": IMPORTATO,
            "dettaglio": categoria.name if categoria else ("classificazione automatica" if automatica else ""),
            "id": documento.pk,
            "url": documento.get_absolute_url(),
            "titolo": documento.title,
        }
    except Exception as e:  # noqa: BLE001 - un file rotto non deve fermare gli altri
        logger.exception("Caricamento di %s fallito", nome)
        return {"stato": ERRORE, "dettaglio": f"{type(e).__name__}"}
