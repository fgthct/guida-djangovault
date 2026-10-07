#!/usr/bin/env bash
# HTTPS privato con un certificato vero (Let's Encrypt), senza aprire il server a Internet.
#
# Il certificato si ottiene con la «sfida DNS» (DNS-01): non serve che il server sia raggiungibile da fuori, e il nome
# (per esempio dv.morpheoproject.com) può puntare a un indirizzo PRIVATO (192.168.x.x o quello di WireGuard). Così il
# telefono vede un certificato valido, la PWA si installa, e dall'esterno il server resta irraggiungibile.
# Per il rinnovo automatico, con qualsiasi DNS (anche Aruba), si usa acme-dns: sul DNS del dominio si aggiunge UNA volta
# sola un record CNAME per _acme-challenge; poi i rinnovi non hanno più bisogno del tuo DNS.
#
#   ./scripts/certificato.sh emetti dv.morpheoproject.com   1ª volta: dice quale CNAME aggiungere; 2ª: ottiene il certificato
#   ./scripts/certificato.sh attiva dv.morpheoproject.com   cambia .env.prod in modo HTTPS (copia di sicurezza: .env.prod.http)
#   ./scripts/certificato.sh rinnova                        rinnova se mancano meno di 30 giorni e ricarica Nginx
#   ./scripts/certificato.sh cron                           programma il rinnovo ogni notte
#   ./scripts/certificato.sh stato                          nome e scadenza del certificato in uso
#   ./scripts/certificato.sh cname dv.morpheoproject.com    ripete quale record CNAME aggiungere sul DNS
#   ./scripts/certificato.sh disattiva                      torna al .env.prod di prima (http)
#
# Variabili: DV_ACME_IMAGE (neilpang/acme.sh) · ACMEDNS_BASE_URL (https://auth.acme-dns.io)
set -euo pipefail
cd "$(dirname "$0")/.."

IMMAGINE="${DV_ACME_IMAGE:-neilpang/acme.sh}"
ACMEDNS="${ACMEDNS_BASE_URL:-https://auth.acme-dns.io}"
ACME_DIR="$PWD/deploy/acme"
CERTS="$(grep -E '^DV_CERTS_DIR=' .env.prod 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
CERTS="${CERTS:-$PWD/deploy/certs}"
case "$CERTS" in /*) ;; *) CERTS="$PWD/${CERTS#./}" ;; esac

rosso() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
giallo() { printf '\033[33m%s\033[0m\n' "$*" >&2; }

acme() {
  docker run --rm -v "$ACME_DIR:/acme.sh" -v "$CERTS:/certs" -e "ACMEDNS_BASE_URL=$ACMEDNS" "$IMMAGINE" "$@"
}

prepara() {
  command -v docker >/dev/null || { rosso "docker non trovato."; exit 1; }
  mkdir -p "$ACME_DIR" "$CERTS"
}

# I file creati dal contenitore sono di root: li riporto all'utente (e la chiave resta leggibile solo a lui).
sistema_permessi() {
  docker run --rm -v "$ACME_DIR:/acme.sh" -v "$CERTS:/certs" --entrypoint sh "$IMMAGINE" \
    -c "chown -R $(id -u):$(id -g) /acme.sh /certs; chmod 700 /acme.sh; chmod 600 /certs/privkey.pem 2>/dev/null; chmod 644 /certs/fullchain.pem 2>/dev/null; true" >/dev/null
}

impronta() { [ -f "$CERTS/fullchain.pem" ] && sha256sum "$CERTS/fullchain.pem" | cut -d' ' -f1 || echo nessuna; }

ricarica_nginx() {
  if docker compose -f compose.prod.yml --env-file .env.prod ps -q nginx 2>/dev/null | grep -q .; then
    docker compose -f compose.prod.yml --env-file .env.prod exec -T nginx nginx -s reload && echo "Nginx ricaricato con il nuovo certificato."
  fi
}

# Il valore del CNAME è l'identificativo che acme-dns ha assegnato al nome (salvato in deploy/acme).
cname_atteso() {
  local id
  id="$(grep -rhE "ACMEDNS_SUBDOMAIN" "$ACME_DIR" 2>/dev/null | head -n1 | sed -E "s/.*ACMEDNS_SUBDOMAIN=//; s/[\"' ]//g" || true)"
  [ -n "$id" ] && printf '%s.%s' "$id" "$(printf '%s' "$ACMEDNS" | sed -E 's|^https?://||; s|^auth\.||; s|/.*$||' | sed 's|^|auth.|')"
}

mostra_cname() {
  local nome="$1" valore
  valore="$(cname_atteso || true)"
  if [ -n "$valore" ]; then
    giallo "Aggiungi sul DNS di morpheoproject.com (Aruba) questo record CNAME:"
    giallo "    Tipo:    CNAME"
    giallo "    Nome:    _acme-challenge.${nome%%.*}"
    giallo "    Valore:  $valore"
    giallo "Poi aspetta 5-10 minuti e rilancia:  ./scripts/certificato.sh emetti $nome"
  else
    giallo "Non trovo l'identificativo di acme-dns in $ACME_DIR: leggi il messaggio sopra."
  fi
}

nome_valido() { [[ "${1:-}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; }

cmd_emetti() {
  local nome="${1:-}"
  nome_valido "$nome" || { rosso "Serve un nome di sito valido, per esempio dv.morpheoproject.com"; exit 1; }
  prepara
  local uscita rc=0
  uscita="$(acme --issue --dns dns_acmedns -d "$nome" --server letsencrypt --keylength ec-256 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    sistema_permessi  # i file creati dal contenitore sono di root: servono leggibili per mostrare il CNAME
    printf '%s\n' "$uscita" | grep -v -i "password" | grep -v "wait for 10 seconds\|--dnssleep\|dnscheck\|Checking .* for _acme" | tail -n 25
    echo
    if printf '%s' "$uscita" | grep -qi "CNAME\|Timed out waiting for DNS\|Error checking DNS"; then
      mostra_cname "$nome"
    else
      rosso "Certificato non ottenuto: leggi il messaggio qui sopra."
    fi
    exit 1
  fi
  acme --install-cert -d "$nome" --ecc --fullchain-file /certs/fullchain.pem --key-file /certs/privkey.pem >/dev/null
  sistema_permessi
  verde "Certificato ottenuto per $nome."
  cmd_stato
  echo "Ora:  ./scripts/certificato.sh attiva $nome  &&  ./scripts/prod.sh up   e   ./scripts/certificato.sh cron"
}

cmd_attiva() {
  local nome="${1:-}"
  nome_valido "$nome" || { rosso "Serve il nome del sito, per esempio dv.morpheoproject.com"; exit 1; }
  [ -f .env.prod ] || { rosso "Manca .env.prod."; exit 1; }
  [ -f "$CERTS/fullchain.pem" ] && [ -f "$CERTS/privkey.pem" ] || { rosso "Prima il certificato:  ./scripts/certificato.sh emetti $nome"; exit 1; }
  [ -f .env.prod.http ] || cp -p .env.prod .env.prod.http
  imposta() { # imposta CHIAVE VALORE
    if grep -qE "^$1=" .env.prod; then sed -i "s|^$1=.*|$1=$2|" .env.prod; else printf '\n%s=%s\n' "$1" "$2" >> .env.prod; fi
  }
  aggiungi() { # aggiungi CHIAVE VALORE: lo mette nell'elenco se non c'è già
    local att; att="$(grep -E "^$1=" .env.prod | tail -n1 | cut -d= -f2- || true)"
    case ",$att," in *",$2,"*) ;; *) imposta "$1" "${att:+$att,}$2" ;; esac
  }
  imposta DV_NGINX_MODE https
  imposta DV_SERVER_NAME "$nome"
  imposta DJANGO_SECURE_SSL 1
  aggiungi DJANGO_ALLOWED_HOSTS "$nome"
  aggiungi DJANGO_CSRF_TRUSTED_ORIGINS "https://$nome"
  verde ".env.prod aggiornato per HTTPS (la versione di prima è in .env.prod.http)."
  echo "Ora avvia:  ./scripts/prod.sh up     poi apri  https://$nome/"
}

cmd_disattiva() {
  [ -f .env.prod.http ] || { rosso "Non c'è .env.prod.http: nulla da ripristinare."; exit 1; }
  cp -p .env.prod.http .env.prod
  verde ".env.prod ripristinato com'era prima. Ora:  ./scripts/prod.sh up"
}

cmd_rinnova() {
  prepara
  local prima dopo
  prima="$(impronta)"
  acme --cron >/dev/null 2>&1 || { rosso "Rinnovo non riuscito."; exit 1; }
  sistema_permessi
  dopo="$(impronta)"
  if [ "$prima" != "$dopo" ]; then ricarica_nginx; echo "$(date '+%F %T') certificato rinnovato"; fi
}

cmd_cron() {
  command -v crontab >/dev/null || { rosso "crontab non trovato (pacchetto «cron»)."; exit 1; }
  local marca="# djangovault-certificato $PWD"
  local altre; altre="$( (crontab -l 2>/dev/null || true) | grep -vF "$marca" || true)"
  { [ -n "$altre" ] && printf '%s\n' "$altre"; printf '%s\n' "17 4 * * * cd '$PWD' && ./scripts/certificato.sh rinnova >> deploy/acme/rinnovo.log 2>&1 $marca"; } | crontab -
  verde "Rinnovo programmato ogni notte alle 04:17 (log: deploy/acme/rinnovo.log). Rinnova solo quando mancano meno di 30 giorni."
}

cmd_stato() {
  [ -f "$CERTS/fullchain.pem" ] || { echo "Nessun certificato in $CERTS."; return 0; }
  command -v openssl >/dev/null || { echo "Certificato presente (openssl non installato per leggerlo)."; return 0; }
  openssl x509 -in "$CERTS/fullchain.pem" -noout -subject -issuer -enddate | sed 's/^/  /'
  if openssl x509 -in "$CERTS/fullchain.pem" -noout -checkend $((30*86400)) >/dev/null; then
    echo "  Scade fra più di 30 giorni."
  else
    giallo "  Scade fra meno di 30 giorni: lancia  ./scripts/certificato.sh rinnova  (e controlla che il cron sia attivo)."
  fi
}

case "${1:-}" in
  emetti) shift; cmd_emetti "${1:-}" ;;
  attiva) shift; cmd_attiva "${1:-}" ;;
  disattiva) cmd_disattiva ;;
  rinnova) cmd_rinnova ;;
  cron) cmd_cron ;;
  stato) cmd_stato ;;
  cname) shift; nome_valido "${1:-}" || { rosso "Serve il nome del sito."; exit 1; }; mostra_cname "$1" ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
