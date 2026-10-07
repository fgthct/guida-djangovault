#!/usr/bin/env bash
# Riscarica htmx e Alpine.js (build CSP) da npm dentro static/vendor/.
# Serve solo quando si vogliono aggiornare le librerie: non serve per avviare il progetto.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
( cd "$tmp" && npm pack --silent htmx.org@2 @alpinejs/csp >/dev/null
  mkdir h a
  tar -xzf htmx.org-*.tgz -C h
  tar -xzf alpinejs-csp-*.tgz -C a )
cp "$tmp/h/package/dist/htmx.min.js" static/vendor/htmx.min.js
cp "$tmp/a/package/dist/cdn.min.js" static/vendor/alpine-csp.min.js
hv="$(grep -m1 '"version"' "$tmp/h/package/package.json" | tr -dc '0-9.')"
av="$(grep -m1 '"version"' "$tmp/a/package/package.json" | tr -dc '0-9.')"
printf 'htmx.org      %s   (htmx.min.js)\n@alpinejs/csp %s   (alpine-csp.min.js, build compatibile con la Content Security Policy)\nAggiornare con: ./scripts/vendor_update.sh\n' "$hv" "$av" > static/vendor/VERSIONS.txt
cat static/vendor/VERSIONS.txt
