#!/usr/bin/env python3
"""Dal laptop: manda a DjangoVault (sul server) i file nuovi di una cartella e li fa importare.

    ./scripts/invia_cartella.py ~/Documenti/libri --categoria "Libri informatica"
    ./scripts/invia_cartella.py ~/Documenti --per-sottocartelle      # categoria = prima sottocartella
    ./scripts/invia_cartella.py ~/Documenti/guide --prova            # elenca cosa invierebbe, senza inviare

Solo libreria standard + `rsync` e `ssh` (già presenti su Linux). I file NON vengono toccati né spostati sul laptop.
Le immagini (PNG, JPG…) non si inviano, a meno di --con-immagini. Un piccolo registro (~/.cache/djangovault/inviati.json) ricorda cosa è già stato importato (per contenuto, non per
nome): rilanciare il comando manda solo ciò che è nuovo o cambiato. Con --tutto si ignora il registro (il server
comunque non crea doppioni: li riconosce dal contenuto).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

IMMAGINI = ["png", "jpg", "jpeg", "gif", "webp"]  # nessun testo da cercare: di norma non si inviano (--con-immagini)
ESTENSIONI = "pdf,doc,docx,odt,rtf,txt,md,xls,xlsx,ods,csv,ppt,pptx,odp".split(",") + IMMAGINI
MAX_MB = 50  # come DOCUMENTS_MAX_UPLOAD_MB sul server
REGISTRO = Path(os.environ.get("DV_REGISTRO", Path.home() / ".cache" / "djangovault" / "inviati.json"))
IMPORTATO, PRESENTE = "importato", "presente"


@dataclass
class File:
    rel: str
    assoluto: Path
    dimensione: int
    sha: str = ""


def nascosto(rel: Path) -> bool:
    return any(p.startswith(".") or p.startswith("~$") for p in rel.parts) or rel.name.lower() in {"thumbs.db", "desktop.ini"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blocco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blocco)
    return h.hexdigest()


class Registro:
    """sha256 già importati + cache degli sha per (percorso, dimensione, data) per non rileggere i file grandi."""

    def __init__(self, percorso: Path = None):
        self.percorso = Path(percorso or REGISTRO)
        self.dati = {"inviati": {}, "cache": {}}
        try:
            self.dati.update(json.loads(self.percorso.read_text()))
        except (OSError, ValueError):
            pass

    def sha(self, f: File) -> str:
        st = f.assoluto.stat()
        chiave = str(f.assoluto)
        c = self.dati["cache"].get(chiave)
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime_ns:
            return c["sha"]
        valore = sha256(f.assoluto)
        self.dati["cache"][chiave] = {"size": st.st_size, "mtime": st.st_mtime_ns, "sha": valore}
        return valore

    def inviato(self, sha: str) -> bool:
        return sha in self.dati["inviati"]

    def segna(self, sha: str, rel: str) -> None:
        self.dati["inviati"][sha] = rel

    def salva(self) -> None:
        self.percorso.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.percorso.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.dati, indent=1))
        tmp.replace(self.percorso)


def candidati(radice: Path, estensioni=ESTENSIONI, max_mb: int = MAX_MB) -> tuple[list[File], list[tuple[str, str]]]:
    """(file inviabili, [(percorso, motivo) per quelli scartati])."""
    radice = Path(radice)
    buoni, scartati = [], []
    for p in sorted(radice.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(radice)
        if nascosto(rel):
            continue
        n = p.stat().st_size
        if p.suffix.lower().lstrip(".") not in estensioni:
            scartati.append((str(rel), "immagine (usa --con-immagini)" if p.suffix.lower().lstrip(".") in IMMAGINI else "tipo non ammesso"))
        elif n == 0:
            scartati.append((str(rel), "file vuoto"))
        elif n > max_mb * 1024 * 1024:
            scartati.append((str(rel), f"oltre {max_mb} MB"))
        else:
            buoni.append(File(str(rel), p, n))
    return buoni, scartati


def nuovi(file: list[File], registro: Registro, tutto: bool = False) -> list[File]:
    """I file da inviare: sha calcolato, già importati esclusi, doppioni fra loro uniti."""
    visti: set[str] = set()
    risultato = []
    for f in file:
        f.sha = registro.sha(f)
        if f.sha in visti or (not tutto and registro.inviato(f.sha)):
            continue
        visti.add(f.sha)
        risultato.append(f)
    return risultato


def comando_remoto(progetto: str, opzioni: list[str]) -> str:
    """Comando da eseguire sul server via ssh."""
    return f"cd {shlex.quote(progetto)} && ./scripts/prod.sh importa " + " ".join(shlex.quote(o) for o in opzioni)


def leggi_esiti(testo: str) -> list[tuple[str, str, str]]:
    """Righe `@@ stato<TAB>percorso<TAB>dettaglio` → [(stato, percorso, dettaglio)]."""
    esiti = []
    for riga in testo.splitlines():
        if riga.startswith("@@ "):
            parti = (riga[3:].split("\t") + ["", ""])[:3]
            esiti.append((parti[0], parti[1], parti[2]))
    return esiti


def opzioni_importazione(o) -> list[str]:
    """Le opzioni di `importa_cartella` che corrispondono a quelle scelte sul laptop.

    I documenti che arrivano dal laptop partono SENZA il consenso all'assistente IA: lo si dà dopo, documento per
    documento (o in blocco con `ai_consenti`), oppure subito con --con-consenso-ia."""
    opzioni: list[str] = []
    if o.categoria:
        opzioni += ["--categoria", o.categoria]
    if o.per_sottocartelle:
        opzioni += ["--per-sottocartelle"]
    if not o.con_consenso_ia:
        opzioni += ["--senza-consenso-ia"]
    if o.classifica:
        opzioni += ["--classifica"]
    return opzioni


def main(argv=None) -> int:
    a = argparse.ArgumentParser(description="Manda i file nuovi di una cartella a DjangoVault e li fa importare.")
    a.add_argument("cartella")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--categoria", help="categoria unica per tutti i file")
    g.add_argument("--per-sottocartelle", action="store_true", help="categoria = prima sottocartella di ogni file")
    a.add_argument("--con-consenso-ia", action="store_true", help="consente all'assistente «Chiedi» di leggere i documenti inviati (di norma NO: si decide poi, documento per documento)")
    a.add_argument("--senza-consenso-ia", action="store_true", help="(già il comportamento predefinito: resta per compatibilità con i cron esistenti)")
    a.add_argument("--classifica", action="store_true", help="i file senza categoria decisa qui li classifica il sistema (vedi «Da smistare»)")
    a.add_argument("--prova", action="store_true", help="elenca soltanto, non invia nulla")
    a.add_argument("--con-immagini", action="store_true", help="invia anche PNG, JPG, GIF e WebP (di norma escluse: senza testo)")
    a.add_argument("--tutto", action="store_true", help="ignora il registro e rimanda anche ciò che risulta già inviato")
    a.add_argument("--host", default="scrivania", help="nome ssh del server (default: scrivania)")
    a.add_argument("--progetto", default="progetti/djangovault", help="cartella di DjangoVault sul server (rispetto alla home)")
    o = a.parse_args(argv)

    radice = Path(o.cartella).expanduser()
    if not radice.is_dir():
        print(f"«{radice}» non è una cartella.", file=sys.stderr)
        return 2
    estensioni = ESTENSIONI if o.con_immagini else [e for e in ESTENSIONI if e not in IMMAGINI]
    buoni, scartati = candidati(radice, estensioni)
    registro = Registro()
    da_inviare = nuovi(buoni, registro, o.tutto)
    registro.salva()  # conserva la cache degli sha
    for rel, motivo in scartati:
        print(f"  salto  {rel}  ({motivo})")
    print(f"Trovati {len(buoni)} file inviabili; nuovi: {len(da_inviare)}.")
    if not da_inviare:
        print("Niente da inviare.")
        return 0
    mb = sum(f.dimensione for f in da_inviare) / 1024 / 1024
    for f in da_inviare:
        print(f"  {'da inviare' if o.prova else 'invio'}  {f.rel}")
    if o.prova:
        print(f"PROVA: {len(da_inviare)} file ({mb:.0f} MB) verrebbero inviati. Nulla è stato inviato.")
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as elenco:
        elenco.write("\0".join(f.rel for f in da_inviare))
    try:
        r = subprocess.run(
            ["rsync", "-a", "--from0", f"--files-from={elenco.name}", "--partial", "--info=progress2",
             f"{radice}/", f"{o.host}:{o.progetto}/inbox/"]
        )
    finally:
        os.unlink(elenco.name)
    if r.returncode != 0:
        print("Invio fallito (server acceso? ssh funziona?). Nulla è stato importato.", file=sys.stderr)
        return 1

    opzioni = opzioni_importazione(o)
    print("Importazione sul server…")
    r = subprocess.run(["ssh", o.host, comando_remoto(o.progetto, opzioni)], capture_output=True, text=True)
    sys.stdout.write("".join(riga + "\n" for riga in r.stdout.splitlines() if not riga.startswith("@@ ")))
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        print("Importazione fallita: i file restano in inbox/ sul server; rilancia il comando.", file=sys.stderr)
        return 1
    per_rel = {f.rel: f for f in da_inviare}
    for stato, rel, dettaglio in leggi_esiti(r.stdout):
        f = per_rel.get(rel)
        if stato in (IMPORTATO, PRESENTE) and f:
            registro.segna(f.sha, f.rel)
        elif f:
            print(f"  {stato}  {rel}  {dettaglio}")
    registro.salva()
    print("Fatto. L'elaborazione (testo, OCR, indice) prosegue in background sul server: per i libri richiede un po'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
