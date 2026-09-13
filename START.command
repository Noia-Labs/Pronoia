#!/usr/bin/env bash
# ASCII fallback for unzip tools that do not preserve Chinese filenames.
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/configure_api_and_launch.sh"
