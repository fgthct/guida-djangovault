"""Ingestione automatica: cartella osservata sul server e casella email dedicata.

Entrambi i canali portano i file nello stesso percorso del caricamento da browser (stessi controlli di tipo, dimensione
e doppioni) e chiedono la classificazione automatica: la categoria la propone il sistema (vedi «Da smistare»).

**Cartella osservata** (`INGESTIONE_CARTELLA`, di norma `inbox/arrivi/`): ciò che ci metti dentro viene importato e tolto
dalla cartella. Un file si prende solo se è fermo da `INGESTIONE_ETA_MINIMA` secondi (così non si legge un file ancora in
copia). Un file in una sottocartella prende il nome della sottocartella come categoria. I file che non si possono
importare (tipo non ammesso, file vuoto, troppo grande, errore) vanno in `_scartati/`, con il motivo nel registro dei log.

**Posta** (IMAP): si leggono i messaggi non letti; si accettano solo da `INGESTIONE_MITTENTI` **e** solo se il provider
ha verificato SPF o DKIM (altrimenti il mittente si potrebbe falsificare). Ogni allegato ammesso diventa un documento; una
email senza allegati diventa una nota di testo. Il messaggio resta nella casella, solo segnato come letto.
"""

from __future__ import annotations

import imaplib
import logging
import re
import shutil
from email import message_from_bytes, policy
from email.utils import parseaddr
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from . import upload_web
from .import_folder import ERRORE, IMPORTATO, PRESENTE, SALTATO, importa_cartella

logger = logging.getLogger(__name__)

SCARTATI = "_scartati"
MASSIMO_MESSAGGI = 20  # per controllo: il resto al prossimo giro


class IngestioneNonConfigurata(Exception):
    pass


def proprietario():
    """L'utente a cui appartengono i documenti in arrivo: `INGESTIONE_UTENTE`, altrimenti il primo superutente."""
    User = get_user_model()
    if settings.INGESTIONE_UTENTE:
        u = User.objects.filter(username=settings.INGESTIONE_UTENTE, is_active=True).first()
        if u is None:
            raise IngestioneNonConfigurata(f"INGESTIONE_UTENTE «{settings.INGESTIONE_UTENTE}» non esiste o non è attivo.")
        return u
    u = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
    if u is None:
        raise IngestioneNonConfigurata("Non c'è nessun superutente a cui assegnare i documenti.")
    return u


# --------------------------------------------------------------------------
# Cartella osservata
# --------------------------------------------------------------------------

def osserva_cartella(*, prova: bool = False) -> dict:
    """Importa ciò che c'è nella cartella osservata. Ritorna un riepilogo (utile ai log e al comando)."""
    radice = Path(settings.INGESTIONE_CARTELLA)
    try:
        radice.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Cartella osservata %s non disponibile", radice, exc_info=True)
        return {"attiva": False, "motivo": f"cartella {radice} non disponibile"}
    owner = proprietario()
    r = importa_cartella(
        radice,
        owner=owner,
        per_sottocartelle=True,
        ai_allowed=settings.INGESTIONE_CONSENSO_IA,
        rimuovi=not prova,
        prova=prova,
        senza_immagini=not settings.INGESTIONE_IMMAGINI,
        classifica=True,
        esclusi=(SCARTATI,),
        piu_vecchi_di=settings.INGESTIONE_ETA_MINIMA,
    )
    spostati = 0
    if not prova:
        for e in r.esiti:
            if e.stato in (SALTATO, ERRORE):
                spostati += _scarta(radice, e.percorso, e.stato, e.dettaglio)
    riepilogo = {
        "attiva": True,
        "importati": r.conta(IMPORTATO),
        "presenti": r.conta(PRESENTE),
        "scartati": spostati if not prova else r.conta(SALTATO) + r.conta(ERRORE),
        "dettagli": [(e.stato, e.percorso, e.dettaglio) for e in r.esiti],
    }
    if riepilogo["importati"] or riepilogo["presenti"] or riepilogo["scartati"]:
        logger.info(
            "Cartella osservata: importati %s, già presenti %s, scartati %s",
            riepilogo["importati"], riepilogo["presenti"], riepilogo["scartati"],
        )
    return riepilogo


def _scarta(radice: Path, rel: str, stato: str, motivo: str) -> int:
    """Sposta un file non importabile in `_scartati/` (stesso percorso relativo), senza sovrascrivere nulla."""
    origine = radice / rel
    if not origine.is_file():
        return 0
    destinazione = radice / SCARTATI / rel
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while destinazione.exists():
        destinazione = destinazione.with_name(f"{destinazione.stem}.{n}{destinazione.suffix}")
        n += 1
    try:
        shutil.move(str(origine), str(destinazione))
    except OSError:
        logger.warning("Impossibile spostare %s in %s", origine, destinazione, exc_info=True)
        return 0
    logger.warning("Scartato %s (%s: %s) → %s/", rel, stato, motivo, SCARTATI)
    return 1


# --------------------------------------------------------------------------
# Posta
# --------------------------------------------------------------------------

def posta_configurata() -> bool:
    return bool(
        settings.INGESTIONE_IMAP_HOST and settings.INGESTIONE_IMAP_UTENTE and settings.INGESTIONE_IMAP_PASSWORD
        and settings.INGESTIONE_MITTENTI
    )


def mittente_ammesso(indirizzo: str) -> bool:
    """`indirizzo` è nell'elenco (indirizzo intero oppure dominio con la chiocciola davanti)."""
    indirizzo = (indirizzo or "").strip().lower()
    if not indirizzo or "@" not in indirizzo:
        return False
    dominio = "@" + indirizzo.rsplit("@", 1)[1]
    return any(m == indirizzo or (m.startswith("@") and m == dominio) for m in settings.INGESTIONE_MITTENTI)


def autenticato(msg) -> bool:
    """Il provider ha verificato il mittente? Conta solo l'intestazione più in alto (aggiunta dall'ultimo server che ha
    ricevuto il messaggio): quelle sotto le può scrivere chiunque."""
    if not settings.INGESTIONE_RICHIEDI_AUTENTICAZIONE:
        return True
    righe = msg.get_all("Authentication-Results") or []
    if not righe:
        return False
    sopra = str(righe[0]).lower()
    return bool(re.search(r"\b(dkim|spf)=pass\b", sopra))


def _nome_sicuro(nome: str, predefinito: str) -> str:
    nome = Path((nome or "").replace("\\", "/")).name.strip().strip(".")
    nome = re.sub(r"[\x00-\x1f]", "", nome)
    return nome[:150] or predefinito


def _allegati(msg) -> list[tuple[str, bytes]]:
    trovati = []
    for parte in msg.iter_attachments():
        nome = parte.get_filename()
        if not nome:
            continue
        dati = parte.get_payload(decode=True)  # (un messaggio allegato a un altro dà una lista: lo si salta)
        if isinstance(dati, bytes) and dati:
            trovati.append((_nome_sicuro(nome, "allegato"), dati))
    return trovati


def _corpo(msg) -> tuple[str, str]:
    """(oggetto, testo semplice) del messaggio."""
    oggetto = " ".join(str(msg.get("Subject", "")).split())
    parte = msg.get_body(preferencelist=("plain",))
    testo = parte.get_content().strip() if parte is not None else ""
    return oggetto, testo


def _elabora_messaggio(msg, owner) -> dict:
    mittente = parseaddr(str(msg.get("From", "")))[1]
    esito = {"mittente": mittente, "oggetto": " ".join(str(msg.get("Subject", "")).split())[:80], "documenti": 0, "presenti": 0, "saltati": 0}
    if not mittente_ammesso(mittente):
        esito["ignorato"] = "mittente non ammesso"
        return esito
    if not autenticato(msg):
        esito["ignorato"] = "mittente non verificato (SPF/DKIM)"
        return esito
    file_da_creare = [
        (nome, dati) for nome, dati in _allegati(msg)
        if settings.INGESTIONE_IMMAGINI or Path(nome).suffix.lower().lstrip(".") not in {"png", "jpg", "jpeg", "gif", "webp"}
    ]
    if not file_da_creare and settings.INGESTIONE_POSTA_CORPO:
        oggetto, testo = _corpo(msg)
        if len(testo) >= 20:
            titolo = _nome_sicuro(oggetto, "Nota da email")
            file_da_creare = [(f"{titolo}.txt", (f"{oggetto}\n\n{testo}\n").encode())]
    for nome, dati in file_da_creare:
        r = upload_web.carica_file(
            uploaded=SimpleUploadedFile(nome, dati), owner=owner,
            scelta_categoria=upload_web.CAT_AUTOMATICA, ai_allowed=settings.INGESTIONE_CONSENSO_IA,
        )
        chiave = {IMPORTATO: "documenti", PRESENTE: "presenti"}.get(r["stato"], "saltati")
        esito[chiave] += 1
        if r["stato"] in (SALTATO, ERRORE):
            logger.warning("Allegato %s di %s non importato: %s", nome, mittente, r.get("dettaglio"))
    return esito


def leggi_posta(*, prova: bool = False) -> dict:
    """Legge i messaggi non letti della casella e ne importa gli allegati. Un messaggio, una volta esaminato (anche se
    ignorato), si segna come letto; non si cancella mai nulla dalla casella."""
    if not posta_configurata():
        return {"attiva": False, "motivo": "posta non configurata (host, utente, password e mittenti sono tutti obbligatori)"}
    owner = proprietario()
    esiti = []
    try:
        imap = imaplib.IMAP4_SSL(settings.INGESTIONE_IMAP_HOST, settings.INGESTIONE_IMAP_PORTA)
    except OSError as e:
        logger.warning("Posta: server %s non raggiungibile: %s", settings.INGESTIONE_IMAP_HOST, e)
        return {"attiva": True, "errore": "server di posta non raggiungibile", "messaggi": []}
    try:
        imap.login(settings.INGESTIONE_IMAP_UTENTE, settings.INGESTIONE_IMAP_PASSWORD)
        imap.select(settings.INGESTIONE_IMAP_CARTELLA)
        _stato, trovati = imap.search(None, "UNSEEN")
        for numero in (trovati[0].split() if trovati and trovati[0] else [])[:MASSIMO_MESSAGGI]:
            _stato, dati = imap.fetch(numero, "(BODY.PEEK[])")  # PEEK: non segna come letto finché non abbiamo finito
            grezzo = next((p[1] for p in dati if isinstance(p, tuple)), None)
            if not grezzo:
                continue
            msg = message_from_bytes(grezzo, policy=policy.default)
            try:
                esito = _elabora_messaggio(msg, owner) if not prova else {"mittente": parseaddr(str(msg.get("From", "")))[1], "oggetto": str(msg.get("Subject", ""))[:80], "prova": True}
            except Exception:  # noqa: BLE001 - un messaggio rotto non deve bloccare la casella
                logger.exception("Posta: messaggio %s non elaborabile", numero)
                esito = {"mittente": "", "oggetto": "", "errore": "messaggio non elaborabile"}
            esiti.append(esito)
            if not prova:
                imap.store(numero, "+FLAGS", "\\Seen")
    except imaplib.IMAP4.error as e:
        logger.warning("Posta: errore IMAP (%s)", e)
        return {"attiva": True, "errore": f"errore IMAP: {e}", "messaggi": esiti}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    totale = sum(e.get("documenti", 0) for e in esiti)
    if esiti:
        logger.info("Posta: %s messaggi esaminati, %s documenti creati", len(esiti), totale)
    return {"attiva": True, "messaggi": esiti, "documenti": totale}
