#!/usr/bin/env bash
# Verifica che le porte pubblicate da Docker siano libere PRIMA di avviare lo stack.
# Se una è occupata dice da chi e propone la prima porta libera successiva.
set -euo pipefail
cd "$(dirname "$0")/.."

# Se lo stack è già attivo, le porte sono nostre: niente da controllare.
if [ -n "$(docker compose ps -q 2>/dev/null || true)" ]; then
  echo "Stack già avviato: controllo porte non necessario."
  exit 0
fi

leggi() {  # leggi NOME DEFAULT  (dal .env se presente, altrimenti il default)
  local v=""
  [ -f .env ] && v="$(grep -E "^$1=" .env | tail -n1 | cut -d= -f2- | tr -d "'\"" || true)"
  echo "${v:-$2}"
}

# Una porta è occupata se non riusciamo a prenderla noi (exit 0 = occupata).
# Non serve 'ss': basta provare un bind con Python.
occupata() {
  python3 - "$1" <<'PY'
import socket, sys
s = socket.socket()
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(0)
sys.exit(1)
PY
}

# Nome del processo che usa la porta (solo se 'ss' è disponibile).
chi_usa() {
  command -v ss >/dev/null 2>&1 || return 0
  ss -ltnpH "sport = :$1" 2>/dev/null | head -n1 | sed -nE 's/.*users:\(\("([^"]+)".*/\1/p'
}

prossima_libera() {
  local p=$(( $1 + 1 ))
  while occupata "$p"; do p=$(( p + 1 )); done
  echo "$p"
}

web="$(leggi DV_WEB_PORT 8100)"
db="$(leggi DV_DB_PORT 5440)"
redis="$(leggi DV_REDIS_PORT 6390)"

errori=0
for voce in "DV_WEB_PORT:$web:app web" "DV_DB_PORT:$db:PostgreSQL" "DV_REDIS_PORT:$redis:Redis"; do
  IFS=: read -r nome porta etichetta <<<"$voce"
  if occupata "$porta"; then
    errori=1
    chi="$(chi_usa "$porta")"
    echo "✗ Porta $porta ($etichetta) occupata${chi:+ da: $chi}."
    echo "  Imposta in .env:  $nome=$(prossima_libera "$porta")"
  else
    echo "✓ Porta $porta ($etichetta) libera."
  fi
done

if [ "$web" = "$db" ] || [ "$web" = "$redis" ] || [ "$db" = "$redis" ]; then
  echo "✗ Due servizi hanno la stessa porta: correggi .env."
  errori=1
fi

exit "$errori"
