"""Elaborazione asincrona delle versioni: testo estratto, OCR, anteprima.

Il database è la fonte di verità. Ogni `DocumentVersion` ha uno stato
(in coda → in corso → completata | non applicabile | errore) e Redis/Celery sono solo il
campanello che avvisa il worker. Per questo:

- l'elaborazione è **idempotente**: ripeterla (messaggio duplicato, riavvio) non fa danni;
- chi la avvia «prenota» la riga con `select_for_update(skip_locked=True)`: due worker non
  lavorano mai la stessa versione;
- il lavoro pesante avviene **fuori da ogni transazione**: nessun lock tenuto durante un OCR;
- se un messaggio si perde o un worker muore, `riprendi_elaborazioni()` (periodico) rimette
  in coda ciò che è rimasto fermo troppo a lungo.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from core.services import audit

from .. import extraction
from ..models import DocumentVersion

logger = logging.getLogger(__name__)
P = DocumentVersion.Processing

# Esiti di `elabora_versione`
OK, NON_APPLICABILE, ERRORE, RIPROVA, IGNORATA = "ok", "non_applicabile", "errore", "riprova", "ignorata"


class ElaborazioneInCorso(ValueError):
    """La versione è in elaborazione in questo momento: non si può rimettere in coda."""


# --------------------------------------------------------------------------
# Messa in coda
# --------------------------------------------------------------------------

def accoda(version_id: int) -> None:
    """Chiede l'elaborazione DOPO il commit: il worker non vede mai una versione non ancora salvata.

    Da chiamare dentro la transazione che crea la versione. Se il broker è fermo il caricamento
    del file non fallisce: la versione resta «in coda» e la riprende `riprendi_elaborazioni`.
    """
    transaction.on_commit(lambda: _invia(version_id))


def _invia(version_id: int) -> bool:
    from ..tasks import elabora_versione_task

    try:
        elabora_versione_task.delay(version_id)
        return True
    except Exception:
        logger.warning(
            "Broker non raggiungibile: la versione %s resta in coda e sarà ripresa in automatico.",
            version_id, exc_info=True,
        )
        return False


def richiedi_rielaborazione(*, user, version: DocumentVersion) -> DocumentVersion:
    """Rimette in coda una versione (per esempio dopo un errore). Rifiuta se è in corso adesso."""
    with transaction.atomic():
        v = DocumentVersion.objects.select_for_update().select_related("document").get(pk=version.pk)
        if v.processing_status == P.RUNNING and not _scaduta(v):
            raise ElaborazioneInCorso("La versione è in elaborazione in questo momento.")
        v.processing_status = P.PENDING
        v.processing_message = ""
        v.processing_attempts = 0
        v.processing_updated_at = timezone.now()
        v.save(update_fields=["processing_status", "processing_message", "processing_attempts", "processing_updated_at"])
        audit.record(
            "version.reprocess_requested", user=user, obj=v.document, versione=v.version_number
        )
        accoda(v.pk)
    return v


# --------------------------------------------------------------------------
# Elaborazione
# --------------------------------------------------------------------------

def _scaduta(v: DocumentVersion) -> bool:
    """Una versione «in corso» ferma da troppo tempo: il worker che la lavorava è morto."""
    limite = timezone.now() - timedelta(minutes=settings.PROCESSING_STALE_MINUTES)
    return v.processing_updated_at is None or v.processing_updated_at < limite


def _prenota(version_id: int, *, forza: bool) -> DocumentVersion | None:
    """Porta la versione a «in corso» se e solo se è il suo turno. None = niente da fare."""
    with transaction.atomic():
        v = (
            DocumentVersion.objects.select_for_update(skip_locked=True)
            .select_related("document")
            .filter(pk=version_id)
            .first()
        )
        if v is None:  # eliminata, oppure presa in carico da un altro worker in questo istante
            return None
        if v.processing_status == P.RUNNING and not _scaduta(v):
            return None
        if v.processing_status in (P.DONE, P.SKIPPED, P.FAILED) and not forza:
            return None
        if v.processing_attempts >= settings.PROCESSING_MAX_ATTEMPTS and not forza:
            _chiudi_con_errore(v, "Elaborazione interrotta più volte: riprova con «Rielabora».")
            return None
        v.processing_status = P.RUNNING
        v.processing_attempts += 1
        v.processing_updated_at = timezone.now()
        v.processing_message = ""
        v.save(update_fields=["processing_status", "processing_attempts", "processing_updated_at", "processing_message"])
        return v


def _chiudi_con_errore(v: DocumentVersion, messaggio: str) -> None:
    """Segna l'errore (dentro una transazione già aperta) e lo scrive nel registro."""
    v.processing_status = P.FAILED
    v.processing_message = messaggio[:500]
    v.processing_updated_at = timezone.now()
    v.save(update_fields=["processing_status", "processing_message", "processing_updated_at"])
    audit.record(
        "version.processing_failed", username="sistema", obj=v.document,
        versione=v.version_number, motivo=messaggio[:200],
    )


def _copia_in_locale(v: DocumentVersion, cartella: Path) -> Path:
    """Copia il file dallo storage in una cartella temporanea (i programmi esterni vogliono un percorso)."""
    destinazione = cartella / f"input.{extraction.estensione(v.original_name) or 'bin'}"
    try:
        with v.file.open("rb") as sorgente, destinazione.open("wb") as uscita:
            for blocco in sorgente.chunks(1024 * 1024):
                uscita.write(blocco)
    except FileNotFoundError as exc:
        raise extraction.ErroreEstrazione("File non trovato nell'archivio.") from exc
    return destinazione


def _riusabile(v: DocumentVersion) -> DocumentVersion | None:
    """Un'altra versione con lo STESSO contenuto già elaborata (tipico: un ripristino)."""
    return (
        DocumentVersion.objects.filter(checksum=v.checksum, processing_status=P.DONE)
        .exclude(pk=v.pk)
        .order_by("-processed_at")
        .first()
    )


def elabora_versione(version_id: int, *, forza: bool = False) -> str:
    """Elabora una versione. Ritorna uno tra OK, NON_APPLICABILE, ERRORE, RIPROVA, IGNORATA.

    Non solleva mai per un problema del file: lo registra nello stato. Solleva solo se il
    database stesso è irraggiungibile (e allora Celery riproverà).
    """
    from celery.exceptions import SoftTimeLimitExceeded

    v = _prenota(version_id, forza=forza)
    if v is None:
        return IGNORATA

    risultato = None
    origine = None if forza else _riusabile(v)
    try:
        if origine is None:
            with tempfile.TemporaryDirectory(prefix="dv-in-") as tmp:
                risultato = extraction.estrai(_copia_in_locale(v, Path(tmp)), v.original_name)
    except extraction.FormatoNonSupportato as exc:
        _concludi(v, P.SKIPPED, messaggio=str(exc))
        return NON_APPLICABILE
    except extraction.ErroreEstrazione as exc:
        _concludi(v, P.FAILED, messaggio=str(exc))
        return ERRORE
    except SoftTimeLimitExceeded:
        _concludi(v, P.FAILED, messaggio="Tempo massimo di elaborazione superato.")
        return ERRORE
    except Exception:
        logger.exception("Errore imprevisto elaborando la versione %s", version_id)
        if v.processing_attempts < settings.PROCESSING_MAX_ATTEMPTS:
            _concludi(v, P.PENDING, messaggio="Errore imprevisto: nuovo tentativo in corso.")
            return RIPROVA
        _concludi(v, P.FAILED, messaggio="Errore imprevisto durante l'elaborazione (dettagli nei log del server).")
        return ERRORE

    _concludi(v, P.DONE, risultato=risultato, origine=origine)
    return OK


def _concludi(
    v: DocumentVersion, stato: str, *, messaggio: str = "", risultato=None, origine: DocumentVersion | None = None
) -> None:
    """Scrive l'esito in una transazione breve. Ignora l'esito se nel frattempo lo stato è cambiato."""
    nuovo_file = None
    try:
        with transaction.atomic():
            corrente = (
                DocumentVersion.objects.select_for_update().select_related("document").filter(pk=v.pk).first()
            )
            if corrente is None or corrente.processing_status != P.RUNNING:
                return  # eliminata o già gestita da qualcun altro
            vecchia_anteprima = (corrente.preview.storage, corrente.preview.name) if corrente.preview else None

            corrente.processing_status = stato
            corrente.processing_message = messaggio[:500]
            corrente.processing_updated_at = timezone.now()
            campi = ["processing_status", "processing_message", "processing_updated_at"]

            if stato == P.DONE:
                if origine is not None:  # stesso contenuto: si copia, non si rifà
                    corrente.extracted_text = origine.extracted_text
                    corrente.page_count = origine.page_count
                    corrente.ocr_used = origine.ocr_used
                    corrente.text_truncated = origine.text_truncated
                    png = origine.preview.read() if origine.preview else None
                    if origine.preview:
                        origine.preview.close()
                else:
                    corrente.extracted_text = risultato.text
                    corrente.page_count = risultato.pages
                    corrente.ocr_used = risultato.ocr
                    corrente.text_truncated = risultato.truncated
                    corrente.processing_message = risultato.note[:500]
                    png = risultato.preview
                if png:
                    corrente.preview.save("anteprima.png", ContentFile(png), save=False)
                    nuovo_file = (corrente.preview.storage, corrente.preview.name)
                else:
                    corrente.preview = ""
                corrente.processed_at = timezone.now()
                indicizza = bool(corrente.extracted_text)
                # Il testo può essere cambiato: i vecchi passaggi non valgono più, va rifatta l'indicizzazione.
                corrente.indexed_model, corrente.indexed_at, corrente.index_attempted_at = "", None, None
                campi += ["indexed_model", "indexed_at", "index_attempted_at", "extracted_text", "page_count", "ocr_used", "text_truncated", "preview", "processed_at"]
            elif stato == P.FAILED:
                audit.record(
                    "version.processing_failed", username="sistema", obj=corrente.document,
                    versione=corrente.version_number, motivo=messaggio[:200],
                )
            corrente.save(update_fields=campi)
            if stato == P.DONE:
                # Il testo (nuovo) è pronto: passaggi e vettori per la ricerca per significato, in un secondo task.
                from . import indexing

                if indicizza:
                    indexing.accoda_indicizzazione(corrente.pk)
                else:  # niente testo: nessun passaggio da calcolare, ma la versione risulta indicizzata
                    corrente.chunks.all().delete()
                    DocumentVersion.objects.filter(pk=corrente.pk).update(
                        indexed_model=indexing.modello_corrente(), indexed_at=timezone.now()
                    )

            if vecchia_anteprima and vecchia_anteprima[1] != (nuovo_file or ("", ""))[1]:
                transaction.on_commit(lambda: _rimuovi(*vecchia_anteprima))
    except Exception:
        if nuovo_file:
            _rimuovi(*nuovo_file)  # niente file orfani se il salvataggio fallisce
        raise


def _rimuovi(storage, nome: str) -> None:
    try:
        storage.delete(nome)
    except Exception:
        logger.exception("Anteprima non rimossa: %s", nome)


# --------------------------------------------------------------------------
# Rete di sicurezza periodica
# --------------------------------------------------------------------------

def riprendi_elaborazioni(*, massimo: int = 200) -> int:
    """Rimette in coda ciò che è fermo da troppo tempo. Ritorna quante versioni ha rimesso in coda.

    - «in corso» da più di PROCESSING_STALE_MINUTES: il worker è morto → di nuovo in coda
      (o in errore se ha già esaurito i tentativi);
    - «in coda» senza movimento da più di 2 minuti: il messaggio si è perso o il broker era fermo
      (succede anche dopo un aggiornamento, per le versioni caricate prima della Fase 6).
    """
    ora = timezone.now()
    stantie = ora - timedelta(minutes=settings.PROCESSING_STALE_MINUTES)
    attese = ora - timedelta(minutes=2)

    da_inviare: list[int] = []
    with transaction.atomic():
        ferme = (
            DocumentVersion.objects.select_for_update(skip_locked=True)
            .select_related("document")
            .filter(processing_status=P.RUNNING, processing_updated_at__lt=stantie)
        )
        for v in ferme:
            if v.processing_attempts >= settings.PROCESSING_MAX_ATTEMPTS:
                _chiudi_con_errore(v, "Elaborazione interrotta più volte: riprova con «Rielabora».")
            else:
                v.processing_status = P.PENDING
                v.processing_updated_at = ora
                v.save(update_fields=["processing_status", "processing_updated_at"])
                da_inviare.append(v.pk)

        in_coda = (
            DocumentVersion.objects.select_for_update(skip_locked=True)
            .filter(processing_status=P.PENDING)
            .filter(_in_coda_ferma(attese, stantie))
            .order_by("pk")[:massimo]
        )
        for v in in_coda:
            v.processing_updated_at = ora  # si ritenta tra 15 minuti, non a ogni giro del pianificatore
            v.save(update_fields=["processing_updated_at"])
            da_inviare.append(v.pk)

    inviati = 0
    for pk in da_inviare:
        inviati += _invia(pk)
    if da_inviare:
        logger.info("Riprese %s elaborazioni ferme (%s inviate al broker)", len(da_inviare), inviati)
    return len(da_inviare)


def _in_coda_ferma(attese, stantie):
    """In coda mai toccata da più di 2 minuti, oppure ritoccata da più di PROCESSING_STALE_MINUTES."""
    from django.db.models import Q

    return (Q(processing_updated_at__isnull=True) & Q(created_at__lt=attese)) | Q(processing_updated_at__lt=stantie)
