#!/usr/bin/env bash
# Reconfigure the model API, then launch Pronoia.
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/configure_api_and_launch.sh" --reconfigure
