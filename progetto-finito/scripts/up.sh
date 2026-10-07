#!/usr/bin/env bash
# Avvia DjangoVault in Docker: crea .env se manca, controlla le porte, costruisce e avvia.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Creo .env da .env.example con segreti casuali..."
  cp .env.example .env
  chiave="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  password="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  sed -i "s/^DJANGO_SECRET_KEY=.*/DJANGO_SECRET_KEY=$chiave/; s/^DB_PASSWORD=.*/DB_PASSWORD=$password/" .env
fi

./scripts/check_ports.sh

docker compose up -d --build --wait

porta="$(grep -E '^DV_WEB_PORT=' .env | cut -d= -f2)"
echo
echo "DjangoVault è in esecuzione su http://localhost:${porta:-8100}/"
echo "Crea il primo utente con:  docker compose exec django python manage.py createsuperuser"
