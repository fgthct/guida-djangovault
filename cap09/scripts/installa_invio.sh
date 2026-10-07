#!/usr/bin/env bash
# Sul LAPTOP: manda da solo a DjangoVault i file nuovi di una cartella, a intervalli regolari (cron).
# Si può rilanciare quando si vuole: per ogni cartella c'è una sola voce.
#
#   ./scripts/installa_invio.sh CARTELLA                    ogni 30 minuti
#   ./scripts/installa_invio.sh CARTELLA --ogni 10          ogni 10 minuti
#   ./scripts/installa_invio.sh CARTELLA -- --con-consenso-ia      (ciò che segue «--» va a invia_cartella.py)
#   ./scripts/installa_invio.sh CARTELLA --togli            toglie l'invio automatico di quella cartella
#
# Sul laptop servono questo file, invio_automatico.sh e invia_cartella.py (nella stessa cartella). Il registro
# ~/.cache/djangovault/inviati.json ricorda cosa è già dentro: non si rimanda nulla due volte. Log in
# ~/.cache/djangovault/invio.log (si scrive solo quando succede qualcosa).
set -euo pipefail

qui="$(cd "$(dirname "$0")" && pwd)"
[ $# -ge 1 ] && [[ "$1" != -* ]] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
cartella="$(cd "$1" 2>/dev/null && pwd)" || { echo "«$1» non è una cartella." >&2; exit 1; }
shift
ogni=30; togli=0; extra=()
while [ $# -gt 0 ]; do
  case "$1" in
    --ogni) ogni="${2:?manca il numero di minuti}"; shift 2 ;;
    --togli) togli=1; shift ;;
    --) shift; extra=("$@"); break ;;
    *) echo "Opzione sconosciuta: $1" >&2; exit 1 ;;
  esac
done
[[ "$ogni" =~ ^[0-9]+$ ]] && [ "$ogni" -ge 1 ] && [ "$ogni" -le 1440 ] || { echo "--ogni vuole un numero di minuti fra 1 e 1440." >&2; exit 1; }
command -v crontab >/dev/null || { echo "crontab non trovato (installa il pacchetto «cron»)." >&2; exit 1; }
command -v flock >/dev/null || { echo "flock non trovato (pacchetto «util-linux»)." >&2; exit 1; }
for f in invio_automatico.sh invia_cartella.py; do
  [ -f "$qui/$f" ] || { echo "Non trovo $qui/$f (deve stare accanto a questo file)." >&2; exit 1; }
done

marca="# djangovault-invio ${cartella}"
mkdir -p "$HOME/.cache/djangovault"
altre="$( (crontab -l 2>/dev/null || true) | grep -vF "$marca" || true)"
if [ "$togli" = 1 ]; then
  printf '%s\n' "$altre" | sed '/^$/d' | crontab -
  echo "Invio automatico di «$cartella» tolto."
  exit 0
fi
args=""; for a in "${extra[@]+"${extra[@]}"}"; do args+=" $(printf '%q' "$a")"; done
if [ "$ogni" -ge 60 ] && [ $((ogni % 60)) -eq 0 ]; then quando="7 */$((ogni / 60)) * * *"; elif [ "$ogni" -ge 60 ]; then
  echo "Sopra l'ora l'intervallo deve essere un multiplo di 60 minuti." >&2; exit 1
else quando="*/$ogni * * * *"; fi
riga="$quando PATH=/usr/local/bin:/usr/bin:/bin '$qui/invio_automatico.sh' '$cartella'$args >> '$HOME/.cache/djangovault/invio.log' 2>&1 $marca"
printf '%s\n' "${altre:+$altre$'\n'}$riga" | crontab -
echo "Installato:"
echo "  $riga"
echo "Prova subito (nulla viene inviato):  python3 '$qui/invia_cartella.py' '$cartella' --prova"
