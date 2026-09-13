#!/usr/bin/env bash
# Print a deterministic digest of files that affect the Vite production build.

set -e
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"
FRONTEND_DIR="$ROOT_DIR/frontend"

hash_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    openssl dgst -sha256 "$1" | awk '{print $NF}'
  fi
}

hash_stream() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    openssl dgst -sha256 | awk '{print $NF}'
  fi
}

if ! command -v shasum >/dev/null 2>&1 && ! command -v openssl >/dev/null 2>&1; then
  printf 'Neither shasum nor openssl is available.\n' >&2
  exit 1
fi

{
  for relative in \
    package.json package-lock.json pnpm-lock.yaml \
    index.html tsconfig.json vite.config.ts \
    postcss.config.js tailwind.config.js; do
    [ -f "$FRONTEND_DIR/$relative" ] && printf '%s\n' "$FRONTEND_DIR/$relative"
  done
  [ -d "$FRONTEND_DIR/src" ] && find "$FRONTEND_DIR/src" -type f -print
} | LC_ALL=C sort | while IFS= read -r source_file; do
  relative_path="${source_file#"$FRONTEND_DIR/"}"
  printf '%s  %s\n' "$(hash_file "$source_file")" "$relative_path"
done | hash_stream
