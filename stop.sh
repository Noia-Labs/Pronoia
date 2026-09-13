#!/usr/bin/env bash
# 停止 Pronoia 本地前后端服务。
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/stop_pronoia.sh" "$@"
