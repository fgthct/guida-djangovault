#!/usr/bin/env bash
# Dal LAPTOP: scarica le copie di DjangoVault (backups/<data>/ del server) in una cartella locale.
#
# Le copie sono immutabili: si scaricano solo quelle nuove, si controllano (SHA-256) e non si cancella mai nulla
# dal laptop per ordine del server. Se il server è spento o irraggiungibile, riprova alla prossima volta, senza errori.
# Si tengono le ultime DV_LOCALE_KEEP copie (30).
#
# Variabili (opzionali): DV_SERVER (scrivania) · DV_REMOTO (progetti/djangovault/backups) · DV_DEST (~/Backup/djangovault)
set -uo pipefail

server="${DV_SERVER:-scrivania}"
remoto="${DV_REMOTO:-progetti/djangovault/backups}"
dest="${DV_DEST:-$HOME/Backup/djangovault}"
tieni="${DV_LOCALE_KEEP:-30}"
log() { echo "$(date '+%F %T') $*"; }

mkdir -p "$dest" && chmod 700 "$dest"
exec 9>"$dest/.lock"
flock -n 9 || { log "un altro scaricamento è in corso"; exit 0; }

# Solo le cartelle datate (20YYMMDD-HHMMSS) con i loro file: mai il log né altro.
rsync -a --ignore-existing --timeout=120 -e "ssh -o BatchMode=yes -o ConnectTimeout=8" \
  --include='20*/' --include='20*/*' --exclude='*' "$server:$remoto/" "$dest/" 2>"$dest/.errori"
rc=$?
if [ $rc -ne 0 ]; then
  log "server non raggiungibile o copia interrotta (rsync $rc): riprovo alla prossima ora. $(head -c 200 "$dest/.errori" | tr '\n' ' ')"
  # una cartella scaricata a metà non va tenuta: si controlla comunque qui sotto
fi
rm -f "$dest/.errori"

nuove=0
for d in "$dest"/20*/; do
  [ -d "$d" ] || continue
  if [ -f "$d/.verificata" ]; then continue; fi
  if [ -f "$d/SHA256SUMS" ] && (cd "$d" && sha256sum -c SHA256SUMS >/dev/null 2>&1); then
    touch "$d/.verificata"; nuove=$((nuove + 1)); log "scaricata e verificata: $(basename "$d") ($(du -sh "$d" | cut -f1))"
  else
    log "incompleta o danneggiata, la rimuovo (si riscarica alla prossima volta): $(basename "$d")"
    rm -rf "$d"
  fi
done

# Si tengono solo le ultime N.
ls -1d "$dest"/20*/ 2>/dev/null | sort | head -n "-$tieni" | xargs -r rm -rf
[ "$nuove" -gt 0 ] || [ $rc -ne 0 ] || log "nessuna copia nuova"
exit 0
