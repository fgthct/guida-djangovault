"""Estrazione di testo e anteprima da un file.

Modulo puro: non conosce modelli né Celery, riceve un percorso e restituisce un `Estratto`.
Così si prova con file veri senza database e lo stesso codice serve a task, comando e test.

Principi:
- ogni programma esterno (pdftotext, pdftoppm, tesseract, soffice) gira SENZA shell, con un
  timeout e in una cartella temporanea che sparisce alla fine;
- i file ZIP/XML (docx, xlsx, pptx, odt…) si leggono con `defusedxml` e con limiti sulla
  dimensione decompressa, contro le "bombe" di compressione;
- l'anteprima è sempre facoltativa: se fallisce, il testo estratto resta valido;
- i messaggi di `ErroreEstrazione` sono pensati per essere mostrati all'utente.
"""

from __future__ import annotations

import functools
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from defusedxml import ElementTree as ET
from django.conf import settings
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

TESTO = {"txt", "md", "csv"}
IMMAGINI = {"png", "jpg", "jpeg", "gif", "webp"}
OOXML = {"docx", "xlsx", "pptx"}
ODF = {"odt", "ods", "odp"}
# Formati binari vecchi: si leggono solo se LibreOffice è installato (li converte in PDF).
VECCHI = {"doc", "xls", "ppt", "rtf"}

# Limite alla dimensione decompressa di un archivio Office/ODF.
MAX_DECOMPRESSO = 200 * 1024 * 1024
# Sotto questa quantità di caratteri per pagina un PDF è considerato "scansione": serve l'OCR.
MIN_CARATTERI_PER_PAGINA = 25


class ErroreEstrazione(Exception):
    """Il file non si può elaborare. Il messaggio è mostrabile all'utente."""


class FormatoNonSupportato(ErroreEstrazione):
    """Nessun testo da estrarre da questo formato: non è un errore, è un «non applicabile»."""


@dataclass
class Estratto:
    text: str = ""
    pages: int | None = None
    ocr: bool = False
    truncated: bool = False
    preview: bytes | None = None  # PNG
    note: str = ""  # avviso non fatale (es. "OCR limitato alle prime 30 pagine")


# --------------------------------------------------------------------------
# Strumenti
# --------------------------------------------------------------------------

def estensione(nome: str) -> str:
    return Path(nome).suffix.lower().lstrip(".")


def _timeout() -> int:
    return settings.PROCESSING_SUBPROCESS_TIMEOUT


def _esegui(comando: list[str], *, timeout: int | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Esegue un programma esterno: niente shell, timeout, un solo thread per Tesseract."""
    ambiente = {**os.environ, "OMP_THREAD_LIMIT": "1", "LC_ALL": "C.UTF-8"}
    try:
        return subprocess.run(
            comando, capture_output=True, timeout=timeout or _timeout(), cwd=cwd, env=ambiente, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ErroreEstrazione(f"{Path(comando[0]).name}: tempo scaduto dopo {exc.timeout:.0f} secondi.") from exc
    except FileNotFoundError as exc:
        raise ErroreEstrazione(f"Programma «{Path(comando[0]).name}» non installato sul server.") from exc


def programma_disponibile(nome: str) -> bool:
    return shutil.which(nome) is not None


def libreoffice() -> str | None:
    return shutil.which(settings.PROCESSING_SOFFICE)


@functools.lru_cache(maxsize=1)
def lingue_ocr() -> str:
    """Le lingue richieste che Tesseract ha davvero (ita+eng → solo quelle installate)."""
    richieste = [l for l in settings.PROCESSING_OCR_LANGS.split("+") if l]
    try:
        out = _esegui(["tesseract", "--list-langs"], timeout=15).stdout.decode("utf-8", "replace")
    except ErroreEstrazione:
        return "+".join(richieste)
    installate = {riga.strip() for riga in out.splitlines()[1:] if riga.strip()}
    disponibili = [l for l in richieste if l in installate]
    return "+".join(disponibili or (["eng"] if "eng" in installate else richieste))


_CONTROLLO = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RIGHE_VUOTE = re.compile(r"\n{3,}")
_SPAZI_FINE_RIGA = re.compile(r"[ \t]+\n")


def pulisci_testo(testo: str) -> str:
    """Toglie i caratteri che PostgreSQL rifiuta (NUL) o che sono solo rumore, e compatta le righe vuote."""
    testo = testo.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n").replace("\xa0", " ")
    testo = _CONTROLLO.sub("", testo)
    testo = _SPAZI_FINE_RIGA.sub("\n", testo)
    return _RIGHE_VUOTE.sub("\n\n", testo).strip()


def _limita(testo: str, estratto: Estratto) -> None:
    massimo = settings.PROCESSING_TEXT_MAX_CHARS
    testo = pulisci_testo(testo)
    if len(testo) > massimo:
        testo = testo[:massimo]
        estratto.truncated = True
    estratto.text = testo


def _png_da_immagine(img: Image.Image) -> bytes:
    """Riduce l'immagine alla larghezza dell'anteprima e la codifica in PNG (sfondo scuro se trasparente)."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fondo = Image.new("RGBA", img.size, (24, 24, 27, 255))  # come il tema scuro dell'app
        fondo.alpha_composite(img)
        img = fondo
    img = img.convert("RGB")
    larghezza = settings.PROCESSING_PREVIEW_WIDTH
    img.thumbnail((larghezza, int(larghezza * 1.5)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _apri_immagine(percorso: Path) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = settings.PROCESSING_MAX_IMAGE_PIXELS
    try:
        img = Image.open(percorso)
        img.load()
        return img
    except Image.DecompressionBombError as exc:
        raise ErroreEstrazione("Immagine troppo grande per essere elaborata.") from exc
    except Exception as exc:
        raise ErroreEstrazione("Immagine non valida o danneggiata.") from exc


# --------------------------------------------------------------------------
# Testo semplice
# --------------------------------------------------------------------------

def _decodifica(dati: bytes) -> str:
    for codifica in ("utf-8-sig", "utf-16"):
        try:
            if codifica == "utf-16" and not dati.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return dati.decode(codifica)
        except UnicodeDecodeError:
            continue
    return dati.decode("cp1252", errors="replace")


def _da_testo(percorso: Path, risultato: Estratto) -> None:
    massimo = settings.PROCESSING_TEXT_MAX_CHARS * 4  # byte: oltre non serve leggere
    with percorso.open("rb") as f:
        dati = f.read(massimo + 1)
    if len(dati) > massimo:
        dati = dati[:massimo]
        risultato.truncated = True
    _limita(_decodifica(dati), risultato)


# --------------------------------------------------------------------------
# PDF (testo, OCR, anteprima)
# --------------------------------------------------------------------------

def _pagine_pdf(percorso: Path) -> int | None:
    proc = _esegui(["pdfinfo", str(percorso)])
    if proc.returncode != 0:
        errore = proc.stderr.decode("utf-8", "replace").lower()
        if "incorrect password" in errore or "encrypted" in errore:
            raise ErroreEstrazione("Il PDF è protetto da password: impossibile leggerlo.")
        raise ErroreEstrazione("PDF non valido o danneggiato.")
    for riga in proc.stdout.decode("utf-8", "replace").splitlines():
        if riga.startswith("Pages:"):
            try:
                return int(riga.split(":", 1)[1])
            except ValueError:
                return None
    return None


def _testo_pdf(percorso: Path, cartella: Path) -> str:
    uscita = cartella / "testo.txt"
    proc = _esegui(["pdftotext", "-enc", "UTF-8", "-q", str(percorso), str(uscita)])
    if proc.returncode != 0 or not uscita.exists():
        raise ErroreEstrazione("Impossibile leggere il testo del PDF (file danneggiato o protetto).")
    with uscita.open("rb") as f:
        return f.read(settings.PROCESSING_TEXT_MAX_CHARS * 4).decode("utf-8", "replace")


def _pagine_come_png(percorso: Path, cartella: Path, *, prima: int, ultima: int, dpi: int | None = None,
                     larghezza: int | None = None) -> list[Path]:
    prefisso = cartella / "pag"
    comando = ["pdftoppm", "-png", "-f", str(prima), "-l", str(ultima)]
    if dpi:
        comando += ["-r", str(dpi)]
    if larghezza:
        comando += ["-scale-to-x", str(larghezza), "-scale-to-y", "-1"]
    proc = _esegui(comando + [str(percorso), str(prefisso)])
    if proc.returncode != 0:
        raise ErroreEstrazione("Impossibile convertire le pagine del PDF in immagini.")
    return sorted(cartella.glob("pag-*.png"))


def _ocr_immagine(percorso: Path) -> str:
    proc = _esegui(["tesseract", str(percorso), "stdout", "-l", lingue_ocr(), "--psm", "3"])
    if proc.returncode != 0:
        raise ErroreEstrazione("OCR non riuscito.")
    return proc.stdout.decode("utf-8", "replace")


def _anteprima_pdf(percorso: Path, cartella: Path) -> bytes | None:
    try:
        sotto = cartella / "anteprima"
        sotto.mkdir()
        pagine = _pagine_come_png(percorso, sotto, prima=1, ultima=1, larghezza=settings.PROCESSING_PREVIEW_WIDTH)
        if pagine:
            with Image.open(pagine[0]) as img:
                return _png_da_immagine(img)
    except Exception:  # l'anteprima è facoltativa
        logger.info("Anteprima PDF non generata", exc_info=True)
    return None


def _da_pdf(percorso: Path, cartella: Path, risultato: Estratto) -> None:
    risultato.pages = _pagine_pdf(percorso)
    testo = _testo_pdf(percorso, cartella)
    pagine = max(risultato.pages or 1, 1)
    densita = len(re.sub(r"\s", "", testo)) / pagine
    if densita < MIN_CARATTERI_PER_PAGINA:
        # Poco testo per pagina: probabile scansione, si prova l'OCR sulle prime pagine.
        massimo = settings.PROCESSING_OCR_MAX_PAGES
        ocr_dir = cartella / "ocr"
        ocr_dir.mkdir()
        immagini = _pagine_come_png(
            percorso, ocr_dir, prima=1, ultima=min(pagine, massimo), dpi=settings.PROCESSING_OCR_DPI
        )
        blocchi = [_ocr_immagine(p) for p in immagini]
        riconosciuto = "\n\n".join(blocchi)
        if len(riconosciuto.strip()) > len(testo.strip()):
            testo = riconosciuto
            risultato.ocr = True
            if pagine > massimo:
                risultato.note = f"OCR eseguito solo sulle prime {massimo} pagine di {pagine}."
    _limita(testo, risultato)
    risultato.preview = _anteprima_pdf(percorso, cartella)


# --------------------------------------------------------------------------
# Immagini
# --------------------------------------------------------------------------

def _da_immagine(percorso: Path, cartella: Path, risultato: Estratto) -> None:
    img = _apri_immagine(percorso)
    risultato.pages = 1
    risultato.preview = _png_da_immagine(img)
    # Tesseract legge un PNG pulito: evita formati che non conosce (webp) e metadati strani.
    png = cartella / "ocr.png"
    ImageOps.exif_transpose(img).convert("RGB").save(png, format="PNG")
    testo = _ocr_immagine(png)
    risultato.ocr = True
    _limita(testo, risultato)


# --------------------------------------------------------------------------
# Office (OOXML) e OpenDocument
# --------------------------------------------------------------------------

def _apri_zip(percorso: Path) -> zipfile.ZipFile:
    try:
        archivio = zipfile.ZipFile(percorso)
    except zipfile.BadZipFile as exc:
        raise ErroreEstrazione("File Office/OpenDocument non valido o danneggiato.") from exc
    if sum(i.file_size for i in archivio.infolist()) > MAX_DECOMPRESSO:
        raise ErroreEstrazione("L'archivio decompresso è troppo grande per essere elaborato.")
    return archivio


def _xml(archivio: zipfile.ZipFile, nome: str):
    try:
        with archivio.open(nome) as f:
            return ET.parse(f).getroot()
    except KeyError:
        return None
    except ET.ParseError as exc:
        raise ErroreEstrazione(f"Contenuto XML non valido in «{nome}».") from exc
    except Exception as exc:  # defusedxml solleva eccezioni dedicate per i contenuti pericolosi
        raise ErroreEstrazione(f"Contenuto non sicuro o illeggibile in «{nome}».") from exc


def _locale(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paragrafi_ooxml(radice, contenitore: str) -> list[str]:
    """Testo per paragrafo (`w:p` in Word, `a:p` in PowerPoint)."""
    righe = []
    for p in radice.iter():
        if _locale(p.tag) != contenitore:
            continue
        pezzi = []
        for e in p.iter():
            nome = _locale(e.tag)
            if nome == "t" and e.text:
                pezzi.append(e.text)
            elif nome in ("tab",):
                pezzi.append("\t")
            elif nome in ("br", "cr"):
                pezzi.append("\n")
        righe.append("".join(pezzi))
    return righe


def _da_docx(archivio: zipfile.ZipFile) -> str:
    parti = ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"]
    righe: list[str] = []
    for nome in parti:
        radice = _xml(archivio, nome)
        if radice is not None:
            righe += _paragrafi_ooxml(radice, "p")
    if not righe:
        raise ErroreEstrazione("Documento Word senza contenuto leggibile.")
    return "\n".join(righe)


def _da_pptx(archivio: zipfile.ZipFile) -> str:
    diapositive = sorted(
        (n for n in archivio.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
    )
    blocchi = []
    for numero, nome in enumerate(diapositive, 1):
        radice = _xml(archivio, nome)
        if radice is not None:
            testo = "\n".join(r for r in _paragrafi_ooxml(radice, "p") if r.strip())
            if testo:
                blocchi.append(f"[Diapositiva {numero}]\n{testo}")
    return "\n\n".join(blocchi)


def _da_xlsx(archivio: zipfile.ZipFile) -> str:
    righe = []
    condivise = _xml(archivio, "xl/sharedStrings.xml")
    if condivise is not None:
        for si in condivise:
            righe.append("".join(t.text or "" for t in si.iter() if _locale(t.tag) == "t"))
    # Testo scritto direttamente nelle celle (inlineStr) dei fogli.
    for nome in sorted(n for n in archivio.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
        foglio = _xml(archivio, nome)
        if foglio is None:
            continue
        for cella in foglio.iter():
            if _locale(cella.tag) == "c" and cella.get("t") == "inlineStr":
                righe.append("".join(t.text or "" for t in cella.iter() if _locale(t.tag) == "t"))
    return "\n".join(r for r in righe if r.strip())


def _testo_odf(elemento) -> str:
    """Testo di un nodo OpenDocument: `text:s` sono spazi, `text:tab` tabulazioni, `text:line-break` a capo."""
    pezzi = [elemento.text or ""]
    for figlio in elemento:
        nome = _locale(figlio.tag)
        if nome == "s":
            pezzi.append(" " * int(figlio.get("{urn:oasis:names:tc:opendocument:xmlns:text:1.0}c", "1") or 1))
        elif nome == "tab":
            pezzi.append("\t")
        elif nome == "line-break":
            pezzi.append("\n")
        else:
            pezzi.append(_testo_odf(figlio))
        pezzi.append(figlio.tail or "")
    return "".join(pezzi)


def _da_odf(archivio: zipfile.ZipFile) -> str:
    radice = _xml(archivio, "content.xml")
    if radice is None:
        raise ErroreEstrazione("Documento OpenDocument senza contenuto.")
    righe = []
    for e in radice.iter():
        if _locale(e.tag) in ("p", "h"):
            righe.append(_testo_odf(e))
    return "\n".join(righe)


def _da_archivio(percorso: Path, ext: str, risultato: Estratto) -> None:
    with _apri_zip(percorso) as archivio:
        if ext == "docx":
            testo = _da_docx(archivio)
        elif ext == "pptx":
            testo = _da_pptx(archivio)
        elif ext == "xlsx":
            testo = _da_xlsx(archivio)
        else:
            testo = _da_odf(archivio)
    _limita(testo, risultato)


# --------------------------------------------------------------------------
# LibreOffice (facoltativo): anteprime di Office e formati vecchi
# --------------------------------------------------------------------------

def _converti_in_pdf(percorso: Path, cartella: Path) -> Path | None:
    """Converte con LibreOffice in un PDF. Un profilo temporaneo per ogni esecuzione evita i blocchi tra processi."""
    soffice = libreoffice()
    if not soffice:
        return None
    profilo = cartella / "profilo-lo"
    uscita = cartella / "lo-out"
    uscita.mkdir()
    proc = _esegui(
        [soffice, f"-env:UserInstallation=file://{profilo}", "--headless", "--norestore", "--nolockcheck",
         "--convert-to", "pdf", "--outdir", str(uscita), str(percorso)],
        timeout=max(_timeout(), 180),
        cwd=cartella,
    )
    pdf = next(uscita.glob("*.pdf"), None)
    if proc.returncode != 0 or pdf is None:
        raise ErroreEstrazione("LibreOffice non è riuscito a convertire il file.")
    return pdf


def _da_vecchio_formato(percorso: Path, cartella: Path, risultato: Estratto) -> None:
    if not libreoffice():
        raise FormatoNonSupportato(
            "Formato vecchio (doc, xls, ppt, rtf): per leggerlo serve LibreOffice sul server."
        )
    pdf = _converti_in_pdf(percorso, cartella)
    _da_pdf(pdf, cartella, risultato)
    risultato.ocr = False  # il PDF nasce dal documento, non da una scansione


# --------------------------------------------------------------------------
# Punto d'ingresso
# --------------------------------------------------------------------------

def estrai(percorso: Path, nome_originale: str) -> Estratto:
    """Estrae testo (e, se possibile, un'anteprima PNG) dal file in `percorso`.

    Solleva `FormatoNonSupportato` se il formato non ha testo da estrarre e `ErroreEstrazione`
    se il file è danneggiato o l'elaborazione non riesce.
    """
    ext = estensione(nome_originale)
    risultato = Estratto()
    with tempfile.TemporaryDirectory(prefix="dv-elab-") as tmp:
        cartella = Path(tmp)
        if ext in TESTO:
            _da_testo(percorso, risultato)
        elif ext == "pdf":
            _da_pdf(percorso, cartella, risultato)
        elif ext in IMMAGINI:
            _da_immagine(percorso, cartella, risultato)
        elif ext in OOXML | ODF:
            _da_archivio(percorso, ext, risultato)
            risultato.preview = _anteprima_con_libreoffice(percorso, cartella, risultato)
        elif ext in VECCHI:
            _da_vecchio_formato(percorso, cartella, risultato)
        else:
            raise FormatoNonSupportato(f"Nessun testo da estrarre dai file «.{ext or '?'}».")
    return risultato


def _anteprima_con_libreoffice(percorso: Path, cartella: Path, risultato: Estratto) -> bytes | None:
    """Prima pagina e numero di pagine tramite PDF. Qualunque problema qui NON invalida il testo già estratto."""
    if not libreoffice():
        return None
    try:
        pdf = _converti_in_pdf(percorso, cartella)
        risultato.pages = _pagine_pdf(pdf)
        return _anteprima_pdf(pdf, cartella)
    except Exception:
        logger.info("Anteprima Office non generata", exc_info=True)
        return None
