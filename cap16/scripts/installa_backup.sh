#!/usr/bin/env bash
# Installa (o toglie) le copie automatiche di DjangoVault. Si può rilanciare quando si vuole: non crea doppioni.
#
#   ./scripts/installa_backup.sh server     sul SERVER: ogni notte alle 03:30 fa `prod.sh backup` (backups/<data>/)
#   ./scripts/installa_backup.sh laptop     sul LAPTOP: ogni ora scarica le copie nuove (scarica_backup.sh)
#   ./scripts/installa_backup.sh server|laptop --togli    toglie la voce di cron
#
# Sul laptop servono solo questo file e scarica_backup.sh (nella stessa cartella), non tutto il progetto.
set -euo pipefail

qui="$(cd "$(dirname "$0")" && pwd)"
marca="# djangovault-backup"
modo="${1:-}"; azione="${2:-}"
[ "$modo" = "server" ] || [ "$modo" = "laptop" ] || { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
command -v crontab >/dev/null || { echo "crontab non trovato (installa il pacchetto «cron»)." >&2; exit 1; }

PATHCRON="/usr/local/bin:/usr/bin:/bin"
if [ "$modo" = "server" ]; then
  progetto="$(cd "$qui/.." && pwd)"
  [ -x "$progetto/scripts/prod.sh" ] || { echo "Non trovo $progetto/scripts/prod.sh: lancialo dal progetto sul server." >&2; exit 1; }
  riga="30 3 * * * cd '$progetto' && PATH=$PATHCRON ./scripts/prod.sh backup >> backups/backup.log 2>&1 $marca"
  mkdir -p "$progetto/backups"
else
  [ -x "$qui/scarica_backup.sh" ] || { echo "Non trovo $qui/scarica_backup.sh (deve stare accanto a questo file e essere eseguibile)." >&2; exit 1; }
  mkdir -p "$HOME/.cache/djangovault"
  riga="17 * * * * PATH=$PATHCRON '$qui/scarica_backup.sh' >> '$HOME/.cache/djangovault/scarica_backup.log' 2>&1 $marca"
fi

# Si tolgono le voci precedenti dello stesso tipo (stessa marca e stesso modo), poi si aggiunge quella nuova.
tipo="prod.sh backup"; [ "$modo" = "laptop" ] && tipo="scarica_backup.sh"
altre="$( (crontab -l 2>/dev/null || true) | grep -vF "$marca" || true)"
mie_altre="$( (crontab -l 2>/dev/null || true) | grep -F "$marca" | grep -vF "$tipo" || true)"
nuovo="$altre"
[ -n "$mie_altre" ] && nuovo="${nuovo:+$nuovo$'\n'}$mie_altre"
if [ "$azione" = "--togli" ]; then
  printf '%s\n' "$nuovo" | sed '/^$/d' | crontab -
  echo "Copie automatiche ($modo) tolte."
else
  printf '%s\n' "${nuovo:+$nuovo$'\n'}$riga" | crontab -
  echo "Installato ($modo):"
  echo "  $riga"
fi
