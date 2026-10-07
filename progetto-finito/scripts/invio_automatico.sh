#!/usr/bin/env bash
# Dal LAPTOP: manda a DjangoVault i file NUOVI di una cartella (lo lancia cron ogni tanto: vedi installa_invio.sh).
#
#   ./scripts/invio_automatico.sh CARTELLA [opzioni di invia_cartella.py]
#
# Fa le cose giuste da solo, senza fare rumore: se la cartella non c'è (disco USB non inserito) o il server è spento, salta
# e riprova la prossima volta; due esecuzioni non si sovrappongono; scrive nel log solo quando ha davvero mandato qualcosa
# o qualcosa è andato storto. Di default chiede la classificazione automatica (--classifica).
# Variabili (opzionali): DV_SERVER (scrivania) · DV_PROGETTO (progetti/djangovault)
set -uo pipefail

[ $# -ge 1 ] || { sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
cartella="$1"; shift
qui="$(cd "$(dirname "$0")" && pwd)"
server="${DV_SERVER:-scrivania}"
progetto="${DV_PROGETTO:-progetti/djangovault}"
log() { echo "$(date '+%F %T') [$cartella] $*"; }

[ -d "$cartella" ] || exit 0  # disco non inserito o cartella spostata: non è un errore, riprova alla prossima
mkdir -p "$HOME/.cache/djangovault"
chiave="$(printf '%s' "$cartella" | md5sum | cut -c1-12)"
exec 9>"$HOME/.cache/djangovault/invio-$chiave.lock"
flock -n 9 || exit 0  # un altro invio della stessa cartella è ancora in corso

ssh -o BatchMode=yes -o ConnectTimeout=8 "$server" true 2>/dev/null || exit 0  # server spento o fuori rete

uscita="$(python3 "$qui/invia_cartella.py" "$cartella" --host "$server" --progetto "$progetto" --classifica "$@" 2>&1)"
rc=$?
if [ $rc -ne 0 ]; then
  log "ERRORE (codice $rc)"; printf '%s\n' "$uscita" | tail -n 15
elif ! printf '%s' "$uscita" | grep -q "Niente da inviare"; then
  log "inviato:"; printf '%s\n' "$uscita" | tail -n 15
fi
exit 0
