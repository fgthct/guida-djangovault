#!/usr/bin/env bash
# Build del CSS con la CLI standalone di Tailwind (nessun Node necessario).
#   ./scripts/tailwind.sh build   -> genera static/css/app.css minificato
#   ./scripts/tailwind.sh watch   -> rigenera a ogni modifica (sviluppo)
set -euo pipefail
cd "$(dirname "$0")/.."

modo="${1:-build}"
bin="bin/tailwindcss"

if [ ! -x "$bin" ]; then
  os="$(uname -s)"; arch="$(uname -m)"
  case "$os-$arch" in
    Linux-x86_64)  asset="tailwindcss-linux-x64" ;;
    Linux-aarch64) asset="tailwindcss-linux-arm64" ;;
    Darwin-arm64)  asset="tailwindcss-macos-arm64" ;;
    Darwin-x86_64) asset="tailwindcss-macos-x64" ;;
    *) echo "Piattaforma non supportata: $os-$arch" >&2; exit 1 ;;
  esac
  mkdir -p bin
  echo "Scarico la CLI di Tailwind ($asset)..."
  curl -fsSL -o "$bin" "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/$asset"
  chmod +x "$bin"
fi

case "$modo" in
  build) "$bin" -i frontend/css/app.css -o static/css/app.css --minify ;;
  watch) "$bin" -i frontend/css/app.css -o static/css/app.css --watch ;;
  *) echo "Uso: $0 [build|watch]" >&2; exit 1 ;;
esac
