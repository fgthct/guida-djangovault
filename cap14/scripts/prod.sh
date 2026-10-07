#!/usr/bin/env bash
# Gestione di DjangoVault in produzione (compose.prod.yml).
#
#   ./scripts/prod.sh init [nome-sito] [--http]   crea .env.prod con segreti casuali
#   ./scripts/prod.sh check                        controlla .env.prod e i file necessari
#   ./scripts/prod.sh up                           controlla, costruisce e avvia
#   ./scripts/prod.sh down | status | logs [servizio]
#   ./scripts/prod.sh superuser                    crea il primo utente amministratore
#   ./scripts/prod.sh importa [opzioni]            importa i file di inbox/ (vedi scripts/invia_cartella.py)
#   ./scripts/prod.sh categorizza Cat titolo…      assegna una categoria a documenti già importati
#   ./scripts/prod.sh classifica [opzioni]         propone la categoria dei documenti senza (--valuta per misurare, --prova per vedere)
#   ./scripts/prod.sh ingestione cartella|posta|stato  controlla subito la cartella osservata (inbox/arrivi) o la casella email
#   ./scripts/prod.sh duplicati                    elenca i documenti che sembrano versioni dello stesso testo
#   ./scripts/prod.sh confronta ID ID…             quanto si assomigliano i testi di più documenti (sola lettura)
#   ./scripts/prod.sh elimina ID… [--conferma]     elimina documenti per id (senza --conferma: prova; con: backup + elimina)
#   ./scripts/prod.sh backup                       copia di database e file in backups/<data>/
#   ./scripts/prod.sh restore backups/<data> --yes ripristina (SOVRASCRIVE i dati attuali)
#
# `DV_STACK=dev ./scripts/prod.sh backup` fa la copia dell'ambiente di sviluppo (compose.yml + .env):
# serve per portare i dati in produzione con `restore`.
set -euo pipefail
cd "$(dirname "$0")/.."

STACK="${DV_STACK:-prod}"
if [ "$STACK" = "dev" ]; then
  ENVFILE=.env
  dc() { docker compose "$@"; }
else
  ENVFILE=.env.prod
  dc() { docker compose -f compose.prod.yml --env-file .env.prod "$@"; }
fi

var() { grep -E "^$1=" "$ENVFILE" | tail -n1 | cut -d= -f2- || true; }
rosso() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
giallo() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
serve_env() { [ -f "$ENVFILE" ] || { rosso "Manca $ENVFILE: crealo con  ./scripts/prod.sh init"; exit 1; }; }

cmd_init() {
  [ -f .env.prod ] && { rosso ".env.prod esiste già: non lo sovrascrivo."; exit 1; }
  local sito="" modo=https
  for a in "$@"; do case "$a" in --http) modo=http ;; *) sito="$a" ;; esac; done
  cp .env.prod.example .env.prod
  chiave="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  password="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  percorso="gestione-$(python3 -c 'import secrets; print(secrets.token_hex(4))')/"
  sed -i "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$chiave|; s|^DB_PASSWORD=.*|DB_PASSWORD=$password|; s|^DJANGO_ADMIN_PATH=.*|DJANGO_ADMIN_PATH=$percorso|" .env.prod
  sed -i "s|^DV_NGINX_MODE=.*|DV_NGINX_MODE=$modo|" .env.prod
  if [ "$modo" = http ]; then
    sed -i "s|^DJANGO_SECURE_SSL=.*|DJANGO_SECURE_SSL=0|" .env.prod
  fi
  if [ -n "$sito" ]; then
    sed -i "s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=$sito|; s|^DV_SERVER_NAME=.*|DV_SERVER_NAME=$sito|" .env.prod
    sed -i "s|^DJANGO_CSRF_TRUSTED_ORIGINS=.*|DJANGO_CSRF_TRUSTED_ORIGINS=$( [ "$modo" = https ] && echo https || echo http )://$sito|" .env.prod
  fi
  chmod 600 .env.prod
  echo "Creato .env.prod (modo $modo). Apri il file: controlla il nome del sito e metti ANTHROPIC_API_KEY."
  echo "L'indirizzo dell'amministrazione è /$percorso"
}

cmd_check() {
  serve_env
  local errori=0
  for v in DJANGO_SECRET_KEY DB_PASSWORD; do
    if [ -z "$(var $v)" ] || [ "$(var $v)" = cambiami ]; then rosso "$v non impostata in $ENVFILE"; errori=1; fi
  done
  if [ "$STACK" = prod ]; then
    host="$(var DJANGO_ALLOWED_HOSTS)"
    if [ -z "$host" ] || [ "$host" = dms.example.org ]; then rosso "DJANGO_ALLOWED_HOSTS: metti il nome vero del sito"; errori=1; fi
    if [ "$(var DJANGO_DEBUG)" = 1 ]; then rosso "DJANGO_DEBUG deve essere spento"; errori=1; fi
    if [ "$(var DV_NGINX_MODE)" = https ]; then
      certs="$(var DV_CERTS_DIR)"; certs="${certs:-./deploy/certs}"
      for f in fullchain.pem privkey.pem; do
        [ -f "$certs/$f" ] || { rosso "Manca $certs/$f (certificato HTTPS). Oppure usa il modo http: DV_NGINX_MODE=http e DJANGO_SECURE_SSL=0"; errori=1; }
      done
    elif [ "$(var DJANGO_SECURE_SSL)" != 0 ]; then
      rosso "Con DV_NGINX_MODE=http serve DJANGO_SECURE_SSL=0, altrimenti il sito rimanda a un HTTPS che non c'è"; errori=1
    fi
    [ "$(var DJANGO_ADMIN_PATH)" = admin/ ] && giallo "Attenzione: DJANGO_ADMIN_PATH è ancora admin/ (prevedibile)."
  fi
  [ -z "$(var ANTHROPIC_API_KEY)" ] && giallo "ANTHROPIC_API_KEY vuota: l'assistente «Chiedi» resterà spento."
  [ -f static/css/app.css ] || { rosso "Manca static/css/app.css: esegui prima ./scripts/tailwind.sh build"; errori=1; }
  [ "$errori" = 0 ] && echo "Controllo superato." || exit 1
}

cmd_up() {
  cmd_check
  dc up -d --build --wait
  echo
  echo "DjangoVault è in esecuzione. Primo utente:  ./scripts/prod.sh superuser"
}

cmd_superuser() { serve_env; dc exec django python manage.py createsuperuser; }

# Importa i file arrivati in inbox/ (li manda scripts/invia_cartella.py dal laptop). Opzioni: --categoria X,
# --per-sottocartelle, --senza-consenso-ia, --prova. I file importati (o già presenti) escono dalla cartella.
cmd_importa() {
  serve_env
  mkdir -p inbox
  dc exec -T django python manage.py importa_cartella /app/inbox --rimuovi "$@"
}

# Assegna una categoria a documenti già importati: ./scripts/prod.sh categorizza Romanzi zolfo "altro titolo"
cmd_categorizza() {
  serve_env
  dc exec -T django python manage.py categorizza "$@"
}

# Propone la categoria dei documenti senza: ./scripts/prod.sh classifica --valuta | --prova | (senza opzioni) | --applica
cmd_classifica() {
  serve_env
  dc exec -T django python manage.py classifica "$@"
}

# Controlla subito l'ingestione automatica: ./scripts/prod.sh ingestione cartella [--prova] | posta [--prova] | stato
cmd_ingestione() {
  serve_env
  mkdir -p inbox/arrivi
  dc exec -T worker python manage.py ingestione "$@"
}

# Elenca i documenti che sembrano versioni dello stesso testo (sola lettura): ./scripts/prod.sh duplicati
cmd_duplicati() {
  serve_env
  dc exec -T django python manage.py duplicati "$@"
}

# Elimina documenti per id. Senza --conferma è una PROVA; con --conferma fa prima un backup.
#   ./scripts/prod.sh elimina 26 29 34            (prova)
#   ./scripts/prod.sh elimina 26 29 34 --conferma
cmd_elimina() {
  serve_env
  case " $* " in
    *" --conferma "*) echo "Prima faccio un backup…"; cmd_backup ;;
  esac
  dc exec -T django python manage.py elimina_documenti "$@"
}

# Confronta il testo di più documenti (sola lettura): ./scripts/prod.sh confronta 3 17 55
cmd_confronta() {
  serve_env
  dc exec -T django python manage.py confronta "$@"
}

cmd_backup() {
  serve_env
  local db user dest
  db="$(var DB_NAME)"; db="${db:-djangovault}"; user="$(var DB_USER)"; user="${user:-djangovault}"
  dest="backups/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dest"
  echo "Database…"
  dc exec -T postgres pg_dump -U "$user" -d "$db" -Fc > "$dest/db.dump"
  echo "File dei documenti…"
  dc exec -T django tar czf - -C /app media > "$dest/media.tar.gz"
  [ -s "$dest/db.dump" ] || { rosso "La copia del database è vuota: annullo."; rm -rf "$dest"; exit 1; }
  (cd "$dest" && sha256sum db.dump media.tar.gz > SHA256SUMS)
  chmod -R go-rwx "$dest"
  # Si tengono le ultime N copie (DV_BACKUP_KEEP, 14 di default).
  tieni="${DV_BACKUP_KEEP:-14}"
  ls -1d backups/*/ 2>/dev/null | sort | head -n "-$tieni" | xargs -r rm -rf
  echo "Copia pronta in $dest ($(du -sh "$dest" | cut -f1)). Portala anche fuori da questo computer."
}

cmd_restore() {
  serve_env
  local src="${1:-}" db user
  [ -n "$src" ] && [ -f "$src/db.dump" ] && [ -f "$src/media.tar.gz" ] || { rosso "Uso: ./scripts/prod.sh restore backups/<data> --yes"; exit 1; }
  [ "${2:-}" = "--yes" ] || { rosso "Il ripristino SOVRASCRIVE database e file attuali. Aggiungi --yes per confermare."; exit 1; }
  (cd "$src" && sha256sum -c SHA256SUMS >/dev/null) || { rosso "Controllo di integrità fallito: la copia è danneggiata."; exit 1; }
  db="$(var DB_NAME)"; db="${db:-djangovault}"; user="$(var DB_USER)"; user="${user:-djangovault}"
  echo "Fermo l'applicazione…"
  dc stop nginx worker django 2>/dev/null || dc stop worker django
  dc up -d --wait postgres
  echo "Database…"
  dc exec -T postgres pg_restore -U "$user" -d "$db" --clean --if-exists --no-owner < "$src/db.dump"
  echo "File dei documenti…"
  dc run --rm --no-deps -T django sh -c 'rm -rf /app/media/* && tar xzf - -C /app' < "$src/media.tar.gz"
  echo "Riavvio…"
  dc up -d --wait
  echo "Ripristino completato."
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  check) cmd_check ;;
  up) cmd_up ;;
  down) serve_env; dc down ;;
  status) serve_env; dc ps ;;
  logs) serve_env; shift; dc logs -f --tail=100 "$@" ;;
  superuser) cmd_superuser ;;
  importa) shift; cmd_importa "$@" ;;
  categorizza) shift; cmd_categorizza "$@" ;;
  classifica) shift; cmd_classifica "$@" ;;
  ingestione) shift; cmd_ingestione "$@" ;;
  duplicati) shift; cmd_duplicati "$@" ;;
  elimina) shift; cmd_elimina "$@" ;;
  confronta) shift; cmd_confronta "$@" ;;
  backup) cmd_backup ;;
  restore) shift; cmd_restore "$@" ;;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
